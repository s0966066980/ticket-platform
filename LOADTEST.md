# LOADTEST：壓測情境、結果與瓶頸分析

所有數字均來自本文件記錄的實際執行；原始輸出保存在 `loadtests/results/`（fixtures 與 sessions 含測試憑證，不入版控；`results/` 亦不入版控，本文件引用的 JSON／CSV 在交付機器上保留）。腳本與執行方式見 `loadtests/README.md`。

## 測試機器與環境

| 項目 | 值 |
| --- | --- |
| 主機 | Intel Core Ultra 7 265KF，20 logical CPU，30 GiB RAM，NVMe（`pg_test_fsync` fdatasync 約 0.32 ms） |
| OS | Ubuntu（Linux 7.0.0-42-generic） |
| 容器 | Docker 29.9.1，Compose v5.6.0；受測系統為 `docker-compose.yml` + `docker-compose.load.yml`（獨立專案 `ticket-load`，port 8081，乾淨 volume） |
| 受測配置 | `API_WORKERS=4`（4 個 uvicorn 程序，各 40 執行緒）、PostgreSQL 16 `max_connections=300`、連線池 20+20／程序、`DATABASE_POOL_TIMEOUT_SECONDS=5`、Nginx 1 worker／core、65,536 fd |
| 負載產生器 | 同一台機器上的 Locust 2.47（單一程序）與 `websockets` 17；因此結果是「負載產生器與受測系統共用 20 核」的保守數字 |
| 程式版本 | commit `__COMMIT__`（`integration/mvp`） |

限制：同機壓測使客戶端與伺服器互相搶 CPU；`p50/p95/p99` 以客戶端（Locust）觀測為準，包含 Nginx 排隊與網路時間，伺服器端 API 處理時間另記於 `access_logs`。

## 情境 A：單一活動 10,000 座位、3,000 虛擬使用者持續 60 秒

設定：`scripts/generate_load_data.py booking` 直接以 SQL 建立 3,000 個帳號與一個 10,000 席已發布活動；`prepare_sessions.py` 先完成 3,000 次登入；Locust 以 100 users/s 啟動，待全部 3,000 人就緒後**重設統計**再量測 60 秒穩態。每人反覆「訂 1 張（70% 區域配位、30% 指定座位）→ 付款成功」，思考時間 50–200 ms，受真實的每人 4 張限購約束。

### 最終結果（commit `__COMMIT__`，2026-11-17 01:37 UTC）

| 端點 | 請求數 | 失敗 | p50 | p95 | p99 | max | req/s |
| --- | --- | --- | --- | --- | --- | --- | --- |
| POST bookings | 18,505 | 0 | 6.5 s | 9.3 s | 11.0 s | 15.0 s | 284 |
| POST payments | 10,000 | 0 | 6.9 s | 8.5 s | 9.9 s | 11.0 s | 154 |
| 合計 | 28,505 | 0 (0%) | 6.7 s | 9.0 s | 10.0 s | 15.0 s | 438 |

業務結果（HTTP 狀態 × 錯誤碼）：`201` 10,000（**10,000 席全部售出**）、`202` 10,000 筆付款受理、`409 SEAT_UNAVAILABLE` 3,081、`409 INSUFFICIENT_SEATS` 2,206、`409 SEAT_CONTENTION` 292（可重試）、`409 QUOTA_EXCEEDED` 32。系統錯誤率 0%（無 5xx、無客戶端逾時）；Nginx 無 error／crit 日誌。

壓測結束後一致性檢查（`scripts/check_consistency.py --require-settled`，`loadtests/results/final/booking-consistency.json`）：**通過**——重複售出座位 0、已付款明細／座位不符 0、訂單張數／金額不符 0、超過限購 0、有效保留遺失 0、無效保留 0、付款不符 0、事件版本缺口 0、缺少 Outbox 0；待清理逾時訂單 0、待沖正 0、未發布 Outbox 0。

### 第一次執行（基線，修正前，同一機器）

| 指標 | 基線 | 最終 |
| --- | --- | --- |
| 合計 req/s | 256 | 438 |
| 失敗率 | 49%（7,475 客戶端 15 秒逾時 + 174 HTTP 500） | 0% |
| 成功購票／60 s | 2,365 | 10,000（售罄） |
| p50 / p95 | 4.8 s / 16 s（逾時） | 6.7 s / 9.0 s |

### 瓶頸分析與修正過程

