"""契约迁移：盖章版本、回填原值、重算明细与站点汇总、单事务切换、幂等重跑。"""
from __future__ import annotations

import pytest

from app import contract, migrate, pipeline, store

YUAN_CSV = (
    "site,category,order_id,order_date,amount,qty\n"
    "beijing,grocery,1001,2026-11-05,10.00,2\n"
    "beijing,grocery,1002,2026-11-05,20.00,1\n"
    "shanghai,apparel,1003,2026-11-06,5.50,4\n"
).encode("utf-8")

CENTS_CSV = (
    "site,category,order_id,order_date,amount,qty\n"
    "beijing,grocery,2001,2026-11-05,1000,2\n"
    "shenzhen,grocery,2002,2026-11-05,250,1\n"
).encode("utf-8")


@pytest.fixture()
def conn(tmp_path):
    connection = store.connect(tmp_path / "t.sqlite3")
    store.init_schema(connection)
    yield connection
    connection.close()


def _import_then_strip_version(conn, raw: bytes, name: str, version: str) -> str:
    """走正常管道入库，再把版本/原值列清回 NULL，模拟升级前的历史库。"""
    result = pipeline.run(raw, name, conn=conn, contract_version=version)
    assert result["status"] == "committed", result.get("detail")
    batch_id = result["batch_id"]
    conn.execute("UPDATE batches SET contract_version = NULL WHERE batch_id = ?", (batch_id,))
    conn.execute("UPDATE fact_orders SET amount_raw = NULL WHERE batch_id = ?", (batch_id,))
    return batch_id


def _legacy_db(conn) -> tuple[str, str]:
    """两个历史批次（一元一分），版本与原值均为 NULL，且汇总被人为改错。"""
    yuan_batch = _import_then_strip_version(conn, YUAN_CSV, "yuan.csv", "v1-yuan")
    cents_batch = _import_then_strip_version(conn, CENTS_CSV, "cents.csv", "v2-cents")
    # 模拟「站点汇总和总金额对不上」：把聚合与批次总额改错
    conn.execute("UPDATE batch_aggregates SET amount_cents = amount_cents + 100"
                 " WHERE batch_id = ? AND site = 'beijing'", (yuan_batch,))
    conn.execute("UPDATE batches SET total_amount_cents = total_amount_cents + 100,"
                 " row_count = row_count + 1 WHERE batch_id = ?", (yuan_batch,))
    return yuan_batch, cents_batch


def test_status_before_and_after(conn):
    _legacy_db(conn)
    before = migrate.status(conn)
    assert before["applied"] is False
    assert before["unstamped_batches"] == 2
    assert before["details_missing_amount_raw"] == 5

    outcome = migrate.run(conn)
    assert outcome["status"] == "applied"

    after = migrate.status(conn)
    assert after["applied"] is True
    assert after["unstamped_batches"] == 0
    assert after["details_missing_amount_raw"] == 0
    assert after["batches_by_version"] == {"v1-yuan": 2}   # NULL 一律按历史元口径盖章


def test_migration_stamps_versions_and_backfills_raw(conn):
    yuan_batch, cents_batch = _legacy_db(conn)
    outcome = migrate.run(conn)
    assert outcome["status"] == "applied"
    assert outcome["stamped_batches"] == 2
    assert outcome["backfilled_details"] == 5

    # 历史数据一律按元口径盖章：_legacy_db 里两个批次都被清成 NULL，
    # 迁移后都盖 v1-yuan；真实场景中新口径批次入库时就带 v2-cents，
    # 迁移不会改写已有盖章（见 test_migration_keeps_existing_v2_stamp_and_raw_format）
    assert store.get_batch(conn, yuan_batch)["contract_version"] == "v1-yuan"
    assert store.get_batch(conn, cents_batch)["contract_version"] == "v1-yuan"

    yuan_raws = [r["amount_raw"] for r in conn.execute(
        "SELECT amount_raw FROM fact_orders WHERE batch_id = ? ORDER BY row_no",
        (yuan_batch,)).fetchall()]
    assert yuan_raws == ["10.00", "20.00", "5.50"]      # 元口径反推：分 → 元字符串


def test_migration_keeps_existing_v2_stamp_and_raw_format(conn):
    cents_batch = _import_then_strip_version(conn, CENTS_CSV, "cents.csv", "v2-cents")
    # 这个批次保留 v2 盖章，只清掉原值 → 迁移应按分口径回填
    conn.execute("UPDATE batches SET contract_version = 'v2-cents' WHERE batch_id = ?",
                 (cents_batch,))
    outcome = migrate.run(conn)
    assert outcome["status"] == "applied"
    raws = [r["amount_raw"] for r in conn.execute(
        "SELECT amount_raw FROM fact_orders WHERE batch_id = ? ORDER BY row_no",
        (cents_batch,)).fetchall()]
    assert raws == ["1000", "250"]                       # 分口径反推：分 → 整数字符串
    assert store.get_batch(conn, cents_batch)["contract_version"] == "v2-cents"


