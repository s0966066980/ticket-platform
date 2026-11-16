# 第一版本（MVP）基本架構與開發執行計劃

**專案名稱：** 高併發活動搶票與即時監控平台  
**開發期限：** 2026/11/18  
**開發平台：** Ubuntu 26.04 LTS  
**開發模式：** Modular Monolith（模組化單體架構）  
**版本目標：** V1.0 MVP

---

# 一、第一版本的交付目標

第一版本必須達到以下標準：

1. Ubuntu 可以透過 Docker Compose 一鍵啟動所有服務。
2. 系統自動建立 Admin、User、Viewer 三種測試帳號。
3. 使用者可以註冊、登入、登出。
4. 可以建立活動、設定區域與票價。
5. 可以匯入 CSV / JSON 座位資料。
6. 可以瀏覽座位圖及即時座位狀態。
7. 可以指定座位或區域自動配位。
8. 可以完成訂單、模擬付款及取消訂單。
9. 具備防止座位超賣及每人限購 4 張機制。
10. 管理者可以查看即時流量、延遲、錯誤率及連線數。
11. 可以產生 Excel 銷售報表。
12. 具備基本自動化測試及併發測試。
13. GitHub 保留完整 Commit 歷史。

# 二、第一版本技術架構

## 2.1 技術選型

| 分類 | 技術 | 第一版用途 |
|---|---|---|
| OS | Ubuntu 26.04 | 開發與部署 |
| Backend | FastAPI | REST API、WebSocket |
| Database | PostgreSQL 16 | 訂單、座位、使用者 |
| ORM | SQLAlchemy 2.0 | 資料庫操作 |
| Migration | Alembic | Schema 管理 |
| Cache | Redis 7 | Pub/Sub、暫存 |
| Background Worker | Python Worker | 匯入、逾時、事件處理 |
| Frontend | React + Vite | 使用者與管理介面 |
| UI | Tailwind CSS | 介面樣式 |
| Chart | Recharts | 監控圖表 |
| Excel | OpenPyXL | 銷售報表 |
| Testing | Pytest | 自動化測試 |
| Load Test | Locust | 併發壓測 |
| Deployment | Docker Compose | 一鍵部署 |

第一版不拆分成多個獨立微服務。Backend 以模組化方式實作，Worker 獨立執行，降低部署及除錯成本。

## 2.2 系統架構圖

```text
                  Users / Admin / Viewer
                           |
                           v
                     React Frontend
                           |
                           v
                     Nginx Gateway
                           |
               +-----------+-----------+
               |                       |
               v                       v
          FastAPI REST API         WebSocket
               |                       |
       +-------+--------+              |
       |       |        |              |
       v       v        v              |
      Auth  Booking   Events            |
       |       |        |              |
       +-------+--------+              |
               |                       |
               v                       |
          PostgreSQL                   |
               |                       |
          Outbox Events                |
               |                       |
               v                       |
          Python Worker                |
               |                       |
               v                       |
             Redis <-------------------+
               |
               v
         Event Broadcasting

       PostgreSQL
            |
            +---- Metrics
            +---- Access Logs
            +---- Seat Events
            +---- Orders
            |
            v
       Admin Dashboard
```

### 架構原則

- PostgreSQL 是唯一真實資料來源。
- Redis 不負責決定座位是否售出。
- 座位鎖定必須在 PostgreSQL Transaction 內完成。
- Redis Pub/Sub 負責即時通知，不保證事件持久性。
- Outbox 負責事件可靠傳遞。
- 客戶端使用事件序號偵測漏失，必要時重新取得座位快照。
- Worker 可以重複執行，但業務操作必須具備冪等性。

# 三、專案目錄架構

第一版統一採用以下 Repository 結構：

```text
ticket-platform/
│
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── database.py
│   │   ├── dependencies.py
│   │   │
│   │   ├── models/
│   │   │   ├── user.py
│   │   │   ├── event.py
│   │   │   ├── seat.py
│   │   │   ├── order.py
│   │   │   ├── payment.py
│   │   │   └── monitoring.py
│   │   │
│   │   ├── schemas/
│   │   ├── api/
│   │   │   └── v1/
│   │   │       ├── auth.py
│   │   │       ├── users.py
│   │   │       ├── events.py
│   │   │       ├── seats.py
│   │   │       ├── bookings.py
│   │   │       ├── payments.py
│   │   │       ├── reports.py
│   │   │       └── admin.py
│   │   │
│   │   ├── services/
│   │   │   ├── auth_service.py
│   │   │   ├── event_service.py
│   │   │   ├── import_service.py
│   │   │   ├── booking_service.py
│   │   │   ├── payment_service.py
│   │   │   └── monitoring_service.py
│   │   │
│   │   ├── realtime/
│   │   │   ├── websocket.py
│   │   │   └── broadcaster.py
│   │   │
│   │   ├── workers/
│   │   │   ├── main.py
│   │   │   ├── import_worker.py
│   │   │   ├── expiry_worker.py
│   │   │   └── outbox_worker.py
│   │   │
│   │   └── utils/
│   │
│   ├── migrations/
│   ├── tests/
│   │   ├── unit/
│   │   ├── integration/
│   │   └── concurrency/
│   │
│   ├── requirements.txt
│   └── Dockerfile
│
├── frontend/
│   ├── src/
│   │   ├── api/
│   │   ├── components/
│   │   ├── pages/
│   │   │   ├── Login.tsx
│   │   │   ├── Events.tsx
│   │   │   ├── EventDetail.tsx
│   │   │   ├── MyOrders.tsx
│   │   │   ├── AdminDashboard.tsx
│   │   │   └── AdminEvents.tsx
│   │   ├── hooks/
│   │   ├── stores/
│   │   └── App.tsx
│   ├── package.json
│   └── Dockerfile
│
├── nginx/
│   └── nginx.conf
│
├── scripts/
│   ├── seed.py
│   ├── run_tests.sh
│   └── check_consistency.py
│
├── sample_data/
│   ├── seats.csv
│   └── seats.json
│
├── loadtests/
│   ├── locustfile.py
│   └── websocket_test.py
│
├── docs/
│   └── architecture.png
│
├── docker-compose.yml
├── .env.example
├── .gitignore
├── README.md
├── DESIGN.md
├── LOADTEST.md
├── AI_USAGE.md
└── AGENTS.md
```

