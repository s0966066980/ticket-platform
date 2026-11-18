## Q1 座位不超賣

### 1. 「指定座位購買」從 API 到提交的路徑

入口 `POST /api/v1/events/{event_id}/bookings`（`backend/app/api/bookings.py:32-40`）→ `reserve_order`（`backend/app/booking.py:194`）→ `_reserve`（`backend/app/booking.py:137`）。一次 HTTP 請求對應一個 PostgreSQL 交易（READ COMMITTED），流程與保護點如下：

| 步驟 | 位置 | 動作 | 保護 |
| --- | --- | --- | --- |
| 1 | `booking.py:198-200` | `claim_key`：在獨立短交易以 `INSERT … ON CONFLICT DO NOTHING` 取得冪等處理資格（`idempotency.py:52-95`） | 唯一鍵 `(user_id, operation, key)`；同 key 只會有一個處理者 |
| 2 | `booking.py:140` | `lock_claim`：主交易 `SELECT … FOR UPDATE` 冪等列並驗證 generation | **保護開始**：冪等列鎖 |
| 3 | `booking.py:140` | `lock_quota`：`INSERT … ON CONFLICT DO NOTHING` 後 `FOR UPDATE` 該帳號／活動額度列 | 同帳號同活動的所有建單／取消／付款在此序列化 |
| 4 | `booking.py:144` | 指定座位 `SELECT … FOR UPDATE`（依 ID 排序） | 座位列鎖：競爭同座位的交易在此排隊；固定排序避免死結 |
| 5 | `booking.py:162-170` | 讀 `clock_timestamp()`，檢查已發布且在售票時段、座位可售（AVAILABLE 或已逾時 HELD）、有效張數 + 本次 ≤ 4 | 在持有鎖的狀態下讀到的是最新提交值（READ COMMITTED + `populate_existing`） |
| 6 | `booking.py:174-180` | 建立訂單，**條件式 UPDATE** 座位：`WHERE id IN … AND (AVAILABLE OR (HELD AND held_until <= now))` 改為 HELD；受影響列數必須等於張數 | 即使步驟 4 的鎖有任何疏漏，資料庫仍只會更新仍可售的列 |
| 7 | `booking.py:185-186` | 回應 body 與冪等回應先準備好，`emit_seat_changes`（`booking.py:45-76`）是交易的最後一步 | 進版本鎖前所有待寫入都已 flush（`booking.py:57`） |
| 8 | `booking.py:58` | `UPDATE event_streams … RETURNING current_version`：取得活動版本列鎖並配號 | 版本配號與軟刪除共用的最後一把鎖；每個活動的提交在此序列化 |
| 9 | `booking.py:64-75` | 單一 CTE 在鎖下以新快照重讀活動（已刪除、未發布或不在售票時段則不寫入），寫入 `seat_events` 與 `outbox_events`；寫入 0 列即拋錯回滾 | 刪除與建單只能一邊成功 |
| 10 | `booking.py:190` | `session.commit()` | **保護釋放**：所有列鎖在提交時釋放 |

保護機制：PostgreSQL 列鎖（`FOR UPDATE`）序列化對同一座位、同一帳號額度、同一活動版本的寫入；條件式 UPDATE 的 `WHERE` 子句是最後防線——它只會把「此刻仍可售」的列改成 HELD，更新列數不足就整筆回滾。保證的是：任一座位在任一時刻最多屬於一筆有效訂單（`seats.current_order_id`），且成交明細有 `UNIQUE(seat_id) WHERE sold_at IS NOT NULL`（`backend/app/models.py:162-180`）作為成交層的防線。

### 2. 最可能讓同座位賣兩次的故障順序

情境：兩個 API 副本 A、B 同時收到不同帳號對同一座位的請求；A 在取得座位鎖後、提交前程序被暫停（例如 GC 停頓、容器被 `SIGSTOP`）超過鎖位期限不可能（期限 5 分鐘），但更危險的是 **A 的連線在提交後才逾時**，客戶端以相同 key 重送到 B：

