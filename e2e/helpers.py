"""Shared page-driving helpers for the e2e suite (role/label selectors only)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from playwright.sync_api import Locator, Page, expect

TAIPEI = timezone(timedelta(hours=8))
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

ADMIN = ("admin@example.test", "Admin-local-2026!")
USER = ("user@example.test", "User-local-2026!")
VIEWER = ("viewer@example.test", "Viewer-local-2026!")


@dataclass
class Zone:
    code: str
    name: str
    price: int


@dataclass
class Flow:
    """State the ordered flow tests hand to each other."""

    title: str = ""
    zones: list[Zone] = field(default_factory=list)
    request_id: str = ""
    paid_order_id: str = ""
    sold_seat: str = ""


def fee(subtotal: int) -> int:
    """Server rule: 5% service fee, rounded half up to whole TWD."""
    return (subtotal * 5 + 50) // 100


def twd(amount: int) -> re.Pattern[str]:
    """Matches the UI's zh-TW TWD rendering (``$1,296`` / ``NT$1,296``)."""
    return re.compile(r"\$" + re.escape(f"{amount:,}") + r"(?!\d)")


def taipei_local(delta: timedelta) -> str:
    """Value for a ``datetime-local`` input, in the UI's Asia/Taipei timezone."""
    return (datetime.now(TAIPEI) + delta).strftime("%Y-%m-%dT%H:%M")


def login(page: Page, credentials: tuple[str, str]) -> None:
    email, password = credentials
    page.goto("/")
    page.get_by_label("電子郵件").fill(email)
    page.get_by_label("密碼").fill(password)
    page.get_by_role("button", name="登入平台").click()
    expect(page.get_by_role("heading", name="值得親臨的現場")).to_be_visible()
    expect(page.get_by_text(email, exact=True)).to_be_visible()


def open_event(page: Page, title: str) -> None:
    """From the catalogue, open the activity card carrying ``title``."""
    card = page.get_by_role("button").filter(has=page.get_by_role("heading", name=title, exact=True))
    expect(card).to_be_visible()
    card.click()
    expect(page.get_by_role("heading", name=title, exact=True, level=1)).to_be_visible()


def sync_badge(page: Page) -> Locator:
    return page.get_by_role("status").filter(has_text=re.compile("即時同步中|未同步"))


def wait_synced(page: Page, timeout: float = 15_000) -> None:
    expect(sync_badge(page)).to_have_text("即時同步中", timeout=timeout)


def seat(page: Page, label: str, state: str) -> Locator:
    """Seat-map button, e.g. ``seat(page, "R01 排 1 號", "可售")``."""
    return page.get_by_role("button", name=f"{label}，{state}", exact=True)


def seats_in_state(page: Page, state: str) -> Locator:
    return page.get_by_role("button", name=re.compile(f"，{state}$"))


def create_draft_activity(page: Page, title: str, zones: list[Zone]) -> None:
    """Fill the activity form with an open sales window and the given zones."""
    page.get_by_role("button", name="建立活動").click()
    expect(page.get_by_role("heading", name="建立新活動")).to_be_visible()
    page.get_by_label("活動名稱").fill(title)
    page.get_by_label("分類").fill("E2E")
    page.get_by_label("場地").fill("E2E 測試場地")
    page.get_by_label("活動介紹").fill("由瀏覽器端到端測試自動建立的活動。")
    page.get_by_label("開賣時間（臺北）").fill(taipei_local(timedelta(days=-1)))
    page.get_by_label("停售時間（臺北）").fill(taipei_local(timedelta(days=30)))
    for _ in zones[1:]:
        page.get_by_role("button", name="新增區域").click()
    editors = page.get_by_role("button", name=re.compile(r"^移除區域 \d+$")).locator("xpath=..")
    expect(editors).to_have_count(len(zones))
    for index, zone in enumerate(zones):
        editor = editors.nth(index)
        editor.get_by_label("代碼").fill(zone.code)
        editor.get_by_label("區域名稱").fill(zone.name)
        editor.get_by_label("票價（TWD）").fill(str(zone.price))
    page.get_by_role("button", name="建立活動草稿").click()
    expect(page.get_by_role("heading", name=title, exact=True, level=1)).to_be_visible()
    expect(page.get_by_text("草稿", exact=True)).to_be_visible()


def upload_seats(page: Page, path: str, mode: str = "all_or_nothing") -> None:
    page.get_by_label("座位檔案").set_input_files(path)
    page.get_by_label("匯入模式").select_option(mode)
    page.get_by_role("button", name="上傳座位").click()


def order_id_on_page(page: Page) -> str:
    """Full order UUID shown under the order heading."""
    expect(page.get_by_role("heading", name="訂單詳情")).to_be_visible()
    text = page.get_by_text(UUID_RE).first
    expect(text).to_be_visible()
    value = text.inner_text().strip()
    assert UUID_RE.match(value), value
    return value


def open_order(page: Page, order_id: str) -> None:
    page.get_by_role("button", name="我的訂單").click()
    expect(page.get_by_role("heading", name="我的訂單")).to_be_visible()
    page.get_by_role("button", name=f"訂單 {order_id[:8]}").click()
    assert order_id_on_page(page) == order_id
