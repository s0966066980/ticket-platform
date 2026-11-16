# 核心 Schema 草案

狀態：使用者已確認此模型與實作流程，並授權開始開發。本文件不是已執行的 migration 證據；後續核心模型的實質變更須提出具體差異。

## 共通規則

- 主體 ID 使用 UUID；金額使用整數，單價用 INTEGER、小計及總額用 BIGINT，幣別固定 TWD。
- 時間使用 TIMESTAMPTZ，應用程式輸入必須帶時區。整數版本及 generation 非負，識別碼與關鍵外鍵不得為空。
- 欄位中的 created_at／updated_at 為 DB 時間；截止判斷另在保護鎖取得後讀取 clock_timestamp()。
- 狀態用有 CHECK 約束的文字欄位，讓 Alembic 演進不依賴 PostgreSQL enum 型別變更。
- 所有座位、明細與訂單的 event_id 透過複合外鍵保持一致，避免只檢查個別 ID 存在而容許跨活動組合。
- 列表中的 UNIQUE、FK、CHECK 與索引均為預計建立的約束；跨列或跨表不變量不能假裝由單列 CHECK 保證。

## 1. 身分與活動

| 資料表 | 主要欄位 | 約束與主要索引 |
| --- | --- | --- |
| users | id、email、password_hash、role、is_active、auth_version、created_at、updated_at | UNIQUE lower(email)；role 為 ADMIN／USER／VIEWER；auth_version >= 0 |
| sessions | id、user_id、token_hash、auth_version、expires_at、revoked_at、created_at | FK user；UNIQUE token_hash；索引 user_id、expires_at；只保存 token 雜湊 |
| events | id、owner_id、title、description、category、venue、status、sales_start、sales_end、performance_start、performance_end、version、deleted_at、created_at、updated_at | FK owner；DRAFT／PUBLISHED；sales_end > sales_start；演出時間同時提供時 end > start；索引 owner、發布狀態／開賣時間、建立時間 |
| zones | id、event_id、code、name、price_twd、created_at | FK event；UNIQUE(event_id, code) 與 UNIQUE(id, event_id)；price_twd >= 0 |

活動修改用 `WHERE id = :id AND version = :expected` 遞增版本；版本不符不覆寫。發布後僅接受展示欄位的修改。軟刪除先鎖 `events FOR NO KEY UPDATE`、再鎖該活動的 event_streams，重新確認沒有任何訂單與執行中匯入後設定 deleted_at。購票在相同 stream 保護內重新檢查 deleted_at IS NULL，避免刪除檢查與建單交錯；FK RESTRICT 只保護物理刪除，不能取代軟刪除的共同鎖。

## 2. 訂單、額度與座位

| 資料表 | 主要欄位 | 約束與主要索引 |
| --- | --- | --- |
| orders | id、event_id、user_id、status、ticket_count、subtotal_twd、fee_twd、total_twd、currency、expires_at、accepted_payment_id、paid_at、canceled_at、expired_at、created_at、updated_at | FK event/user/accepted payment；UNIQUE(id,event_id)、UNIQUE(id,event_id,user_id)；ticket_count 1–4；金額非負；fee = (subtotal*5+50)/100；total = subtotal+fee；currency=TWD；索引(user_id,event_id,status,expires_at) 與待付款期限 |
| user_event_quotas | user_id、event_id、max_tickets、created_at | 複合 PK(user_id,event_id)；FK user/event；max_tickets=4。此列是交易 mutex，不保存可過期失真的 held_count |
| seats | id、event_id、zone_id、row_label、seat_number、status、current_order_id、held_by、held_until、version、created_at、updated_at | UNIQUE(event_id,zone_id,row_label,seat_number)、UNIQUE(id,event_id)；複合 FK(zone_id,event_id)；複合 FK(current_order_id,event_id)；HELD 的 held_by 另須與對應訂單購票者一致；索引(event_id,zone_id,status,id)、held_until |
| order_items | id、order_id、event_id、seat_id、zone_code_snapshot、row_label_snapshot、seat_number_snapshot、unit_price_twd、sold_at | 複合 FK(order_id,event_id)、FK(seat_id,event_id)；UNIQUE(order_id,seat_id)；unit_price_twd >= 0；UNIQUE(seat_id) WHERE sold_at IS NOT NULL |

座位狀態 CHECK：

| 狀態 | 欄位條件 |
| --- | --- |
| AVAILABLE | current_order_id、held_by、held_until 皆為 NULL |
| HELD | current_order_id、held_by、held_until 皆非 NULL |
| SOLD | current_order_id 非 NULL，held_by、held_until 為 NULL |

HELD 的 held_by 與訂單購票者以 `(current_order_id,event_id,held_by)` 複合 FK 對應 orders 的唯一組合；SOLD／AVAILABLE 因 held_by 為 NULL 不適用該複合檢查。