| 時間 | A（副本 1） | B（副本 2） | 資料庫 |
| --- | --- | --- | --- |
| t0 | 取得冪等列、額度列、座位 S 的鎖 | 同 key 重送：`claim_key` 看到 IN_PROGRESS 且租約未到 → 回 409 IDEMPOTENCY_IN_PROGRESS | S 鎖在 A |
| t1 | 條件式 UPDATE S → HELD；commit 成功，但回應在網路上遺失 | | S.current_order_id = 訂單 A |
| t2 | | 客戶端再以同 key 重送：`claim_key` 看到 COMPLETED → 重播原 201 與原訂單（`idempotency.py:74`） | 不再建立 |
| t2' | | 若改用**不同帳號**的請求：取得 S 的鎖後讀到 HELD 且 `held_until > now` → 409 SEAT_UNAVAILABLE（`booking.py:166-167`）；就算檢查被繞過，條件式 UPDATE 影響 0 列 → 409（`booking.py:179`） | S 不變 |

另一個順序：A 在 t0 取得鎖後被暫停 30 秒。B 在座位鎖上等待（不會超時，PostgreSQL 無 lock timeout 設定），A 恢復後提交，B 才取得鎖並讀到最新狀態 → 拒絕。如果 A 的連線被資料庫斷掉（例如 `kill -9` A），交易回滾、鎖釋放，B 正常成立；A 的冪等列租約 30 秒後到期可被重試接手（generation +1，`idempotency.py:89`），舊處理者若還活著，`lock_claim` 會因 generation 不符而拒絕（`idempotency.py:99`）。我找不到在這個實作下能讓兩筆有效訂單指向同一座位的交錯；條件式 UPDATE 的列數檢查是獨立於鎖的第二道保證。

擋不住的情況（誠實列出）：若有人直接以 SQL 繞過服務層更新 `seats`，或移除 `current_order_id` 的複合外鍵。這些不是請求路徑能到達的狀態。

### 3. 併發測試與故意破壞的輸出

測試：`backend/tests/test_booking.py:51-65` `test_two_accounts_competing_for_one_seat_have_only_one_success`，以 `threading.Barrier` 讓兩個帳號同時送出同一座位的請求，斷言狀態碼恰為 `[201, 409]` 且快照中恰一個 HELD。

破壞方式（在隔離工作樹與拋棄式資料庫進行，完成後 `git checkout` 還原；正式程式未動）：

1. `booking.py:144` 移除 `.with_for_update()`（先讀後寫）。
2. `booking.py:178` 把條件式 UPDATE 的 `_eligible(now)` 述詞移除（無條件更新）。

實際輸出（2026-11-17，PostgreSQL 16.11 容器）：

```
FAILED tests/test_booking.py::test_two_accounts_competing_for_one_seat_have_only_one_success
>       assert sorted(response.status_code for response in responses)==[201,409]
E       assert [201, 201] == [201, 409]
E         At index 1 diff: 201 != 409
1 failed, 1 passed, 9 deselected, 2 warnings in 0.84s
```

兩個帳號都拿到 201，同一座位被賣兩次；只移除其中一項保護時測試仍通過，證明兩道防線各自有效。還原後整個套件重新通過（`110 passed`）。

## Q2 部分失敗後的狀態收斂

### 1. 系統中的時間窗

| # | 時間窗 | 已改的資料 | 還不知道的部分 | 收斂機制 |
| --- | --- | --- | --- | --- |
| W1 | 訂單／座位已提交，Outbox 尚未發布 | `seats`、`orders`、`outbox_events` | WebSocket 訂閱者 | worker `publish_outbox`（`backend/app/feed.py:87`）每 0.25 秒掃描；客戶端 5 秒版本核對 |
| W2 | 付款嘗試已建立，worker 已向金流扣款，回呼尚未送達 | `payments.PENDING`、`gateway.charges` | 訂單仍 PENDING_PAYMENT | 金流持久化回呼並重試（`backend/app/gateway.py:201`）；`reconcile_payments` 每 GATEWAY_POLL_SECONDS 查帳（`backend/app/payments.py:350`） |
| W3 | 鎖位期限已過，訂單仍是 PENDING_PAYMENT | 時間 | `orders.status`、`seats.status` 仍顯示 HELD | 業務上立即視為逾時（新訂單可接手，`booking.py:120-121`）；worker `expire_due_orders` 每秒收斂（`booking.py:264`） |
| W4 | 匯入批次與 checkpoint 已提交，worker 中止 | `seats`、`import_jobs.checkpoint` | job 仍 RUNNING、租約未釋放 | 租約到期後其他 worker 以新 generation 接手，不重做已提交批次（`backend/app/imports.py:162-188`） |
| W5 | 遙測已 XADD 到 Redis，尚未落 PostgreSQL | Redis Stream | `access_logs`、`system_metrics` | collector 先 commit 再 XACK（`backend/app/observability.py:566`）；pending entries 由 `XAUTOCLAIM` 接手 |

