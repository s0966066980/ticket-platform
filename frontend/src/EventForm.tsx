import { useState, type FormEvent } from "react";
import { ArrowLeft, Plus, Trash2 } from "lucide-react";
import { mutate, type TicketEvent } from "./api";
import { Notice } from "./App";
const localTime = (value?: string) =>
  value
    ? new Date(Date.parse(value) + 8 * 3600000).toISOString().slice(0, 16)
    : "";
export default function EventForm({
  event,
  onDone,
  onCancel,
}: {
  event?: TicketEvent;
  onDone: (id: string) => void;
  onCancel: () => void;
}) {
  const [error, setError] = useState<unknown>(),
    [busy, setBusy] = useState(false),
    [zones, setZones] = useState(
      event?.zones?.map((z) => ({
        code: z.code,
        name: z.name,
        price_twd: z.price_twd,
      })) || [{ code: "A", name: "A 區", price_twd: 1000 }],
    );
  const frozen = event?.status === "PUBLISHED";
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const f = new FormData(e.currentTarget);
    const body: Record<string, unknown> = {
      title: f.get("title"),
      description: f.get("description"),
      category: f.get("category"),
      venue: f.get("venue"),
    };
    if (!frozen) {
      body.sales_start = new Date(
        `${f.get("sales_start")}:00+08:00`,
      ).toISOString();
      body.sales_end = new Date(`${f.get("sales_end")}:00+08:00`).toISOString();
      body.zones = zones;
    }
    try {
      const saved = await mutate<TicketEvent>(
        event ? `/events/${event.id}` : "/events",
        body,
        event ? "PATCH" : "POST",
        event?.version,
      );
      onDone(saved.id);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <button className="link back" onClick={onCancel}>
        <ArrowLeft size={16} />
        返回
      </button>
      <header className="page-heading">
        <div>
          <p className="eyebrow">CREATE A MOMENT</p>
          <h1>{event ? "編輯活動" : "建立新活動"}</h1>
          <p className="muted">先建立草稿、匯入座位，確認後再發布。</p>
        </div>
      </header>
      <section className="panel editor">
        <Notice error={error} />
        <form onSubmit={submit}>
          <div className="form-grid">
            <label className="span-2">
              活動名稱
              <input
                name="title"
                defaultValue={event?.title}
                required
                maxLength={200}
              />
            </label>
            <label>
              分類
              <input
                name="category"
                defaultValue={event?.category}
                required
                placeholder="音樂、劇場、展演"
              />
            </label>
            <label>
              場地
              <input name="venue" defaultValue={event?.venue} required />
            </label>
            <label className="span-2">
              活動介紹
              <textarea
                name="description"
                rows={5}
                defaultValue={event?.description}
                required
              />
            </label>
            <label>
              開賣時間（臺北）
              <input
                name="sales_start"
                type="datetime-local"
                defaultValue={localTime(event?.sales_start)}
                required
                disabled={frozen}
              />
            </label>
            <label>
              停售時間（臺北）
              <input
                name="sales_end"
                type="datetime-local"
                defaultValue={localTime(event?.sales_end)}
                required
                disabled={frozen}
              />
            </label>
          </div>
          <div className="section-label">
            <h2>區域與票價</h2>
            {!frozen && (
              <button
                type="button"
                className="secondary"
                onClick={() =>
                  setZones([...zones, { code: "", name: "", price_twd: 0 }])
                }
              >
                <Plus size={16} />
                新增區域
              </button>
            )}
          </div>
          {zones.map((z, i) => (
            <div className="zone-editor" key={i}>
              <label>
                代碼
                <input
                  value={z.code}
                  required
                  disabled={frozen}
                  onChange={(e) =>
                    setZones(
                      zones.map((x, j) =>
                        i === j ? { ...x, code: e.target.value } : x,
                      ),
                    )
                  }
                />
              </label>
              <label>
                區域名稱
                <input
                  value={z.name}
                  required
                  disabled={frozen}
                  onChange={(e) =>
                    setZones(
                      zones.map((x, j) =>
                        i === j ? { ...x, name: e.target.value } : x,
                      ),
                    )
                  }
                />
              </label>
              <label>
                票價（TWD）
                <input
                  type="number"
                  min={0}
                  step={1}
                  value={z.price_twd}
                  required
                  disabled={frozen}
                  onChange={(e) =>
                    setZones(
                      zones.map((x, j) =>
                        i === j
                          ? { ...x, price_twd: Number(e.target.value) }
                          : x,
                      ),
                    )
                  }
                />
              </label>
              {!frozen && (
                <button
                  type="button"
                  className="icon-button"
                  aria-label={`移除區域 ${i + 1}`}
                  disabled={zones.length === 1}
                  onClick={() => setZones(zones.filter((_, j) => i !== j))}
                >
                  <Trash2 size={18} />
                </button>
              )}
            </div>
          ))}
          {frozen && (
            <p className="notice">
              已發布活動的座位、區域、票價與售票時段已固定。
            </p>
          )}
          <div className="actions editor-actions">
            <button className="primary" disabled={busy}>
              {busy ? "儲存中…" : event ? "儲存修改" : "建立活動草稿"}
            </button>
            <button type="button" className="secondary" onClick={onCancel}>
              取消
            </button>
          </div>
        </form>
      </section>
    </>
  );
}
