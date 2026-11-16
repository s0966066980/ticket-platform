# 序場前端

React / TypeScript / Vite，Tailwind CSS 與 Recharts。介面使用繁體中文，時間顯示 Asia/Taipei、金額顯示 TWD。

```sh
npm ci
npm run dev
npm test
npm run build
```

開發伺服器將 `/api` 與 `/ws` 代理至 `127.0.0.1:8000`；Compose 以相同來源 Nginx 代理，不需要在瀏覽器設定 API 位址。共同合約見 [API contract](../docs/api-contract.md)。

提供登入／註冊、固定快照活動清單、活動草稿建立與版本化編輯、CSV/JSON 匯入與錯誤進度、發布、區域載入的座位圖、指定／自動配位、訂單／付款／取消、Excel 下載，以及 Admin 指標／告警／使用者／request_id 查詢。前端顯示權限不取代後端授權。

座位圖每次僅渲染 400 個座位，提供區域與排數篩選及區段切換。WebSocket 未通過認證、斷線或版本缺口時暫停選位；重新取得一致快照後恢復。HTTP 付款或鎖位回應遺失時，重試沿用相同 Idempotency-Key；金額以伺服器回應為準。

公開 UI 測試覆蓋 Viewer 操作限制、失同步禁止選位、登出失敗保留狀態，以及付款失聯重試。這些測試在 HTTP/WS 邊界使用替身，不代表真實後端或完整瀏覽器端到端驗收已完成。開發環境曾成功啟動 Vite，但 CUA 無可用瀏覽器，視覺 QA 尚待可用瀏覽器環境驗證。
