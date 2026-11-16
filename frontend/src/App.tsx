import {
  lazy,
  Suspense,
  useEffect,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import {
  CalendarDays,
  Ticket,
  Activity,
  LogOut,
  ArrowUpRight,
  Plus,
  MapPin,
  RefreshCw,
  Layers3,
} from "lucide-react";
import {
  request,
  ApiError,
  date,
  type User,
  type TicketEvent,
  type Page,
} from "./api";
import EventDetail from "./EventDetail";
import EventForm from "./EventForm";
import Orders from "./Orders";
const Admin = lazy(() => import("./Admin"));

export function Notice({ error }: { error: unknown }) {
  return error ? (
    <div className="notice error" role="alert">
      {error instanceof Error ? error.message : String(error)}
      {error instanceof ApiError && error.requestId && (
        <small>追蹤編號：{error.requestId}</small>
      )}
    </div>
  ) : null;
}
export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="empty">
      <Layers3 size={30} />
      <p>{children}</p>
    </div>
  );
}
export function Status({ value }: { value: string }) {
  const labels: Record<string, string> = {
    PUBLISHED: "已發布",
    DRAFT: "草稿",
    PENDING_PAYMENT: "待付款",
    PAID: "已付款",
    CANCELED: "已取消",
    EXPIRED: "已逾時",
    RUNNING: "執行中",
    QUEUED: "排隊中",
    SUCCEEDED: "完成",
    FAILED: "失敗",
    FAILED_VALIDATION: "驗證失敗",
    FAILED_SYSTEM: "系統錯誤",
    REVERSAL_PENDING: "沖正中",
    REVERSED: "已沖正",
  };
  return (
    <span className={`badge ${value.toLowerCase()}`}>
      {labels[value] || value}
    </span>
  );
}
function Auth({ onLogin }: { onLogin: (user: User) => void }) {
  const [register, setRegister] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>();
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const f = new FormData(e.currentTarget),
      body = { email: f.get("email"), password: f.get("password") };
    try {
      if (register)
        await request("/auth/register", {
          method: "POST",
          body: JSON.stringify(body),
        });
      const data = await request<{ token: string; user: User }>("/auth/login", {
        method: "POST",
        body: JSON.stringify(body),
      });
      sessionStorage.setItem("ticket-token", data.token);
      onLogin(data.user);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="auth-layout">
      <div className="auth-art">
        <div className="wordmark">
          <Ticket />
          序場<span>TICKET PLATFORM</span>
        </div>
        <div>
          <p className="eyebrow">LET THE MOMENT BEGIN</p>
          <h1>
            每個好座位，
            <br />
            都是故事的開始。
          </h1>
          <p>
            從第一聲倒數，到最後一次安可。
            <br />
            讓期待安心落座。
          </p>
        </div>
        <small>即時座位 · 安心預訂 · 透明交易</small>
      </div>
      <main className="auth-main">
        <div className="auth-card">
          <p className="eyebrow">WELCOME TO THE SHOW</p>
          <h2>{register ? "建立你的帳號" : "歡迎回到序場"}</h2>
          <p className="muted">登入後，探索活動、預訂座位與查看訂單。</p>
          <Notice error={error} />
          <form onSubmit={submit}>
            <label>
              電子郵件
              <input
                name="email"
                type="email"
                autoComplete="email"
                required
                placeholder="you@example.com"
              />
            </label>
            <label>
              密碼
              <input
                name="password"
                type="password"
                minLength={8}
                autoComplete={register ? "new-password" : "current-password"}
                required
                placeholder="至少 8 個字元"
              />
            </label>
            <button className="primary wide" disabled={busy}>
              {busy ? "處理中…" : register ? "註冊並登入" : "登入平台"}
              <ArrowUpRight size={18} />
            </button>
          </form>
          <button
            className="link"
            onClick={() => {
              setRegister(!register);
              setError(null);
            }}
          >
            {register ? "已有帳號？返回登入" : "還沒有帳號？建立帳號"}
          </button>
          <p className="auth-note">
            測試帳號請參閱專案 README。所有時間以臺北時區顯示。
          </p>
        </div>
      </main>
    </div>
  );
}
export function EventList({
  user,
  onOpen,
  onCreate,
}: {
  user: User;
  onOpen: (id: string) => void;
  onCreate: () => void;
}) {
  const [events, setEvents] = useState<TicketEvent[]>([]),
    [error, setError] = useState<unknown>(),
    [loading, setLoading] = useState(true),
    [cursor, setCursor] = useState<string | null>(null),
    [snapshot, setSnapshot] = useState<string>(),
    [query, setQuery] = useState({
      category: "",
      venue: "",
      status: "",
      sort: "-created_at",
    });
  async function load(next = false) {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams(
        Object.entries(query).filter(([, v]) => v),
      );
      if (next && cursor) {
        params.set("cursor", cursor);
        if (snapshot) params.set("snapshot_id", snapshot);
      }
      const data = await request<Page<TicketEvent>>(`/events?${params}`);
      setEvents((old) => (next ? [...old, ...data.items] : data.items));
      setCursor(data.next_cursor);
      setSnapshot(data.snapshot_id);
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    void load();
  }, []);
  return (
    <>
      <header className="page-heading">
        <div>
          <p className="eyebrow">FIND YOUR NEXT MOMENT</p>
          <h1>值得親臨的現場</h1>
          <p className="muted">探索每一場期待，選擇屬於你的座位。</p>
        </div>
        {user.role !== "VIEWER" && (
          <button className="primary" onClick={onCreate}>
            <Plus size={18} />
            建立活動
          </button>
        )}
      </header>
      <section className="catalog-banner">
        <div>
          <span className="eyebrow">YOUR SEAT. YOUR STORY.</span>
          <h2>把喜歡，留給現場。</h2>
          <p>每人每場最多 4 張，鎖位保留 5 分鐘。</p>
        </div>
        <div className="ticket-illustration">
          <Ticket size={92} strokeWidth={1} />
          <span>
            ADMIT ONE
            <br />
            <strong>LIVE / TOGETHER</strong>
          </span>
        </div>
      </section>
      <form
        className="filters"
        onSubmit={(e) => {
          e.preventDefault();
          void load();
        }}
      >
        <input
          aria-label="活動分類"
          placeholder="所有分類"
          value={query.category}
          onChange={(e) => setQuery({ ...query, category: e.target.value })}
        />
        <input
          aria-label="場地篩選"
          placeholder="搜尋場地"
          value={query.venue}
          onChange={(e) => setQuery({ ...query, venue: e.target.value })}
        />
        <select
          aria-label="活動狀態"
          value={query.status}
          onChange={(e) => setQuery({ ...query, status: e.target.value })}
        >
          <option value="">全部狀態</option>
          <option value="PUBLISHED">已發布</option>
          {user.role !== "VIEWER" && <option value="DRAFT">草稿</option>}
        </select>
        <select
          aria-label="排序"
          value={query.sort}
          onChange={(e) => setQuery({ ...query, sort: e.target.value })}
        >
          <option value="-created_at">最新建立</option>
          <option value="sales_start">開賣時間</option>
          <option value="title">活動名稱</option>
        </select>
        <button className="secondary" disabled={loading}>
          <RefreshCw size={16} />
          更新清單
        </button>
      </form>
      <Notice error={error} />
      <div className="section-label">
        <h2>活動一覽</h2>
        <span>{events.length} 場活動</span>
      </div>
      <div className="event-grid">
        {events.map((event, i) => (
          <button
            className="event-card"
            key={event.id}
            onClick={() => onOpen(event.id)}
          >
            <div className={`event-cover cover-${i % 4}`}>
              <span>{event.category || "LIVE EVENT"}</span>
              <div className="cover-type">{event.title.slice(0, 12)}</div>
              <span className="cover-arrow">
                <ArrowUpRight />
              </span>
            </div>
            <div className="event-body">
              <Status value={event.status} />
              <h3>{event.title}</h3>
              <p>
                <MapPin size={15} />
                {event.venue}
              </p>
              <p>
                <CalendarDays size={15} />
                {date(event.sales_start)} 開賣
              </p>
            </div>
          </button>
        ))}
      </div>
      {loading && (
        <p role="status" className="muted">
          正在載入活動…
        </p>
      )}
      {!loading && !events.length && !error && (
        <Empty>目前沒有符合條件的活動。</Empty>
      )}
      {cursor && (
        <button
          className="secondary load-more"
          disabled={loading}
          onClick={() => void load(true)}
        >
          載入更多活動
        </button>
      )}
      <p className="footnote">清單快照保留 10 分鐘；重新整理可取得最新活動。</p>
    </>
  );
}
export default function App() {
  const [user, setUser] = useState<User | null>(null),
    [checking, setChecking] = useState(
      () => !!sessionStorage.getItem("ticket-token"),
    ),
    [view, setView] = useState("events"),
    [eventId, setEventId] = useState<string | null>(null),
    [editing, setEditing] = useState<TicketEvent>(),
    [orderId, setOrderId] = useState<string | null>(null),
    [globalError, setGlobalError] = useState<unknown>();
  useEffect(() => {
    const expire = () => {
      sessionStorage.removeItem("ticket-token");
      setUser(null);
      setChecking(false);
    };
    window.addEventListener("session-expired", expire);
    if (sessionStorage.getItem("ticket-token"))
      request<User>("/auth/me")
        .then(setUser)
        .catch(expire)
        .finally(() => setChecking(false));
    return () => window.removeEventListener("session-expired", expire);
  }, []);
  if (checking)
    return (
      <div className="loading-screen" role="status">
        正在確認登入狀態…
      </div>
    );
  if (!user) return <Auth onLogin={setUser} />;
  async function logout() {
    try {
      await request("/auth/logout", { method: "POST" });
      sessionStorage.removeItem("ticket-token");
      setUser(null);
    } catch (error) {
      setGlobalError(error);
    }
  }
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="wordmark">
          <Ticket />
          序場<span>TICKET PLATFORM</span>
        </div>
        <p className="nav-caption">你的現場指南</p>
        <nav>
          <button
            className={view === "events" ? "active" : ""}
            onClick={() => {
              setView("events");
              setEventId(null);
            }}
          >
            <CalendarDays />
            探索活動
          </button>
          {user.role !== "VIEWER" && (
            <button
              className={view === "orders" ? "active" : ""}
              onClick={() => {
                setOrderId(null);
                setView("orders");
              }}
            >
              <Ticket />
              我的訂單
            </button>
          )}
          {user.role === "ADMIN" && (
            <button
              className={view === "admin" ? "active" : ""}
              onClick={() => setView("admin")}
            >
              <Activity />
              營運監控
            </button>
          )}
        </nav>
        <div className="sidebar-bottom">
          <div className="avatar">{user.email[0].toUpperCase()}</div>
          <div>
            <strong>
              {
                { ADMIN: "管理者", USER: "購票會員", VIEWER: "檢視者" }[
                  user.role
                ]
              }
            </strong>
            <small>{user.email}</small>
          </div>
          <button aria-label="登出" onClick={() => void logout()}>
            <LogOut size={18} />
          </button>
        </div>
      </aside>
      <main className="workspace">
        <div className="topbar">
          <span>活動搶票與即時監控平台</span>
          <span className="timezone">Asia/Taipei · TWD</span>
        </div>
        <div className="page-content">
          <Notice error={globalError} />
          {view === "events" && !eventId && (
            <EventList
              user={user}
              onOpen={setEventId}
              onCreate={() => {
                setEditing(undefined);
                setView("create");
              }}
            />
          )}
          {view === "events" && eventId && (
            <EventDetail
              key={eventId}
              id={eventId}
              user={user}
              onBack={() => setEventId(null)}
              onEdit={(event) => {
                setEditing(event);
                setView("create");
              }}
              onOrder={(id) => {
                setOrderId(id);
                setView("orders");
              }}
            />
          )}
          {view === "create" && user.role !== "VIEWER" && (
            <EventForm
              event={editing}
              onCancel={() => setView("events")}
              onDone={(id) => {
                setEventId(id);
                setView("events");
              }}
            />
          )}
          {view === "orders" && user.role !== "VIEWER" && (
            <Orders
              id={orderId}
              onOpen={setOrderId}
              onBack={() => setOrderId(null)}
            />
          )}{" "}
          {view === "admin" && user.role === "ADMIN" && (
            <Suspense fallback={<p role="status">載入監控…</p>}>
              <Admin />
            </Suspense>
          )}
        </div>
      </main>
    </div>
  );
}
