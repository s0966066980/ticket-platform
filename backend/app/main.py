import re
from contextvars import ContextVar
from uuid import uuid4

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api import register_routers
from app.database import get_engine
from app.errors import AppError, register_error_handlers

REQUEST_ID = ContextVar("request_id", default=None)
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def create_app() -> FastAPI:
    app = FastAPI(title="Ticket Platform", version="0.1.0")
    register_error_handlers(app)

    @app.middleware("http")
    async def request_context(request, call_next):
        provided = request.headers.get("X-Request-ID", "")
        request_id = provided if _REQUEST_ID_PATTERN.fullmatch(provided) else str(uuid4())
        request.state.request_id = request_id
        token = REQUEST_ID.set(request_id)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            REQUEST_ID.reset(token)

    @app.get("/health", tags=["health"])
    def health():
        return {"status": "ok"}

    @app.get("/ready", tags=["health"])
    def ready():
        try:
            with get_engine().connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            raise AppError(503, "DATABASE_UNAVAILABLE", "Database is not ready") from exc
        return {"status": "ready"}

    register_routers(app)
    return app


app = create_app()