def test_migration_recomputes_aggregates_and_totals(conn):
    yuan_batch, _ = _legacy_db(conn)
    # 迁移前：汇总与总额确实对不上（被人为改错 +100 分、+1 行）
    broken = store.get_batch(conn, yuan_batch)
    assert broken["total_amount_cents"] == 3650          # 3550 + 100
    assert broken["row_count"] == 4                      # 3 + 1

    outcome = migrate.run(conn)
    assert outcome["status"] == "applied"
    assert yuan_batch in outcome["changed_batches"]

    fixed = store.get_batch(conn, yuan_batch)
    assert fixed["row_count"] == 3
    assert fixed["total_amount_cents"] == 3550           # 10.00+20.00+5.50 元
    assert fixed["total_qty"] == 7
    by_site = {g["site"]: g for g in fixed["aggregates"]}
    assert by_site["beijing"]["amount_cents"] == 3000
    assert by_site["shanghai"]["amount_cents"] == 550
    assert sum(g["row_count"] for g in fixed["aggregates"]) == 3


def test_migration_is_atomic_and_switch_is_one_shot(conn):
    _legacy_db(conn)
    marker_before = store.get_meta(conn, "contract_migration")
    outcome = migrate.run(conn, fail_after_batches=1)    # 处理完第 1 个批次即注入故障
    assert outcome["status"] == "failed"
    assert outcome["rolled_back"] is True
    # 失败时保留上一份完整结果：版本仍 NULL、原值仍 NULL、标记未变、汇总仍是错的
    assert store.get_meta(conn, "contract_migration") == marker_before
    after = migrate.status(conn)
    assert after["applied"] is False
    assert after["unstamped_batches"] == 2
    assert after["details_missing_amount_raw"] == 5
    # 重跑同一升级：成功完成
    retry = migrate.run(conn)
    assert retry["status"] == "applied"
    assert migrate.status(conn)["applied"] is True


def test_migration_rerun_is_idempotent_and_adds_no_batches(conn):
    _legacy_db(conn)
    first = migrate.run(conn)
    assert first["status"] == "applied"
    batches_after_first = store.stats(conn)["batches"]
    snapshot = conn.execute(
        "SELECT batch_id, contract_version, total_amount_cents, row_count FROM batches"
        " ORDER BY batch_id").fetchall()
    snapshot = [tuple(r) for r in snapshot]

    second = migrate.run(conn)
    assert second["status"] == "already_applied"
    assert store.stats(conn)["batches"] == batches_after_first   # 不新增批次
    again = [tuple(r) for r in conn.execute(
        "SELECT batch_id, contract_version, total_amount_cents, row_count FROM batches"
        " ORDER BY batch_id")]
    assert again == snapshot                                      # 也不改任何行


def test_old_version_results_remain_queryable(conn):
    yuan_batch, cents_batch = _legacy_db(conn)
    migrate.run(conn)
    for batch_id in (yuan_batch, cents_batch):
        batch = store.get_batch(conn, batch_id)
        assert batch is not None
        assert batch["contract_version"] == "v1-yuan"
        assert batch["aggregates"] and batch["sample_rows"]


def test_migration_rejects_inconsistent_detail(conn):
    batch_id = _import_then_strip_version(conn, YUAN_CSV, "yuan.csv", "v1-yuan")
    # 造一条无法自洽的明细：原值按元算是 10.00 元，库存却写 500 分
    conn.execute("UPDATE fact_orders SET amount_raw = '10.00', amount_cents = 500"
                 " WHERE batch_id = ? AND row_no = 1", (batch_id,))
    outcome = migrate.run(conn)
    assert outcome["status"] == "failed"
    assert "不自洽" in outcome["error"]
    # 回滚后旧结果完整保留
    assert migrate.status(conn)["applied"] is False
    assert store.get_batch(conn, batch_id)["total_amount_cents"] == 3550


def test_empty_batch_aborts_migration(conn):
    _legacy_db(conn)
    conn.execute(
        "INSERT INTO batches(batch_id, filename, sha256, encoding, bom, row_count,"
        " total_amount_cents, total_qty, forced, created_at)"
        " VALUES('B-GHOST', 'ghost.csv', 'g' * 64, 'utf-8', 0, 1, 100, 1, 0,"
        " '2026-11-08 00:00:00')")
    outcome = migrate.run(conn)
    assert outcome["status"] == "failed"
    assert "没有任何明细行" in outcome["error"]
    assert migrate.status(conn)["applied"] is False


# ---------------------------------------------------------------- 老库升级路径

# 升级前的旧库结构（无 contract_version / amount_raw 列），用于验证真实升级路径
LEGACY_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE batches (
  batch_id TEXT PRIMARY KEY, filename TEXT NOT NULL, sha256 TEXT NOT NULL,
  encoding TEXT NOT NULL, bom INTEGER NOT NULL DEFAULT 0,
  row_count INTEGER NOT NULL, total_amount_cents INTEGER NOT NULL,
  total_qty INTEGER NOT NULL, forced INTEGER NOT NULL DEFAULT 0,
  superseded_by TEXT, created_at TEXT NOT NULL, note TEXT);
