import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi } from "vitest";
import App from "./App";

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

describe("即時座位推送", () => {
  it("收到批次重新同步訊號時重新取得座位快照並恢復同步", async () => {
    sessionStorage.setItem("ticket-token", "token");
    const sockets: FakeSocket[] = [];
    class FakeSocket {
      static OPEN = 1;
      readyState = 1;
      onopen: null | (() => void) = null;
      onmessage: null | ((e: { data: string }) => void) = null;
      onclose: null | (() => void) = null;
      onerror: null | (() => void) = null;
      constructor() {
        sockets.push(this);
        setTimeout(() => this.onopen?.(), 0);
      }
      send(data: string) {
        if (JSON.parse(data).type === "auth")
          setTimeout(
            () =>
              this.onmessage?.({
                data: JSON.stringify({ type: "auth_ok", version: 1 }),
              }),
            0,
          );
      }
      close() {}
    }
    vi.stubGlobal("WebSocket", FakeSocket);
    let seatVersion = 1;
    let seatStatus = "AVAILABLE";
    const seatCalls: number[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        let body: unknown = event;
        if (path.endsWith("/auth/me"))
          body = { id: "u", email: "user@test.com", role: "USER" };
        else if (path.includes("/seats")) {
          seatCalls.push(seatVersion);
          body = {
            version: seatVersion,
            items: [
              {
                id: "s",
                zone_id: "z",
                row_label: "1",
                seat_number: "1",
                status: seatStatus,
              },
            ],
          };
        } else if (path.includes("/events?"))
          body = { items: [event], next_cursor: null };
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
    expect(await screen.findByText("即時同步中")).toBeInTheDocument();
    const calls = seatCalls.length;
    // A seat import finished: the feed only says "resync", with no per-seat patch.
    seatVersion = 2;
    seatStatus = "SOLD";
    sockets[0].onmessage?.({
      data: JSON.stringify({
        type: "seat_changed",
        event_id: "e",
        version: 2,
        changes: [],
        resync_required: true,
      }),
    });
    await waitFor(() => expect(seatCalls.length).toBeGreaterThan(calls));
    expect(await screen.findByText("即時同步中")).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: /1 排 1 號，售出/ }),
    ).toBeDisabled();
  });
});