最危險的是 **W2**：牽涉金錢，且與 W3 疊加——金流已扣款、回呼遺失、期間鎖位逾時、座位被別人買走，之後成功回呼才到達。

### 2. 在 W2 被 `kill -9` 之後

假設 backend 在 worker 已呼叫金流 `POST /charges`、金流已持久化 SUCCESS、回呼尚未送達時被 `kill -9`，或 worker 在 `_finish_job` 前被殺：

1. 金流端：`gateway.charges` 的 `callback_delivered_at` 為空，背景投遞迴圈以租約／generation 領取並重試（指數退避，上限 60 秒），直到 backend 回 2xx（`gateway.py:147-212`）。
2. backend 重啟後：回呼進入 `process_callback`（`payments.py:154`），依固定順序鎖額度→訂單→付款→座位→版本列，再讀 DB 時間（`payments.py:183-184`）。若訂單仍在期限內且座位仍屬該訂單 → PAID；若已逾時／被接手 → 訂單收斂為 EXPIRED／保持終止，付款標為 `REVERSAL_PENDING` 並寫入沖正 Outbox（`payments.py:219`），worker 以 `provider_charge_id` 為冪等鍵向金流沖正。
3. 若回呼也遺失：`reconcile_payments` 以簽章 GET 查帳並重播相同回呼內容（測試 `test_reconciliation_recovers_a_lost_callback`）。
4. Outbox 中的 `payment_requested` 工作若在 `_finish_job` 前被殺：租約 30 秒後到期，另一 worker 重新領取；金流以 `payment_key = payment_id` 冪等，回原 charge（測試 `test_two_workers_do_not_double_execute_payment_jobs`）。

需要多久：依賴服務可用時，上限約為「金流重試退避（≤60 秒）+ worker 輪詢（5 秒）」；不需人工介入。實測 W1 的版本：在 booking 提交後 0.29 秒 `SIGKILL` worker，重啟後 3.61 秒內未發布的 Outbox 列全部發布、逾時訂單收斂為 EXPIRED、座位回到 AVAILABLE、版本 3/3（腳本見 `loadtests/results/final/kill9.txt`）。需要人工的情況：金流持續拒絕沖正（`payments.reversal_attempts` 持續增加，`last_error` 保留）——目前只在 Admin 後台可查，尚未有專用告警（見 README 已知問題）。

### 3. 逾時判斷使用哪一個時鐘

資料庫時鐘 `clock_timestamp()`，且在取得必要的額度、訂單與座位列鎖之後才讀（`booking.py:162`、`booking.py:236`、`payments.py:184`）；版本區段內的售票時段檢查直接在 SQL 以 `clock_timestamp()` 判斷（`booking.py:63`）。理由：期限由資料庫時間寫入（`expires_at = now + HOLD_SECONDS`），比較也用同一來源，多個 API／worker 副本的 OS 時鐘偏差不影響；`now()` 固定於交易起點，等鎖期間可能已過期，所以用 `clock_timestamp()`。

不 `sleep` 的測試方式：直接用 SQL 把 `orders.expires_at`／`seats.held_until` 改成過去（`backend/tests/test_worker.py:40-56`、`test_payments.py:281`、`test_booking.py:120`、`test_auth.py:87` 對 session 到期同樣處理）。時鐘不一致：應用程式不以自己的時鐘判斷期限，所以沒有「應用時鐘偏差」路徑可測；監控管線的時鐘偏差由 `test_unanchored_replica_clock_is_flagged_not_silently_misplaced`（`test_observability.py:291`）覆蓋。

### 4. 同一筆逾時工作被兩個 worker 同時執行

結果仍正確。`expire_due_orders`（`booking.py:264`）只做無鎖的候選查詢，每筆再呼叫 `_finalize_pending`：先鎖額度列、再 `SELECT … FOR UPDATE` 訂單（`booking.py:227-228`），讀到 `status != 'PENDING_PAYMENT'` 就直接回傳（`booking.py:229-234`），座位釋放的 UPDATE 另外限定 `current_order_id = 該訂單 AND status = 'HELD'`（`booking.py:249`）。第二個 worker 要嘛等鎖後看到終態、要嘛更新 0 列。付款工作則靠 Outbox 列的 `FOR UPDATE SKIP LOCKED` 領取與 generation 檢查（`payments.py:249`、`payments.py:264`），測試 `test_two_workers_do_not_double_execute_payment_jobs`（`test_payments.py:554`）。

