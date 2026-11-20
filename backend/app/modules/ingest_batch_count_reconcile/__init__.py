"""装载批次数对账（功能题实现）。

落库形态（二选一之选型）：``ingest_batches.report_json`` 不可变快照。
失败闭（二选一之选型）：业务变更在 SAVEPOINT 内先写后算，对账不一致整批回滚，
失败批次行在外层事务留痕，状态 ``reconcile_failed``。

本模块只放纯对账逻辑：基线快照、库内实际变化派生、报告比对、只读再核证。
四个必备字段：events_inserted / occupancies_opened / occupancies_closed / held_count。
"""

import json
from datetime import datetime, timezone

STATUS = "implemented"

REQUIRED_FIELDS = (
    "events_inserted",
    "occupancies_opened",
    "occupancies_closed",
    "held_count",
)

BATCH_COMMITTED = "committed"
BATCH_RECONCILE_FAILED = "reconcile_failed"
VALID_STATUSES = (BATCH_COMMITTED, BATCH_RECONCILE_FAILED)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _existing_event_ids(conn, event_ids):
    if not event_ids:
        return set()
    marks = ",".join("?" for _ in event_ids)
    rows = conn.execute(
        f"SELECT id FROM parcel_events WHERE id IN ({marks})", tuple(event_ids)
    ).fetchall()
    return {r["id"] for r in rows}


def _occupancy_sets(conn):
    rows = conn.execute("SELECT id, status FROM occupancies").fetchall()
    all_ids = {r["id"] for r in rows}
    released_ids = {r["id"] for r in rows if r["status"] == "released"}
    return all_ids, released_ids


def snapshot(conn, event_ids):
    """批前基线：全部来自库内实读，不依赖内存计数。"""
    occ_ids, released_ids = _occupancy_sets(conn)
    return {
        "event_ids": set(event_ids),
        "events_before": _existing_event_ids(conn, event_ids),
        "occ_before": occ_ids,
        "released_before": released_ids,
        "quarantine_before": conn.execute(
            "SELECT COUNT(*) AS c FROM quarantine"
        ).fetchone()["c"],
    }


def actual_changes(conn, base, batch_id, event_ids):
    """批后从库内派生本批实际变化（对账的「真值」侧）。"""
    events_after = _existing_event_ids(conn, event_ids)
    occ_after, released_after = _occupancy_sets(conn)
    held = conn.execute(
        "SELECT COUNT(*) AS c FROM quarantine WHERE batch_id=?", (batch_id,)
    ).fetchone()["c"]
    return {
        "events_inserted": len(events_after - base["events_before"]),
        "occupancies_opened": len(occ_after - base["occ_before"]),
        "occupancies_closed": len(released_after - base["released_before"]),
        "held_count": held,
    }


def verify(reported, actual):
    """逐字段比对，返回 (是否一致, 不一致明细)。"""
    mismatches = []
    for field in REQUIRED_FIELDS:
        r = reported.get(field)
        a = actual.get(field)
        if r != a:
            mismatches.append({"field": field, "reported": r, "actual": a})
    return not mismatches, mismatches


def build_report(batch_id, status, reported, actual, mismatches, total, created_at=None):
    """构造将落库的不可变快照（report_json）。"""
    if status not in VALID_STATUSES:
        raise ValueError(f"非法批次状态: {status}")
    return {
        "batch_id": batch_id,
        "status": status,
        "created_at": created_at or _now(),
        "total": total,
        "accepted": reported["events_inserted"],
        **{f: reported[f] for f in REQUIRED_FIELDS},
        "actual": {f: actual[f] for f in REQUIRED_FIELDS},
        "mismatches": mismatches,
    }


def reverify(conn, batch_row):
    """只读再核证：对已落库快照做一次库内实时交叉核对，不产生任何写入。

    返回不可变快照 + 实时派生值 + 当前比对结论。
    """
    report = json.loads(batch_row["report_json"])
    event_ids = report.get("event_ids", [])
    live = {"events_inserted": 0, "occupancies_opened": 0, "occupancies_closed": 0}

    if event_ids:
        marks = ",".join("?" for _ in event_ids)
        live["events_inserted"] = conn.execute(
            f"SELECT COUNT(*) AS c FROM parcel_events WHERE id IN ({marks})",
            tuple(event_ids),
        ).fetchone()["c"]

    # 按快照锚定的占用 id 精确回读：新开=行仍在；关闭=行在且 status=released
    opened_ids = report.get("opened_occupancy_ids", [])
    closed_ids = report.get("closed_occupancy_ids", [])
    occ_ids = [*opened_ids, *closed_ids]
    if occ_ids:
        marks = ",".join("?" for _ in occ_ids)
        rows = conn.execute(
            f"SELECT id, status FROM occupancies WHERE id IN ({marks})", tuple(occ_ids)
        ).fetchall()
        present = {r["id"] for r in rows}
        released = {r["id"] for r in rows if r["status"] == "released"}
        live["occupancies_opened"] = sum(1 for oid in opened_ids if oid in present)
        live["occupancies_closed"] = sum(1 for oid in closed_ids if oid in released)
    live["held_count"] = conn.execute(
        "SELECT COUNT(*) AS c FROM quarantine WHERE batch_id=?",
        (batch_row["id"],),
    ).fetchone()["c"]

    snapshot_counts = {f: report.get(f) for f in REQUIRED_FIELDS}
    ok, mismatches = verify(snapshot_counts, live)
    return {
        "batch_id": batch_row["id"],
        "status": batch_row["status"],
        "snapshot": report,
        "live_actual": live,
        "live_matches_snapshot": ok,
        "live_mismatches": mismatches,
    }
