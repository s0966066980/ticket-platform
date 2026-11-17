"""Ordered browser flow through the ticketing platform.

The tests in this module run in file order and share one activity created by
the first test (``FLOW``). Each test still gets fresh browser contexts and
logs in on its own; a test whose prerequisite step failed fails explicitly
rather than being skipped. The suite is repeatable against a stack that
already holds data because the activity title is unique per run.
"""
from __future__ import annotations

import re
import time
import uuid
from pathlib import Path

import openpyxl
import pytest
from playwright.sync_api import Page, expect

from helpers import (
    ADMIN,
    USER,
    VIEWER,
    Flow,
    Zone,
    create_draft_activity,
    fee,
    login,
    open_event,
    open_order,
    order_id_on_page,
    seat,
    seats_in_state,
    twd,
    upload_seats,
    wait_synced,
)

REPO = Path(__file__).resolve().parent.parent
SEATS_CSV = REPO / "sample_data" / "seats.csv"
INVALID_SEATS_CSV = REPO / "sample_data" / "invalid_seats.csv"

# Prices chosen so the 5% fee needs rounding: 1234 -> 61.7 -> 62; 2468 -> 123.4 -> 123.
ZONES = [Zone("A", "A 區", 1234), Zone("B", "B 區", 2345)]
SEAT_LABELS = ["R01 排 1 號", "R01 排 2 號", "R01 排 3 號", "R01 排 4 號", "R02 排 1 號", "R02 排 2 號"]

FLOW = Flow()


def require(condition: object, step: str) -> None:
    if not condition:
        pytest.fail(f"prerequisite step did not complete: {step}")


def test_a_user_creates_imports_and_publishes_activity(page: Page):
    request_ids: list[str] = []

    def capture(response):
        if "/api/v1/events" in response.url and response.request.method == "POST":
            value = response.headers.get("x-request-id")
            if value:
                request_ids.append(value)

    page.on("response", capture)
    login(page, USER)
    title = f"E2E 活動 {uuid.uuid4().hex[:10]}"
    create_draft_activity(page, title, ZONES)

    upload_seats(page, str(SEATS_CSV))
    progress = page.get_by_text(re.compile(r"^處理 \d+ / \d+ 列"))
    expect(progress).to_have_text("處理 6 / 6 列 · 新增 6 · 錯誤 0", timeout=30_000)
    expect(page.get_by_text("完成", exact=True)).to_be_visible()
    expect(seats_in_state(page, "可售")).to_have_count(6, timeout=15_000)

    page.get_by_role("button", name="發布活動").click()
    expect(page.get_by_role("status").filter(has_text="活動已發布")).to_be_visible()
    expect(page.get_by_text("已發布", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="發布活動")).to_have_count(0)

    assert request_ids, "no X-Request-ID captured on the activity creation response"
    FLOW.title, FLOW.zones, FLOW.request_id = title, ZONES, request_ids[0]


def test_b_viewer_sees_live_seat_map(new_context):
    require(FLOW.title, "test_a published the activity")
    viewer = new_context("viewer").new_page()
    login(viewer, VIEWER)
    open_event(viewer, FLOW.title)
    wait_synced(viewer)
    expect(seats_in_state(viewer, "可售")).to_have_count(6)
    for label in SEAT_LABELS:
        expect(seat(viewer, label, "可售")).to_be_visible()
    expect(viewer.get_by_role("button", name="建立活動")).to_have_count(0)


def test_c_user_reserves_seat_and_viewer_sees_lock_pushed(page: Page, new_context):
    require(FLOW.title, "test_a published the activity")
    viewer = new_context("viewer").new_page()
    login(viewer, VIEWER)
    open_event(viewer, FLOW.title)
    wait_synced(viewer)
    expect(seat(viewer, "R01 排 1 號", "可售")).to_be_visible()

    login(page, USER)
    open_event(page, FLOW.title)
    wait_synced(page)
    seat(page, "R01 排 1 號", "可售").click()
    expect(page.get_by_text("已選擇 1 個座位")).to_be_visible()
    expect(page.get_by_text("票價小計").locator("xpath=..")).to_contain_text(twd(ZONES[0].price))
    page.get_by_role("button", name="鎖定座位並建立訂單").click()

    order_id = order_id_on_page(page)
    expect(page.get_by_text("待付款", exact=True)).to_be_visible()
    expect(page.get_by_role("row").filter(has_text="R01 排 1 號")).to_contain_text("A")
    subtotal = ZONES[0].price
    summary = page.get_by_role("heading", name="付款摘要").locator("xpath=..")
    expect(summary.get_by_text("票價小計（1 張）").locator("xpath=..")).to_contain_text(twd(subtotal))
    expect(summary.get_by_text("服務費", exact=True).locator("xpath=..")).to_contain_text(twd(fee(subtotal)))
    expect(summary.get_by_text("應付總額").locator("xpath=..")).to_contain_text(twd(subtotal + fee(subtotal)))
    expect(page.get_by_role("timer")).to_be_visible()

    # Pushed over the viewer's WebSocket: no reload, no manual resync.
    expect(seat(viewer, "R01 排 1 號", "鎖定")).to_be_visible(timeout=10_000)
    expect(seats_in_state(viewer, "可售")).to_have_count(5)
    FLOW.paid_order_id, FLOW.sold_seat = order_id, "R01 排 1 號"


