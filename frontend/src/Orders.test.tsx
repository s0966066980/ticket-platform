import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import Orders from "./Orders";
it("付款回應遺失後重試沿用同一個 Idempotency-Key，金額以訂單回應為準", async () => {
  const keys: string[] = [];
  const order = {
    id: "o",
    event_id: "e",
    status: "PENDING_PAYMENT",
    ticket_count: 1,
    subtotal_twd: 1000,
    fee_twd: 50,
    total_twd: 1050,
    expires_at: new Date(Date.now() + 300000).toISOString(),
    created_at: "2026-01-01T00:00:00Z",
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("/payments")) {
        keys.push(new Headers(init?.headers).get("Idempotency-Key") || "");
        if (keys.length === 1) throw new TypeError("connection lost");
        return new Response(
          JSON.stringify({ id: "payment", status: "PENDING" }),
          { status: 201 },
        );
      }
      return new Response(JSON.stringify(order), { status: 200 });
    }),
  );
  render(<Orders id="o" onOpen={() => {}} onBack={() => {}} />);
  expect(await screen.findByText(/1,050/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "模擬付款成功" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("無法連線");
  await userEvent.click(screen.getByRole("button", { name: "模擬付款成功" }));
  expect(
    await screen.findByText("付款已送出，正在等待金流回呼確認。"),
  ).toBeInTheDocument();
  expect(keys).toHaveLength(2);
  expect(keys[0]).not.toBe("");
  expect(keys[1]).toBe(keys[0]);
});
