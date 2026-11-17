import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  ArrowLeft,
  Download,
  RefreshCw,
  Radio,
  Upload,
  Check,
  ChevronLeft,
  ChevronRight,
} from "lucide-react";
import {
  request,
  mutate,
  money,
  date,
  downloadReport,
  type TicketEvent,
  type Seat,
  type User,
  type Order,
} from "./api";
import { Notice, Status, Empty } from "./App";
interface Job {
  id: string;
  status: string;
  total_rows: number;
  processed_rows: number;
  inserted_rows: number;
  error_rows: number;
  failure_code?: string;
}
export function ImportPanel({
  id,
  onDone,
}: {
  id: string;
  onDone: () => void;
}) {
  const [job, setJob] = useState<Job>(),
    [error, setError] = useState<unknown>(),
    [errors, setErrors] = useState<
      { row_number: number; reason: string; code: string }[]
    >([]),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!job || !["QUEUED", "RUNNING"].includes(job.status)) return;
    const timer = setInterval(
      () =>
        request<Job>(`/imports/${job.id}`)
          .then((j) => {
            setJob(j);
            if (!["QUEUED", "RUNNING"].includes(j.status)) {
              onDone();
              if (j.error_rows)
                request<{ items: typeof errors }>(`/imports/${j.id}/errors`)
                  .then((x) => setErrors(x.items))
                  .catch(setError);
            }
          })
          .catch(setError),
      1000,
    );
    return () => clearInterval(timer);
  }, [job?.id, job?.status]);
  async function upload(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setErrors([]);
    try {
      const form = new FormData(e.currentTarget),
        file = form.get("file") as File;
      if (file.size > 20_000_000) throw new Error("檔案上限為 20MB。");
      const j = await request<Job>(`/events/${id}/seats/import`, {
        method: "POST",
        body: form,
      });
      setJob(j);
      if (j.error_rows) {
        const x = await request<{ items: typeof errors }>(
          `/imports/${j.id}/errors`,
        );
        setErrors(x.items);
      }
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel">
      <h2>
        <Upload size={19} />
        座位匯入
      </h2>
      <p className="muted">
        CSV 或 JSON，欄位為 zone_code、row_label、seat_number。上限 20MB /
        100,000 列；相同檔案會沿用原工作。
      </p>
      <form onSubmit={upload} className="form-grid">
        <label>
          座位檔案
          <input
            name="file"
            type="file"
            accept=".csv,.json,text/csv,application/json"
            required
          />
        </label>
        <label>
          匯入模式
          <select name="mode">
            <option value="all_or_nothing">全部成功才匯入</option>
            <option value="skip_invalid">略過錯誤列</option>
          </select>
        </label>
        <button className="primary" disabled={busy}>
          {busy ? "上傳中…" : "上傳座位"}
        </button>
      </form>
      <Notice error={error} />
      {job && (
        <div className="job-progress">
          <Status value={job.status} />
          <progress
            max={Math.max(job.total_rows, 1)}
            value={job.processed_rows}
          />
          <p>
            處理 {job.processed_rows} / {job.total_rows} 列 · 新增{" "}
            {job.inserted_rows} · 錯誤 {job.error_rows}
          </p>
          {job.status === "FAILED_SYSTEM" && (
            <button
              className="secondary"
              onClick={() =>
                mutate<Job>(`/imports/${job.id}/retry`, {})
                  .then(setJob)
                  .catch(setError)
              }
            >
              重試原工作
            </button>
          )}
          {errors.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>來源列</th>
                    <th>原因</th>
                  </tr>
                </thead>
                <tbody>
                  {errors.map((x, i) => (
                    <tr key={i}>
                      <td>{x.row_number}</td>
                      <td>
                        {x.reason} <small>{x.code}</small>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
export default function EventDetail({
  id,
  user,
  onBack,
  onOrder,
  onEdit,
}: {
  id: string;
  user: User;
  onBack: () => void;
  onOrder: (id: string) => void;
  onEdit: (event: TicketEvent) => void;
}) {
  const [event, setEvent] = useState<TicketEvent>(),
    [seats, setSeats] = useState<Seat[]>([]),
    [zone, setZone] = useState(""),
    [selected, setSelected] = useState<string[]>([]),
    [quantity, setQuantity] = useState(1),
    [mode, setMode] = useState("manual"),
    [offset, setOffset] = useState(0),
    [row, setRow] = useState(""),
    [synced, setSynced] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(),
    [success, setSuccess] = useState("");
  const version = useRef(-1),
    buffer = useRef<
      {
        version: number;
        changes?: Partial<Seat>[];
        resync_required?: boolean;
      }[]
    >([]),
    loading = useRef(false),
    alive = useRef(true),
    lastFrameAt = useRef(0),
    socketRef = useRef<WebSocket | null>(null);
  async function loadEvent() {
    const item = await request<TicketEvent>(`/events/${id}`);
    setEvent(item);
  }
  async function snapshot() {
    if (loading.current || !alive.current) return;
    loading.current = true;
    setSynced(false);
    try {
      const result = await request<{ version: number; items: Seat[] }>(
        `/events/${id}/seats`,
      );
      if (!alive.current) return;
      version.current = result.version;
      let rows = result.items;
      const pending = buffer.current.sort((a, b) => a.version - b.version);
      buffer.current = [];
      let gap = false;
      for (const frame of pending) {
        if (frame.version <= version.current) continue;
        if (frame.version !== version.current + 1 || frame.resync_required) {
          gap = true;
          break;
        }
        rows = rows.map((s) => ({
          ...s,
          ...frame.changes?.find((x) => x.id === s.id),
        }));
        version.current = frame.version;
      }
      setSeats(rows);
      setSelected([]);
      setSynced(!gap && socketRef.current?.readyState === WebSocket.OPEN);
      if (gap) setTimeout(() => void snapshot(), 300);
    } catch (e) {
      if (alive.current) setError(e);
    } finally {
      loading.current = false;
    }
  }
  useEffect(() => {
    alive.current = true;
    void loadEvent().catch(setError);
    let retry: ReturnType<typeof setTimeout>;
    let socket: WebSocket;
    let disposed = false;
    function connect() {
      setSynced(false);
      socket = new WebSocket(
        `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws/events/${id}`,
      );
      socketRef.current = socket;
      socket.onopen = () =>
        (lastFrameAt.current = Date.now()) &&
        socket.send(
          JSON.stringify({
            type: "auth",
            token: sessionStorage.getItem("ticket-token"),
          }),
        );
      socket.onmessage = ({ data }) => {
        lastFrameAt.current = Date.now();
        try {
          const frame = JSON.parse(data);
          if (frame.type === "auth_ok") {
            void snapshot();
            return;
          }
          if (frame.type === "heartbeat") {
            // The server reports the version it has already published. If we
            // are behind, the frame may still be in flight: give it one second
            // (plus jitter so 2,000 viewers do not refetch at the same instant)
            // and resync only if it has not arrived by then.
            if (version.current >= 0 && frame.version > version.current) {
              const expected = frame.version;
              setTimeout(
                () => {
                  if (alive.current && version.current < expected) {
                    setSynced(false);
                    void snapshot();
                  }
                },
                1000 + Math.random() * 1000,
              );
            }
            return;
          }
          if (frame.type === "seat_changed") {
            if (loading.current || version.current < 0) {
              if (buffer.current.length >= 256) {
                buffer.current = [];
                setSynced(false);
                socket.close();
                return;
              }
              buffer.current.push(frame);
              return;
            }
            if (frame.version <= version.current) return;
            if (frame.version !== version.current + 1 || frame.resync_required) {
              // A gap, or a bulk change (seat import) that carries no per-seat
              // patches: the snapshot is the only correct recovery.
              setSynced(false);
              void snapshot();
              return;
            }
            version.current = frame.version;
            setSeats((old) =>
              old.map((s) => ({
                ...s,
                ...frame.changes?.find((x: Partial<Seat>) => x.id === s.id),
              })),
            );
            setSelected([]);
          }
        } catch {
          setSynced(false);
          void snapshot();
        }
      };
      socket.onclose = () => {
        setSynced(false);
        if (!disposed) retry = setTimeout(connect, 2000);
      };
      socket.onerror = () => {
        setSynced(false);
        socket.close();
      };
    }
    connect();
    const timer = setInterval(() => {
      if (socket.readyState !== WebSocket.OPEN) return;
      // The server pushes a version heartbeat at least every 5 s. A half-open
      // connection delivers nothing: treat 15 s of silence as out of sync and
      // reconnect instead of trusting a stale seat map.
      if (lastFrameAt.current && Date.now() - lastFrameAt.current > 15000) {
        setSynced(false);
        socket.close();
        return;
      }
      socket.send(JSON.stringify({ type: "heartbeat" }));
    }, 5000);
    void request<{ version: number; items: Seat[] }>(`/events/${id}/seats`)
      .then((data) => {
        if (alive.current && version.current < 0) {
          setSeats(data.items);
          version.current = data.version;
        }
      })
      .catch(setError);
    return () => {
      alive.current = false;
      disposed = true;
      clearTimeout(retry);
      clearInterval(timer);
      socket.close();
    };
  }, [id]);
  async function action(fn: () => Promise<unknown>, message = "") {
    setBusy(true);
    setError(null);
    setSuccess("");
    try {
      await fn();
      if (message) setSuccess(message);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  async function book() {
    await action(async () => {
      const order = await mutate<Order>(
        `/events/${id}/bookings`,
        mode === "manual"
          ? { seat_ids: selected.slice().sort() }
          : { zone_id: zone, quantity },
      );
      onOrder(order.id);
    });
  }
  if (!event)
    return (
      <>
        <button className="link" onClick={onBack}>
          <ArrowLeft size={16} />
          返回活動
        </button>
        <Notice error={error} />
        <p role="status">載入活動…</p>
      </>
    );
  const manager = user.role === "ADMIN" || event.owner_id === user.id,
    canBuy = user.role !== "VIEWER" && event.status === "PUBLISHED";
  const visible = seats.filter(
    (s) => (!zone || s.zone_id === zone) && (!row || s.row_label.includes(row)),
  );
  const selectedPrice = seats
    .filter((s) => selected.includes(s.id))
    .reduce(
      (sum, s) =>
        sum + (event.zones?.find((z) => z.id === s.zone_id)?.price_twd || 0),
      0,
    );
  const saleOpen =
    Date.now() >= Date.parse(event.sales_start) &&
    Date.now() < Date.parse(event.sales_end);
  return (
    <>
      <button className="link back" onClick={onBack}>
        <ArrowLeft size={16} />
        返回活動清單
      </button>
      <header className="page-heading">
        <div>
          <Status value={event.status} />
          <h1>{event.title}</h1>
          <p className="muted">
            {event.venue} · {event.category}
          </p>
        </div>
        <div className="actions">
          {manager && (
            <button className="secondary" onClick={() => onEdit(event)}>
              編輯活動
            </button>
          )}
          {(manager || user.role === "VIEWER") && (
            <button
              className="secondary"
              onClick={() => void action(() => downloadReport(id))}
            >
              <Download size={16} />
              銷售報表
            </button>
          )}
        </div>
      </header>
      <Notice error={error} />
      {success && (
        <div className="notice" role="status">
          <Check size={18} />
          {success}
        </div>
      )}
      <section className="panel event-info">
        <p>{event.description}</p>
        <div className="info-strip">
          <span>
            開賣 <strong>{date(event.sales_start)}</strong>
          </span>
          <span>
            停售 <strong>{date(event.sales_end)}</strong>
          </span>
          <span>
            鎖位保留 <strong>5 分鐘</strong>
          </span>
        </div>
      </section>
      {manager && event.status === "DRAFT" && (
        <>
          <ImportPanel
            id={id}
            onDone={() => void loadEvent().catch(setError)}
          />
          <section className="panel actions">
            <p>座位匯入完成後即可發布。發布後座位配置、票價與售票時段固定。</p>
            <button
              className="primary"
              disabled={busy}
              onClick={() =>
                void action(async () => {
                  await mutate(
                    `/events/${id}/publish`,
                    {},
                    "POST",
                    event.version,
                  );
                  await loadEvent();
                }, "活動已發布")
              }
            >
              發布活動
            </button>
            <button
              className="danger"
              disabled={busy}
              onClick={() => {
                if (confirm("確定刪除此活動？有訂單的活動無法刪除。"))
                  void action(async () => {
                    await request(`/events/${id}`, { method: "DELETE" });
                    onBack();
                  });
              }}
            >
              刪除活動
            </button>
          </section>
        </>
      )}
      <div className="booking-layout">
        <section className="panel seat-panel">
          <div className="section-label">
            <h2>選擇你的座位</h2>
            <span
              className={`sync-state ${synced ? "live" : ""}`}
              role="status"
            >
              <Radio size={15} />
              {synced ? "即時同步中" : "未同步，暫停選位"}
            </span>
          </div>
          <div className="seat-toolbar">
            <label>
              區域
              <select
                value={zone}
                onChange={(e) => {
                  setZone(e.target.value);
                  setOffset(0);
                  setSelected([]);
                }}
              >
                <option value="">所有區域</option>
                {event.zones?.map((z) => (
                  <option key={z.id} value={z.id}>
                    {z.name} · {money(z.price_twd)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              排數篩選
              <input
                value={row}
                onChange={(e) => {
                  setRow(e.target.value);
                  setOffset(0);
                }}
                placeholder="例如 A、1"
              />
            </label>
            <button className="secondary" onClick={() => void snapshot()}>
              <RefreshCw size={15} />
              重新同步
            </button>
          </div>
          <div className="stage">舞 臺 STAGE</div>
          <div className="seat-legend">
            <span>
              <i className="seat-dot available" />
              可售
            </span>
            <span>
              <i className="seat-dot selected" />
              已選
            </span>
            <span>
              <i className="seat-dot held" />
              鎖定
            </span>
            <span>
              <i className="seat-dot sold" />
              售出
            </span>
          </div>
          <div className="seat-grid">
            {visible.slice(offset, offset + 400).map((s) => (
              <button
                key={s.id}
                title={`${s.row_label} 排 ${s.seat_number} 號`}
                aria-label={`${s.row_label} 排 ${s.seat_number} 號，${s.status === "AVAILABLE" ? "可售" : s.status === "SOLD" ? "售出" : "鎖定"}`}
                aria-pressed={selected.includes(s.id)}
                className={`seat ${s.status.toLowerCase()} ${selected.includes(s.id) ? "selected" : ""}`}
                disabled={
                  !synced ||
                  !canBuy ||
                  !saleOpen ||
                  s.status !== "AVAILABLE" ||
                  busy
                }
                onClick={() => {
                  setMode("manual");
                  setSelected((old) =>
                    old.includes(s.id)
                      ? old.filter((x) => x !== s.id)
                      : old.length < 4
                        ? [...old, s.id]
                        : old,
                  );
                }}
              >
                {s.row_label}-{s.seat_number}
              </button>
            ))}
          </div>
          {!seats.length && <Empty>尚未匯入座位。</Empty>}
          {visible.length > 400 && (
            <div className="pagination">
              <button
                className="secondary"
                aria-label="上一區段"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - 400))}
              >
                <ChevronLeft />
              </button>
              <span>
                {offset + 1}–{Math.min(offset + 400, visible.length)} /{" "}
                {visible.length}
              </span>
              <button
                className="secondary"
                aria-label="下一區段"
                disabled={offset + 400 >= visible.length}
                onClick={() => setOffset(offset + 400)}
              >
                <ChevronRight />
              </button>
            </div>
          )}
        </section>
        {user.role !== "VIEWER" && (
          <aside className="panel checkout">
            <p className="eyebrow">YOUR RESERVATION</p>
            <h2>預訂摘要</h2>
            <div className="segmented">
              <button
                className={mode === "manual" ? "active" : ""}
                onClick={() => setMode("manual")}
              >
                指定座位
              </button>
              <button
                className={mode === "auto" ? "active" : ""}
                onClick={() => setMode("auto")}
              >
                系統配位
              </button>
            </div>
            {mode === "auto" ? (
              <>
                <label>
                  配位區域
                  <select
                    value={zone}
                    onChange={(e) => {
                      setZone(e.target.value);
                      setSelected([]);
                    }}
                  >
                    <option value="">請選擇區域</option>
                    {event.zones?.map((z) => (
                      <option key={z.id} value={z.id}>
                        {z.name} · {money(z.price_twd)}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  張數
                  <select
                    value={quantity}
                    onChange={(e) => setQuantity(Number(e.target.value))}
                  >
                    {[1, 2, 3, 4].map((n) => (
                      <option key={n}>{n}</option>
                    ))}
                  </select>
                </label>
                <p className="muted">同區域足額配位，不保證同排或相鄰。</p>
              </>
            ) : (
              <p className="selection-summary">
                {selected.length
                  ? `已選擇 ${selected.length} 個座位`
                  : "請從座位圖選擇，最多 4 張。"}
              </p>
            )}
            <div className="price-line">
              <span>票價小計</span>
              <strong>
                {money(
                  mode === "manual"
                    ? selectedPrice
                    : (event.zones?.find((z) => z.id === zone)?.price_twd ||
                        0) * quantity,
                )}
              </strong>
            </div>
            <p className="muted">
              另收整筆票價 5% 服務費，最終金額以訂單為準。
            </p>
            <button
              className="primary wide"
              disabled={
                busy ||
                !synced ||
                !canBuy ||
                !saleOpen ||
                (mode === "manual" ? !selected.length : !zone)
              }
              onClick={() => void book()}
            >
              {busy ? "處理中…" : "鎖定座位並建立訂單"}
            </button>
            {!saleOpen && <p className="muted">目前不在售票時段。</p>}
            <p className="footnote">
              鎖位後請於 5 分鐘內付款；未付款可整筆取消。
            </p>
          </aside>
        )}
      </div>
    </>
  );
}
