"""Application configuration, read from environment variables (and an optional .env file).

Every setting is documented in .env.example. Secrets are only ever read server-side.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    # Core
    app_name: str = "PitchForce Feedback"
    secret_key: str = Field(default="dev-insecure-change-me", description="Signs session cookies and share links")
    database_url: str = ""  # default: sqlite file under DATA_DIR
    data_dir: Path = ROOT / "data"
    base_url: str = "http://127.0.0.1:8000"
    environment: str = "development"  # development | production | test
    session_max_age_hours: int = 12
    cookie_secure: bool = False

    # Bootstrap admin (created on first start if no users exist)
    admin_email: str = ""
    admin_password: str = ""

    # Uploads
    max_upload_mb: int = 8192
    max_document_upload_mb: int = 100
    upload_chunk_mb: int = 8
    allowed_video_ext: str = ".mp4,.m4v,.mov,.mkv,.webm,.avi"

    # Archives (safe extraction)
    zip_max_files: int = 200
    zip_max_total_mb: int = 300
    zip_max_ratio: int = 100

    # Background worker
    worker_concurrency: int = 2
    job_lease_seconds: int = 120
    job_max_attempts: int = 3
    worker_poll_seconds: float = 1.0

    # Media
    ffmpeg_path: str = ""  # empty: use the bundled imageio-ffmpeg binary
    clip_render_timeout_s: int = 3600

    # AI provider: "offline" (deterministic rule-based, no data leaves the server) or "anthropic"
    ai_provider: str = "offline"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: str = "medium"
    anthropic_fallbacks: bool = True
    # Prices in USD per million tokens, used only for cost estimates shown to the operator.
    ai_price_input_per_mtok: float = 4.0
    ai_price_output_per_mtok: float = 20.0

    # Transcription provider: "none" or "openai_compatible" (any Whisper-compatible HTTP endpoint)
    transcription_provider: str = "none"
    transcription_api_url: str = ""
    transcription_api_key: str = ""
    transcription_model: str = "whisper-1"
    transcription_price_per_minute: float = 0.006

    # Official EcoVG content
    calendar_urls: str = "https://events.ecovg.com/events"
    calendar_allowed_hosts: str = "ecovg.com,events.ecovg.com,www.ecovg.com,events.pitch-force.com"
    calendar_cache_hours: int = 24
    advisory_url: str = "https://ecovg.com/entrepreneur-services/"
    outbound_fetch_enabled: bool = True

    # Share links
    share_link_days: int = 14

    # Retention: days to keep uploaded sources after an event is archived (0 = keep until deleted)
    retention_days: int = 0

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{(self.data_dir / 'pitchforce.sqlite').as_posix()}"

    @property
    def storage_dir(self) -> Path:
        p = self.data_dir / "storage"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def video_exts(self) -> set[str]:
        return {e.strip().lower() for e in self.allowed_video_ext.split(",") if e.strip()}

    @property
    def calendar_url_list(self) -> list[str]:
        return [u.strip() for u in self.calendar_urls.split(",") if u.strip()]

    @property
    def calendar_hosts(self) -> set[str]:
        return {h.strip().lower() for h in self.calendar_allowed_hosts.split(",") if h.strip()}

    @property
    def ai_configured(self) -> bool:
        return self.ai_provider == "anthropic" and bool(self.anthropic_api_key)

    @property
    def transcription_configured(self) -> bool:
        return self.transcription_provider != "none" and bool(self.transcription_api_url and self.transcription_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
