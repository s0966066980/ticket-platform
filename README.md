# 高併發活動搶票與即時監控平台

FastAPI 模組化單體，搭配 React 購票介面、PostgreSQL 座位／訂單交易、Redis 即時通知，以及獨立模擬金流。完整規格與已確認決策分別見 [goal.md](goal.md)、[實作設計](docs/implementation-plan.md)、[核心 Schema](docs/core-schema.md) 和 [驗收清單](docs/acceptance-plan.md)。

## 技術棧與服務

| 服務 | 技術與用途 |
| --- | --- |
| nginx | 唯一對外入口、API 與 WebSocket 反向代理 |
| frontend | React、Vite、TypeScript；build 後以 Nginx 提供靜態檔 |
| backend | Python 3.13、FastAPI、SQLAlchemy 2、psycopg、Alembic |
| worker | 匯入、逾時、Outbox、付款處理與監控背景工作 |
| mock-gateway | 獨立模擬金流程序與簽章回呼 |
| postgres | PostgreSQL 16；訂單與座位唯一真實來源 |
| redis | Redis 7；Pub/Sub 與持久化診斷 Streams |

Excel 使用 OpenPyXL；自動化測試使用 Pytest、前端測試與 PostgreSQL 16；完整壓測另依 `docs/acceptance-plan.md` 執行。

## Docker 一鍵啟動

需要 Docker Engine 與 Compose plugin，以及可用的 Docker daemon 權限。

```bash
cp .env.example .env
docker compose up -d --build
```

不複製 `.env` 時也能使用 Compose 中相同的本機展示預設值。環境參數與預設帳密的用途見 `.env.example`；若修改 DB 使用者、資料庫或密碼，需一併更新 `DATABASE_URL`。

- 操作介面：<http://localhost:8080>
- Swagger：<http://localhost:8080/docs>
- OpenAPI：<http://localhost:8080/openapi.json>
- API 前綴：`/api/v1`
- 即時座位：`/ws/events/{event_id}`，第一則訊息進行 token 認證
- 健康檢查：<http://localhost:8080/health>

啟動時 backend 在資料庫 advisory lock 內依序執行 migration 與可重複的 seed，避免多副本啟動競爭；worker 和 mock-gateway 在 backend 健康後啟動，避免它們搶先使用尚未建立的資料表。資料庫與 Redis 使用 healthcheck 控制 readiness。[Compose 啟動順序說明](https://docs.docker.com/compose/how-tos/startup-order/)

```bash
docker compose ps
docker compose logs -f backend worker mock-gateway
docker compose down
```

Nginx 將不含查詢字串的 JSON 存取日誌保存在 proxy-logs volume，worker 可由 NGINX_LOG_PATH 批次讀取並關聯 request_id。

一般 `down` 保留命名 volume 中的資料。`docker compose down -v` 會刪除資料庫、Redis、上傳與診斷 volume，僅用於確定要清空的展示環境。

## 自動建立的測試帳號

| 角色 | Email | 本機展示密碼 |
| --- | --- | --- |
| Admin | admin@example.test | Admin-local-2026! |
| User | user@example.test | User-local-2026! |
| Viewer | viewer@example.test | Viewer-local-2026! |

可於首次啟動前透過 `.env` 的 `SEED_*` 設定調整。Seed 重跑不應覆寫已修改的既有帳號。

## 本機開發與測試

需要 Python 3.13+、Node.js 24、npm，以及 PostgreSQL **16** server binaries。安裝依賴後，以單一指令執行測試：

```bash
PYTHON_BIN=python3.13 ./scripts/setup.sh
./scripts/run_tests.sh
```

腳本先驗證 CLI 合約，再建立獨立的暫存 PostgreSQL 16 cluster，執行 migration 與後端測試，最後執行前端測試及 production build。測試結束停止自己啟動的 cluster，保留位置可查的日誌；不會啟動或停止系統資料庫。測試來源不改用 SQLite。

```bash
./scripts/run_tests.sh --backend-only
./scripts/run_tests.sh --frontend-only
PG_BIN=/usr/lib/postgresql/16/bin ./scripts/pg16.sh check
python3 -m unittest discover -s scripts/tests -v
docker compose config --quiet
```

如需手動啟動專用測試 DB：

```bash
./scripts/pg16.sh start
./scripts/pg16.sh url
./scripts/pg16.sh stop
```

只接受名稱以 `ticket_test` 開頭的專用測試資料庫。也可明確提供 `TEST_DATABASE_URL`；此資料庫必須可丟棄，測試可能重設資料。`PG_BIN`、`PG_TEST_PORT` 及 `PG_RUNTIME_DIR` 的用法見 `./scripts/pg16.sh --help`。

後端開發入口（已準備自己的 development database／Redis 時）：

```bash
cd backend
../.venv/bin/python -m alembic upgrade head
../.venv/bin/python -m app.seed
../.venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

另一終端可在 `frontend/` 執行 `npm run dev`。Host 開發必須將 `DATABASE_URL`、`REDIS_URL` 等改成實際可連線位址；Compose 內的服務名稱不代表 host 可解析的名稱。

## 已知問題與未完成驗收

目前部署票單完成的是配置與 CLI 工具；其餘功能進度以 `.scratch/mvp/issues/` 為準。

- 此工作階段無法存取 Docker socket，因此尚未實際 build 或啟動七個容器；`docker compose config` 的成功只代表配置有效。
- 本機 PG16 binaries 已確認存在，但此工作階段禁止 TCP／Unix socket 綁定，無法由這裡證明 migration 或交易測試通過。
- 依賴安裝需可用的套件網路。受限環境的安裝失敗須明確回報，不能以靜默跳過測試代替成功。
- 指定的 100K／60 秒匯入、500K 訂單／512MB Excel、3,000 搶票者、2,000 WebSocket 與故障恢復情境，尚待真正執行並記錄原始證據。
- `DESIGN.md`、`LOADTEST.md`、`AI_USAGE.md` 與完整操作／部署驗證由整合交付票單補齊，不預填量測數字。

開發過程採 integration branch 與獨立票單 worktree，保留階段 commit，GitHub 目標為私有 `s0966066980/ticket-platform`。
