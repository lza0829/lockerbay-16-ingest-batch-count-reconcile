"""装载批次对账本地夹具冒烟（SQLite，隔离临时库）。

覆盖：
1. 正常一批：报告四字段与库内独立派生逐字段核对；搁置行带 batch_id；
   不可变快照触发器；只读再核证；幂等重跑。
2. 人为撕裂（两种模式）：批次 reconcile_failed，业务变更整批回滚，库内前后无变化，
   失败批次行留痕且记录差异明细。

运行（在 backend/ 下）：.venv/bin/python -I scripts/reconcile_smoke.py
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

PASS = "通过"
FAIL = "失败"
_failures = []


def check(name, cond, detail=""):
    print(f"  [{PASS if cond else FAIL}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


def dump_tables(conn):
    out = {}
    for table in ("parcel_events", "occupancies", "cells", "quarantine"):
        out[table] = sorted(
            tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        )
    return out


def make_workspace(tear=None):
    tmp = Path(tempfile.mkdtemp(prefix="lockerbay-reconcile-"))
    data_dir = tmp / "data"
    fix_dir = tmp / "fixtures"
    data_dir.mkdir()
    fix_dir.mkdir()
    shutil.copy(BACKEND.parent / "fixtures" / "seed_bay.json", fix_dir / "seed_bay.json")
    shutil.copy(BACKEND.parent / "fixtures" / "ingest_batch.json", fix_dir / "ingest_batch.json")
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["FIXTURES_DIR"] = str(fix_dir)
    if tear:
        os.environ["LOCKERBAY_TEAR_MODE"] = tear
    else:
        os.environ.pop("LOCKERBAY_TEAR_MODE", None)
    # 延迟导入，确保环境变量生效
    for mod in list(sys.modules):
        if mod == "app" or mod.startswith("app."):
            del sys.modules[mod]
    from app import seed
    from app.services import ingest as ingest_svc
    from app.modules import ingest_batch_count_reconcile as reconcile
    from app.db import connect

    seed.init_db()
    return tmp, connect, ingest_svc, reconcile


def case_normal():
    print("用例 1：正常一批 + 四字段对账 + 搁置 + 不可变 + 再核证 + 幂等")
    tmp, connect, ingest_svc, reconcile = make_workspace()
    conn = connect()
    before_events = conn.execute("SELECT COUNT(*) c FROM parcel_events").fetchone()["c"]
    before_occ = conn.execute("SELECT COUNT(*) c FROM occupancies").fetchone()["c"]
    before_rel = conn.execute(
        "SELECT COUNT(*) c FROM occupancies WHERE status='released'"
    ).fetchone()["c"]
    before_q = conn.execute("SELECT COUNT(*) c FROM quarantine").fetchone()["c"]

    result = ingest_svc.run_batch()
    bid = result["batch_id"]
    check("批次状态 committed", result["status"] == "committed", result["status"])
    rep = result["report"]
    check("报告含四字段", all(f in rep for f in reconcile.REQUIRED_FIELDS))
    check("events_inserted=3", rep["events_inserted"] == 3, rep)
    check("occupancies_opened=1", rep["occupancies_opened"] == 1, rep)
    check("occupancies_closed=1", rep["occupancies_closed"] == 1, rep)
    check("held_count=1", rep["held_count"] == 1, rep)

    # 库内独立派生（不读报告，只信库）
    actual_events = conn.execute(
        "SELECT COUNT(*) c FROM parcel_events WHERE id IN ('E5','E6','E7')"
    ).fetchone()["c"]
    actual_opened = conn.execute(
        "SELECT COUNT(*) c FROM occupancies WHERE id='OCC-CAB-A-03-E5'"
    ).fetchone()["c"]
    actual_closed = conn.execute(
        "SELECT COUNT(*) c FROM occupancies WHERE id='OCC-CAB-A-02-E2' AND status='released'"
    ).fetchone()["c"]
    held_rows = conn.execute(
        "SELECT reason, member_json, batch_id FROM quarantine WHERE batch_id=?", (bid,)
    ).fetchall()
    check("库内事件增量=3", actual_events == 3)
    check("库内新开占用=1（OCC-CAB-A-03-E5）", actual_opened == 1)
    check("库内关闭占用=1（E2 转 released）", actual_closed == 1)
    check("库内搁置=1 且带 batch_id", len(held_rows) == 1 and held_rows[0]["batch_id"] == bid)
    check("搁置原因指向格口非 free", "非 free" in held_rows[0]["reason"], held_rows[0]["reason"])
    check("搁置成员即 E7", json.loads(held_rows[0]["member_json"])["id"] == "E7")

    # 总数勾稽：报告字段与库内总变化一致
    check(
        "事件总数勾稽",
        conn.execute("SELECT COUNT(*) c FROM parcel_events").fetchone()["c"]
        == before_events + rep["events_inserted"],
    )
    check(
        "占用总数勾稽",
        conn.execute("SELECT COUNT(*) c FROM occupancies").fetchone()["c"]
        == before_occ + rep["occupancies_opened"],
    )
    check(
        "关闭总数勾稽",
        conn.execute("SELECT COUNT(*) c FROM occupancies WHERE status='released'").fetchone()["c"]
        == before_rel + rep["occupancies_closed"],
    )
    check(
        "搁置总数勾稽",
        conn.execute("SELECT COUNT(*) c FROM quarantine").fetchone()["c"]
        == before_q + rep["held_count"],
    )

    # 格口状态物化
    cell03 = conn.execute("SELECT status FROM cells WHERE id='CAB-A-03'").fetchone()["status"]
    cell02 = conn.execute("SELECT status FROM cells WHERE id='CAB-A-02'").fetchone()["status"]
    cellb01 = conn.execute("SELECT status FROM cells WHERE id='CAB-B-01'").fetchone()["status"]
    check("CAB-A-03 转 occupied", cell03 == "occupied", cell03)
    check("CAB-A-02 转 free", cell02 == "free", cell02)
    check("CAB-B-01 保持 occupied（搁置未动账）", cellb01 == "occupied", cellb01)

    # 落库形态：report_json 不可变快照
    row = conn.execute("SELECT * FROM ingest_batches WHERE id=?", (bid,)).fetchone()
    check("批次行已落库且 status=committed", row is not None and row["status"] == "committed")
    stored = json.loads(row["report_json"])
    check("落库快照四字段与回执一致", all(stored[f] == rep[f] for f in reconcile.REQUIRED_FIELDS))
    check("快照含 actual 真值侧", stored["actual"]["held_count"] == 1)
    immutable = True
    try:
        conn.execute("UPDATE ingest_batches SET status='x' WHERE id=?", (bid,))
        immutable = False
    except sqlite3.Error:
        conn.rollback()
    try:
        conn.execute("DELETE FROM ingest_batches WHERE id=?", (bid,))
        immutable = False
    except sqlite3.Error:
        conn.rollback()
    check("不可变触发器拦截 UPDATE/DELETE", immutable)
    row_again = conn.execute("SELECT status FROM ingest_batches WHERE id=?", (bid,)).fetchone()
    check("拦截后批次行原样保留", row_again is not None and row_again["status"] == "committed")

    # 只读再核证
    verify = reconcile.reverify(conn, row)
    check("只读再核证：快照与库内实时一致", verify["live_matches_snapshot"] is True, verify)
    check("再核证四字段回读正确", verify["live_actual"] == {
        "events_inserted": 3,
        "occupancies_opened": 1,
        "occupancies_closed": 1,
        "held_count": 1,
    }, verify["live_actual"])

    # 幂等：重跑同夹具，事件已存在不得重复物化
    second = ingest_svc.run_batch()
    check("重跑仍 committed", second["status"] == "committed")
    check("重跑四字段全 0（无重复物化）", all(second["report"][f] == 0 for f in reconcile.REQUIRED_FIELDS),
          second["report"])
    check(
        "重跑后占用数不膨胀",
        conn.execute("SELECT COUNT(*) c FROM occupancies").fetchone()["c"] == before_occ + 1,
    )
    conn.close()
    shutil.rmtree(tmp)


def case_tear(tear_mode, mismatch_field):
    print(f"用例 2：人为撕裂 {tear_mode} → reconcile_failed 且整批回滚")
    tmp, connect, ingest_svc, reconcile = make_workspace(tear=tear_mode)
    conn = connect()
    before = dump_tables(conn)
    result = ingest_svc.run_batch()
    bid = result["batch_id"]
    check("回执状态 reconcile_failed", result["status"] == "reconcile_failed", result["status"])
    rep = result["report"]
    fields = {m["field"] for m in rep["mismatches"]}
    check(f"差异明细含 {mismatch_field}", mismatch_field in fields, rep["mismatches"])

    after = dump_tables(conn)
    check("业务四表整批回滚、前后无变化", before == after)
    check("撕裂事件未残留", conn.execute(
        "SELECT COUNT(*) c FROM parcel_events WHERE id IN ('E5','E6','E7')"
    ).fetchone()["c"] == 0)
    check("本批无搁置行残留（同处保存点）",
          conn.execute("SELECT COUNT(*) c FROM quarantine WHERE batch_id=?", (bid,)).fetchone()["c"] == 0)

    row = conn.execute("SELECT * FROM ingest_batches WHERE id=?", (bid,)).fetchone()
    check("失败批次行仍留痕", row is not None and row["status"] == "reconcile_failed")
    stored = json.loads(row["report_json"])
    check("留痕快照记录 reported/actual 两侧",
          all(f in stored and f in stored["actual"] for f in reconcile.REQUIRED_FIELDS))
    check("留痕快照 status 与表列一致", stored["status"] == "reconcile_failed")
    conn.close()
    shutil.rmtree(tmp)


def case_exception_rollback():
    print("用例 3：业务异常（非对账失败）→ 整事务回滚且无批次行冒充成功")
    tmp, connect, ingest_svc, reconcile = make_workspace()
    conn = connect()
    before = dump_tables(conn)
    batch_count_before = conn.execute("SELECT COUNT(*) c FROM ingest_batches").fetchone()["c"]
    conn.close()

    # 让占用物化在写库中途抛错：用代理连接拦截 occupancies 插入
    import app.services.ingest as ingest_mod

    class Boom(Exception):
        pass

    class ConnProxy:
        def __init__(self, real):
            object.__setattr__(self, "_real", real)

        def execute(self, sql, params=()):
            if sql.lstrip().upper().startswith("INSERT INTO OCCUPANCIES"):
                raise Boom("模拟占用物化写库失败")
            return self._real.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._real, name)

        def __setattr__(self, name, value):
            setattr(self._real, name, value)

    orig_connect = ingest_mod.connect
    ingest_mod.connect = lambda: ConnProxy(orig_connect())
    try:
        raised = False
        try:
            ingest_svc.run_batch()
        except Boom:
            raised = True
        check("异常向上抛出", raised)
    finally:
        ingest_mod.connect = orig_connect

    conn = connect()
    after = dump_tables(conn)
    check("异常路径业务四表零变化", before == after)
    check("异常路径无批次行落库（不冒充成功）",
          conn.execute("SELECT COUNT(*) c FROM ingest_batches").fetchone()["c"] == batch_count_before)
    check("无本批事件残留",
          conn.execute("SELECT COUNT(*) c FROM parcel_events WHERE id IN ('E5','E6','E7')").fetchone()["c"] == 0)
    conn.close()
    shutil.rmtree(tmp)


if __name__ == "__main__":
    case_normal()
    case_tear("drop-event", "events_inserted")
    case_tear("occ-open", "occupancies_opened")
    case_tear("hold-row", "held_count")
    case_exception_rollback()
    print()
    if _failures:
        print(f"冒烟未通过：{len(_failures)} 项 -> {_failures}")
        sys.exit(1)
    print("全部对账冒烟通过")