座位座標唯一限制只保證座位身分不重複，並不單獨保證不超賣。保留的互斥由受鎖保護的條件式 UPDATE 保證；成交以 seat.current_order_id、訂單期限及 SOLD 轉移檢查，再以成交明細的 partial UNIQUE 作防線。取消或逾時的歷史明細仍保留，不能對所有歷史 order_items.seat_id 直接加 UNIQUE。

有效購票張數在鎖住該帳號／活動額度列後，以 PAID 訂單張數與 `PENDING_PAYMENT AND expires_at > db_now` 的張數求和。所有訂單建立與狀態轉移都使用同一 mutex。額度列 CHECK 並不能證明實際訂單符合限購，必須有並行整合測試。

訂單張數、小計與明細的對應由同一交易服務寫入並驗證，定期一致性檢查另行比對；不使用跨表 CHECK。第一版不新增多筆可獨立修改的額度累加計數器。

已逾時 HELD 可由新訂單重新保留，而舊訂單尚未被 worker 更新成 EXPIRED；舊訂單在業務上已逾時。舊工作的釋放 UPDATE 必須同時比對 current_order_id，絕不能只按 seat_id 釋放。

## 3. 付款與 API 重試

| 資料表 | 主要欄位 | 約束與主要索引 |
| --- | --- | --- |
| payments | id、order_id、status、amount_twd、currency、provider_charge_id、request_id、failure_code、reversal_key、reversal_attempts、next_retry_at、last_error、succeeded_at、reversed_at、created_at、updated_at | FK order；UNIQUE(id,order_id)、provider_charge_id（非空時）、reversal_key；每訂單最多一個 PENDING 嘗試的 partial UNIQUE；狀態 PENDING／FAILED／SUCCEEDED／REVERSAL_PENDING／REVERSED；金額非負且 currency=TWD |
| payment_callbacks | provider_event_id、payment_id、payload_hash、payload、received_at、processed_at、result_code、request_id | PK provider_event_id；FK payment；處理紀錄與訂單／付款變更同交易提交，重送不再產生副作用 |
| idempotency_keys | id、user_id、operation、key、request_hash、state、generation、lease_until、response_status、response_body、order_id、payment_id、created_at、expires_at | UNIQUE(user_id,operation,key)；FK user/order/payment；IN_PROGRESS／COMPLETED；索引 state/lease_until、expires_at |

orders.accepted_payment_id 以 `(accepted_payment_id,id)` 複合 FK 指向 payments 的 `(id,order_id)`，確保付款屬於該訂單；接受後不得改指向另一筆。PAID 須有 accepted_payment_id 與 paid_at；CANCELED／EXPIRED 不接受成功付款轉回 PAID。金額吻合由交易服務檢查，另有一致性檢查驗證此跨表條件。

取得必要鎖後才讀付款截止時間，再處理終態；回呼交易的重播結果、付款沖正工作及付款變更一起提交。狀態為 REVERSAL_PENDING 不等於已完成退回，必須保存重試與結果。

金流使用獨立邏輯 schema：

| 資料表 | 主要欄位 | 約束與主要索引 |
| --- | --- | --- |
| gateway.charges | id、payment_key、order_reference、amount_twd、currency、outcome、provider_event_id、callback_payload、callback_attempts、next_callback_at、callback_delivered_at、lease_generation、lease_until、reversal_key、reversed_at、request_id、created_at | UNIQUE payment_key、provider_event_id、reversal_key；金額及幣別由 backend 請求決定；結果與 callback 重送狀態持久保存；重複扣款／沖正請求回原結果 |

gateway 不直接改 backend 的訂單或座位。它與 backend 共用 PostgreSQL 容器但隔離邏輯資料，模擬的是獨立服務的持久化與回呼邊界。

## 4. 匯入與工作恢復

| 資料表 | 主要欄位 | 約束與主要索引 |
| --- | --- | --- |
| import_jobs | id、event_id、submitted_by、authorization_snapshot、file_hash、file_path、original_name、format、mode、status、phase、total_rows、processed_rows、inserted_rows、error_rows、checkpoint、generation、lease_owner、lease_until、failure_code、request_id、created_at、finished_at | FK event/user；UNIQUE(event_id,file_hash)；CSV／JSON；ALL_OR_NOTHING／SKIP_INVALID；QUEUED／RUNNING／SUCCEEDED／FAILED_VALIDATION／FAILED_SYSTEM；計數非負；索引 status/lease_until |
| import_rows | job_id、row_number、source_line、zone_code、row_label、seat_number、validation_state、resolved_zone_id、applied_seat_id | PK(job_id,row_number)；FK job；索引(job_id,zone_code,row_label,seat_number)；為 staging，不是可售座位 |
| import_errors | id、job_id、row_number、source_line、code、reason | FK job；索引(job_id,row_number)；檔案層錯誤可無資料列號，必須明示為檔案錯誤 |

