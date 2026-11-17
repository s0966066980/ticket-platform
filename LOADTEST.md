# LOADTEST：壓測情境、結果與瓶頸分析

所有數字均來自本文件記錄的實際執行。最新一輪是 2026-11-17 10:22–10:28 UTC 在 commit `7d40bc8` 上以 `./scripts/loadtest.sh all` 一次跑完四個情境（全部通過），原始輸出在 `loadtests/results/goalcheck-20261117T102221Z/`；伺服器端處理時間與付款結清時間是執行後以 SQL 查詢壓測資料庫取得，查詢結果另存於同一目錄。前一輪是 08:25–08:31 UTC 在 commit `9ae20b1` 上的執行（`loadtests/results/rerun-20261117T082509Z/`），較早的執行保存在 `loadtests/results/final/`。fixtures 與 sessions 含測試憑證，`results/` 亦不入版控，本文件引用的 JSON／CSV 在交付機器上保留。重現方式：`./scripts/loadtest.sh all`；各步驟細節見 `loadtests/README.md`。

## 測試機器與環境

| 項目 | 值 |
| --- | --- |
| 主機 | Intel Core Ultra 7 265KF，20 logical CPU，30 GiB RAM；Docker 資料位於 Kingston SKC2500M8 2 TB NVMe（`pg_test_fsync` fdatasync 約 0.32 ms） |
| OS | Ubuntu 26.04.1 LTS（Linux 7.0.0-42-generic） |
| 容器 | Docker 29.9.1，Compose v5.6.0；受測系統為 `docker-compose.yml` + `docker-compose.load.yml`（獨立專案 `ticket-load`，對外 8081，設定見 `loadtests/load.env`），以 `./scripts/loadtest.sh` 啟動與執行 |
| 受測配置 | `API_WORKERS=4`（4 個 uvicorn 程序，各 40 執行緒）、PostgreSQL 16 `max_connections=300`、連線池 20+20／程序、`DATABASE_POOL_TIMEOUT_SECONDS=5`、Nginx 1 worker／core、65,536 fd |
| 負載產生器 | 同一台機器上的 Locust 2.47（單一程序）與 `websockets` 17；因此結果是「負載產生器與受測系統共用 20 核」的保守數字 |
| 程式版本 | 每個情境的結果標題各自標示執行時的 commit；這些 commit 都在 `main` 的歷史中 |
| 同機其他服務 | 最新一輪執行時，展示堆疊（`ticket-platform`，8080）、測試用 PostgreSQL／Redis 容器與一組 RAGFlow（5 個容器）也在執行；開跑前主機可用記憶體約 14 GiB（`machine.txt`）。本輪未做開跑前的 CPU 快照；前一輪（`9ae20b1`）的快照合計約 0.1 核。本輪開跑前已執行完 `./scripts/run_tests.sh` 與瀏覽器 E2E，E2E 用的臨時堆疊也已移除 |

限制：同機壓測使客戶端與伺服器互相搶 CPU；`p50/p95/p99` 以客戶端（Locust）觀測為準，包含 Nginx 排隊與網路時間，伺服器端 API 處理時間另記於 `access_logs`。

## 情境 A：單一活動 10,000 座位、3,000 虛擬使用者持續 60 秒

設定：`scripts/generate_load_data.py booking` 直接以 SQL 建立 3,000 個帳號與一個 10,000 席已發布活動；`prepare_sessions.py` 先完成 3,000 次登入；Locust 以 100 users/s 啟動，待全部 3,000 人就緒後**重設統計**再量測 60 秒穩態。每人反覆「訂 1 張（70% 區域配位、30% 指定座位）→ 付款成功」，思考時間 50–200 ms，受真實的每人 4 張限購約束。客戶端逾時 15 秒。穩態結束後腳本等待該活動所有付款結清，檢查沒有付款晚於保留期限，再以 `--require-settled` 做一致性稽核。

### 最新結果（commit `7d40bc8`，穩態 2026-11-17 10:23:27–10:24:27 UTC）

**穩態 60.2 秒**（`booking.json`：Locust 在第 60 秒時的統計，客戶端觀測；逐端點請求數由同一檔案的 HTTP 狀態 × 錯誤碼計數加總）

| 端點 | 請求數 | 失敗 | req/s |
| --- | --- | --- | --- |
| POST bookings | 13,122 | 1 | 218 |
| POST payments | 9,613 | 0 | 160 |
| 合計 | 22,735 | 1（0.004%） | 378 |

