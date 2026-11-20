import json

from fastapi import APIRouter, Body, HTTPException

from app.db import connect
from app.seed import overdue_hours
from app.services import bay as bay_svc
from app.services import ingest as ingest_svc

router = APIRouter(prefix="/api")


@router.get("/overview")
def overview():
    conn = connect()
    try:
        data = bay_svc.overview(conn)
        data["overdue_hours"] = overdue_hours(conn)
        return data
    finally:
        conn.close()


@router.get("/bay")
def bay_matrix():
    conn = connect()
    try:
        return bay_svc.cabinet_matrix(conn)
    finally:
        conn.close()


@router.get("/occupancies")
def list_occupancies(status: str | None = None):
    conn = connect()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM occupancies WHERE status=? ORDER BY opened_at DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM occupancies ORDER BY opened_at DESC LIMIT 100").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/events")
def list_events():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, cabinet_id, cell_id, event_type, event_ts, tracking_no, channel FROM parcel_events ORDER BY event_ts, id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/quarantine")
def list_quarantine():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, reason, member_json, created_at FROM quarantine ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/occupancy-proofs")
def occupancy_proofs():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM occupancy_proofs ORDER BY id DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest():
    return ingest_svc.run_batch()


@router.get("/ingest/batches")
def list_ingest_batches():
    """只读：批次对账快照列表（最新在前）。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, created_at, status, report_json FROM ingest_batches ORDER BY created_at DESC, id DESC"
        ).fetchall()
        out = []
        for r in rows:
            report = json.loads(r["report_json"])
            out.append(
                {
                    "batch_id": r["id"],
                    "created_at": r["created_at"],
                    "status": r["status"],
                    "events_inserted": report.get("events_inserted"),
                    "occupancies_opened": report.get("occupancies_opened"),
                    "occupancies_closed": report.get("occupancies_closed"),
                    "held_count": report.get("held_count"),
                }
            )
        return out
    finally:
        conn.close()


@router.get("/ingest/batches/{batch_id}")
def get_ingest_batch(batch_id: str):
    """只读：单批次不可变对账快照（report_json 原样返回）。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, created_at, status, report_json FROM ingest_batches WHERE id=?",
            (batch_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "批次不存在")
        return {
            "batch_id": row["id"],
            "created_at": row["created_at"],
            "status": row["status"],
            "report": json.loads(row["report_json"]),
        }
    finally:
        conn.close()


@router.get("/ingest/batches/{batch_id}/verify")
def verify_ingest_batch(batch_id: str):
    """只读核证：对已落库快照做库内实时再核对，不产生任何写入。"""
    from app.modules import ingest_batch_count_reconcile as reconcile

    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, status, report_json FROM ingest_batches WHERE id=?",
            (batch_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "批次不存在")
        return reconcile.reverify(conn, row)
    finally:
        conn.close()


@router.get("/settings")
def get_settings():
    conn = connect()
    try:
        return {"overdue_hours": overdue_hours(conn)}
    finally:
        conn.close()


@router.put("/settings/overdue-hours")
def put_overdue(body: dict = Body(...)):
    hours = int(body.get("overdue_hours", 48))
    if hours < 1:
        raise HTTPException(400, "超时回收至少 1 小时")
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('overdue_hours', ?)",
            (str(hours),),
        )
        conn.commit()
        return {"overdue_hours": hours}
    finally:
        conn.close()