CREATE TABLE fact_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL, row_no INTEGER NOT NULL,
  order_id INTEGER NOT NULL, site TEXT NOT NULL, category TEXT NOT NULL,
  order_date TEXT NOT NULL, amount_cents INTEGER NOT NULL, qty INTEGER NOT NULL,
  memo TEXT);
CREATE TABLE batch_aggregates (
  batch_id TEXT NOT NULL, site TEXT NOT NULL, amount_cents INTEGER NOT NULL,
  qty INTEGER NOT NULL, row_count INTEGER NOT NULL, PRIMARY KEY (batch_id, site));
CREATE TABLE import_attempts (
  attempt_id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT NOT NULL,
  sha256 TEXT NOT NULL, encoding TEXT, status TEXT NOT NULL,
  error_count INTEGER NOT NULL DEFAULT 0, row_count INTEGER NOT NULL DEFAULT 0,
  batch_id TEXT, detail TEXT, errors_json TEXT, created_at TEXT NOT NULL);
"""


def test_upgrade_from_old_schema_database(tmp_path):
    """旧版应用留下的库文件：列都不存在 → 补列 → 单事务迁移 → 数据可查。"""
    import sqlite3

    db = tmp_path / "legacy.sqlite3"
    raw = sqlite3.connect(str(db))
    raw.executescript(LEGACY_SCHEMA)
    raw.execute(
        "INSERT INTO batches(batch_id, filename, sha256, encoding, bom, row_count,"
        " total_amount_cents, total_qty, forced, created_at)"
        " VALUES('B-OLD-1', 'old.csv', 'o' * 64, 'utf-8', 0, 2, 3050, 3, 0,"
        " '2026-11-01 09:00:00')")
    raw.execute(
        "INSERT INTO fact_orders(batch_id, row_no, order_id, site, category, order_date,"
        " amount_cents, qty) VALUES('B-OLD-1', 1, 1001, 'beijing', 'grocery',"
        " '2026-11-01', 1000, 2)")
    raw.execute(
        "INSERT INTO fact_orders(batch_id, row_no, order_id, site, category, order_date,"
        " amount_cents, qty) VALUES('B-OLD-1', 2, 1002, 'shanghai', 'grocery',"
        " '2026-11-01', 2050, 1)")
    raw.execute(
        "INSERT INTO batch_aggregates VALUES('B-OLD-1', 'beijing', 9999, 2, 1)")   # 错的汇总
    raw.execute(
        "INSERT INTO batch_aggregates VALUES('B-OLD-1', 'shanghai', 2050, 1, 1)")
    raw.commit()
    raw.close()

    conn = store.connect(db)
    store.init_schema(conn)                     # 启动时补列（幂等）
    outcome = migrate.run(conn)                 # 升级后一次性切换
    assert outcome["status"] == "applied"
    assert outcome["stamped_batches"] == 1
    assert outcome["backfilled_details"] == 2

    batch = store.get_batch(conn, "B-OLD-1")
    assert batch["contract_version"] == "v1-yuan"
    assert batch["total_amount_cents"] == 3050
    by_site = {g["site"]: g for g in batch["aggregates"]}
    assert by_site["beijing"]["amount_cents"] == 1000      # 错的 9999 已被重算修正
    raws = [r["amount_raw"] for r in batch["sample_rows"]]
    assert raws == ["10.00", "20.50"]                       # 按元口径回填原值

    rerun = migrate.run(conn)
    assert rerun["status"] == "already_applied"
    assert store.stats(conn)["batches"] == 1
    conn.close()


# ---------------------------------------------------------------- API 层

def test_api_migration_endpoints(client):
    # 启动即自动迁移（seed 批次盖章 + 重算），GET 可见状态
    status_body = client.get("/api/migration").json()
    assert status_body["applied"] is True
    assert status_body["batches_by_version"] == {"v1-yuan": 1}

    batches_before = client.get("/api/batches").json()["stats"]["batches"]
    rerun = client.post("/api/migration")
    assert rerun.status_code == 200
    assert rerun.json()["status"] == "already_applied"   # 重跑同一升级不新增批次
    assert client.get("/api/batches").json()["stats"]["batches"] == batches_before

    # 旧版（seed）批次结果仍可查
    seed_page = client.get("/result/B-20261107-001")
    assert seed_page.status_code == 200
    assert "v1-yuan" in seed_page.text


def test_migration_form_endpoint_redirects(client):
    res = client.post("/migration/run", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"].startswith("/review?migration=")
    page = client.get(res.headers["location"])
    assert page.status_code == 200
    assert "契约迁移" in page.text


def test_health_reports_migration(client):
    body = client.get("/health").json()
    assert body["contract_migration"]["applied"] is True
    assert body["contract_migration"]["migration_id"] == migrate.MIGRATION_ID
