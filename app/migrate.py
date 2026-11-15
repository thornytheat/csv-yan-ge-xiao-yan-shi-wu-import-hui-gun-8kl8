"""契约迁移：重算订单明细与站点汇总，并一次性切换到「版本化契约」状态。

背景：金额口径从元切换到分之后，库内可能同时存在新旧两套解释（旧批次没标
契约版本、明细没有原始字符串、站点汇总与明细对不上）。本模块把「升级」做成
一个可重入的动作：

1. 盖章：给没有契约版本的历史批次补上 v1-yuan（历史数据全部是元口径）；
2. 重算明细：为每条明细回填 amount_raw（按批次版本口径由 amount_cents 反推），
   并按版本规则复核 amount_raw → amount_cents 必须一致，不一致即失败；
3. 重算汇总：以明细为准重算每个批次的站点聚合与批次总额，整体重写；
4. 一次性切换：以上全部 + meta.contract_migration 标记在同一个事务里提交；
   任一步失败 → ROLLBACK，库内保留上一份完整结果，不会出现半新半旧；
5. 幂等：重跑同一迁移直接返回 already_applied，不新增批次、不改任何行；
   旧版本批次照常可查（/result/{batch_id}、/api/batches）。
"""
from __future__ import annotations

import sqlite3

from . import contract, store

MIGRATION_ID = "contract-versioning-2026.12.1"
META_KEY = "contract_migration"


class MigrationError(RuntimeError):
    """迁移过程中发现无法自洽的数据；触发整体回滚。"""


def status(conn: sqlite3.Connection) -> dict:
    """迁移状态：是否已应用、还残留多少未盖章/未回填的行。"""
    store.ensure_columns(conn)
    applied = store.get_meta(conn, META_KEY) == MIGRATION_ID
    batches = conn.execute("SELECT COUNT(*) AS c FROM batches").fetchone()["c"]
    unstamped = conn.execute(
        "SELECT COUNT(*) AS c FROM batches WHERE contract_version IS NULL OR contract_version = ''"
    ).fetchone()["c"]
    missing_raw = conn.execute(
        "SELECT COUNT(*) AS c FROM fact_orders WHERE amount_raw IS NULL"
    ).fetchone()["c"]
    by_version = {
        row["contract_version"] or "legacy": row["c"]
        for row in conn.execute(
            "SELECT contract_version, COUNT(*) AS c FROM batches GROUP BY contract_version"
        )
    }
    return {
        "migration_id": MIGRATION_ID,
        "applied": applied,
        "batches": batches,
        "unstamped_batches": unstamped,
        "details_missing_amount_raw": missing_raw,
        "batches_by_version": by_version,
    }


def _recompute_batch(conn: sqlite3.Connection, batch: sqlite3.Row) -> dict:
    """单批次重算：回填/复核明细，以明细为准重写站点聚合与批次总额。"""
    batch_id = batch["batch_id"]
    version = contract.resolve_version(batch["contract_version"])

    details = conn.execute(
        "SELECT id, site, amount_cents, amount_raw, qty FROM fact_orders"
        " WHERE batch_id = ? ORDER BY row_no",
        (batch_id,),
    ).fetchall()
    if not details:
        raise MigrationError(f"批次 {batch_id} 没有任何明细行，无法重算（疑似历史脏数据）。")

    backfilled = 0
    for row in details:
        raw = row["amount_raw"]
        if raw is None or raw == "":
            raw = contract.format_amount_raw(int(row["amount_cents"]), version)
            conn.execute("UPDATE fact_orders SET amount_raw = ? WHERE id = ?", (raw, row["id"]))
            backfilled += 1
        try:
            derived = contract.parse_amount_cents(raw, version)
        except ValueError as exc:
            raise MigrationError(f"批次 {batch_id} 明细 #{row['id']} 金额原值无法按 {version} 复核：{exc}") from exc
        if derived != int(row["amount_cents"]):
            raise MigrationError(
                f"批次 {batch_id} 明细 #{row['id']} 金额不自洽：库存 {row['amount_cents']} 分，"
                f"按 {version} 口径由原值「{raw}」算得 {derived} 分。"
            )

    site_totals: dict[str, dict] = {}
    total_cents = 0
    total_qty = 0
    for row in details:
        bucket = site_totals.setdefault(row["site"], {"amount_cents": 0, "qty": 0, "row_count": 0})
        bucket["amount_cents"] += int(row["amount_cents"])
        bucket["qty"] += int(row["qty"])
        bucket["row_count"] += 1
        total_cents += int(row["amount_cents"])
        total_qty += int(row["qty"])

    conn.execute("DELETE FROM batch_aggregates WHERE batch_id = ?", (batch_id,))
    for site, bucket in site_totals.items():
        conn.execute(
            "INSERT INTO batch_aggregates(batch_id, site, amount_cents, qty, row_count)"
            " VALUES(?,?,?,?,?)",
            (batch_id, site, bucket["amount_cents"], bucket["qty"], bucket["row_count"]),
        )
    conn.execute(
        "UPDATE batches SET row_count = ?, total_amount_cents = ?, total_qty = ?"
        " WHERE batch_id = ?",
        (len(details), total_cents, total_qty, batch_id),
    )

    return {
        "batch_id": batch_id,
        "contract_version": version,
        "rows": len(details),
        "backfilled_amount_raw": backfilled,
        "total_cents_before": int(batch["total_amount_cents"]),
        "total_cents_after": total_cents,
        "row_count_before": int(batch["row_count"]),
        "changed": (int(batch["total_amount_cents"]) != total_cents
                    or int(batch["total_qty"]) != total_qty
                    or int(batch["row_count"]) != len(details)),
    }


