import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi } from "vitest";
import App from "./App";
describe("購票平台公開介面", () => {
  it("登入後讓 Viewer 瀏覽活動，但不提供建立活動操作", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL) =>
          new Response(
            JSON.stringify(
              String(input).includes("/auth/login")
                ? {
                    token: "valid-token",
                    user: {
                      id: "u",
                      email: "viewer@example.test",
                      role: "VIEWER",
                    },
                  }
                : {
                    items: [
                      {
                        id: "e",
                        title: "夏日音樂會",
                        description: "一起聽見夏天",
                        venue: "臺北流行音樂中心",
                        category: "音樂",
                        status: "PUBLISHED",
                        version: 1,
                        sales_start: "2026-01-01T00:00:00Z",
                        sales_end: "2027-01-01T00:00:00Z",
                      },
                    ],
                    next_cursor: null,
                  },
            ),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
      ),
    );
    render(<App />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("電子郵件"), "viewer@example.test");
    await user.type(screen.getByLabelText("密碼"), "password123");
    await user.click(screen.getByRole("button", { name: "登入平台" }));
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "夏日音樂會" }),
      ).toBeInTheDocument(),
    );
    expect(
      screen.queryByRole("button", { name: "建立活動" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("檢視者")).toBeInTheDocument();
  });
  it("即時連線尚未確認時不允許選位", async () => {
    sessionStorage.setItem("ticket-token", "token");
    vi.stubGlobal(
      "WebSocket",
      class {
        onopen = null;
        onmessage = null;
        onclose = null;
        onerror = null;
        send() {}
        close() {}
      },
    );
    const event = {
      id: "e",
      owner_id: "u",
      title: "春季現場",
      description: "音樂演出",
      category: "音樂",
      venue: "臺北",
      status: "PUBLISHED",
      version: 1,
      sales_start: "2026-01-01T00:00:00Z",
      sales_end: "2027-01-01T00:00:00Z",
      zones: [{ id: "z", code: "A", name: "A區", price_twd: 1000 }],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        const body = path.endsWith("/auth/me")
          ? { id: "u", email: "user@test.com", role: "USER" }
          : path.includes("/seats")
            ? {
                version: 1,
                items: [
                  {
                    id: "s",
                    zone_id: "z",
                    row_label: "1",
                    seat_number: "1",
                    status: "AVAILABLE",
                  },
                ],
              }
            : path.includes("/events?")
              ? { items: [event], next_cursor: null }
              : event;
        return new Response(JSON.stringify(body), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );
    render(<App />);
    await userEvent.click(
      await screen.findByRole("button", { name: /春季現場/ }),
    );
    expect(await screen.findByText("未同步，暫停選位")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "1 排 1 號，可售" }),
    ).toBeDisabled();
  });
  it("登出未獲伺服器確認時保留登入並顯示可重試錯誤", async () => {
    sessionStorage.setItem("ticket-token", "token");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).endsWith("/logout")) throw new TypeError("offline");
        return new Response(
          JSON.stringify(
            String(input).endsWith("/auth/me")
              ? { id: "u", email: "user@test.com", role: "USER" }
              : { items: [], next_cursor: null },
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }),
    );
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "登出" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("無法連線");
    expect(
      screen.getByRole("button", { name: "我的訂單" }),
    ).toBeInTheDocument();
  });
});