## Q3 重試、冪等與限購

### 1. 同一 Idempotency-Key 重送

`claim_key`（`backend/app/idempotency.py:52-95`）在獨立短交易內處理：

| 情況 | 回傳 | 位置與原因 |
| --- | --- | --- |
| 第一次已成功 | 重播原 HTTP 狀態與 body（同一訂單 ID） | `idempotency.py:74`、`:84`：冪等列與訂單在同一交易提交（`booking.py:186-190`），所以「DB 已提交但回應遺失」也會得到原結果 |
| 第一次仍在處理中 | 409 `IDEMPOTENCY_IN_PROGRESS`，附 `Retry-After: 1` | `idempotency.py:76`、`:87`：租約（30 秒）未到期；租約到期則接手並 generation +1（`:89`），舊處理者在 `lock_claim` 被拒（`:99`） |
| 相同 key、不同內容 | 409 `IDEMPOTENCY_KEY_REUSED` | `idempotency.py:73`：正規化後的請求雜湊不同 |

測試：`test_booking.py:31`、`:43`、`:152`、`:190`；付款同樣契約 `test_payments.py:50-108`。

### 2. 同時以兩個 key 送 3 + 2 張

限購檢查與寫入在同一個交易內：`lock_quota` 取得額度列鎖（`booking.py:140`）→ 權威求和（`booking.py:168`：PAID + 未逾時 PENDING_PAYMENT）→ 建單與座位 UPDATE（`booking.py:174-180`）→ commit（`booking.py:190`）。兩個請求在額度列鎖上序列化：第一個提交後，第二個才做求和，看到 3 → 3 + 2 > 4 → 409 `QUOTA_EXCEEDED`。若檢查與寫入不在同一交易（例如先用一個交易檢查、再開一個交易寫入），兩個請求都會讀到 0 並各自成立，合計 5 張。測試：`test_booking.py:67` `test_concurrent_different_keys_cannot_exceed_account_quota`（屏障同步的 3 + 2 與 4 + 4）。

### 3. 配位在哪裡挑選

在資料庫內：`SELECT … WHERE zone = ? AND 可售 ORDER BY id LIMIT n FOR UPDATE SKIP LOCKED`（`booking.py:152`）。暴露問題的並行情境：兩個帳號同時對同一區域各要 2 張，剩 3 席——若讀到應用程式記憶體再挑選，兩者會挑到相同的前 2 席，一方的條件式 UPDATE 會影響 0 列而失敗（或在無條件更新下超賣）；`SKIP LOCKED` 讓第二個交易直接跳過被鎖的席位，拿到不同的席位或在不足時回 409（區分 `SEAT_CONTENTION` 可重試與 `INSUFFICIENT_SEATS`，`booking.py:153-157`）。測試：`test_booking.py:82` `test_region_allocation_is_full_and_distinct_across_accounts`。

## Q4 即時推送

### 1. 從提交到所有訂閱者的路徑與順序

1. 交易內：`UPDATE event_streams … RETURNING current_version` 取得活動版本列鎖並配號，同一交易寫入 `seat_events` 與 `outbox_events`（`booking.py:45-76`，由購票、取消／逾時與付款成交共用：`booking.py:189`、`:251`、`payments.py:223`）。版本號只在持有 `event_streams` 列鎖時分配且與座位變更同交易提交，因此**同一活動的提交順序 = 版本順序**，由資料庫保證。
2. worker `publish_outbox`（`feed.py:87-101`）→ `_publish_event`（`feed.py:50-84`）：以交易層 advisory lock（`feed.py:60`）確保同一活動同時只有一個發布者，依 `aggregate_version` 排序 `PUBLISH seat:{event_id}`（`feed.py:70`），成功後標記 `published_at`（`feed.py:76`）並推進 `published_version`。
3. 每個 API 副本的 `FeedHub._listen`（`feed.py:317`）`PSUBSCRIBE seat:*`，解析後 `dispatch` 到該活動的每條連線佇列，`pump` 依佇列順序送出。
4. 瀏覽器只套用 `version == last + 1`，忽略 ≤ last，跳號則重取快照（`frontend/src/EventDetail.tsx` 的 `onmessage`）。