1. **活動版本列的序列化區段過長**（主要瓶頸）。設計上每個活動的所有座位變更都要在 `event_streams` 列鎖下分配版本。基線實作在取得版本鎖之後才做售票時段、座位可售、限購等檢查與寫入，連被拒絕的請求都排隊；`pg_stat_activity` 在 1,500 人診斷中顯示 160 個 API 執行緒有約 150 個同時處於 `Lock:tuple`，等待同一列。修正分兩步：(a) 所有拒絕判斷與座位條件式 UPDATE 移到版本鎖之前（額度列鎖與座位列鎖已足以保證限購與不超賣）；(b) 版本區段縮為三次往返：`UPDATE event_streams … RETURNING`（取鎖並配號）→ 一個在鎖下重新讀取活動並寫入 `seat_events` 與 Outbox 的 CTE → COMMIT；回應 body、冪等完成、回呼紀錄、稽核都在進鎖前 flush。
2. **Nginx 檔案描述元耗盡**。3,000 條同時連線使 Nginx 記錄 `accept4() failed (24: No file descriptors available)`，請求在邊緣排隊超過客戶端 15 秒逾時，部分以 499／500 結束。修正：`worker_processes auto`、`worker_rlimit_nofile 65536`、`worker_connections 16384`、上游 keepalive 256。
3. **連線池等待不受控**。SQLAlchemy 預設 `pool_timeout=30s` 讓請求在池上排隊 30 秒而非快速失敗；改為 5 秒並把池逾時／連線中斷映射為可重試的 `503 DATABASE_UNAVAILABLE`（帶 `Retry-After`，客戶端沿用同一 Idempotency-Key）。
4. **API 程序數**。4 個 uvicorn 程序配 20+20 連線池；實驗以 8 個程序（8 × 40 = 320 > `max_connections` 300）造成大量 503／502，且負載產生器本身已達 118% CPU，因此維持 4 個程序作為此機器上的受測配置，不以更多程序換取無法量測的數字。

剩餘瓶頸：穩態下 3,000 人幾乎零思考時間地重送，API 在約 440 req/s 飽和，排隊造成 6–7 秒的客戶端延遲；伺服器端每請求處理時間（含在池與鎖上的等待）同量級。單一活動的版本配號仍是序列化點（成功購票與付款成交各佔一次），本次最終結果中成功購票 10,000 筆 + 付款 10,000 筆在 60 秒內完成，相當於每秒約 330 次版本配號。要進一步提升需跨機器的負載產生器與多個 API 容器，以及對 `seat_events`／`outbox_events` 寫入的批次化；這些未執行，不列數字。

## 情境 B：2,000 條即時連線同時訂閱同一活動

設定：`loadtests/websocket_benchmark.py`，2,000 條經 Nginx 的 WebSocket 連線以同一 Viewer 身分完成應用層 token 握手並取得同一份 HTTP 快照；另一帳號在 60 秒穩態期間反覆「保留一席 → 取消」產生真實座位變更。每次變更量測「HTTP 請求送出 → 各連線收到對應版本的 frame」的客戶端延遲（nearest-rank 百分位），檢查版本連續、重同步次數與斷線。

__SCENARIO_B__

## 批次匯入：100,000 列 60 秒內完成

設定：`scripts/generate_load_data.py seats` 產生 100,000 列 CSV（1,292,033 bytes）與 JSON（6,392,003 bytes）；`loadtests/import_benchmark.py` 建立草稿活動、上傳、每 500 ms 輪詢 job 直到終態，記錄受理時間與完成時間。

__IMPORT__

## Excel：500,000 筆訂單、API 記憶體上限 512 MiB

設定：`scripts/generate_load_data.py report` 以 SQL 建立 500,000 筆一張票訂單（多數為歷史取消／逾時，已付款子集受限購約束）；`loadtests/limited_api.py` 以 `RLIMIT_AS = 512 MiB`（位址空間，比 RSS 更嚴）啟動單一 API 程序；`report_benchmark.py` 串流下載、每 20 ms 取樣 `/proc/<pid>/status` 的 VmRSS 峰值，並以唯讀模式掃描工作簿驗證三個工作表與 500,000 列。

__EXCEL__

## 故障實驗（非效能，補充 DESIGN Q2）

- **worker `kill -9`**（`loadtests/results/final/kill9.txt`）：購票提交後 0.18 秒以 SIGKILL 殺掉 worker，此時該活動有 1 筆未發布 Outbox；同時把該訂單期限改到過去。`docker compose start worker` 後 2.87 秒內：Outbox 全部發布、訂單收斂為 EXPIRED、座位回到 AVAILABLE、`event_streams` 版本 3／已發布 3；無人工介入。
- **故意破壞不超賣保護**（`loadtests/results/final/sabotage-q1.txt`）：移除座位 `FOR UPDATE` 與條件式 UPDATE 述詞後，併發測試得到 `[201, 201]`，同一座位賣給兩個帳號；還原後測試通過。

## 未執行或有條件的項目

- 跨機器壓測、多 API 容器水平擴展、長時間（>60 秒）穩定性：未執行。
- 主機斷電／儲存故障下的持久性：僅以程序 `kill -9` 驗證，不代表硬體故障已驗證。
- 所有情境皆在負載產生器與受測系統同機的條件下量測。
