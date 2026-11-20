# 格湾（LockerBay）

**骨架：** 入柜/取件事件装载 → 格口占用物化 → 超时回收 → 占用账对账

- 领域：数据工程（去皮=事件装载 + 占用状态物化 + 超时闸/回收 + 占用核证）
- 端口：前端 **6100**，接口 **11100**（避开 Chrome 封锁的 6000–6063）
- 公有仓前缀：`lza0829/lockerbay-NN-*`
- 非仓库库存 CRUD、非运单轨迹轴、非营业日封账、非会话窗切分

本地冒烟：

```powershell
cd claude-workspace/lockerbay/_scaffold/backend
pip install -r requirements.txt
$env:DATA_DIR="$PWD/data"; $env:FIXTURES_DIR="$PWD/../fixtures"
python -m uvicorn app.main:app --host 127.0.0.1 --port 11100

cd ../frontend
npm install
npm run dev
```

## 装载批次对账（ingest_batch_count_reconcile）

每批装载物化不可变对账报告（`ingest_batches.report_json`，触发器禁 UPDATE/DELETE），
必备四字段 `events_inserted / occupancies_opened / occupancies_closed / held_count`，
报告值与库内本批实际变化逐字段核对；不一致则 SAVEPOINT 整批回滚，批次标
`reconcile_failed` 留痕，绝不冒充成功。只读核证接口：

- `GET /api/ingest/batches`：批次快照列表
- `GET /api/ingest/batches/{id}`：单批不可变快照
- `GET /api/ingest/batches/{id}/verify`：库内实时再核证（零写入）

本地夹具与 SQLite 冒烟（均用临时隔离库，不污染开发库）：

```bash
cd backend
.venv/bin/python -I scripts/reconcile_smoke.py   # 对账/撕裂回滚/异常回滚/幂等
.venv/bin/python -I scripts/http_smoke.py        # HTTP 端到端 + 老库迁移
```

撕裂注入（仅测试用）：`LOCKERBAY_TEAR_MODE=drop-event|occ-open|hold-row`。