def run(conn: sqlite3.Connection, *, fail_after_batches: int | None = None) -> dict:
    """执行迁移；fail_after_batches 仅供测试注入故障，证明失败时整体回滚。"""
    store.ensure_columns(conn)
    before = status(conn)
    if before["applied"]:
        return {"status": "already_applied", "migration_id": MIGRATION_ID,
                "detail": "同一迁移已应用过，本次不新增批次、不改动任何行。",
                "batches": [], "stamped_batches": 0, "backfilled_details": 0,
                "batches_before": before["batches"], "batches_after": before["batches"]}

    batches_total = before["batches"]
    reports: list[dict] = []
    stamped = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 1) 盖章：历史数据全部产生于元口径时代，NULL 版本 → v1-yuan
        for row in conn.execute(
            "SELECT batch_id FROM batches WHERE contract_version IS NULL OR contract_version = ''"
        ).fetchall():
            conn.execute(
                "UPDATE batches SET contract_version = ? WHERE batch_id = ?",
                (contract.DEFAULT_VERSION, row["batch_id"]),
            )
            stamped += 1

        # 2)+3) 逐批次重算明细与站点汇总
        batches = conn.execute(
            "SELECT * FROM batches ORDER BY created_at ASC, batch_id ASC"
        ).fetchall()
        for index, batch in enumerate(batches):
            reports.append(_recompute_batch(conn, batch))
            if fail_after_batches is not None and index + 1 >= fail_after_batches:
                raise RuntimeError(
                    f"注入故障：处理完第 {index + 1} 个批次后中断（用于证明迁移整体回滚）"
                )

        # 4) 同一事务里落下迁移标记 = 一次性切换
        store.set_meta(conn, META_KEY, MIGRATION_ID)
        conn.execute("COMMIT")
    except Exception as exc:  # noqa: BLE001 - 迁移失败必须整体回滚
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:  # pragma: no cover - 防御：事务未建立时无需回滚
            pass
        after = status(conn)
        return {
            "status": "failed",
            "migration_id": MIGRATION_ID,
            "error": str(exc),
            "detail": (f"迁移失败已整体回滚：{exc}。库内保留上一份完整结果，"
                       "修正后可安全重跑同一迁移。"),
            "rolled_back": True,
            "batches_before": batches_total,
            "batches_after": after["batches"],
            "unstamped_batches_after": after["unstamped_batches"],
        }

    after = status(conn)
    return {
        "status": "applied",
        "migration_id": MIGRATION_ID,
        "detail": (f"迁移完成：{stamped} 个批次盖章契约版本，"
                   f"{sum(r['backfilled_amount_raw'] for r in reports)} 条明细回填原值，"
                   f"{len(reports)} 个批次的站点汇总已按明细重算并一次性切换。"),
        "batches": reports,
        "stamped_batches": stamped,
        "backfilled_details": sum(r["backfilled_amount_raw"] for r in reports),
        "rewritten_aggregates": len(reports),
        "changed_batches": [r["batch_id"] for r in reports if r["changed"]],
        "batches_before": before["batches"],
        "batches_after": after["batches"],
    }