| 合計延遲 | p50 | p95 | p99 |
| --- | --- | --- | --- |
| 客戶端（含 Nginx 排隊與網路） | 7.4 s | 9.8 s | 11.0 s |

逐端點百分位只有整段統計才有（`booking_stats.csv`），其中包含停止後最多 20 秒內完成的在途請求：

| 端點 | 請求數 | 失敗 | p50 | p95 | p99 | max |
| --- | --- | --- | --- | --- | --- | --- |
| POST bookings | 15,455 | 1 | 7.2 s | 10.0 s | 12.0 s | 15.0 s |
| POST payments | 10,000 | 0 | 7.1 s | 9.2 s | 9.8 s | 10.3 s |
| 合計 | 25,455 | 1 | 7.2 s | 9.7 s | 11.0 s | 15.0 s |

業務結果（穩態期間，HTTP 狀態 × 錯誤碼）：`201` 10,000（**10,000 席全部售出**）、`202` 9,613 筆付款受理（其餘 387 筆在穩態結束後受理）、`409 SEAT_UNAVAILABLE` 2,282、`409 INSUFFICIENT_SEATS` 556、`409 SEAT_CONTENTION` 281（可重試）、`409 QUOTA_EXCEEDED` 2。唯一的失敗是 10:23:57 一筆訂票在 15 秒內未收到回應（`booking_failures.csv`：客戶端判定為傳輸失敗，沿用同一 Idempotency-Key）。執行期間 `access_logs` 沒有任何 5xx，Nginx 也沒有 error／crit 日誌。

**付款結清**（`booking-settlement.json`、`booking-settlement-timing.txt`）：穩態結束後 50.5 秒內 10,000 筆付款全部結清，結果全部是 `PAID/SUCCEEDED`；仍待處理 0、晚於保留期限而被沖正 0、售出座位 10,000。付款從受理到成功的時間 p50 54.7 秒、p99 58.8 秒、最長 59.0 秒；距離保留期限（`HOLD_SECONDS=300`）最少仍有 222.9 秒。

**一致性檢查**（`scripts/check_consistency.py --require-settled`，`booking-consistency.json`）：**通過**，11 項違規全部為 0——重複售出座位、已付款明細／座位不符、售出座位／訂單不符、訂單張數／金額不符、超過限購、有效保留遺失、無效保留、付款不符、售出明細對應未付款訂單、事件版本缺口、缺少 Outbox；待清理逾時訂單 0、待沖正 0、未發布 Outbox 0。

**伺服器端處理時間**（同一穩態時間窗內 `access_logs` 的 `duration_us`，`booking-server-access.txt`；以伺服器時間戳切窗，筆數與客戶端略有出入）

| 端點 | 狀態 | 筆數 | p50 | p95 | p99 |
| --- | --- | --- | --- | --- | --- |
| POST bookings | 201 | 10,001 | 7.5 s | 10.0 s | 11.5 s |
| POST bookings | 409 | 2,754 | 7.3 s | 8.5 s | 9.1 s |
| POST payments | 202 | 9,220 | 7.2 s | 9.3 s | 9.9 s |
| POST payments/callback | 200 | 157 | 4.1 s | 7.1 s | 8.2 s |

**資源用量**：本輪沒有對情境 A 做 `docker stats` 取樣。下表是前一輪（`9ae20b1`）的取樣（每約 2 秒一次，穩態 30 個樣本，`rerun-20261117T082509Z/run-resources.csv`、`booking-resources-summary.txt`；CPU 以單核 100% 計，主機共 20 核）：

| 容器 | CPU 平均 | CPU 最高 | 記憶體最高 |
| --- | --- | --- | --- |
| backend（4 個 API 程序合計） | 438% | 489% | 618 MiB |
| postgres | 278% | 431% | 847 MiB |
| worker | 64% | 119% | 105 MiB |
| mock-gateway | 25% | 70% | 81 MiB |
| nginx | 5.1% | 7.1% | 206 MiB |
| redis | 3.5% | 5.3% | 11 MiB |

### 與先前執行比較

