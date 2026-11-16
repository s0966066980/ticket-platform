"""Explicit router registry. Each feature module exports router and optionally ws_router."""
from importlib import import_module, util
from fastapi import FastAPI

ROUTER_MODULES = (
    "auth", "admin_users", "events", "bookings", "orders", "imports",
    "payments", "feed", "admin_metrics", "reports",
)


def register_routers(app: FastAPI) -> None:
    for name in ROUTER_MODULES:
        qualified = f"app.api.{name}"
        if util.find_spec(qualified) is None:
            continue
        module = import_module(qualified)
        if hasattr(module, "router"):
            app.include_router(module.router, prefix="/api/v1")
        if hasattr(module, "ws_router"):
            app.include_router(module.ws_router)