多副本：每個副本都從 Redis 收到相同順序的訊息；跨副本的客戶端比較的是同一組 DB 版本號，所以順序與完整性判斷不依賴副本。Redis 到達順序在故障下不保證（重發布、重連），但那只會造成重複或跳號，兩者都由版本規則處理。測試 `test_every_api_replica_delivers_the_same_versions`（`test_feed.py:189`）以兩個應用實例驗證。

### 2. 客戶端如何得知漏了事件

- 版本跳號（`frame.version != last + 1`）→ 立即重取 `GET /events/{id}/seats` 快照，快照帶 `version`，期間收到的 frames 緩衝後依版本續接（`EventDetail.tsx` `snapshot()`）。
- 伺服器每 `WS_VERSION_CHECK_SECONDS`（5 秒）向 DB 讀 `published_version`（worker 已交給 Redis 的版本，`feed.py:115-125`）並推 `{type:heartbeat, version}`（`feed.py:293-311`）。客戶端落後時先給 1–2 秒寬限（frame 可能仍在途中，見 `LOADTEST.md` 情境 B 第一次執行的教訓），仍落後才重取快照；因此即使最後一筆事件漏掉、之後再無事件，也會在約 5–7 秒內發現並恢復。回報 `published_version` 而非 `current_version` 的理由：客戶端真正可能漏掉的只有已發布的版本，未發布的版本不是缺口。
- 匯入完成只送 `resync_required: true` 的 frame，客戶端視同跳號重取（測試 `test_feed.py:172`、`frontend/src/Feed.test.tsx`）。
- 斷線：`onclose` 後 2 秒重連、重新握手、重取快照；重連前畫面顯示「未同步，暫停選位」。

### 3. 慢速客戶端緩衝滿時

選擇：每連線 `asyncio.Queue(maxsize=WS_QUEUE_SIZE=256)`；滿時標記 4008 並取消該連線的 pump、關閉連線（`feed.py:156`、`:276-284`），不丟中間事件也不阻塞其他訂閱者。捨棄的是該客戶端的這條連線（它必須重連並重取整份快照）；換來的是 API 記憶體上限固定（256 × frame）、其他客戶端不受慢客戶端拖累、版本連續規則不被「丟掉中間事件」破壞。

最壞情況：伺服器主動關閉時客戶端立刻得知（`onclose`），畫面標示未同步並停用選位，2 秒後重連 + 一次快照請求；畫面「錯」的程度是停留在關閉前的狀態、且不能選位，時間約 2 秒 + 快照往返。真正的最壞情況是**半開連線**（網路靜默中斷、TCP 未重置）：伺服器端 5 秒核對會失敗並清理，但客戶端在收到 TCP 錯誤前不會知道；前端加了 15 秒無 frame 看門狗（每 5 秒檢查，超過 15 秒沒有任何伺服器 frame 即視為失同步並重連），因此畫面最多顯示 15 秒的舊狀態，期間仍可能送出會被後端以 409 拒絕的選位。最終可購買性永遠由後端交易判定，畫面錯不會造成超賣。

### 4. 情境 B 實測

實際執行（`LOADTEST.md` 情境 B 最新一輪，commit `7d40bc8`，緊接情境 A 之後在同一資料庫執行）：2,000 條經 Nginx 的 WebSocket 連線全部完成應用層 token 認證，並在 61.2 秒穩態期間全部存活；80 次真實座位變更每一次都在 0.32 秒內送達全部 2,000 條連線，共 160,000 次送達、0 次未送達；送達延遲 p50 251 ms、p95 280 ms、p99 300 ms（HTTP 送出到收到 frame，客戶端單調時鐘）；伺服器推送約 3,009 frames/s；重同步 0、斷線 0；backend（4 個 API 程序合計）平均 19.0% 單核（含連線建立時 99.6% 的尖峰）、記憶體最高 713 MiB；結束後一致性稽核 0 違規。延遲主要來自 0.25 秒的 Outbox 輪詢。第一次執行暴露了 heartbeat 以 `current_version` 回報造成的誤判重同步，已修正為 `published_version`（見 `LOADTEST.md`）。

## Q5 權限與憑證失效

### 1. 立即失效與成本

