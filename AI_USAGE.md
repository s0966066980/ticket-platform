# AI 使用說明

目前我的專案主要由兩個 AI 編碼代理在使用者監督下完成：設計主要框架由Codex來進行，後續程式碼修改與測試由Claude Code接手整合、補齊付款／即時推送／監控、執行壓測與撰寫交付文件。

## 說明你使用了哪些 AI 工具、用在哪些部分

| 工具 | 用途 |
| --- | --- |
| OpenAI Codex| 需求確認、`GLOSSARY.md`／ADR、`docs/implementation-plan.md`、`docs/core-schema.md`、票單圖；基礎架構、認證、活動、購票交易、匯入、報表、壓測腳本 |
| Claude Code | 接手整合；付款與模擬金流、監控管線、即時推送；效能瓶頸分析與修正；本機 compose 端到端驗證；壓測執行 |


## 列出**至少 3 處 AI 產生的程式碼有誤**的地方，以及你如何發現、如何修正

1. **大範圍文字替換把畫面引用插進 Auth 元件，造成 `ReferenceError`。**
發現：公開介面測試失敗。  修正：改為限定範圍的替換，測試轉綠。
2. **登出在 `finally` 無條件清除本機登入狀態，伺服器登出失敗或斷線時使用者仍被登出，憑證卻未撤銷。**
發現：新增「登出未獲伺服器確認時保留登入並顯示可重試錯誤」的介面測試先失敗。  修正：只在伺服器確認後清除，失敗時保留 session 並顯示錯誤。
3. **Admin：防重複請求的狀態範圍太大。**
發現：指標請求尚未完成時，共用旗標仍為 true；此時切到使用者分頁，載入函式會直接返回。指標完成後也不會自動補發使用者請求。這是把「同一分頁避免重複載入」誤做成「所有分頁互相阻擋」。。  修正：改為各分頁獨立旗標。
4. **DB 時鐘校準同步跑在請求交接路徑上（連線逾時 5 秒）**，DB不可用時觸發校準的請求可能被拖慢。
發現： middleware 會等待 handoff 完成。即使使用 thread pool，請求仍在等待結果。DB 無法連線時，校準可能受到 5 秒連線逾時拖累。  修正：校準移到背景執行緒，請求路徑只讀錨點。
5. **Redis 失敗時把退避時間歸零**，Redis 停機期間每個請求都重新嘗試連線。
發現：例外分支執行 config_until = 0，使下一次交接一定符合「現在已超過探測時間」，再次連線。持續故障時，就反覆探測。現在改為保留五秒探測間隔，失敗時清除可用狀態。  修正：耐久性探測固定每 5 秒重試一次。
6. **回應 JSON 解析只捕捉 `ValueError`，陣列型 body 會在中介軟體內丟 `AttributeError`。把「合法 JSON」錯當成「JSON 物件」**
發現：直接使用 json.loads(body).get('code')。[] 能成功解析，因此不會拋 ValueError；但解析結果是 list，呼叫 .get() 才拋 AttributeError。舊版又對所有 JSON 回應做解析，所以成功的陣列回應也可能觸發。  修正：擴大例外處理，且只對 4xx 緩衝 body。
7. **存取事件在最後一個 body chunk 送出前就交接**，把交接延遲增加了使用者等待時間。
發現：草稿先 await record()，才 await send(message)；因此 Redis 或本機落盤交接會延後最後一段回應送出。  修正：送出後才記錄。

### 即時推送與 worker

16. **呼叫 `app.add_event_handler('shutdown', …)` 註冊 feed hub 的關閉處理**，但該方法在 FastAPI 0.163／Starlette 1.10 已移除，所有 WebSocket 訂閱在握手後立刻斷線。發現：`backend/tests/test_feed.py` 第一輪執行即失敗（`AttributeError: 'FastAPI' object has no attribute 'add_event_handler'`）。修正：改為附加到 `app.router.on_shutdown`（存在時）。
17. **`run_once()` 先執行 Outbox 發布、再執行逾時收斂**，單一週期內逾時產生的座位事件要等下一週期才發布。發現：`backend/tests/test_worker.py::test_one_worker_cycle_expires_due_holds_and_publishes_their_events` 斷言 `unpublished == 0` 失敗（實際 1）。修正：工作群組順序改為發布最後執行。
18. **慢速訂閱者的關閉依賴 pump 自己讀到結束標記**，但卡在 `send_text` 的 pump 永遠讀不到。發現：以永不讀取的假 WebSocket 寫溢位測試時推理出來，重構為取消 pump task。

### 效能相關（由壓測暴露，非單一行錯誤）

19. **購票交易把活動事件版本列的鎖取在所有檢查之前**（Codex 依設計文件實作，設計本身也如此寫），使所有被拒絕的請求（限購、座位不可用）也要排隊等同一把鎖。發現：第一次情境 A 壓測完成的購票請求上限約 90/s、62% 客戶端逾時。修正：把所有拒絕判斷與座位條件式更新移到版本鎖之前，版本鎖只保護版本遞增與刪除競爭（`backend/app/booking.py:162-190`），並以刪除／購票競爭測試確認語意不變。壓測前後數字見 `LOADTEST.md`。

20. **縮短版本區段的重構把「活動已被刪除」與「不在售票時段」合併成同一個 409。** 刪除／購票競爭測試在重複執行時偶發 `(204, 409)` 而非預期的 `(204, 404)`：安全性未受影響（訂單仍回滾），但狀態碼錯誤。發現：`test_deletion_and_first_booking_cannot_both_succeed` 連續執行 6 次中失敗 2 次。修正：只在寫入 0 列的失敗路徑補一次 `deleted_at` 查詢以區分 404 與 409（`backend/app/booking.py:70-75`），再連跑 8 次全部通過。
21. **壓測 harness 的 `limited_api.py` 以 `uvicorn --workers 1` 啟動**，uvicorn 因而成為 supervisor、由子程序處理匯出，基準測試量到的是錯誤的程序（RSS 2.3 MB、`single_process=false`）。發現：第一次 500K 匯出結果的 `enforced_address_space_hard_limit_bytes=-1`。修正：不帶 `--workers`，單一程序直接承受 `RLIMIT_AS`。

## 如何避免 AI 錯誤進入交付

- 每個模組先有公開介面測試，再有實作；草稿程式一律視為未驗證，避免混入。
- 合併後需要重跑整個測試套件與 compose 端到端的測試，子代理的口頭回報需要作測試查證。
- 故意破壞保護機制的實驗只在隔離的工作樹與拋棄式資料庫進行，取得失敗輸出後還原。
- 所有數字（壓測、記憶體、恢復時間）均保留原始輸出於 `loadtests/results/`，未執行的項目在 `README.md` 與 `LOADTEST.md` 如實標示。
- 每項工作必須嚴格根據goal.md
- 交叉使用主流AI模型比對檢查