def test_d_user_pays_and_viewer_sees_seat_sold(page: Page, new_context):
    require(FLOW.paid_order_id, "test_c created the pending order")
    viewer = new_context("viewer").new_page()
    login(viewer, VIEWER)
    open_event(viewer, FLOW.title)
    wait_synced(viewer)
    expect(seat(viewer, FLOW.sold_seat, "鎖定")).to_be_visible()

    login(page, USER)
    open_order(page, FLOW.paid_order_id)
    expect(page.get_by_text("待付款", exact=True)).to_be_visible()
    page.get_by_label("模擬回呼延遲（秒）").fill("0")
    page.get_by_role("button", name="模擬付款成功").click()
    expect(page.get_by_role("status").filter(has_text="付款已送出")).to_be_visible()
    # The order view polls every 2 s; the gateway callback and worker confirm the charge.
    expect(page.get_by_text("已付款", exact=True).first).to_be_visible(timeout=15_000)
    expect(page.get_by_text("付款已完成，期待與你在現場相見。")).to_be_visible()
    expect(page.get_by_role("button", name="模擬付款成功")).to_have_count(0)
    expect(page.get_by_text("完成", exact=True)).to_be_visible(timeout=15_000)

    expect(seat(viewer, FLOW.sold_seat, "售出")).to_be_visible(timeout=15_000)
    expect(seats_in_state(viewer, "可售")).to_have_count(5)


def test_e_user_reserves_by_zone_then_cancels_and_seats_return(page: Page, new_context):
    require(FLOW.sold_seat, "test_d paid for the first seat")
    viewer = new_context("viewer").new_page()
    login(viewer, VIEWER)
    open_event(viewer, FLOW.title)
    wait_synced(viewer)
    expect(seats_in_state(viewer, "可售")).to_have_count(5)

    login(page, USER)
    open_event(page, FLOW.title)
    wait_synced(page)
    page.get_by_role("button", name="系統配位").click()
    zone_select = page.get_by_label("配位區域")
    zone_value = zone_select.locator("option", has_text=ZONES[0].name).get_attribute("value")
    assert zone_value, "zone option for A 區 missing"
    zone_select.select_option(zone_value)
    page.get_by_label("張數").select_option("2")
    subtotal = ZONES[0].price * 2
    expect(page.get_by_text("票價小計").locator("xpath=..")).to_contain_text(twd(subtotal))
    page.get_by_role("button", name="鎖定座位並建立訂單").click()

    order_id_on_page(page)
    expect(page.get_by_text("待付款", exact=True)).to_be_visible()
    expect(page.get_by_role("row").filter(has_text="排")).to_have_count(2)
    summary = page.get_by_role("heading", name="付款摘要").locator("xpath=..")
    expect(summary.get_by_text("票價小計（2 張）").locator("xpath=..")).to_contain_text(twd(subtotal))
    expect(summary.get_by_text("服務費", exact=True).locator("xpath=..")).to_contain_text(twd(fee(subtotal)))
    expect(summary.get_by_text("應付總額").locator("xpath=..")).to_contain_text(twd(subtotal + fee(subtotal)))
    expect(seats_in_state(viewer, "鎖定")).to_have_count(2, timeout=10_000)
    expect(seats_in_state(viewer, "可售")).to_have_count(3)

    page.get_by_role("button", name="取消整筆訂單").click()  # confirm() is auto-accepted
    expect(page.get_by_role("status").filter(has_text="訂單已取消。")).to_be_visible()
    expect(page.get_by_text("已取消", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="取消整筆訂單")).to_have_count(0)

    expect(seats_in_state(viewer, "鎖定")).to_have_count(0, timeout=10_000)
    expect(seats_in_state(viewer, "可售")).to_have_count(5)
    expect(seat(viewer, FLOW.sold_seat, "售出")).to_be_visible()


