import json
import os
from datetime import datetime, timezone
from uuid import uuid4

from app.db import FIXTURES_DIR, connect
from app.modules import ingest_batch_count_reconcile as reconcile

# 撕裂注入仅用于对账失败回滚测试，生产路径恒为 None / 空串
TEAR_MODES = ("drop-event", "occ-open", "hold-row")


def run_batch(tear: str | None = None):
    """跑一批夹具：先在 SAVEPOINT 内写业务变更，再按库内实际变化对账。

    对账一致 → 释放保存点、批次 committed；不一致 → 回滚保存点（业务全撤），
    批次 reconcile_failed 留痕。失败闭走「先写后算、不一致整批回滚」路径。
    """
    if tear is None:
        tear = os.environ.get("LOCKERBAY_TEAR_MODE") or None
    if tear not in (None, *TEAR_MODES):
        raise ValueError(f"非法撕裂模式: {tear}")

    path = FIXTURES_DIR / "ingest_batch.json"
    if not path.exists():
        return {"batch_id": None, "status": "no_fixture", "message": "无夹具"}
    data = json.loads(path.read_text(encoding="utf-8"))
    events = data.get("events", [])
    event_ids = [ev["id"] for ev in events]

    batch_id = f"B-{uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = connect()
    conn.isolation_level = None  # 显式事务，自行 BEGIN/COMMIT
    try:
        base = reconcile.snapshot(conn, event_ids)

        # 报告侧计数（随业务写入累计），稍后必须与库内真值逐字段对上
        reported = {
            "events_inserted": 0,
            "occupancies_opened": 0,
            "occupancies_closed": 0,
            "held_count": 0,
        }
        opened_ids = []
        closed_ids = []
        inserted_event_ids = []

        conn.execute("BEGIN")
        conn.execute("SAVEPOINT sp_batch")
        try:
            for ev in events:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO parcel_events(id, cabinet_id, cell_id, event_type, event_ts, tracking_no, payload_json, channel) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        ev["id"],
                        ev["cabinet_id"],
                        ev.get("cell_id"),
                        ev["event_type"],
                        ev["event_ts"],
                        ev.get("tracking_no"),
                        json.dumps(ev.get("payload", {}), ensure_ascii=False),
                        "primary",
                    ),
                )
                if cur.rowcount == 1:
                    reported["events_inserted"] += 1
                    inserted_event_ids.append(ev["id"])
                else:
                    # 事件已在库（历史批次处理过）：不得重复物化占用
                    continue

                etype = ev["event_type"]
                cell_id = ev.get("cell_id")
                if etype not in ("drop", "pickup") or not cell_id:
                    _hold(conn, batch_id, ev, "事件类型未识别或缺少格口，无法物化")
                    reported["held_count"] += 1
                elif etype == "drop":
                    cell = conn.execute(
                        "SELECT status FROM cells WHERE id=?", (cell_id,)
                    ).fetchone()
                    if cell is None:
                        _hold(conn, batch_id, ev, "格口不存在")
                        reported["held_count"] += 1
                    elif cell["status"] != "free":
                        _hold(conn, batch_id, ev, f"格口 {cell_id} 非 free，投柜搁置")
                        reported["held_count"] += 1
                    else:
                        oid = f"OCC-{cell_id}-{ev['id']}"
                        conn.execute(
                            "INSERT INTO occupancies(id, cell_id, tracking_no, opened_at, closed_at, status, last_event_id) VALUES (?,?,?,?,?,?,?)",
                            (oid, cell_id, ev["tracking_no"], ev["event_ts"], None, "occupied", ev["id"]),
                        )
                        conn.execute("UPDATE cells SET status='occupied' WHERE id=?", (cell_id,))
                        reported["occupancies_opened"] += 1
                        opened_ids.append(oid)
                else:  # pickup
                    cell = conn.execute(
                        "SELECT status FROM cells WHERE id=?", (cell_id,)
                    ).fetchone()
                    row = None
                    if cell is not None:
                        row = conn.execute(
                            "SELECT id FROM occupancies WHERE cell_id=? AND status='occupied' ORDER BY opened_at DESC LIMIT 1",
                            (cell_id,),
                        ).fetchone()
                    if cell is None or row is None:
                        _hold(conn, batch_id, ev, "无在占占用可关闭，取件搁置")
                        reported["held_count"] += 1
                    else:
                        conn.execute(
                            "UPDATE occupancies SET closed_at=?, status='released', last_event_id=? WHERE id=?",
                            (ev["event_ts"], ev["id"], row["id"]),
                        )
                        conn.execute("UPDATE cells SET status='free' WHERE id=?", (cell_id,))
                        reported["occupancies_closed"] += 1
                        closed_ids.append(row["id"])

            # 人为撕裂：在对账前破坏库内一致性
            if tear == "drop-event" and inserted_event_ids:
                conn.execute(
                    "DELETE FROM parcel_events WHERE id=?", (inserted_event_ids[0],)
                )
            elif tear == "occ-open" and opened_ids:
                conn.execute("DELETE FROM occupancies WHERE id=?", (opened_ids[0],))
            elif tear == "hold-row":
                conn.execute("DELETE FROM quarantine WHERE batch_id=?", (batch_id,))

            # 真值侧：全部从库内本批实际变化派生
            actual = reconcile.actual_changes(conn, base, batch_id, event_ids)
            ok, mismatches = reconcile.verify(reported, actual)

            if ok:
                conn.execute("RELEASE SAVEPOINT sp_batch")
                status = reconcile.BATCH_COMMITTED
            else:
                # 账不平：业务变更整批回滚，不得冒充成功
                conn.execute("ROLLBACK TO SAVEPOINT sp_batch")
                conn.execute("RELEASE SAVEPOINT sp_batch")
                status = reconcile.BATCH_RECONCILE_FAILED
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT sp_batch")
            conn.execute("RELEASE SAVEPOINT sp_batch")
            raise

        report = reconcile.build_report(
            batch_id, status, reported, actual, mismatches, len(events), created_at=now
        )
        # 精确锚定本批涉及的实体，供只读再核证按 id 回读（不做后缀猜测）
        report["event_ids"] = event_ids
        report["opened_occupancy_ids"] = opened_ids
        report["closed_occupancy_ids"] = closed_ids
        conn.execute(
            "INSERT INTO ingest_batches(id, created_at, status, report_json) VALUES (?,?,?,?)",
            (batch_id, now, status, json.dumps(report, ensure_ascii=False)),
        )
        conn.commit()
        return {"batch_id": batch_id, "status": status, "report": report}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _hold(conn, batch_id, ev, reason):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO quarantine(reason, member_json, created_at, batch_id) VALUES (?,?,?,?)",
        (reason, json.dumps(ev, ensure_ascii=False), now, batch_id),
    )
