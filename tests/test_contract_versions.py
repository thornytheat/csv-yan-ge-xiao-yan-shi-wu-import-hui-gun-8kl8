"""契约版本：v1-yuan 按元 / v2-cents 按分；每次导入带明确版本，旧文件兼容。"""
from __future__ import annotations

import pytest

from app import aggregate, contract, importer, pipeline, rows as rows_mod, store

HEADER = ["site", "category", "order_id", "order_date", "amount", "qty"]


@pytest.fixture()
def conn(tmp_path):
    connection = store.connect(tmp_path / "t.sqlite3")
    store.init_schema(connection)
    yield connection
    connection.close()


def make_csv(amounts: list[str], start_id: int = 1001) -> bytes:
    lines = [",".join(HEADER)]
    for i, amount in enumerate(amounts):
        lines.append(f"beijing,grocery,{start_id + i},2026-11-05,{amount},2")
    return ("\n".join(lines) + "\n").encode("utf-8")


# ---------------------------------------------------------------- 契约注册表

def test_version_registry_and_default():
    assert contract.resolve_version(None) == "v1-yuan"
    assert contract.resolve_version("") == "v1-yuan"
    assert contract.resolve_version("v2-cents") == "v2-cents"
    assert contract.amount_unit("v1-yuan") == "yuan"
    assert contract.amount_unit("v2-cents") == "cent"
    with pytest.raises(contract.UnknownContractVersion):
        contract.resolve_version("v9")


def test_specs_differ_only_in_amount():
    v1 = contract.specs_for("v1-yuan")
    v2 = contract.specs_for("v2-cents")
    assert v1["amount"]["type"] == "decimal"
    assert v2["amount"]["type"] == "int"
    for name in ("site", "category", "order_id", "order_date", "qty", "memo"):
        assert v1[name] == v2[name]
    # 模块级 COLUMN_SPECS 仍是默认版（向后兼容）
    assert contract.COLUMN_SPECS["amount"]["type"] == "decimal"


def test_contract_check_carries_version():
    result = contract.check(HEADER, version="v2-cents")
    assert result["ok"] is True
    assert result["contract_version"] == "v2-cents"
    assert result["amount_unit"] == "cent"
    amount_col = [c for c in result["column_contract"] if c["name"] == "amount"][0]
    assert amount_col["type"] == "int"


# ---------------------------------------------------------------- 行校验分口径

def validate(data, version=None):
    return rows_mod.validate(data, HEADER, HEADER, version=version)


def test_v2_cents_amounts_validate_as_integers():
    result = validate([["beijing", "grocery", "1001", "2026-11-05", "12850", "3"]],
                      version="v2-cents")
    assert result.ok is True
    assert result.rows[0]["amount_cents"] == 12850
    assert result.rows[0]["amount_raw"] == "12850"


def test_v2_rejects_yuan_style_decimals():
    result = validate([["beijing", "grocery", "1001", "2026-11-05", "128.50", "3"]],
                      version="v2-cents")
    assert result.ok is False
    assert result.codes() == ["cents_type"]
    assert "v1-yuan" in result.errors[0].reason          # 提示旧文件换版本


def test_v2_cents_range_enforced():
    zero = validate([["beijing", "grocery", "1001", "2026-11-05", "0", "3"]], version="v2-cents")
    assert zero.codes() == ["cents_range"]
    too_big = validate([["beijing", "grocery", "1001", "2026-11-05", "99999999999", "3"]],
                       version="v2-cents")
    assert too_big.codes() == ["cents_range"]


def test_v1_remains_yuan_default():
    result = validate([["beijing", "grocery", "1001", "2026-11-05", "128.50", "3"]])
    assert result.ok is True
    assert result.rows[0]["amount_cents"] == 12850


# ---------------------------------------------------------------- 聚合口径

def test_aggregate_decimal_check_is_unit_aware():
    data = [["beijing", "grocery", "1001", "2026-11-05", "12850", "1"],
            ["shanghai", "grocery", "1002", "2026-11-05", "100", "2"]]
    rows = rows_mod.validate(data, HEADER, HEADER, version="v2-cents").rows
    summary = aggregate.summarize(rows, amount_unit="cent")
    assert summary["consistent"] is True
    assert summary["total_amount_cents"] == 12950
    # 若误按元口径复核（×100），自证必然失败——口径必须显式
    assert aggregate.summarize(rows, amount_unit="yuan")["consistent"] is False


# ---------------------------------------------------------------- 管道与导入

def test_pipeline_commits_cents_file_with_explicit_version(conn):
    raw = make_csv(["1200", "1337", "1474"])
    result = pipeline.run(raw, "cents.csv", conn=conn, contract_version="v2-cents")
    assert result["status"] == "committed"
    assert result["contract_version"] == "v2-cents"
    assert result["aggregate"]["consistent"] is True
    assert result["aggregate"]["total_amount_cents"] == 1200 + 1337 + 1474
    batch = store.get_batch(conn, result["batch_id"])
    assert batch["contract_version"] == "v2-cents"
    assert batch["total_amount"] == "40.11"          # 4011 分
    assert batch["sample_rows"][0]["amount_raw"] == "1200"