| 指標 | 基線（修正前） | commit `109ef9d` | commit `9ae20b1` | 最新 `7d40bc8` |
| --- | --- | --- | --- | --- |
| 穩態 req/s | 256 | 426 | 410 | 378 |
| 穩態 p50 / p95 / p99 | 4.8 s / 16 s（逾時） | 6.8 s / 9.2 s / 11.0 s | 6.7 s / 9.0 s / 10.0 s | 7.4 s / 9.8 s / 11.0 s |
| 失敗率 | 49%（7,475 客戶端 15 秒逾時 + 174 HTTP 500） | 0% | 0.004%（1 次客戶端逾時） | 0.004%（1 次客戶端逾時） |
| 成功購票／60 s | 2,365 | 10,000（售罄） | 10,000（售罄） | 10,000（售罄） |
| 付款全部準時結清 | 未檢查 | 未檢查 | 是（10,000／10,000） | 是（10,000／10,000） |

先前文件中 `109ef9d` 的 438 req/s 取自包含停止後排空的整段 CSV；上表改用與最新一輪相同的穩態 60 秒統計（`loadtests/results/final/booking.json`）。`109ef9d` 當時只看到 10,000 筆付款被受理，並未等待結清；之後從乾淨 clone 重跑時發現模擬金流每 5 秒最多回呼 100 筆，5,735 筆訂單在付款完成前逾時並被沖正，因此 commit `8c3509b` 把扣款與回呼改為最多 16 條執行緒並行排空，腳本也改為等待結清並檢查逾時付款。`9ae20b1` 與最新的 `7d40bc8` 兩輪都是這個修正後的驗證。

最新一輪的 req/s 比 `9ae20b1` 低約 8%，p50 高約 0.7 秒。兩輪之間有程式變更（`backend/app/booking.py`、`backend/app/api/orders.py`、`backend/app/api/admin_metrics.py`、`backend/app/pagination.py`），同機其他服務的負載也不一定相同；差異的原因未拆解量測。

### 瓶頸分析與修正過程

1. **活動版本列的序列化區段過長**（主要瓶頸）。設計上每個活動的所有座位變更都要在 `event_streams` 列鎖下分配版本。基線實作在取得版本鎖之後才做售票時段、座位可售、限購等檢查與寫入，連被拒絕的請求都排隊；`pg_stat_activity` 在 1,500 人診斷中顯示 160 個 API 執行緒有約 150 個同時處於 `Lock:tuple`，等待同一列。修正分兩步：(a) 所有拒絕判斷與座位條件式 UPDATE 移到版本鎖之前（額度列鎖與座位列鎖已足以保證限購與不超賣）；(b) 版本區段縮為三次往返：`UPDATE event_streams … RETURNING`（取鎖並配號）→ 一個在鎖下重新讀取活動並寫入 `seat_events` 與 Outbox 的 CTE → COMMIT；回應 body、冪等完成、回呼紀錄、稽核都在進鎖前 flush。
2. **Nginx 檔案描述元耗盡**。3,000 條同時連線使 Nginx 記錄 `accept4() failed (24: No file descriptors available)`，請求在邊緣排隊超過客戶端 15 秒逾時，部分以 499／500 結束。修正：`worker_processes auto`、`worker_rlimit_nofile 65536`、`worker_connections 16384`、上游 keepalive 256。
3. **連線池等待不受控**。SQLAlchemy 預設 `pool_timeout=30s` 讓請求在池上排隊 30 秒而非快速失敗；改為 5 秒並把池逾時／連線中斷映射為可重試的 `503 DATABASE_UNAVAILABLE`（帶 `Retry-After`，客戶端沿用同一 Idempotency-Key）。
4. **API 程序數**。4 個 uvicorn 程序配 20+20 連線池；實驗以 8 個程序（8 × 40 = 320 > `max_connections` 300）造成大量 503／502，且負載產生器本身已達 118% CPU，因此維持 4 個程序作為此機器上的受測配置，不以更多程序換取無法量測的數字。

5. **付款結清落後於受理**（`8c3509b` 修正）。見上一節；修正後，`9ae20b1` 一輪最慢一筆在受理後 56.7 秒成功，最新一輪為 59.0 秒；保留期限分別仍有 231.7 秒與 222.9 秒的餘裕。

剩餘瓶頸：