- 登出：`logout_user` 設定該 session 的 `revoked_at`（`backend/app/auth.py:150-155`）。
- 停用或改角色：`update_user` 遞增 `users.auth_version`（`auth.py:169`）；session 保存建立時的 `auth_version`，認證查詢要求兩者相等（`auth.py:103`），所以該使用者所有既有 session 在下一次請求失敗。
- 每次 HTTP 請求都執行 `authenticate_token`（`auth.py:95-109`）：一個 `sessions ⋈ users` 查詢，條件含 `revoked_at IS NULL`、`expires_at > clock_timestamp()`、`auth_version` 相等、`is_active`。沒有正向快取，所以沒有「快取與資料庫不一致」的窗口；最長不一致時間 = 撤銷提交前已通過認證的在途請求（毫秒級，無法也不宣稱收回）。
- WebSocket：握手驗證後不再逐 frame 查 DB，而是每 5 秒對該活動所有訂閱者的 token 雜湊做一次批次查詢（`feed.py:293-309`），不合格者以 4001 斷線；最長不一致 5 秒 + 已送出的 frame。

量測（2026-11-17，本機 compose，PostgreSQL 16 容器，2,000 次呼叫）：

| 量測 | p50 | p95 | p99 |
| --- | --- | --- | --- |
| `authenticate_token` 純 DB 查詢（ms） | 0.497 | 0.877 | 1.155 |
| `GET /health` 經 Nginx，無認證無 DB（ms） | 0.63 | 1.08 | 1.25 |
| `GET /api/v1/auth/me` 經 Nginx，每請求 DB 認證（ms） | 1.98 | 2.89 | 3.35 |

每請求為撤銷即時性多付約 0.5 ms 的 DB 時間（連同連線池取得約 1.3 ms 端到端）。

### 2. 被停用者的匯入工作

繼續執行，依受理時的授權（`import_jobs.authorization_snapshot`）完成；見 ADR `docs/adr/0002-honor-accepted-import-authorization.md`。理由：受理時已確認授權並持久化，中途取消會留下半完成的座位集合與不明狀態；而停用後他對結果的任何後續操作（讀取、重試、發布）都會在當下授權檢查失敗。

### 3. 系統性驗證授權

- 全端點矩陣測試 `backend/tests/test_authorization_matrix.py`：由 `app.routes` 枚舉所有 `/api/v1` 路由，除白名單（註冊、登入、健康檢查、金流回呼）外，無 token 必須 401；Viewer 對所有寫入方法必須 403（登出除外）。新增端點若忘了加認證依賴會直接讓此測試失敗。
- 各模組另有角色 × 擁有者的個案測試（`test_auth.py:53,64`、`test_events.py`、`test_booking.py`、`test_payments.py:90`、`test_observability.py:110`）。

最容易漏掉的端點：`POST /api/v1/payments/callback`。它不走使用者 token，矩陣測試對它的「無 token → 401」斷言只證明沒有匿名存取，不證明簽章驗證正確；若有人把簽章檢查拿掉而改成接受任何請求，矩陣測試仍會因 401 以外的回應失敗，但若改成接受 bearer token，就要靠 `test_callback_requires_fresh_service_signature_not_user_token`（`test_payments.py:203`）才抓得到。這種「服務對服務」端點沒有角色可比對，是最容易被當成內部端點而放寬的地方。

## Q6 監控數據

### 1. 指標來源、時間窗與儲存

| 指標 | 量測層與來源 | 時間窗 | 儲存 |
| --- | --- | --- | --- |
| RPS | API 程序內 ASGI 中介軟體（`backend/app/observability.py:325-370`），以單調時鐘計時，每請求一筆 | 每秒每來源（API 程序）一桶；後台彙總最近 60 秒 | `access_logs`（每請求，PK event_id）→ `system_metrics(source_id, bucket_at, 1s)` |
| p50/p95/p99 | 同上的 `duration_us`（API 處理時間，不含 Nginx 與網路） | 每秒桶保留排序後的原始樣本；跨來源合併樣本後以 nearest-rank 取百分位（`observability.py:729-798`），不平均各副本百分位 | `system_metrics.latency_summary` |
| 錯誤率 | 同一中介軟體：HTTP 5xx 與未處理例外為系統錯誤；4xx 帶 `code` 的為業務拒絕，另計 | 每秒桶、60 秒彙總 | `access_logs.error_kind`／`business_outcome`，`system_metrics.error_count`／`business_counts` |
| 即時連線數 | feed `connection_delta`（`feed.py` 握手／結束時呼叫 `observability.py:289`），每秒由 heartbeat 送出 gauge | 每秒每來源當前值，彙總為各來源最新值之和；缺 heartbeat 標 `data_complete=false` | `system_metrics.connections` |