def test_pipeline_defaults_to_v1_for_old_files(conn):
    raw = make_csv(["12.00", "13.37"])
    result = pipeline.run(raw, "old.csv", conn=conn)     # 不传版本 → 默认 v1-yuan
    assert result["status"] == "committed"
    assert result["contract_version"] == "v1-yuan"
    batch = store.get_batch(conn, result["batch_id"])
    assert batch["contract_version"] == "v1-yuan"
    assert batch["total_amount"] == "25.37"


def test_yuan_file_under_v2_is_rejected_not_misimported(conn):
    raw = make_csv(["12.00", "13.37"])
    result = pipeline.run(raw, "old.csv", conn=conn, contract_version="v2-cents")
    assert result["status"] == "rejected"
    assert result["row_validation"]["codes"] == ["cents_type"]
    assert store.count_rows(conn) == 0                    # 没有按错误口径写进明细


def test_unknown_contract_version_rejected_before_processing(conn):
    result = pipeline.run(make_csv(["12.00"]), "x.csv", conn=conn,
                          contract_version="v9-silver")
    assert result["status"] == "unknown_contract_version"
    assert "v2-cents" in result["detail"]
    assert store.count_rows(conn) == 0


def test_same_file_same_version_is_duplicate(conn):
    raw = make_csv(["100", "200"])                       # 整数金额两版都合法
    first = pipeline.run(raw, "both.csv", conn=conn, contract_version="v1-yuan")
    assert first["status"] == "committed"
    again = pipeline.run(raw, "both.csv", conn=conn, contract_version="v1-yuan")
    assert again["status"] == "duplicate_file"
    assert again["batch_id"] == first["batch_id"]


def test_same_file_other_version_is_not_duplicate(conn):
    """同一份字节按不同口径解释 = 不同导入：各自成批，金额差 100 倍。"""
    raw = make_csv(["100", "200"])
    as_yuan = pipeline.run(raw, "both.csv", conn=conn, contract_version="v1-yuan")
    as_cents = pipeline.run(raw, "both.csv", conn=conn, contract_version="v2-cents")
    assert as_yuan["status"] == as_cents["status"] == "committed"
    assert as_cents["batch_id"] != as_yuan["batch_id"]
    yuan_batch = store.get_batch(conn, as_yuan["batch_id"])
    cents_batch = store.get_batch(conn, as_cents["batch_id"])
    assert yuan_batch["total_amount_cents"] == 30000     # 300 元
    assert cents_batch["total_amount_cents"] == 300      # 300 分
    # 旧版结果仍可查
    assert yuan_batch["contract_version"] == "v1-yuan"
    assert cents_batch["contract_version"] == "v2-cents"


def test_attempts_record_contract_version(conn):
    raw = make_csv(["12.00"])
    pipeline.run(raw, "x.csv", conn=conn, contract_version="v2-cents")
    attempt = store.list_attempts(conn)[0]
    assert attempt["contract_version"] == "v2-cents"


# ---------------------------------------------------------------- API

def upload(client, content: bytes, name: str, **form):
    data = {"extra_policy": "reject", **form}
    return client.post("/api/import", files={"file": (name, content, "text/csv")}, data=data)


def test_api_import_with_explicit_cents_version(client):
    res = upload(client, make_csv(["1200", "1337"]), "cents.csv", contract_version="v2-cents")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "committed"
    assert body["contract_version"] == "v2-cents"
    assert body["aggregate"]["total_amount_cents"] == 2537


def test_api_import_unknown_version_is_400(client):
    res = upload(client, make_csv(["12.00"]), "x.csv", contract_version="v3")
    assert res.status_code == 400
    body = res.json()
    assert body["status"] == "unknown_contract_version"
    assert any(v["version"] == "v2-cents" for v in body["available_versions"])


def test_api_contracts_lists_versions_and_migration(client):
    body = client.get("/api/contracts").json()
    versions = {v["version"] for v in body["versions"]}
    assert versions == {"v1-yuan", "v2-cents"}
    assert body["default_version"] == "v1-yuan"
    assert body["migration"]["applied"] is True          # 启动时已自动迁移


def test_api_health_exposes_contract_state(client):
    body = client.get("/health").json()
    assert body["default_contract_version"] == "v1-yuan"
    assert body["contract_migration"]["applied"] is True


def test_api_cents_sample_file_imports(client, sample_dir):
    raw = (sample_dir / "cents_240.csv").read_bytes()
    res = upload(client, raw, "cents_240.csv", contract_version="v2-cents")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "committed"
    assert body["written"] == 240
    assert body["aggregate"]["consistent"] is True
    # 分口径总额 = 各行整数分之和
    expected = sum(1200 + (i * 137) % 880000 for i in range(240))
    assert body["aggregate"]["total_amount_cents"] == expected


def test_api_yuan_sample_under_v2_rejected(client, sample_dir):
    res = upload(client, (sample_dir / "normal_500.csv").read_bytes(),
                 "normal_500.csv", contract_version="v2-cents")
    assert res.status_code == 422
    assert res.json()["row_validation"]["codes"] == ["cents_type"]
