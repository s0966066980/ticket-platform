import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  Activity,
  Clock3,
  Radio,
  TriangleAlert,
  Search,
  RefreshCw,
} from "lucide-react";
import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
} from "recharts";
import { request, mutate, date, type User, type Page } from "./api";
import { Notice, Empty } from "./App";
interface Metric {
  bucket_at: string;
  rps: number;
  p50_ms: number;
  p95_ms: number;
  p99_ms: number;
  error_rate: number;
  connections: number;
}
interface Alert {
  id: string;
  rule_id: string;
  status: string;
  triggered_at: string;
  details: unknown;
}
interface Log {
  service: string;
  level: string;
  code: string;
  observed_at: string;
  context: unknown;
}
function UserRow({ user, onSave }: { user: User; onSave: () => void }) {
  const [role, setRole] = useState(user.role),
    [active, setActive] = useState(user.is_active !== false),
    [error, setError] = useState<unknown>(),
    [busy, setBusy] = useState(false);
  async function save() {
    setBusy(true);
    try {
      await mutate(
        `/admin/users/${user.id}`,
        { role, is_active: active },
        "PATCH",
      );
      onSave();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <tr>
      <td>
        <strong>{user.email}</strong>
        <small className="mono">{user.id}</small>
        <Notice error={error} />
      </td>
      <td>
        <select
          aria-label={`${user.email} 角色`}
          value={role}
          onChange={(e) => setRole(e.target.value as User["role"])}
        >
          <option value="ADMIN">Admin</option>
          <option value="USER">User</option>
          <option value="VIEWER">Viewer</option>
        </select>
      </td>
      <td>
        <label className="checkbox">
          <input
            aria-label={`${user.email} 啟用狀態`}
            type="checkbox"
            checked={active}
            onChange={(e) => setActive(e.target.checked)}
          />
          啟用
        </label>
      </td>
      <td>
        <button
          className="secondary"
          disabled={busy}
          onClick={() => void save()}
        >
          儲存
        </button>
      </td>
    </tr>
  );
}
export default function Admin() {
  const [tab, setTab] = useState("metrics"),
    [metrics, setMetrics] = useState<Metric[]>([]),
    [complete, setComplete] = useState(true),
    [alerts, setAlerts] = useState<Alert[]>([]),
    [users, setUsers] = useState<User[]>([]),
    [userCursor, setUserCursor] = useState<string | null>(null),
    [logs, setLogs] = useState<Log[]>([]),
    [searched, setSearched] = useState(false),
    [error, setError] = useState<unknown>(),
    [loading, setLoading] = useState(false);
  const inFlight = useRef<Record<string, boolean>>({});
  async function refresh(next = false) {
    const scope = tab;
    if (inFlight.current[scope]) return;
    inFlight.current[scope] = true;
    try {
      if (tab === "metrics") {
        const [data, a] = await Promise.all([
          request<{ items: Metric[]; data_complete?: boolean }>(
            "/admin/metrics",
          ),
          request<Page<Alert>>("/admin/alerts"),
        ]);
        setMetrics(data.items);
        setComplete(data.data_complete !== false);
        setAlerts(a.items);
      } else if (tab === "users") {
        const result = await request<Page<User>>(
          `/admin/users${next && userCursor ? `?cursor=${encodeURIComponent(userCursor)}` : ""}`,
        );
        setUsers((old) => (next ? [...old, ...result.items] : result.items));
        setUserCursor(result.next_cursor);
      }
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      inFlight.current[scope] = false;
    }
  }
  useEffect(() => {
    void refresh();
    if (tab !== "metrics") return;
    const timer = setInterval(() => void refresh(), 1000);
    return () => clearInterval(timer);
  }, [tab]);
  async function search(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const id = new FormData(e.currentTarget).get("request_id");
      const data = await request<Page<Log>>(
        `/admin/logs/${encodeURIComponent(String(id))}`,
      );
      setLogs(data.items);
      setSearched(true);
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }
  const latest = metrics.at(-1);
  const cards = [
    {
      title: "每秒請求",
      value: latest ? latest.rps.toFixed(1) : "—",
      unit: "RPS",
      icon: Activity,
    },
    {
      title: "p95 處理延遲",
      value: latest ? latest.p95_ms.toFixed(0) : "—",
      unit: "ms",
      icon: Clock3,
    },
    {
      title: "系統錯誤率",
      value: latest ? `${(latest.error_rate * 100).toFixed(2)}` : "—",
      unit: "%",
      icon: TriangleAlert,
    },
    {
      title: "即時連線",
      value: latest ? String(latest.connections) : "—",
      unit: "connections",
      icon: Radio,
    },
  ];
  return (
    <>
      <header className="page-heading">
        <div>
          <p className="eyebrow">OPERATIONS / LIVE</p>
          <h1>現場，由你掌握。</h1>
          <p className="muted">每秒更新，彙總全部 API 副本最近 60 秒的觀測。</p>
        </div>
        <span className="badge published">
          <span className="live-dot" />
          營運中心
        </span>
      </header>
      <div className="tabs" role="tablist" aria-label="管理功能">
        {[
          ["metrics", "系統監控"],
          ["users", "使用者管理"],
          ["logs", "請求追蹤"],
        ].map(([value, label]) => (
          <button
            key={value}
            role="tab"
            aria-selected={tab === value}
            onClick={() => {
              setTab(value);
              setError(null);
            }}
          >
            {label}
          </button>
        ))}
      </div>
      <Notice error={error} />
      {tab === "metrics" && (
        <>
          {!complete && (
            <div className="notice error" role="status">
              部分指標來源尚未回報，以下資料可能不完整；缺漏不代表零流量。
            </div>
          )}
          <div className="stat-grid">
            {cards.map((card) => (
              <section className="panel stat" key={card.title}>
                <div>
                  <span>{card.title}</span>
                  <card.icon size={18} />
                </div>
                <strong>
                  {card.value}
                  <small>{card.unit}</small>
                </strong>
              </section>
            ))}
          </div>
          <section className="panel chart-panel">
            <div className="section-label">
              <h2>API 處理延遲</h2>
              <span>p50 / p95 / p99 · ms</span>
            </div>
            {metrics.length ? (
              <div className="chart">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={metrics}>
                    <CartesianGrid vertical={false} stroke="#e5e9e6" />
                    <XAxis
                      dataKey="bucket_at"
                      tickFormatter={(v) =>
                        new Date(v).toLocaleTimeString("zh-TW", {
                          timeZone: "Asia/Taipei",
                          hour12: false,
                        })
                      }
                      minTickGap={50}
                    />
                    <YAxis />
                    <Tooltip labelFormatter={(v) => date(String(v))} />
                    <Line
                      type="monotone"
                      dataKey="p50_ms"
                      name="p50"
                      stroke="#8eaa99"
                      dot={false}
                      isAnimationActive={false}
                    />
                    <Line
                      type="monotone"
                      dataKey="p95_ms"
                      name="p95"
                      stroke="#22634d"
                      dot={false}
                      isAnimationActive={false}
                    />
                    <Line
                      type="monotone"
                      dataKey="p99_ms"
                      name="p99"
                      stroke="#bd7548"
                      dot={false}
                      isAnimationActive={false}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            ) : (
              <Empty>等待第一筆系統觀測；目前沒有可顯示的指標。</Empty>
            )}
            <p className="footnote">
              顯示 API 處理時間；完整使用者延遲以客戶端壓測為準。
            </p>
          </section>
          <section className="panel">
            <div className="section-label">
              <h2>系統告警</h2>
              <button
                className="icon-button"
                aria-label="更新告警"
                onClick={() => void refresh()}
              >
                <RefreshCw size={17} />
              </button>
            </div>
            {alerts.length ? (
              alerts.map((a) => (
                <div className="alert-row" key={a.id}>
                  <TriangleAlert size={19} />
                  <div>
                    <strong>{a.rule_id}</strong>
                    <small>
                      {date(a.triggered_at)} · {a.status}
                    </small>
                    <pre>{JSON.stringify(a.details, null, 2)}</pre>
                  </div>
                </div>
              ))
            ) : (
              <p className="muted">目前沒有告警紀錄。</p>
            )}
          </section>
        </>
      )}
      {tab === "users" && (
        <section className="panel">
          <h2>帳號與權限</h2>
          <p className="muted">
            停用或修改角色後，原登入憑證將失效；已受理的匯入工作仍依受理時授權完成。
          </p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>使用者</th>
                  <th>角色</th>
                  <th>狀態</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => (
                  <UserRow key={u.id} user={u} onSave={() => void refresh()} />
                ))}
              </tbody>
            </table>
          </div>
          {userCursor && (
            <button className="secondary" onClick={() => void refresh(true)}>
              載入更多使用者
            </button>
          )}
        </section>
      )}
      {tab === "logs" && (
        <section className="panel">
          <h2>追蹤一個請求</h2>
          <p className="muted">
            以 request_id 查看 API、背景工作與金流的關聯紀錄。
          </p>
          <form className="search-form" onSubmit={search}>
            <label>
              Request ID
              <input
                name="request_id"
                required
                placeholder="輸入錯誤訊息或回應中的追蹤編號"
              />
            </label>
            <button className="primary" disabled={loading}>
              <Search size={17} />
              {loading ? "查詢中…" : "查詢紀錄"}
            </button>
          </form>
          {logs.map((log, i) => (
            <article className="log-entry" key={i}>
              <span className="badge">{log.service}</span>
              <strong>{log.code}</strong>
              <small>
                {log.level} · {date(log.observed_at)}
              </small>
              <pre>{JSON.stringify(log.context, null, 2)}</pre>
            </article>
          ))}
          {searched && !logs.length && <Empty>沒有找到此請求的紀錄。</Empty>}
        </section>
      )}
    </>
  );
}