# 四、資料庫基本設計

第一版先完成以下核心資料表。

| Table | 主要用途 |
|---|---|
| users | 使用者與角色 |
| sessions | 登入憑證及撤銷 |
| events | 活動資料 |
| zones | 區域與票價 |
| seats | 座位及鎖定狀態 |
| orders | 訂單 |
| order_items | 訂單座位明細 |
| payments | 付款紀錄 |
| idempotency_keys | API 冪等控制 |
| user_event_quotas | 每人每活動購票額度 |
| import_jobs | 座位匯入進度 |
| import_errors | 匯入錯誤報告 |
| outbox_events | 待發布事件 |
| seat_events | 座位狀態事件 |
| access_logs | API 存取日誌 |
| system_metrics | 每秒系統指標 |

## 4.1 座位狀態

```text
AVAILABLE
    |
    v
  HELD
    |
    +------> SOLD
    |
    +------> AVAILABLE
              (取消或逾時)
```

每個座位必須有：

- event_id
- zone_id
- row_label
- seat_number
- status
- held_by
- held_until
- version

資料庫建立唯一限制：

```sql
UNIQUE(event_id, zone_id, row_label, seat_number)
```

指定座位鎖定採用條件式原子 UPDATE，並在同一交易內建立訂單與購票額度保留。

# 五、核心 API 規劃

API 統一使用 `/api/v1`。

| Method | Endpoint | 功能 |
|---|---|---|
| POST | /auth/register | 註冊 |
| POST | /auth/login | 登入 |
| POST | /auth/logout | 登出 |
| GET | /events | 活動列表 |
| POST | /events | 建立活動 |
| GET | /events/{id} | 活動詳細 |
| PATCH | /events/{id} | 修改活動 |
| DELETE | /events/{id} | 刪除活動 |
| POST | /events/{id}/publish | 發布活動 |
| POST | /events/{id}/seats/import | 匯入座位 |
| GET | /imports/{id} | 查詢匯入進度 |
| GET | /events/{id}/seats | 查詢座位 |
| POST | /events/{id}/bookings | 建立訂單 |
| GET | /orders | 我的訂單 |
| GET | /orders/{id} | 訂單詳細 |
| POST | /orders/{id}/cancel | 取消訂單 |
| POST | /payments/callback | 模擬金流回呼 |
| GET | /events/{id}/report | 活動銷售報表 |
| GET | /admin/metrics | 系統指標 |
| GET | /admin/alerts | 系統告警 |
| GET | /admin/logs/{request_id} | 請求追蹤 |

另外提供：

- `GET /docs`：Swagger API 文件。
- `GET /health`：健康檢查。
- `WS /ws/events/{event_id}`：活動即時座位推送。

# 六、第一版本功能開發順序

## Phase 1：基礎架構（11/16）

**目標：完成可啟動的系統。**

- Ubuntu 開發環境。
- GitHub Repository。
- FastAPI。
- PostgreSQL。
- Redis。
- React。
- Docker Compose。
- Alembic。
- 自動 Seed 三種帳號。
- Health Check。
- Swagger。

驗收：`docker compose up -d --build` 能正常啟動所有服務。

## Phase 2：使用者與活動管理（11/16）

- 註冊、登入、登出。
- Admin / User / Viewer。
- 活動 CRUD。
- 活動發布。
- 區域與票價設定。
- 活動列表及分頁。
- 基本權限驗證。

驗收：不同角色可以執行符合權限的操作。

## Phase 3：座位管理及搶票（11/17）

- CSV / JSON 匯入。
- 座位圖。
- 指定座位鎖定。
- 區域自動配位。
- 訂單建立。
- 每人最多 4 張。
- 5% 服務費。
- 模擬付款。
- 訂單取消。
- 逾時釋放。
- Idempotency Key。
- PostgreSQL 交易保護。

