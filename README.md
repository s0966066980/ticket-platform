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

需要 Python 3.13+、Node.js 24、npm、PostgreSQL 16 server binaries（`/usr/lib/postgresql/16/bin`），以及 Docker（測試用 Redis）。第一次先安裝依賴，之後一行執行全部測試：

```bash
PYTHON_BIN=python3.13 ./scripts/setup.sh    # 第一次：建立 .venv 並安裝後端與前端依賴
./scripts/run_tests.sh                      # 之後每次：一行執行全部測試
```

`PYTHON_BIN` 指向 Python 3.13 直譯器（與 CI、Docker 映像相同的版本）；PATH 上沒有 `python3.13` 時改用完整路徑，例如 uv 安裝的 `~/.local/share/uv/python/cpython-3.13.*/bin/python3`。省略時使用 `python3`，版本低於 3.13 會直接拒絕。

`run_tests.sh` 依序執行：CLI 合約測試 → 建立拋棄式 PostgreSQL 16 cluster（從 55432 起找第一個空閒埠）→ 啟動拋棄式 Redis 7 容器 → migration 與全部後端測試（含併發、即時推送、worker）→ 前端測試與 production build。結束時自動停止資料庫並移除 Redis 容器；任何一項失敗，結束碼即非 0。不需要先啟動應用程式，也不會碰到展示或壓測堆疊。

| 情況 | 指令 |
| --- | --- |
| 只跑後端或前端 | `./scripts/run_tests.sh --backend-only`、`--frontend-only` |
| 指定測試資料庫埠 | `PG_TEST_PORT=55440 ./scripts/run_tests.sh` |
| 使用自己的 Redis | `REDIS_URL=redis://127.0.0.1:6379/0 ./scripts/run_tests.sh` |
| 使用自備的拋棄式資料庫 | `TEST_DATABASE_URL=postgresql+psycopg://<user>@<host>:<port>/ticket_test ./scripts/run_tests.sh`（名稱必須以 `ticket_test` 開頭） |

沒有 Docker 時，依賴 Redis 的測試會被標示為跳過並列出原因；CI 一律提供 Redis 並拒絕任何跳過。

**測試與壓測的差別**：測試驗證功能是否正確，自己建立並刪除資料庫，不需要啟動系統；壓測量測效能，需要整套系統在獨立的 `ticket-load` 堆疊（埠 8081，資料庫埠 55433）上執行，見下方「壓測」。瀏覽器 E2E 則需要展示堆疊（埠 8080）。三者埠號互不衝突，可以同時存在。

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

瀏覽器端到端測試（`e2e/`，pytest + Playwright，Chromium headless）直接操作 compose 啟動的完整系統（`docker compose up -d --build`）：以購票會員建立活動、匯入 `sample_data/seats.csv`、發布，在第二個瀏覽器情境以檢視者驗證座位即時推送（鎖定／售出／釋放）、模擬付款、下載 Excel 報表、Admin 監控與 request_id 追蹤，以及 `invalid_seats.csv` 的錯誤報告。每個測試使用全新的瀏覽器情境且不重試，失敗時在 `e2e/artifacts/` 留下截圖與 Playwright trace（`playwright show-trace`）。依賴獨立於後端：

```bash
UV_CACHE_DIR=.runtime/uv-cache uv pip install --python .venv/bin/python -r e2e/requirements.txt
.venv/bin/python -m playwright install chromium
E2E_BASE_URL=http://localhost:8080 ./scripts/run_e2e.sh      # 先等待 /health，再執行 .venv/bin/python -m pytest e2e -q
```

CI 的第三個 job `e2e` 在前兩個 job 通過後以同一份 compose 檔啟動整套服務執行此套件，並一律上傳 `e2e/artifacts/` 與 `docker compose logs`。

## 壓測

壓測使用獨立的 compose 專案 `ticket-load`（`docker-compose.yml` 加上 `docker-compose.load.yml` 覆蓋層，設定在 `loadtests/load.env`）：對外埠 8081、PostgreSQL 發布在 127.0.0.1:55433 供 fixture 產生與一致性稽核使用，資料與展示堆疊完全分開。`scripts/loadtest.sh` 是唯一入口：

```bash
./scripts/loadtest.sh all       # 啟動壓測堆疊並依序執行 A、B、匯入、Excel 四個情境
./scripts/loadtest.sh up        # 只啟動壓測堆疊
./scripts/loadtest.sh a         # 情境 A：3,000 人搶 10,000 席 60 秒，結束後一致性稽核
./scripts/loadtest.sh b         # 情境 B：2,000 條 WebSocket 60 秒，含資源取樣與一致性稽核
./scripts/loadtest.sh import    # 100,000 列 CSV 與 JSON 匯入
./scripts/loadtest.sh excel     # 50 萬筆訂單 Excel，單一 API 程序 512 MiB 上限（使用 127.0.0.1:8000）
./scripts/loadtest.sh down      # 停止並刪除壓測堆疊與其資料
```

每次執行的原始輸出放在 `loadtests/results/<時間戳>/`，fixture（含測試帳號與 token）放在 `loadtests/fixtures/<時間戳>/`，兩者都不入版控。實際結果、機器規格與瓶頸分析見 [LOADTEST.md](LOADTEST.md)；各腳本的細節見 [loadtests/README.md](loadtests/README.md)。

