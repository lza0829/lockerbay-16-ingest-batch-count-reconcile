"""HTTP 层冒烟 + 老库迁移冒烟（stdlib only，真实 uvicorn 进程）。

运行（backend/ 下）：.venv/bin/python -I scripts/http_smoke.py
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PORT = "11199"
BASE = f"http://127.0.0.1:{PORT}/api"
_failures = []


def check(name, cond, detail=""):
    print(f"  [{'通过' if cond else '失败'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode())


def wait_up(proc):
    for _ in range(100):
        if proc.poll() is not None:
            raise RuntimeError("uvicorn 提前退出: " + proc.stderr.read())
        try:
            urllib.request.urlopen(BASE + "/health", timeout=1)
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("服务未在 20s 内就绪")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="lockerbay-http-"))
    data_dir, fix_dir = tmp / "data", tmp / "fixtures"
    data_dir.mkdir()
    fix_dir.mkdir()
    shutil.copy(BACKEND.parent / "fixtures" / "seed_bay.json", fix_dir)
    shutil.copy(BACKEND.parent / "fixtures" / "ingest_batch.json", fix_dir)

    env = {**os.environ, "DATA_DIR": str(data_dir), "FIXTURES_DIR": str(fix_dir)}
    proc = subprocess.Popen(
        [str(BACKEND / ".venv" / "bin" / "python"), "-I", "-m", "uvicorn",
         "app.main:app", "--app-dir", str(BACKEND),
         "--host", "127.0.0.1", "--port", PORT],
        cwd=BACKEND, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        wait_up(proc)
        print("用例 3：HTTP 端到端（装载 → 快照列表 → 单批 → 只读核证）")
        st, health = call("GET", "/health")
        check("health 200", st == 200 and health["正常"] is True)

        st, run = call("POST", "/ingest/run", {})
        check("装载 200 且 committed", st == 200 and run["status"] == "committed", run.get("status"))
        bid = run["batch_id"]
        check("回执四字段齐全", all(f in run["report"] for f in
              ("events_inserted", "occupancies_opened", "occupancies_closed", "held_count")))

        st, batches = call("GET", "/ingest/batches")
        check("批次列表含本批", st == 200 and any(b["batch_id"] == bid for b in batches))
        check("列表行带四字段", all(f in batches[0] for f in
              ("events_inserted", "occupancies_opened", "occupancies_closed", "held_count")))

        st, one = call("GET", f"/ingest/batches/{bid}")
        check("单批快照 200", st == 200 and one["report"]["batch_id"] == bid)

        st, ver = call("GET", f"/ingest/batches/{bid}/verify")
        check("只读核证一致", st == 200 and ver["live_matches_snapshot"] is True, ver)

        try:
            call("GET", "/ingest/batches/B-nope")
            check("不存在批次 404", False)
        except urllib.error.HTTPError as e:
            check("不存在批次 404", e.code == 404)

        # 只读核证不产生写入：核对批次行数量与报告字节不变
        db = sqlite3.connect(data_dir / "lockerbay.db")
        before = db.execute("SELECT COUNT(*) c, LENGTH(report_json) l FROM ingest_batches").fetchone()
        db.close()
        call("GET", f"/ingest/batches/{bid}/verify")
        call("GET", "/ingest/batches")
        db = sqlite3.connect(data_dir / "lockerbay.db")
        after = db.execute("SELECT COUNT(*) c, LENGTH(report_json) l FROM ingest_batches").fetchone()
        db.close()
        check("只读接口零写入", before == after, (before, after))
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(tmp)

    print("用例 4：老库迁移（旧 schema 无 status / batch_id 列）")
    tmp2 = Path(tempfile.mkdtemp(prefix="lockerbay-migrate-"))
    data_dir2, fix_dir2 = tmp2 / "data", tmp2 / "fixtures"
    data_dir2.mkdir()
    fix_dir2.mkdir()
    shutil.copy(BACKEND.parent / "fixtures" / "seed_bay.json", fix_dir2)
    shutil.copy(BACKEND.parent / "fixtures" / "ingest_batch.json", fix_dir2)

    # 手工建旧版库
    db = sqlite3.connect(data_dir2 / "lockerbay.db")
    db.executescript("""
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    INSERT INTO settings VALUES ('overdue_hours','48');
    CREATE TABLE cabinets (id TEXT PRIMARY KEY, label TEXT NOT NULL, site TEXT NOT NULL);
    CREATE TABLE cells (id TEXT PRIMARY KEY, cabinet_id TEXT NOT NULL, code TEXT NOT NULL,
      size_code TEXT NOT NULL DEFAULT 'M', status TEXT NOT NULL DEFAULT 'free', UNIQUE(cabinet_id, code));
    CREATE TABLE parcel_events (id TEXT PRIMARY KEY, cabinet_id TEXT NOT NULL, cell_id TEXT,
      event_type TEXT NOT NULL, event_ts TEXT NOT NULL, tracking_no TEXT,
      payload_json TEXT NOT NULL DEFAULT '{}', channel TEXT NOT NULL DEFAULT 'primary');
    CREATE TABLE occupancies (id TEXT PRIMARY KEY, cell_id TEXT NOT NULL, tracking_no TEXT NOT NULL,
      opened_at TEXT NOT NULL, closed_at TEXT, status TEXT NOT NULL DEFAULT 'occupied', last_event_id TEXT);
    CREATE TABLE quarantine (id INTEGER PRIMARY KEY AUTOINCREMENT, reason TEXT NOT NULL,
      member_json TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE ingest_batches (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, report_json TEXT NOT NULL DEFAULT '{}');
    CREATE TABLE occupancy_proofs (id INTEGER PRIMARY KEY AUTOINCREMENT, occupancy_id TEXT NOT NULL,
      cell_id TEXT NOT NULL, tracking_no TEXT NOT NULL, opened_at TEXT NOT NULL, closed_at TEXT,
      event_ids_json TEXT NOT NULL, created_at TEXT NOT NULL);
    INSERT INTO ingest_batches(id, created_at, report_json) VALUES ('B-legacy','2026-11-18T00:00:00Z','{"accepted":1}');
    """)
    db.commit()
    db.close()

    env2 = {**os.environ, "DATA_DIR": str(data_dir2), "FIXTURES_DIR": str(fix_dir2)}
    code = (
        f"import sys; sys.path.insert(0, r'{BACKEND}');"
        "from app import seed; seed.init_db();"
        "from app.services.ingest import run_batch;"
        "r = run_batch();"
        "import json, sqlite3;"
        "c = sqlite3.connect(__import__('app.db', fromlist=['DB_PATH']).DB_PATH);"
        "c.row_factory = sqlite3.Row;"
        "legacy = c.execute(\"SELECT status, report_json FROM ingest_batches WHERE id='B-legacy'\").fetchone();"
        "print('LEGACY', dict(legacy));"
        "print('BATCH', r['status'], r['report']['events_inserted'], r['report']['held_count']);"
        "cols = [r[1] for r in c.execute('PRAGMA table_info(quarantine)')];"
        "print('QCOLS', cols);"
    )
    out = subprocess.run(
        [str(BACKEND / ".venv" / "bin" / "python"), "-I", "-c", code],
        cwd=BACKEND, env=env2, capture_output=True, text=True,
    )
    if out.returncode != 0:
        check("迁移脚本执行", False, out.stderr)
    else:
        print(out.stdout)
        check("老批次补默认 status=committed 且原 report_json 保留",
              "LEGACY {'status': 'committed', 'report_json': '{\"accepted\":1}'}" in out.stdout)
        check("迁移后新批次正常 committed", "BATCH committed 3 1" in out.stdout, out.stdout)
        check("quarantine 补出 batch_id 列", "QCOLS ['id', 'reason', 'member_json', 'created_at', 'batch_id']" in out.stdout)
    shutil.rmtree(tmp2)

    print()
    if _failures:
        print(f"HTTP/迁移冒烟未通过：{_failures}")
        sys.exit(1)
    print("HTTP/迁移冒烟全部通过")


if __name__ == "__main__":
    main()