- **API 程序 CPU。** `9ae20b1` 一輪的取樣中，穩態時 backend 平均 438%，相當於 4 個 uvicorn 程序各約用滿一核；主機 20 核中 PostgreSQL 約用 2.8 核，Nginx 與 Redis 幾乎閒置（最新一輪未對情境 A 取樣）。最新一輪的伺服器端處理時間（成功購票 p50 7.5 s）與客戶端延遲（p50 7.4 s）同量級，代表排隊發生在 API 程序內（執行緒、連線池與版本鎖的等待），不是在 Nginx 邊緣。每個程序約一核的上限是依 CPU 取樣推論為 Python 單程序的執行瓶頸，未以 profiler 個別量測。
- **單一活動的版本配號仍是序列化點**。成功購票與付款成交各佔一次，60 秒內 10,000 筆購票加 10,000 筆付款，約每秒 330 次版本配號。
- **付款結清約需 50–55 秒**。模擬金流與 worker 在 API 飽和時仍能於保留期限內結清，但中位數約 51 秒（`9ae20b1`）與 55 秒（最新一輪）；真實金流的延遲與吞吐不同，需另外量測。

要再提升需要更多 API 程序與資料庫連線（本機實驗以 8 個程序超過 `max_connections` 300，見第 4 點）、跨機器的負載產生器與多個 API 容器，以及 `seat_events`／`outbox_events` 寫入的批次化；這些未執行，不列數字。

## 情境 B：2,000 條即時連線同時訂閱同一活動

設定：`loadtests/websocket_benchmark.py`，2,000 條經 Nginx 的 WebSocket 連線以同一 Viewer 身分完成應用層 token 握手並取得同一份 HTTP 快照；另一帳號在 60 秒穩態期間反覆「保留一席 → 取消」產生真實座位變更。每次變更量測「HTTP 請求送出 → 各連線收到對應版本的 frame」的客戶端延遲（nearest-rank 百分位），檢查版本連續、重同步次數與斷線。

### 最新結果（commit `7d40bc8`，2026-11-17 10:25:30 UTC 開始，緊接情境 A 之後在同一資料庫執行；原始輸出 `websocket.json`、逐筆送達 `websocket.deliveries.jsonl`、資源取樣 `websocket-resources.csv`、伺服器端請求 `websocket-server-access.txt`、一致性稽核 `websocket-consistency.json`）

**吞吐（RPS）與錯誤率**

| 指標 | 值 | 錯誤率 |
| --- | --- | --- |
| 連線 | 2,000 條經 Nginx 建立並完成應用層 token 認證，穩態 61.2 秒期間全部存活 | 斷線 0 / 2,000（0%） |
| 座位變更 HTTP 請求（保留 40 次、取消 40 次） | 80 次，1.31 req/s | 失敗 0 / 80（0%）；伺服器端 5xx 0、4xx 0 |
| 伺服器推送 frame（含每 5 秒 heartbeat） | 184,000 個，約 3,009 frames/s | — |
| 座位變更送達（每次變更 × 2,000 連線） | 160,000 次，約 2,616 次/s | 未送達 0 / 160,000（0%） |
| 每次變更送達全部 2,000 條連線 | 80 / 80；單次最慢 0.32 秒（要求 10 秒內） | 逾時 0 |
| 版本跳號／重同步 | 0 | — |

**延遲**

| 量測 | p50 | p95 | p99 |
| --- | --- | --- | --- |
| 送達延遲：HTTP 送出 → 各連線收到對應 frame（客戶端單調時鐘，nearest-rank） | 251 ms | 280 ms | 300 ms |
| 伺服器端處理：保留座位 `POST /bookings`（`access_logs`） | 18.2 ms | 24.5 ms | 27.5 ms |
| 伺服器端處理：取消 `POST /orders/{id}/cancel` | 10.6 ms | 12.9 ms | 15.1 ms |

**資源用量**（`docker stats` 每約 2 秒取樣一次，31 個樣本；CPU 以單核 100% 計，主機共 20 核）

| 容器 | CPU 平均 | CPU 最高 | 記憶體最高 |
| --- | --- | --- | --- |
| backend（4 個 API 程序合計） | 19.0% | 99.6% | 713 MiB |
| postgres | 66.5% | 148% | 1,182 MiB |
| worker | 19.0% | 33.4% | 117 MiB |
| nginx | 3.8% | 11.8% | 214 MiB |
| redis | 2.4% | 2.8% | 8 MiB |

backend 的最高值 99.6% 出現在第一個樣本，與 2,000 條連線同時握手與認證的時間重疊；之後每個樣本介於 2.5% 與 40.6% 之間。前一輪（`9ae20b1`）第一個樣本是 323%，取樣時點與握手尖峰的重疊程度不同，兩者不宜直接比較。

**一致性稽核**：結束 15 秒後以 `--require-settled` 稽核該活動，11 項違規全部為 0，未發布 Outbox 0、待清理逾時訂單 0。

