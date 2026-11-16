from typing import Any
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException


class AppError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: Any = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


def error_response(request: Request, status: int, code: str, message: str, details=None):
    payload = {"code": code, "message": message, "request_id": request.state.request_id}
    if details is not None:
        payload["details"] = details
    return JSONResponse(payload, status_code=status, headers={"X-Request-ID": request.state.request_id})


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def application_error(request: Request, exc: AppError):
        return error_response(request, exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        codes = {401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
        response = error_response(request, exc.status_code, codes.get(exc.status_code, "HTTP_ERROR"), str(exc.detail))
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        details = [{"loc": list(item["loc"]), "type": item["type"], "message": item["msg"]} for item in exc.errors()]
        return error_response(request, 422, "VALIDATION_ERROR", "Request validation failed", details)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        return error_response(request, 500, "INTERNAL_ERROR", "An unexpected error occurred")
