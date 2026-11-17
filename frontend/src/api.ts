export type Role = "ADMIN" | "USER" | "VIEWER";
export interface User {
  id: string;
  email: string;
  role: Role;
  is_active?: boolean;
}
export interface Zone {
  id: string;
  code: string;
  name: string;
  price_twd: number;
}
export interface TicketEvent {
  id: string;
  owner_id: string;
  title: string;
  description: string;
  category: string;
  venue: string;
  status: string;
  version: number;
  sales_start: string;
  sales_end: string;
  zones: Zone[];
}
export interface Seat {
  id: string;
  zone_id: string;
  row_label: string;
  seat_number: string;
  status: string;
  held_until?: string;
}
export interface Order {
  id: string;
  event_id: string;
  status: string;
  ticket_count: number;
  subtotal_twd: number;
  fee_twd: number;
  total_twd: number;
  expires_at: string;
  created_at: string;
  items?: {
    seat_id: string;
    zone_code_snapshot: string;
    row_label_snapshot: string;
    seat_number_snapshot: string;
    unit_price_twd: number;
  }[];
  payments?: { id: string; status: string }[];
}
export interface Page<T> {
  items: T[];
  next_cursor: string | null;
  snapshot_id?: string;
}
export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public requestId?: string,
    public status?: number,
  ) {
    super(message);
  }
}
export async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  const token = sessionStorage.getItem("ticket-token");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData))
    headers.set("Content-Type", "application/json");
  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, { ...init, headers });
  } catch {
    throw new ApiError(
      "NETWORK_ERROR",
      "無法連線，請確認網路後重試；同一操作會保留原重試識別碼。",
    );
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    if (response.status === 401 && !path.startsWith("/auth/"))
      window.dispatchEvent(new Event("session-expired"));
    throw new ApiError(
      body.code || "REQUEST_FAILED",
      body.message || `操作失敗（${response.status}）`,
      body.request_id,
      response.status,
    );
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}
export async function mutate<T>(
  path: string,
  body: unknown,
  method = "POST",
  version?: number,
): Promise<T> {
  const content = JSON.stringify(body),
    slot = `ticket-action:${path}:${content}`;
  let key = sessionStorage.getItem(slot);
  if (!key) {
    key = crypto.randomUUID();
    sessionStorage.setItem(slot, key);
  }
  const headers: Record<string, string> = { "Idempotency-Key": key };
  if (version !== undefined) headers["If-Match"] = String(version);
  try {
    const result = await request<T>(path, { method, body: content, headers });
    sessionStorage.removeItem(slot);
    return result;
  } catch (error) {
    if (
      error instanceof ApiError &&
      error.status &&
      error.status < 500 &&
      error.code !== "IDEMPOTENCY_IN_PROGRESS"
    )
      sessionStorage.removeItem(slot);
    throw error;
  }
}
export async function downloadReport(id: string) {
  const token = sessionStorage.getItem("ticket-token");
  const response = await fetch(`/api/v1/events/${id}/report`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    if (response.status === 401)
      window.dispatchEvent(new Event("session-expired"));
    throw new ApiError(
      data.code || "EXPORT_FAILED",
      data.message || "報表下載失敗",
      data.request_id,
      response.status,
    );
  }
  const url = URL.createObjectURL(await response.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = `sales-${id}.xlsx`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export const money = (value: number) =>
  new Intl.NumberFormat("zh-TW", {
    style: "currency",
    currency: "TWD",
    maximumFractionDigits: 0,
  }).format(value);
export const date = (value: string) =>
  value
    ? new Intl.DateTimeFormat("zh-TW", {
        timeZone: "Asia/Taipei",
        dateStyle: "medium",
        timeStyle: "short",
      }).format(new Date(value))
    : "—";