**瓶頸分析**

1. **推送路徑沒有飽和。** 穩態時 backend 平均只用約五分之一核，Redis 2.4%，Nginx 3.8%；每秒約 3,000 個 frame 的扇出在此規模下成本很低。唯一的尖峰是連線建立瞬間的握手與認證查詢。
2. **延遲由 Outbox 輪詢間隔主導。** 伺服器處理保留約 18 ms，但送達 p50 約 251 ms，與 `OUTBOX_POLL_SECONDS=0.25` 相當；p50 到 p99 只差約 50 ms，代表扇出到 2,000 條連線本身很快。p50 貼近整個輪詢間隔而非一半，推論是 harness 在前一次變更送達後立即送出下一次，使每次變更都剛好落在 worker 輪詢之後；隨機到達的變更平均應只等一半。此推論未另外量測。縮短輪詢可直接降低延遲，代價是 worker 空轉查詢增加。
3. **PostgreSQL 與 worker 的背景負載比單獨執行時高。** 本輪 postgres 平均 66.5%、worker 19.0%，同樣呈每 6–8 秒一次的週期起伏（postgres 在約 3% 與 148% 之間）；`9ae20b1` 一輪是 42.7% 與 20.8%，單獨執行情境 B 的 `8b29aea` 一輪是 8.4% 與 5.3%，該輪全部情境結束後的閒置取樣也回到約 7% 與 6%。兩輪都是緊接情境 A 之後在同一資料庫執行；`9ae20b1` 一輪在 08:29:03 前後觀察到 `seat_events`、`outbox_events`、`charges`、`payment_callbacks` 的 autovacuum／autoanalyze，但週期起伏持續整個穩態。本輪未查 autovacuum 紀錄，起伏的來源仍未定位。送達延遲與一致性不受影響。
4. **此情境未觸及推送路徑的容量上限。** 每秒只有約 1.3 次變更，壓力來自 2,000 倍的扇出而非變更頻率；更高變更頻率或更多連線下的上限未量測，不列數字。負載產生器（單一 Python 程序承接 2,000 條連線）的 CPU 未取樣，但所有變更都在 0.32 秒內被全部連線收到，沒有跡象顯示客戶端成為瓶頸。

**先前執行**：commit `9ae20b1`（08:28 UTC，緊接情境 A）送達延遲 p50 252 ms、p95 286 ms、p99 312 ms，單次變更最慢 0.33 秒；commit `8b29aea`（07:06 UTC，單獨執行）送達延遲 p50 248 ms、p95 273 ms、p99 283 ms。兩輪的其餘指標與本輪相同（斷線 0、未送達 0、重同步 0、稽核通過）。
**第一次執行**（commit 830fe6b）在第 26 次變更失敗：2,000 條連線同時重同步。原因是 heartbeat 回報的是 `current_version`，而座位變更提交到 worker 發布之間有最多 0.25 秒的窗口，落在窗口內的 heartbeat 會讓客戶端誤判漏事件。修正為回報 `published_version`（客戶端真正可能漏掉的只有已發布的版本），前端另加 1–2 秒寬限與抖動。修正後第一次通過的執行（commit 109ef9d）送達延遲 p50 255 ms、p95 313 ms、p99 324 ms。


## 批次匯入：100,000 列 60 秒內完成

設定：`scripts/generate_load_data.py seats` 產生 100,000 列 CSV（1,292,033 bytes）與 JSON（6,392,003 bytes）；`loadtests/import_benchmark.py` 建立草稿活動、上傳、每 500 ms 輪詢 job 直到終態，記錄受理時間與完成時間。

### 最新結果（commit `7d40bc8`，2026-11-17 10:26–10:27 UTC；`import-csv.json`、`import-json.json`）

| 格式 | 檔案大小 | 上傳並受理 | 受理 → 觀察到完成 | 總耗時 | 新增列 | 60 秒內 |
| --- | --- | --- | --- | --- | --- | --- |
| CSV | 1,292,033 bytes | 0.02 s | 13.7 s | 13.8 s | 100,000 | 通過 |
| JSON | 6,392,003 bytes | 0.02 s | 14.3 s | 14.3 s | 100,000 | 通過 |

模式 `all_or_nothing`、錯誤列 0。耗時組成：檔案先完整持久化再受理（< 0.05 秒），worker 以租約領取後分批（`IMPORT_BATCH_SIZE=1000`）解析進 staging，驗證前先對 `import_rows` 與 `seats` 執行 ANALYZE（commit `dca8166`），再驗證檔內重複與既有座位，最後在單一交易新增全部座位並發布 `resync_required` 事件；輪詢粒度 0.5 秒。CPU 主要在 worker 的 Python 解析與 PostgreSQL 寫入，未做並行化。

