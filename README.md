# 高併發活動搶票與即時監控平台

一場熱門演唱會開賣的瞬間，數千名使用者同時搶購有限座位：本專案是一套以 PostgreSQL 交易為唯一真實來源的搶票平台，附帶營運人員與管理者使用的即時監控後台。後端為 FastAPI 模組化單體（API、worker、模擬金流三個程序共用領域服務），前端為 React 即時座位圖與管理介面，Redis 只負責事件扇出與遙測交接，不決定座位是否售出。

- 需求：[goal.md](goal.md)（最終驗收依據）、[project_01.md](project_01.md)（MVP 分期）
- 設計：[docs/design-interview.md](docs/design-interview.md)（Q1–Q18 已確認決策）、[docs/implementation-plan.md](docs/implementation-plan.md)、[docs/core-schema.md](docs/core-schema.md)、[GLOSSARY.md](GLOSSARY.md)、[docs/adr/](docs/adr/)
- 架構圖：[docs/architecture.svg](docs/architecture.svg)（來源與說明：[docs/architecture.md](docs/architecture.md)）
- 設計問答：[DESIGN.md](DESIGN.md)　壓測：[LOADTEST.md](LOADTEST.md)　AI 使用：[AI_USAGE.md](AI_USAGE.md)
- 驗收對照：[docs/acceptance-plan.md](docs/acceptance-plan.md)

## 技術棧與服務

| 服務 | 技術與用途 |
| --- | --- |
| nginx | 唯一對外入口（預設 8080）；反向代理 API、WebSocket、Swagger；輸出含 request_id 的 JSON 存取日誌 |
| frontend | React 19、Vite、TypeScript、Tailwind、Recharts；build 後由 Nginx 提供靜態檔 |
| backend | Python 3.13、FastAPI、SQLAlchemy 2、psycopg 3、Alembic、Argon2；`API_WORKERS` 個 uvicorn 程序 |
| worker | 背景工作：鎖位逾時、座位匯入、Outbox 發布、付款／沖正／查帳、遙測收集、每秒指標、告警 |
| mock-gateway | 獨立模擬金流：持久化 charge、延遲與重試的 HMAC 簽章回呼、冪等沖正、查帳 |
| postgres | PostgreSQL 16；訂單、座位、付款、匯入、事件版本、Outbox、日誌與指標的唯一真實來源 |
| redis | Redis 7（AOF always）；Pub/Sub 座位事件扇出、Streams 遙測交接 |

測試：Pytest（PostgreSQL 16 + Redis 7 整合與併發測試）、Vitest + Testing Library；壓測：Locust 與 websockets；報表：OpenPyXL write-only。

## Docker 一鍵啟動

需要 Docker Engine 與 Compose plugin。

```bash
cp .env.example .env          # 可省略：Compose 內建相同的本機展示預設值
docker compose up -d --build
docker compose ps             # 七個服務：nginx、frontend、backend、worker、mock-gateway、postgres、redis
```

- 操作介面：<http://localhost:8080>
- API 文件（Swagger UI）：<http://localhost:8080/docs>；OpenAPI：<http://localhost:8080/openapi.json>；前端使用的契約摘要：[docs/api-contract.md](docs/api-contract.md)
- API 前綴 `/api/v1`；即時座位 WebSocket `/ws/events/{event_id}`（第一則訊息 `{"type":"auth","token":"…"}`）
- 健康檢查：<http://localhost:8080/health>

啟動時 backend 在資料庫 advisory lock 內依序執行 migration 與可重複的 seed；worker 與 mock-gateway 在 backend 健康後才啟動。`docker compose down` 保留資料；`docker compose down -v` 會刪除資料庫、Redis、上傳檔與遙測 volume。

```bash
docker compose logs -f backend worker mock-gateway
docker compose down
```

## 自動建立的測試帳號

| 角色 | Email | 本機展示密碼 | 權限 |
| --- | --- | --- | --- |
| Admin | admin@example.test | Admin-local-2026! | 管理所有活動、訂單、使用者；監控、告警、request_id 查詢 |
| User | user@example.test | User-local-2026! | 建立活動、匯入座位、發布、鎖位與付款；只能改自己的活動、看自己的訂單與報表 |
| Viewer | viewer@example.test | Viewer-local-2026! | 瀏覽已發布活動、座位與所有活動的報表 |

可在首次啟動前以 `.env` 的 `SEED_*` 變更；seed 重跑不覆寫已修改的帳號。

## 端到端操作路徑

