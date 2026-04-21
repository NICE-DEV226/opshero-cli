"""
CLI configuration — persisted to ~/.opshero/config.json.

Manages:
  - API base URL
  - JWT access + refresh tokens
  - client_id (UUID, generated once per workstation)
  - user profile cache (github_login, tier)
  - patterns cache path

Use `Config.load()` to read and `cfg.save()` to persist.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import platformdirs
from jose import jwt as jose_jwt
from pydantic import BaseModel, Field


# ── Paths ──────────────────────────────────────────────────────────────────────

APP_NAME = "opshero"
APP_AUTHOR = "opshero"

CONFIG_DIR = Path(platformdirs.user_config_dir(APP_NAME, APP_AUTHOR))
DATA_DIR = Path(platformdirs.user_data_dir(APP_NAME, APP_AUTHOR))

CONFIG_FILE = CONFIG_DIR / "config.json"
PATTERNS_CACHE_FILE = DATA_DIR / "patterns_cache.json"
ANALYSES_DB_FILE = DATA_DIR / "analyses.db"


# ── Config model ───────────────────────────────────────────────────────────────

class Config(BaseModel):
    # API (can be overridden via OPSHERO_API_URL environment variable)
    api_url: str = "https://api.opshero.me"
    
    def __init__(self, **data):
        super().__init__(**data)
        # Allow environment variable override
        import os
        env_url = os.getenv("OPSHERO_API_URL")
        if env_url:
            self.api_url = env_url.rstrip("/")

    # Auth
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_expires_at: Optional[datetime] = None

    # Identity
    client_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    github_login: Optional[str] = None
    github_avatar_url: Optional[str] = None
    user_tier: str = "free"

    # Team & Project context (persisted — survives between sessions)
    team_id: Optional[str] = None
    team_name: Optional[str] = None
    active_project_id: Optional[str] = None
    active_project_name: Optional[str] = None

    # Patterns
    patterns_synced_at: Optional[datetime] = None
    patterns_count: int = 0

    @classmethod
    def load(cls) -> "Config":
        """Load config from disk. Returns default config if file doesn't exist."""
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                return cls.model_validate(data)
            except Exception:
                # Corrupt config — start fresh but preserve client_id if possible
                return cls()
        return cls()

    def save(self) -> None:
        """Persist config to disk (creates directory if needed)."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(
            self.model_dump_json(indent=2),
            encoding="utf-8",
        )
        # Restrict permissions on Unix-like systems
        try:
            CONFIG_FILE.chmod(0o600)
        except Exception:
            pass

    def clear_auth(self) -> None:
        """Remove all auth tokens."""
        self.access_token = None
        self.refresh_token = None
        self.token_expires_at = None
        self.github_login = None
        self.github_avatar_url = None
        self.user_tier = "free"

    # ── Token helpers ──────────────────────────────────────────────────────────

    @property
    def is_authenticated(self) -> bool:
        return bool(self.access_token)

    @property
    def is_token_expired(self) -> bool:
        """True if the access token is missing or expired (with 60s buffer)."""
        if not self.access_token:
            return True
        if self.token_expires_at is None:
            # Parse from JWT payload
            try:
                payload = jose_jwt.get_unverified_claims(self.access_token)
                exp = payload.get("exp", 0)
                self.token_expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)
            except Exception:
                return True
        now = datetime.now(tz=timezone.utc)
        return (self.token_expires_at - now).total_seconds() < 60

    def get_auth_header(self) -> dict:
        """Return Authorization header dict for httpx."""
        if not self.access_token:
            return {}
        return {"Authorization": f"Bearer {self.access_token}"}

    # ── Patterns cache ─────────────────────────────────────────────────────────

    @property
    def has_patterns_cache(self) -> bool:
        return PATTERNS_CACHE_FILE.exists() and self.patterns_count > 0

    def load_patterns_cache(self) -> list[dict]:
        """Load locally cached patterns (used in offline mode)."""
        if not PATTERNS_CACHE_FILE.exists():
            return []
        try:
            return json.loads(PATTERNS_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []

    def save_patterns_cache(self, patterns: list[dict]) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PATTERNS_CACHE_FILE.write_text(
            json.dumps(patterns, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        self.patterns_count = len(patterns)
        self.patterns_synced_at = datetime.now(tz=timezone.utc)
        self.save()