先前執行：commit `9ae20b1` 兩種格式各 12.7 秒，`109ef9d` 各 15.7 秒，更早的 commit 為 14.7 秒。`dca8166` 修正的是統計資料過期時驗證退化為二次方的問題（一次乾淨 clone 的執行中 JSON 匯入卡在驗證階段超過 4 分鐘）；`9ae20b1` 與本輪比 `109ef9d` 快，是否來自該修正未另外拆解量測。

## Excel：500,000 筆訂單、API 記憶體上限 512 MiB

設定：`scripts/generate_load_data.py report` 以 SQL 建立 500,000 筆一張票訂單（多數為歷史取消／逾時，已付款子集受限購約束）；`loadtests/limited_api.py` 以 `RLIMIT_AS = 512 MiB`（位址空間，比 RSS 更嚴）啟動單一 API 程序；`report_benchmark.py` 串流下載、每 20 ms 取樣 `/proc/<pid>/status` 的 VmRSS 峰值，並以唯讀模式掃描工作簿驗證三個工作表與 500,000 列。

### 最新結果（commit `7d40bc8`，2026-11-17 10:27:42 UTC 開始；`report.json`）

| 指標 | 值 |
| --- | --- |
| API 程序 | 單一 uvicorn 程序，`RLIMIT_AS` 硬上限 536,870,912 bytes（512 MiB），無子程序 |
| 訂單數 | 500,000（Orders 工作表 500,000 列） |
| 工作表 | Summary, Zones, Orders；表頭含 `Asia/Taipei`；金額欄 3 欄全為數值（非法儲存格 0） |
| 檔案大小 | 32,340,392 bytes |
| 串流下載耗時 | 32.6 s |
| API 峰值 VmRSS（每 20 ms 取樣） | 105 MiB（109,981,696 bytes） |
| 結果 | 通過 |

做法：Summary／Zones／Orders 三個工作表使用同一個 REPEATABLE READ 唯讀快照；Orders 以 server-side cursor 分批（1,000 列）讀取 Core 列而非 ORM 物件，OpenPyXL write-only 模式逐列寫入暫存檔，再以串流回應送出並刪除暫存檔。位址空間上限比 RSS 上限更嚴格（執行緒堆疊也計入），仍留有約 4 倍餘裕。第一次執行量到錯誤的程序（harness 以 `--workers 1` 啟動 uvicorn 使其成為 supervisor），修正後重跑。commit `9ae20b1` 起 harness 另設 `MALLOC_ARENA_MAX=2` 與 2 MiB 執行緒堆疊：一次執行中匯出只用 108 MiB RSS，VmSize 卻達 420 MiB，曾有一次在上限處失敗。先前執行：commit `9ae20b1` 峰值 105 MiB、下載 32.9 秒；commit `df2f73c` 峰值同為 105 MiB、下載 32.5 秒。

## 故障實驗（非效能，補充 DESIGN Q2）

以下兩項在最新一輪未重跑，結果來自先前的執行（`loadtests/results/final/`）。

- **worker `kill -9`**（`loadtests/results/final/kill9.txt`）：購票提交後 0.18 秒以 SIGKILL 殺掉 worker，此時該活動有 1 筆未發布 Outbox；同時把該訂單期限改到過去。`docker compose start worker` 後 2.87 秒內：Outbox 全部發布、訂單收斂為 EXPIRED、座位回到 AVAILABLE、`event_streams` 版本 3／已發布 3；無人工介入。
- **故意破壞不超賣保護**（`loadtests/results/final/sabotage-q1.txt`）：移除座位 `FOR UPDATE` 與條件式 UPDATE 述詞後，併發測試得到 `[201, 201]`，同一座位賣給兩個帳號；還原後測試通過。

## 未執行或有條件的項目

- 跨機器壓測、多 API 容器水平擴展、長時間（>60 秒）穩定性：未執行。接手後確認沒有第二台機器可用，因此不另外執行跨機器情境，也不縮小情境以換取數字。
- 主機斷電／儲存故障下的持久性：僅以程序 `kill -9` 驗證，不代表硬體故障已驗證。
- 所有情境皆在負載產生器與受測系統同機的條件下量測。