驗收：多個使用者同時搶同一座位，只能有一筆成功訂單。

## Phase 4：即時推送與監控（11/17）

- WebSocket。
- Redis Pub/Sub。
- Transactional Outbox。
- 即時座位更新。
- API RPS。
- P50 / P95 / P99。
- 錯誤率。
- 即時連線數。
- Admin Dashboard。
- 基本告警。

驗收：座位被鎖定或售出後，其他觀看者能收到狀態更新。

## Phase 5：報表與自動化測試（11/18）

- Excel 匯出。
- 活動銷售摘要。
- 區域銷售統計。
- 訂單明細。
- Unit Test。
- Integration Test。
- Concurrency Test。
- Locust 基礎壓測。
- 資料一致性檢查。

驗收：主要業務流程全部正常，測試能一行執行。

## Phase 6：文件及正式交付（11/18）

- README.md。
- DESIGN.md。
- LOADTEST.md。
- AI_USAGE.md。
- .env.example。
- 架構圖。
- CSV / JSON 範例。
- Docker Compose 最終驗證。
- GitHub Commit History 檢查。

# 七、第一版本與後續版本的範圍區分

| 功能 | V1 MVP | 後續強化 |
|---|---|---|
| 使用者認證 | 完整基本功能 | 更細緻的安全防護 |
| RBAC | 三種角色 | 系統性權限矩陣 |
| 活動 CRUD | 完整 | 進階搜尋 |
| 座位匯入 | CSV / JSON、背景工作 | 100K / 60 秒最佳化 |
| 座位鎖定 | DB 原子交易 | 更高併發最佳化 |
| 限購 | 原子化 4 張限制 | 複雜促銷規則 |
| 付款 | 模擬金流與回呼 | 故障注入與完整對帳 |
| WebSocket | 即時推送 | 大規模多副本驗證 |
| Outbox | 基本可靠傳遞 | 完整故障恢復測試 |
| Metrics | 即時基本指標 | 高精度分散式聚合 |
| Excel | 三個工作表 | 50 萬筆記憶體最佳化驗收 |
| Load Test | 基礎壓測 | 完整 3,000 / 2,000 壓測 |
| DESIGN.md | 六題基本設計與證據 | 完整破壞性測試證據 |

注意：V1 與後續強化是開發優先順序，不代表規格要求可以直接省略。最終交付時，所有未完成的硬性驗收項目都必須如實標示。

# 八、Docker Compose 服務規劃

第一版統一啟動以下容器：

| Container | 用途 |
|---|---|
| nginx | 前端入口與反向代理 |
| frontend | React 前端 |
| backend | FastAPI API |
| worker | 背景工作 |
| postgres | 主資料庫 |
| redis | 即時事件與快取 |

所有容器透過 Docker Network 互相連接。

啟動：

```bash
docker compose up -d --build
```

檢查：

```bash
docker compose ps
docker compose logs -f backend
docker compose logs -f worker
```

停止：

```bash
docker compose down
```

# 九、第一版本驗收標準

| 測試項目 | 通過條件 |
|---|---|
| Docker | 所有服務正常啟動 |
| Auth | 登入、登出正常 |
| RBAC | 三種角色權限正確 |
| Event | 活動 CRUD 正常 |
| Import | CSV / JSON 匯入正常 |
| Booking | 指定座位購買正常 |
| Auto Allocation | 區域自動配位正常 |
| Overselling | 不會超賣 |
| Quota | 每人每活動最多 4 張 |
| Payment | 模擬付款正常 |
| Cancellation | 訂單取消及釋放正常 |
| WebSocket | 座位即時更新 |
| Dashboard | 即時指標顯示 |
| Excel | 三個工作表正常 |
| Tests | 自動化測試可執行 |
| Documentation | 文件完整 |

# 十、開發管理原則

1. 每個 Phase 完成後立即 Commit。
2. 不允許 AI Agent 未經確認就修改核心資料庫 Schema。
3. 所有交易邏輯必須有自動化測試。
4. 前端不得直接決定座位是否可購買，必須以後端資料庫結果為準。
5. Redis 不得作為訂單或座位狀態的唯一依據。
6. 不允許為了通過測試而移除資料庫唯一限制。
7. 不得在壓測報告中填寫未實際量測的數字。
8. 所有未完成需求必須列入 README 的 Known Issues。
9. 不得 Squash 最終 Git Commit History。

---

# 十一、最終交付目標

在 2026/11/18 前完成一套可以在 Ubuntu 上啟動的搶票平台 MVP。

第一版本最重要的驗收路徑：

**登入 → 建立活動 → 匯入座位 → 發布活動 → 使用者選位 → 鎖定座位 → 建立訂單 → 模擬付款 → 更新座位狀態 → 推送即時事件 → 管理者查看監控 → 匯出 Excel。**

這條流程必須端到端正常運作，並且具備資料庫交易保護及基本自動化測試。

第一版本完成後，再針對 3,000 人搶票、2,000 WebSocket 連線、100,000 筆匯入及 500,000 筆 Excel 匯出進行完整效能最佳化與驗收。
