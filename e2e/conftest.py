"""Browser end-to-end fixtures.

Every test gets fresh browser contexts (no cookies, no sessionStorage, no
WebSocket state carried over). Nothing is retried silently: a failed test
fails, and its screenshot(s) and a Playwright trace per context are written to
``e2e/artifacts/`` so the failure can be replayed with ``playwright show-trace``.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest
from playwright.sync_api import Browser, BrowserContext, Page

ARTIFACTS = Path(__file__).parent / "artifacts"
DEFAULT_TIMEOUT_MS = 10_000


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:120]


@pytest.fixture(scope="session")
def base_url() -> str:
    """Overrides pytest-playwright's base_url: the stack under test."""
    return os.environ.get("E2E_BASE_URL", "http://localhost:8080").rstrip("/")


@pytest.fixture(scope="session", autouse=True)
def _artifact_dir():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    """Expose each phase's report on the item so fixtures see the outcome."""
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)


@pytest.fixture
def new_context(browser: Browser, base_url: str, request: pytest.FixtureRequest):
    """Factory for isolated, traced browser contexts.

    Each context accepts browser dialogs (the UI uses ``confirm`` before
    cancelling an order or deleting an activity) and records a trace. On test
    failure every open page is screenshotted and each trace is saved.
    """
    contexts: list[tuple[str, BrowserContext]] = []

    def factory(name: str) -> BrowserContext:
        context = browser.new_context(
            base_url=base_url,
            locale="zh-TW",
            timezone_id="Asia/Taipei",
            viewport={"width": 1440, "height": 1000},
            accept_downloads=True,
        )
        context.set_default_timeout(DEFAULT_TIMEOUT_MS)
        context.tracing.start(name=name, screenshots=True, snapshots=True, sources=True)
        context.on("page", lambda page: page.on("dialog", lambda dialog: dialog.accept()))
        contexts.append((name, context))
        return context

    yield factory

    failed = any(
        getattr(getattr(request.node, f"rep_{phase}", None), "failed", False)
        for phase in ("setup", "call", "teardown")
    )
    test_slug = _slug(request.node.name)
    for name, context in contexts:
        stem = f"{test_slug}-{_slug(name)}"
        if failed:
            for index, page in enumerate(context.pages):
                try:
                    page.screenshot(path=ARTIFACTS / f"{stem}-page{index}.png", full_page=True)
                except Exception as error:  # the page may already be gone
                    print(f"screenshot of {stem} page {index} failed: {error}")
            context.tracing.stop(path=ARTIFACTS / f"{stem}-trace.zip")
        else:
            context.tracing.stop()
        context.close()


@pytest.fixture
def context(new_context) -> BrowserContext:
    """A fresh browser context for the test's primary actor."""
    return new_context("main")


@pytest.fixture
def page(context: BrowserContext) -> Page:
    page = context.new_page()
    page.on("dialog", lambda dialog: dialog.accept())
    return page


@pytest.fixture(scope="session", autouse=True)
def _require_stack(base_url: str):
    """Fail fast with a clear message when the stack is not reachable."""
    import json
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{base_url}/health", timeout=5) as response:
            body = json.loads(response.read().decode() or "{}")
    except (urllib.error.URLError, OSError, ValueError) as error:
        pytest.exit(f"E2E stack not reachable at {base_url}/health: {error}", returncode=2)
    if body.get("status") != "ok":
        pytest.exit(f"E2E stack unhealthy at {base_url}/health: {body}", returncode=2)


def pytest_sessionstart(session):
    # Stale artifacts from a previous run would hide which failure is current.
    if ARTIFACTS.exists() and not os.environ.get("E2E_KEEP_ARTIFACTS"):
        shutil.rmtree(ARTIFACTS)
