# AI 使用說明

本專案由兩個 AI 編碼代理在使用者監督下完成：設計訪談與前三分之二的實作由 OpenAI Codex 進行（2026-11-16 至 11-17 凌晨），其後由 Anthropic Claude Code（Claude Fable 5.1）接手整合、補齊付款／即時推送／監控、執行壓測與撰寫交付文件（2026-11-17）。所有決策由使用者在設計訪談（`docs/design-interview.md` Q1–Q18）與接手時的六項決議中確認。

## 使用了哪些 AI 工具、用在哪些部分

| 工具 | 用途 |
| --- | --- |
| OpenAI Codex（CLI，多個子代理並行） | 需求澄清訪談、`GLOSSARY.md`／ADR、`docs/implementation-plan.md`、`docs/core-schema.md`、票單圖；票單 01–07、10、12、13 的原始碼與測試（基礎架構、認證、活動、購票交易、匯入、報表、壓測腳本、GitHub Actions） |
| Claude Code（Claude Fable 5.1，主代理 + 兩個子代理） | 接手盤點與整合；票單 08 付款與模擬金流、14 監控管線、09 即時推送與 worker；效能瓶頸分析與修正；本機 compose 端到端驗證；壓測執行；`DESIGN.md`、`LOADTEST.md`、`AI_USAGE.md`、架構圖、README 補完 |
| GitHub Actions（PostgreSQL 16 + Redis 7 service containers） | 由 AI 撰寫的 CI，在 Codex 工作階段沒有 Docker socket 時提供唯一的真實執行證據 |

工作方式：每張票在獨立 git worktree／分支上以 TDD 於公開介面（HTTP、WebSocket、CLI、匯出檔）寫測試，合併前合入 `integration/mvp` 最新內容並重跑全部測試；合併者只以實際執行結果結案。壓測數字、故障實驗輸出與測試輸出均來自真實執行，文件中註明 commit 與時間。

## AI 產生的程式碼有誤之處（實際發現的案例）

以下皆為實際發生、有測試或實際執行可重現的錯誤，依發現時間排列。「發現方式」欄說明是由自動化測試、程式審查或實際執行暴露。

### 前端（Codex，票單 02，commit 73378de 之前）

1. **大範圍文字替換把畫面引用插進 Auth 元件，造成 `ReferenceError`。** 發現：公開介面測試失敗。修正：改為限定範圍的替換，測試轉綠。
2. **登出在 `finally` 無條件清除本機登入狀態，伺服器登出失敗或斷線時使用者仍被登出，憑證卻未撤銷。** 發現：新增「登出未獲伺服器確認時保留登入並顯示可重試錯誤」的介面測試先失敗。修正：只在伺服器確認後清除，失敗時保留 session 並顯示錯誤（`frontend/src/App.test.tsx`）。
3. **初始 HTTP 座位快照可能覆蓋 WebSocket 已套用的較新快照。** 發現：程式審查。修正：以版本比較守衛（`frontend/src/EventDetail.tsx`），不宣稱有量測到實際故障。
4. **Admin 後台用單一全域 in-flight 旗標，指標載入中會跳過使用者分頁的載入。** 發現：程式審查。修正：改為各分頁獨立旗標。

### 付款與模擬金流（Codex 草稿 bd48da8，由 Claude 子代理接手完成）

5. **`initiate_payment` 用 `except Exception` 把所有 SQLAlchemy 失敗變成 500 INTERNAL_ERROR**，違反設計中「可重試基礎設施錯誤回 503 並保留冪等 key」的合約（`booking.reserve_order` 有正確處理）。發現：對照 `backend/app/booking.py` 的錯誤路徑審查。修正：捕捉 `SQLAlchemyError`、回滾、`release_key`、回 503 `DATABASE_UNAVAILABLE`。
6. **沖正工作完成時無條件寫入 `payment.status='REVERSED'` 與 `reversed_at`**，金流已沖正但 backend 遺失確認、租約被接手重放時會覆寫時間並可能覆蓋其他狀態。發現：撰寫「provider 已沖正但 backend 未確認時重試」測試（`backend/tests/test_payments.py::test_worker_reverses_late_payment_idempotently_through_gateway`）。修正：只在 `REVERSAL_PENDING` 時轉移。

### 監控管線（Codex 草稿 8887ac7，由 Claude 子代理接手完成）

