from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://ticket:ticket@postgres:5432/tickets"
    redis_url: str = "redis://redis:6379/0"
    upload_dir: Path = Path("/data/uploads")
    telemetry_spool_dir: Path = Path("/data/telemetry")
    gateway_url: str = "http://mock-gateway:8001"
    callback_url: str = "http://backend:8000/api/v1/payments/callback"
    gateway_secret: str = "local-development-gateway-secret"
    session_ttl_seconds: int = 28800
    hold_seconds: int = 300
    snapshot_ttl_seconds: int = 600
    idempotency_retention_hours: int = 24
    idempotency_lease_seconds: int = 30
    import_max_bytes: int = 20_000_000
    import_max_rows: int = 100_000
    import_batch_size: int = 1000
    worker_poll_seconds: float = 1.0
    outbox_poll_seconds: float = 0.25
    gateway_poll_seconds: float = 1.0
    ws_version_check_seconds: float = 5.0
    ws_queue_size: int = 256
    metrics_window_seconds: int = 60
    display_timezone: str = "Asia/Taipei"
    alert_error_rate: float = 0.05
    alert_p95_seconds: float = 1.0
    alert_outbox_lag_seconds: float = 10.0
    alert_min_requests: int = 20
    alert_sustain_seconds: float = 15.0
    database_pool_size: int = 20
    database_max_overflow: int = 20
    database_pool_timeout_seconds: float = 5.0
    seed_admin_email: str = "admin@example.test"
    seed_admin_password: str = "Admin-test-123!"
    seed_user_email: str = "user@example.test"
    seed_user_password: str = "User-test-123!"
    seed_viewer_email: str = "viewer@example.test"
    seed_viewer_password: str = "Viewer-test-123!"


    @property
    def idempotency_seconds(self) -> int:
        return self.idempotency_retention_hours * 3600

    @property
    def snapshot_seconds(self) -> int:
        return self.snapshot_ttl_seconds


@lru_cache
def get_settings() -> Settings:
    return Settings()