登入 → 建立活動（區域與票價）→ 匯入座位（`sample_data/seats.csv`、`seats.json`；`invalid_seats.csv` 示範錯誤報告）→ 發布 → 使用者選位或區域配位（每筆 1–4 張、每人每活動 4 張、服務費 5% 四捨五入）→ 模擬付款（成功／失敗、可設延遲）→ 座位狀態即時推送 → Admin 查看每秒指標與告警 → 下載 Excel 銷售報表（Summary／Zones／Orders，Asia/Taipei）。

## 環境參數

全部參數與說明見 [.env.example](.env.example)：資料庫與 Redis 位址、金流簽章秘密、測試帳號、鎖位 5 分鐘、冪等租約與保留期、清單快照 10 分鐘、匯入上限（20,000,000 bytes／100,000 列）、worker 與 Outbox 輪詢、WebSocket 佇列與版本核對週期、監控窗口與告警門檻、API 程序數與連線池。

## 本機開發與測試

需要 Python 3.13+、Node.js 24、npm，以及 PostgreSQL 16 server binaries（`/usr/lib/postgresql/16/bin`）。一行執行全部測試：

```bash
PYTHON_BIN=python3.13 ./scripts/setup.sh
REDIS_URL=redis://127.0.0.1:6379/0 ./scripts/run_tests.sh
```

`run_tests.sh` 先驗證 CLI 合約，再建立獨立的暫存 PostgreSQL 16 cluster、執行 migration 與後端測試（含併發測試），最後執行前端測試與 production build。後端測試不以 SQLite 代替。即時推送與 worker 測試需要可連線的 Redis 7（`REDIS_URL`）；未設定時這些測試會被跳過並明確列出，CI 則強制要求不得跳過。

```bash
./scripts/run_tests.sh --backend-only
./scripts/run_tests.sh --frontend-only
TEST_DATABASE_URL=postgresql+psycopg://ticket_test@127.0.0.1:55432/ticket_test ./scripts/run_tests.sh   # 自備可丟棄的 ticket_test* 資料庫
```

後端開發入口（自備 PostgreSQL／Redis 時）：

```bash
cd backend
../.venv/bin/python -m alembic upgrade head
../.venv/bin/python -m app.seed
../.venv/bin/python -m uvicorn app.main:app --reload --port 8000
../.venv/bin/python -m app.worker            # 或 --once 執行單一週期
../.venv/bin/python -m uvicorn app.gateway:app --port 8001
```

前端於 `frontend/` 執行 `npm run dev`。CI（GitHub Actions）在 PostgreSQL 16 與 Redis 7 service container 上執行同一套測試並保留 JUnit 證據。

## 壓測

腳本與執行方式見 [loadtests/README.md](loadtests/README.md)；實際結果、機器規格與瓶頸分析見 [LOADTEST.md](LOADTEST.md)。壓測使用獨立的 compose 專案（`docker-compose.load.yml` 覆蓋層，對 localhost 發布 PostgreSQL 以便產生 fixture 與一致性稽核）：

```bash
docker compose -p ticket-load --env-file .env.load -f docker-compose.yml -f docker-compose.load.yml up -d --build
```

## 已知問題與未完成項目

依 `goal.md` 逐項對照，未完成或有條件通過者如實列出：

- **告警只涵蓋系統錯誤率、p95 延遲與 Outbox 延遲**；業務拒絕率（例如全部回 409 的購票失敗飆升）與金流失敗率有指標但沒有告警規則，沖正卡住也只能在 Admin 後台查詢（DESIGN Q6.2）。
- **壓測在單機上執行**（負載產生器與受測系統同一台機器），數字受彼此影響；情境 A 的客戶端逾時與瓶頸分析見 `LOADTEST.md`，未達成的目標不宣稱通過。
- **已付款訂單的退票與退款**不在範圍（設計訪談 Q3），取消只適用於未付款訂單。
- **慢速 WebSocket 客戶端**的策略是斷線重同步；半開連線在前端 15 秒看門狗觸發前畫面可能停留舊狀態（DESIGN Q4.3）。
- **每秒指標保留每桶的原始延遲樣本**以精確合併百分位，資料量隨流量線性成長，尚無保留期清理。
- **匯入錯誤報告**對檔案內重複列的所有出現都標記錯誤（而非只標第二次出現），比需求更嚴格。
- **瀏覽器自動化 E2E** 未納入 CI；前端以 Vitest 行為測試與 compose 上的手動／腳本煙霧測試驗證。
- Redis 持久化失敗或磁碟滿時遙測會記錄遺失數並標示資料不完整，不會阻擋業務請求；這是刻意的取捨。

開發過程採整合分支與獨立票單 worktree，交付時以 merge commit 併入 `main` 並刪除其餘分支；完整 commit 歷史保留在 `main`，交付倉庫為私有 `s0966066980/ticket-platform`。