交接路徑：中介軟體 → Redis Streams（AOF always）→ worker `collect_telemetry` 批次寫 PostgreSQL 後才 XACK；Redis 不可用時落地本機 spool 再回放。

### 2. 「圖表正常但使用者受害」

情境：所有購票請求都回 HTTP 409（業務拒絕），例如某個 bug 讓所有座位的 `held_until` 永遠不過期，使用者實際上買不到票；系統錯誤率 0%、p95 正常、RPS 正常。另一個情境：模擬金流對每筆付款都回 FAILED，付款 API 仍是 202。

能否偵測：**能**。業務結果與系統錯誤分開計數（`access_logs.business_outcome`、`system_metrics.business_counts`），並有兩條專門的告警規則（`backend/app/observability.py` `_rule_conditions`）：

| 規則 | 來源 | 條件（預設，可由 `.env` 調整） |
| --- | --- | --- |
| `booking_rejection_rate` | 視窗內 `POST /api/v1/events/{event_id}/bookings` 的 `access_logs`，409 佔比 | ≥ 20 筆購票請求且佔比 > 0.8，持續 15 秒 |
| `payment_failure_rate` | 視窗內已結算的 `payments`（FAILED／SUCCEEDED），FAILED 佔比 | ≥ 20 筆且佔比 > 0.5，持續 15 秒 |

門檻故意設高：熱門活動售罄後大量 `INSUFFICIENT_SEATS` 是正常狀態，所以規則回報的是「幾乎沒有人成功」而非「有人被拒」。測試：`test_booking_rejection_alert_fires_when_http_looks_healthy`（25 筆購票 23 筆 409，系統規則保持安靜、業務規則觸發）與 `test_payment_failure_alert_counts_settled_mock_payments`。尚未涵蓋的盲區：付款「一直 PENDING 沒有結果」（金流沉默）不會觸發任何規則，只能從 `pending_reversals`／`payments.next_retry_at` 查詢看出。

### 3. 多副本同時寫入每秒指標

- 每個觀測事件有唯一 `event_id`（PK），collector 以 `ON CONFLICT DO NOTHING` 寫入，重送（XAUTOCLAIM、spool 回放）不重複計算（`observability.py:506-532`，測試 `test_replaying_the_same_event_id_persists_exactly_one_row`）。
- 每秒指標以 `(source_id, bucket_at, window_seconds)` 為鍵重算 upsert（覆寫整桶），不是累加，所以 collector 重啟或重跑不會重複累計；`source_id` 每個程序啟動不同。
- 不遺失：先 commit PostgreSQL 再 XACK；Redis 當機時 spool 到磁碟並記錄交接失敗數（`lost_events`），缺資料標 `data_complete=false` 而非補零。
- 副本時鐘慢 20 秒：事件時間戳不是 OS wall clock，而是以 DB `clock_timestamp()` 校準的錨點加單調時鐘位移（`observability.py:159-256`），所以慢 20 秒的 OS 時鐘不影響分桶。若校準失敗（DB 不可達），事件標 `clock_valid=false`，collector 以收集時間放桶並把該桶標為不完整；圖上會看到該來源的資料延後出現並被標示不完整，而不是出現在 20 秒前的桶造成「過去的 RPS 變高、現在變低」。測試 `test_metrics_merge_samples_across_instances_and_do_not_report_gaps_as_zero`、`test_unanchored_replica_clock_is_flagged_not_silently_misplaced`。

### 4. 兩個來源不一致時相信哪一個

- 業務狀態：永遠相信 PostgreSQL 的 `orders`／`seats`，不相信 Redis、推送 frame 或畫面。
- 指標：相信已落地 PostgreSQL 的 `access_logs`（可去重、可重算）勝過 Redis Stream 長度或程序記憶體計數；`system_metrics` 是由前者重算的投影。
- 延遲：伺服器端 p95 量的是 API 處理時間，壓測報告以客戶端（Locust）觀測為準——使用者受害與否看客戶端數字；兩者差距就是 Nginx 排隊、網路與監控交接的成本（`LOADTEST.md` 兩者並列）。
- 事件順序：相信 `event_streams.current_version`，不相信 Redis 到達順序。
