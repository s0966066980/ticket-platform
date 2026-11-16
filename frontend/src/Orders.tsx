import { useEffect, useState } from "react";
import { ArrowLeft, RefreshCw, CreditCard, Ticket } from "lucide-react";
import { request, mutate, date, money, type Order, type Page } from "./api";
import { Notice, Empty, Status } from "./App";
export default function Orders({
  id,
  onOpen,
  onBack,
}: {
  id?: string | null;
  onOpen: (id: string) => void;
  onBack: () => void;
}) {
  const [items, setItems] = useState<Order[]>([]),
    [order, setOrder] = useState<Order>(),
    [cursor, setCursor] = useState<string | null>(null),
    [error, setError] = useState<unknown>(),
    [busy, setBusy] = useState(false),
    [notice, setNotice] = useState(""),
    [now, setNow] = useState(Date.now()),
    [delay, setDelay] = useState(0);
  async function load(next = false) {
    setError(null);
    try {
      if (id) setOrder(await request<Order>(`/orders/${id}`));
      else {
        const data = await request<Page<Order>>(
          `/orders${next && cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`,
        );
        setItems((old) => (next ? [...old, ...data.items] : data.items));
        setCursor(data.next_cursor);
      }
    } catch (e) {
      setError(e);
    }
  }
  useEffect(() => {
    setOrder(undefined);
    void load();
    const timer = setInterval(
      () => {
        setNow(Date.now());
        if (id) void load();
      },
      id ? 2000 : 30000,
    );
    return () => clearInterval(timer);
  }, [id]);
  async function action(fn: () => Promise<unknown>, message: string) {
    setBusy(true);
    setError(null);
    try {
      await fn();
      setNotice(message);
      await load();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  const remaining = order
    ? Math.max(0, Math.ceil((Date.parse(order.expires_at) - now) / 1000))
    : 0;
  return (
    <>
      {id && (
        <button className="link back" onClick={onBack}>
          <ArrowLeft size={16} />
          返回我的訂單
        </button>
      )}
      <header className="page-heading">
        <div>
          <p className="eyebrow">YOUR NEXT EXPERIENCE</p>
          <h1>{id ? "訂單詳情" : "我的訂單"}</h1>
          <p className="muted">付款完成前可整筆取消，取消或逾時後釋放座位。</p>
        </div>
        <button className="secondary" onClick={() => void load()}>
          <RefreshCw size={16} />
          更新
        </button>
      </header>
      <Notice error={error} />
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      {!id ? (
        <div className="order-list">
          {items.map((o) => (
            <button
              className="panel order-row"
              key={o.id}
              onClick={() => onOpen(o.id)}
            >
              <Ticket />
              <div>
                <strong>訂單 {o.id.slice(0, 8)}</strong>
                <small>
                  {date(o.created_at)} · {o.ticket_count} 張
                </small>
              </div>
              <Status value={o.status} />
              <strong>{money(o.total_twd)}</strong>
            </button>
          ))}
          {!items.length && !error && (
            <Empty>你目前還沒有訂單，前往探索活動挑選座位。</Empty>
          )}
          {cursor && (
            <button className="secondary" onClick={() => void load(true)}>
              載入更多訂單
            </button>
          )}
        </div>
      ) : order ? (
        <div className="booking-layout">
          <section className="panel">
            <Status value={order.status} />
            <h2>訂單 {order.id.slice(0, 8)}</h2>
            <p className="muted mono">{order.id}</p>
            <p>建立時間：{date(order.created_at)}</p>
            <p>付款期限：{date(order.expires_at)}</p>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>區域</th>
                    <th>座位</th>
                    <th>票價</th>
                  </tr>
                </thead>
                <tbody>
                  {order.items?.map((s) => (
                    <tr key={s.seat_id}>
                      <td>{s.zone_code_snapshot}</td>
                      <td>
                        {s.row_label_snapshot} 排 {s.seat_number_snapshot} 號
                      </td>
                      <td>{money(s.unit_price_twd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {order.payments?.length ? (
              <>
                <h3>付款紀錄</h3>
                {order.payments.map((p) => (
                  <p key={p.id}>
                    <span className="mono">{p.id.slice(0, 8)} </span>
                    <Status value={p.status} />
                  </p>
                ))}
              </>
            ) : null}
          </section>
          <aside className="panel checkout">
            <h2>付款摘要</h2>
            <div className="price-line">
              <span>票價小計（{order.ticket_count} 張）</span>
              <strong>{money(order.subtotal_twd)}</strong>
            </div>
            <div className="price-line">
              <span>服務費</span>
              <strong>{money(order.fee_twd)}</strong>
            </div>
            <div className="price-line total">
              <span>應付總額</span>
              <strong>{money(order.total_twd)}</strong>
            </div>
            {order.status === "PENDING_PAYMENT" && (
              <>
                <div className="countdown" role="timer">
                  {Math.floor(remaining / 60)}:
                  {String(remaining % 60).padStart(2, "0")}
                  <small>剩餘付款時間（以伺服器判定為準）</small>
                </div>
                <label>
                  模擬回呼延遲（秒）
                  <input
                    type="number"
                    value={delay}
                    min={0}
                    max={600}
                    onChange={(e) => setDelay(Number(e.target.value))}
                  />
                </label>
                <button
                  className="primary wide"
                  disabled={busy || !remaining}
                  onClick={() =>
                    void action(
                      () =>
                        mutate(`/orders/${id}/payments`, {
                          outcome: "success",
                          delay_seconds: delay,
                        }),
                      "付款已送出，正在等待金流回呼確認。",
                    )
                  }
                >
                  <CreditCard size={17} />
                  模擬付款成功
                </button>
                <button
                  className="secondary wide"
                  disabled={busy || !remaining}
                  onClick={() =>
                    void action(
                      () =>
                        mutate(`/orders/${id}/payments`, {
                          outcome: "failure",
                          delay_seconds: delay,
                        }),
                      "失敗情境已送出，可於原期限內重試。",
                    )
                  }
                >
                  模擬付款失敗
                </button>
                <button
                  className="danger wide"
                  disabled={busy}
                  onClick={() => {
                    if (confirm("確定取消整筆訂單並釋放座位？"))
                      void action(
                        () => mutate(`/orders/${id}/cancel`, {}),
                        "訂單已取消。",
                      );
                  }}
                >
                  取消整筆訂單
                </button>
              </>
            )}
            {order.status === "PAID" && (
              <p className="notice">付款已完成，期待與你在現場相見。</p>
            )}
            {["CANCELED", "EXPIRED"].includes(order.status) && (
              <p className="muted">
                訂單已終止。晚到的成功收款將自動沖正，請查看付款紀錄。
              </p>
            )}
          </aside>
        </div>
      ) : (
        <p role="status">正在載入訂單…</p>
      )}
    </>
  );
}