def test_f_user_downloads_excel_report(page: Page, tmp_path: Path):
    require(FLOW.paid_order_id, "test_d paid for an order to appear in the report")
    login(page, USER)
    open_event(page, FLOW.title)
    with page.expect_download() as download_info:
        page.get_by_role("button", name="銷售報表").click()
    download = download_info.value
    assert download.suggested_filename.endswith(".xlsx"), download.suggested_filename
    path = tmp_path / download.suggested_filename
    download.save_as(path)
    assert path.stat().st_size > 0
    expect(page.get_by_role("alert")).to_have_count(0)

    workbook = openpyxl.load_workbook(path, read_only=True)
    try:
        assert workbook.sheetnames == ["Summary", "Zones", "Orders"]
        summary = {row[0]: row[1] for row in workbook["Summary"].iter_rows(values_only=True)}
        assert summary["活動名稱"] == FLOW.title
        assert summary["已付款訂單數"] == 1
        assert summary["已付款張數"] == 1
        assert summary["已取消訂單數"] == 1
        assert summary["已付款總額 TWD"] == ZONES[0].price + fee(ZONES[0].price)
        zones = {row[0]: row for row in workbook["Zones"].iter_rows(min_row=2, values_only=True)}
        assert set(zones) == {zone.code for zone in ZONES}
        assert zones["A"][3] == 1 and zones["A"][4] == ZONES[0].price
        orders = {row[0]: row for row in workbook["Orders"].iter_rows(min_row=2, values_only=True)}
        assert orders[FLOW.paid_order_id][1] == "PAID"
        assert sorted(row[1] for row in orders.values()) == ["CANCELED", "PAID"]
    finally:
        workbook.close()


def test_g_admin_dashboard_users_and_request_trace(page: Page):
    require(FLOW.request_id, "test_a captured an X-Request-ID")
    login(page, ADMIN)
    page.get_by_role("button", name="營運監控").click()
    expect(page.get_by_role("heading", name="現場，由你掌握。")).to_be_visible()
    expect(page.get_by_role("tab", name="系統監控")).to_have_attribute("aria-selected", "true")
    for card in ("每秒請求", "p95 處理延遲", "系統錯誤率", "即時連線"):
        expect(page.get_by_text(card, exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="API 處理延遲")).to_be_visible()
    # The previous tests generated traffic, so the per-second series renders a chart.
    expect(page.locator(".recharts-surface")).to_be_visible(timeout=20_000)
    expect(page.locator(".recharts-line")).to_have_count(3)
    expect(page.get_by_role("heading", name="系統告警")).to_be_visible()
    expect(page.get_by_role("alert")).to_have_count(0)

    page.get_by_role("tab", name="使用者管理").click()
    expect(page.get_by_role("heading", name="帳號與權限")).to_be_visible()
    for email, role in ((ADMIN[0], "ADMIN"), (USER[0], "USER"), (VIEWER[0], "VIEWER")):
        expect(page.get_by_role("row").filter(has_text=email)).to_be_visible()
        expect(page.get_by_label(f"{email} 角色")).to_have_value(role)
        expect(page.get_by_label(f"{email} 啟用狀態")).to_be_checked()

    page.get_by_role("tab", name="請求追蹤").click()
    page.get_by_label("Request ID").fill(FLOW.request_id)
    entries = page.locator("article.log-entry")
    # Telemetry reaches PostgreSQL through the worker asynchronously; the id was
    # issued minutes ago, so this normally succeeds on the first query, but the
    # lookup is bounded (30 s) rather than assumed.
    deadline = time.monotonic() + 30
    while True:
        page.get_by_role("button", name="查詢紀錄").click()
        expect(page.get_by_role("button", name="查詢紀錄")).to_be_enabled()
        try:
            expect(entries.first).to_be_visible(timeout=3_000)
            break
        except AssertionError:
            if time.monotonic() > deadline:
                raise
    expect(page.get_by_role("alert")).to_have_count(0)
    expect(entries.first).to_contain_text(re.compile(r"(api|backend|nginx)"))
    assert entries.count() >= 1
    expect(page.get_by_text("沒有找到此請求的紀錄。")).to_have_count(0)


def test_h_invalid_seat_file_shows_error_report(page: Page):
    login(page, USER)
    title = f"E2E 錯誤匯入 {uuid.uuid4().hex[:10]}"
    create_draft_activity(page, title, [Zone("A", "A 區", 500)])
    upload_seats(page, str(INVALID_SEATS_CSV))
    expect(page.get_by_text("驗證失敗", exact=True)).to_be_visible(timeout=30_000)
    expect(page.get_by_text(re.compile(r"^處理 \d+ / \d+ 列"))).to_contain_text(re.compile(r"新增 0 · 錯誤 [1-9]"))
    table = page.get_by_role("table").filter(has=page.get_by_role("columnheader", name="來源列"))
    expect(table).to_be_visible()
    expect(table.get_by_role("columnheader", name="原因")).to_be_visible()
    rows = table.locator("tbody").get_by_role("row")
    # Four data rows are rejected: the duplicate seat (both occurrences, by
    # design stricter than the requirement), the unknown zone and the blank
    # row label. Row numbers count data rows, excluding the CSV header.
    expect(rows).to_have_count(4)
    for index in range(rows.count()):
        cells = rows.nth(index).get_by_role("cell")
        assert re.fullmatch(r"\d+", cells.nth(0).inner_text().strip()), "row number cell"
        assert cells.nth(1).inner_text().strip(), "reason cell"
    assert sorted(int(rows.nth(i).get_by_role("cell").nth(0).inner_text()) for i in range(rows.count())) == [1, 2, 3, 4]
    expect(seats_in_state(page, "可售")).to_have_count(0)
    expect(page.get_by_text("尚未匯入座位。")).to_be_visible()