每批資料、checkpoint 與 inserted_rows 同交易提交；job 最終提交檢查 generation。source_line 用於 CSV 實際來源位置，row_number 用於 JSON 物件序號及工作內固定排序。原 job 的 mode 不隨相同檔案重傳而改變。

all_or_nothing 的驗證階段只寫 staging／errors，不寫 seats；最終 INSERT、job 完成、事件與 Outbox 同交易提交。skip_invalid 的批次 INSERT 與 checkpoint 原子提交，重啟後不將先前已成功新增的列錯當成新上傳的重複錯誤。

## 5. 事件、清單與即時同步

| 資料表 | 主要欄位 | 約束與主要索引 |
| --- | --- | --- |
| event_streams | event_id、current_version、published_version、publisher_generation、publisher_lease_until、updated_at | PK/FK event_id；0 <= published_version <= current_version；版本在座位交易中遞增 |
| seat_events | event_id、version、changes、occurred_at、request_id | PK(event_id,version)；FK event；changes 為同次變更的座位／批次投影或 resync 標記；持久保存可審計事件 |
| outbox_events | id、kind、aggregate_id、aggregate_version、payload、request_id、available_at、attempts、generation、lease_until、published_at、last_error、created_at | kind 包含 seat_changed／payment_requested／payment_reversal_requested；座位事件的 aggregate/version 唯一；索引未發布工作與 available_at |
| list_snapshots | id、session_id、user_id、auth_version、filter_spec、sort_spec、page_size、total_count、created_at、expires_at | FK session/user；綁定查詢與存取者；索引 expires_at |
| list_snapshot_items | snapshot_id、position、event_id、projection | PK(snapshot_id,position)；FK snapshot；event_id 為歷史參考而非限制活動刪除的 FK；projection 保存該次固定展示內容 |

seat_events 的版本是每活動的連續已提交狀態版本，不使用 BIGSERIAL 當成提交順序。Outbox 的 published_at 代表發布嘗試已完成，不代表所有 WebSocket 訂閱者已收到。

清單快照建立後 10 分鐘過期；已撤銷 session、auth_version 不符或當下無權限時不得繼續讀取。清單結果更新、活動修改與刪除不改變原快照的 position 或 projection。

## 6. 診斷與營運

| 資料表 | 主要欄位 | 約束與主要索引 |
| --- | --- | --- |
| access_logs | event_id、request_id、instance_id、method、route_template、status_code、error_kind、business_outcome、duration_us、observed_at、ingested_at、clock_error_us | PK event_id 用於佇列重送去重；索引 request_id、observed_at；duration 非負；route 使用模板避免指標基數失控 |
| service_logs | event_id、request_id、span_id、parent_span_id、service、level、code、context、observed_at、ingested_at | PK event_id；索引 request_id、observed_at；context 不含秘密 |
| system_metrics | source_id、bucket_at、window_seconds、request_count、error_count、business_counts、latency_summary、connections、last_seen_at、data_complete、updated_at | PK(source_id,bucket_at,window_seconds)；索引 bucket_at；來源 ID 每次程序啟動不同；重算 upsert 而非重複累加 |
| alerts | id、rule_id、scope、status、first_seen_at、triggered_at、last_seen_at、resolved_at、details | UNIQUE(rule_id,scope) WHERE resolved_at IS NULL；索引 status/triggered_at；保留已解除紀錄 |

診斷事件先唯一化再參與彙總；指標重算以相同 key 覆寫該桶，避免 collector 重啟重複累計。收集延遲、缺少來源與時鐘校準誤差屬於資料品質，不能用補零或平均副本百分位數掩蓋。

## 7. Migration 與回溯檢查

Alembic 依模組建立表、FK、部分唯一索引與查詢索引；初始化及 upgrade 在全新 PostgreSQL 16 上測試。種子資料可重複執行，測試不得改用 SQLite 代替交易語意。

一致性檢查包含：每座位最多一筆成交明細；PAID 的明細均對應 SOLD 且 current_order_id 正確；訂單張數／小計與明細一致；有效張數不超過 4；終態工作不釋放別人的座位；未完成沖正、事件版本與 Outbox 可追蹤。檢查結果納入壓測報告。

Partial UNIQUE 不能引用別表或使用會隨時間改變的 predicate，因此不設計 `WHERE held_until > now()` 的唯一索引。[PostgreSQL 索引限制](https://www.postgresql.org/docs/16/sql-createindex.html)；跨表不變量也不能由 CHECK 保證。[CHECK 限制](https://www.postgresql.org/docs/16/ddl-constraints.html#DDL-CONSTRAINTS-CHECK-CONSTRAINTS)