7. **路由模板取 `scope['route'].path`，在 FastAPI 0.163 的 `_IncludedRouter` 下是相對路徑（`/admin/users`），遺失 `/api/v1` 前綴**，造成指標按路由彙總錯誤。發現：第一個 spool 測試斷言 `route_template == '/api/v1/admin/users'` 失敗。修正：由請求路徑與 `path_params` 還原模板。
8. **DB 時鐘校準同步跑在請求交接路徑上（連線逾時 5 秒）**，DB 不可用時每個請求都會被拖慢。發現：程式審查。修正：校準移到背景執行緒，請求路徑只讀錨點。
9. **Redis 失敗時把退避時間歸零**，Redis 停機期間每個請求都重新嘗試連線（各 ≥0.2 秒）。發現：程式審查。修正：耐久性探測固定每 5 秒重試一次。
10. **回應 JSON 解析只捕捉 `ValueError`，陣列型 body 會在中介軟體內丟 `AttributeError`。** 發現：程式審查。修正：擴大例外處理，且只對 4xx 緩衝 body。
11. **存取事件在最後一個 body chunk 送出前就交接**，把交接延遲算進使用者回應時間。發現：程式審查。修正：送出後才記錄。
12. **`_trace()` 延遲載入 `app.main`，worker 程序第一次寫服務日誌時會建構整個 FastAPI 應用。** 修正：只在 `app.main` 已載入時取用 request_id。
13. **使用已棄用的 `@app.on_event`。** 修正：包裝 `app.router.lifespan_context`。
14. **文件字串承諾的 `refresh_metrics`、`evaluate_alerts`、CLI 與 admin router 根本不存在**，草稿自己的測試以 404 取代 401 失敗。修正：實作補齊，測試改為 23 個公開介面測試。
15. **`connection_delta` 在 async 程式碼內直接做 Redis／fsync I/O。** 修正：交接移到 daemon 執行緒。

### 即時推送與 worker（Claude 主代理，票單 09）

16. **呼叫 `app.add_event_handler('shutdown', …)` 註冊 feed hub 的關閉處理**，但該方法在 FastAPI 0.163／Starlette 1.10 已移除，所有 WebSocket 訂閱在握手後立刻斷線。發現：`backend/tests/test_feed.py` 第一輪執行即失敗（`AttributeError: 'FastAPI' object has no attribute 'add_event_handler'`）。修正：改為附加到 `app.router.on_shutdown`（存在時）。
17. **`run_once()` 先執行 Outbox 發布、再執行逾時收斂**，單一週期內逾時產生的座位事件要等下一週期才發布。發現：`backend/tests/test_worker.py::test_one_worker_cycle_expires_due_holds_and_publishes_their_events` 斷言 `unpublished == 0` 失敗（實際 1）。修正：工作群組順序改為發布最後執行。
18. **慢速訂閱者的關閉依賴 pump 自己讀到結束標記**，但卡在 `send_text` 的 pump 永遠讀不到。發現：以永不讀取的假 WebSocket 寫溢位測試時推理出來，重構為取消 pump task。

### 效能相關（由壓測暴露，非單一行錯誤）

19. **購票交易把活動事件版本列的鎖取在所有檢查之前**（Codex 依設計文件實作，設計本身也如此寫），使所有被拒絕的請求（限購、座位不可用）也要排隊等同一把鎖。發現：第一次情境 A 壓測完成的購票請求上限約 90/s、62% 客戶端逾時。修正：把所有拒絕判斷與座位條件式更新移到版本鎖之前，版本鎖只保護版本遞增與刪除競爭（`backend/app/booking.py:158-186`），並以刪除／購票競爭測試確認語意不變。壓測前後數字見 `LOADTEST.md`。

20. **縮短版本區段的重構把「活動已被刪除」與「不在售票時段」合併成同一個 409。** 刪除／購票競爭測試在重複執行時偶發 `(204, 409)` 而非預期的 `(204, 404)`：安全性未受影響（訂單仍回滾），但狀態碼錯誤。發現：`test_deletion_and_first_booking_cannot_both_succeed` 連續執行 6 次中失敗 2 次。修正：只在寫入 0 列的失敗路徑補一次 `deleted_at` 查詢以區分 404 與 409（`backend/app/booking.py:69-74`），再連跑 8 次全部通過。
21. **壓測 harness 的 `limited_api.py` 以 `uvicorn --workers 1` 啟動**，uvicorn 因而成為 supervisor、由子程序處理匯出，基準測試量到的是錯誤的程序（RSS 2.3 MB、`single_process=false`）。發現：第一次 500K 匯出結果的 `enforced_address_space_hard_limit_bytes=-1`。修正：不帶 `--workers`，單一程序直接承受 `RLIMIT_AS`。

## 如何避免 AI 錯誤進入交付

- 每個模組先有公開介面測試，再有實作；草稿程式一律視為未驗證。
- 合併者重跑整個測試套件與 compose 端到端煙霧測試，不信任子代理的口頭回報。
- 故意破壞保護機制的實驗只在隔離的工作樹與拋棄式資料庫進行，取得失敗輸出後還原（見 `DESIGN.md` Q1）。
- 所有數字（壓測、記憶體、恢復時間）均保留原始輸出於 `loadtests/results/`，未執行的項目在 `README.md` 與 `LOADTEST.md` 如實標示。
