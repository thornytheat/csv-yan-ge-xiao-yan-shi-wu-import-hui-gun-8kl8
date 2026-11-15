"""编排：字节 → 探测 → 契约 → 行校验 → 聚合 → 事务导入。

路由只负责拿参数和渲染；所有业务判定都在这里和下面的纯函数模块里完成。
"""
from __future__ import annotations

import csv
import io
import sqlite3

from . import aggregate, contract, encoding, importer, report, rows as rows_mod, store


def parse_csv(text: str) -> tuple[list[str], list[list[str]], list[int]]:
    """返回 (表头, 数据行, 被跳过的空行行号)。空行只跳过不报错，单独计数。"""
    reader = csv.reader(io.StringIO(text))
    records: list[list[str]] = []
    for record in reader:
        records.append(record)
    if not records:
        return [], [], []
    header = records[0]
    data: list[list[str]] = []
    blank_lines: list[int] = []
    for offset, record in enumerate(records[1:], start=2):
        if not any((cell or "").strip() for cell in record):
            blank_lines.append(offset)
            continue
        data.append(record)
    return header, data, blank_lines


def run(
    raw: bytes,
    filename: str,
    extra_policy: str = "reject",
    force_new_batch: bool = False,
    conn: sqlite3.Connection | None = None,
    fail_after: int | None = None,
    batch_id: str | None = None,
    contract_version: str | None = None,
) -> dict:
    own_conn = conn is None
    conn = conn or store.connect()
    sha = importer.file_sha256(raw)
    try:
        result: dict = {
            "filename": filename,
            "sha256": sha,
            "bytes": len(raw),
            "extra_policy": extra_policy,
            "force_new_batch": force_new_batch,
            "sample_hex": encoding.sample_hex(raw),
        }

        # 0) 契约版本：每次导入都必须解析出明确版本（缺省 = 默认版，兼容旧文件）
        try:
            version = contract.resolve_version(contract_version)
        except contract.UnknownContractVersion as exc:
            result.update({
                "status": "unknown_contract_version",
                "detail": str(exc),
                "available_versions": contract.versions_info(),
                "rows_before": store.count_rows(conn),
                "rows_after": store.count_rows(conn),
            })
            return result
        result["contract_version"] = version
        result["amount_unit"] = contract.amount_unit(version)

        # 1) 编码探测
        try:
            probe = encoding.probe(raw)
        except encoding.EncodingError as exc:
            attempt_id = store.record_attempt(
                conn, filename=filename, sha256=sha, status="encoding_failed",
                detail=exc.message, contract_version=version,
            )
            result.update({
                "status": "encoding_failed",
                "detail": exc.message,
                "encoding": {
                    "encoding": None, "bom": raw.startswith(encoding.UTF8_BOM),
                    "sample_hex": exc.sample_hex, "attempts": exc.attempts,
                },
                "suggestion": exc.message,
                "attempt_id": attempt_id,
                "rows_before": store.count_rows(conn),
                "rows_after": store.count_rows(conn),
            })
            return result

        result["encoding"] = probe.evidence()

        # 2) 表头契约（按解析出的版本校验）
        header, data, blank_lines = parse_csv(probe.text)
        contract_result = contract.check(header, extra_policy, version=version)
        result["contract"] = contract_result
        result["data_row_count"] = len(data)
        result["skipped_blank_lines"] = blank_lines

        if not contract_result["ok"]:
            detail = "；".join(e["message"] for e in contract_result["errors"])
            header_errors = [
                {"line_no": 1, "row_no": 0, "column": ",".join(e.get("columns", [])),
                 "code": e["code"], "label": "表头契约", "raw": "", "reason": e["message"]}
                for e in contract_result["errors"]
            ]
            attempt_id = store.record_attempt(
                conn, filename=filename, sha256=sha, status="rejected",
                encoding=probe.encoding, error_count=len(contract_result["errors"]),
                row_count=len(data), detail=f"表头契约未通过：{detail}",
                errors=header_errors, contract_version=version,
            )
            result.update({
                "status": "rejected",
                "stage": "contract",
                "detail": detail,
                "attempt_id": attempt_id,
                "report": report.build_report([]),
                "rows_before": store.count_rows(conn),
                "rows_after": store.count_rows(conn),
            })
            return result

        # 3) 行级校验（金额按版本口径：v1 元 / v2 分）
        validation = rows_mod.validate(data, contract_result["header"],
                                       contract_result["columns_used"], version=version)
        result["row_validation"] = {
            "ok": validation.ok,
            "row_count": validation.row_count,
            "error_count": validation.error_count,
            "codes": sorted(set(validation.codes())),
        }
        result["report"] = report.build_report(validation.error_dicts())

        if not validation.ok:
            detail = (f"表头契约通过，但 {validation.error_count} 处行级错误，"
                      f"整份文件未写入（首处：第 {result['report']['first_line']} 行）。")
            attempt_id = store.record_attempt(
                conn, filename=filename, sha256=sha, status="rejected",
                encoding=probe.encoding, error_count=validation.error_count,
                row_count=len(data), detail=detail,
                errors=validation.error_dicts(), contract_version=version,
            )
            result.update({
                "status": "rejected",
                "stage": "rows",
                "detail": detail,
                "attempt_id": attempt_id,
                "rows_before": store.count_rows(conn),
                "rows_after": store.count_rows(conn),
            })
            return result

        # 4) 聚合 + 一致性自证（Decimal 复核按版本口径换算）
        summary = aggregate.summarize(validation.rows, amount_unit=result["amount_unit"])
        result["aggregate"] = summary

        # 5) 事务导入（含重复文件判定：同 sha256 + 同契约版本才算重复）
        outcome = importer.import_batch(
            conn, filename=filename, sha256=sha, rows=validation.rows,
            aggregates=summary, encoding=probe.encoding, bom=probe.bom,
            force_new_batch=force_new_batch, fail_after=fail_after, batch_id=batch_id,
            contract_version=version,
        )
        result["import"] = outcome
        result["status"] = outcome["status"]
        result["batch_id"] = outcome.get("batch_id")
        result["rows_before"] = outcome.get("rows_before")
        result["rows_after"] = outcome.get("rows_after")
        result["written"] = outcome.get("written", 0)
        if outcome["status"] in ("duplicate_file", "rolled_back"):
            result["detail"] = outcome.get("duplicate_hint") or outcome.get("detail", "")
        if outcome.get("duplicate_hint"):
            result["duplicate_hint"] = outcome["duplicate_hint"]
        return result
    finally:
        if own_conn:
            conn.close()
