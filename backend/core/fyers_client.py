"""
backend/core/fyers_client.py — Fyers API v3 auth client for OpenTerminalUI.

Config (.env):
    FYERS_APP_ID        = XXXXXXXX-100   (your app_id / client_id)
    FYERS_SECRET_KEY    = <app secret>
    FYERS_REDIRECT_URI  = http://localhost:8000/fyers/auth/callback
    FYERS_ACCESS_TOKEN  = <daily token>  (written here after session exchange)

Mirrors KiteClient — only auth + profile; data calls live in adapters/fyers.py.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)


def _find_env_file() -> Path | None:
    """Return the project-root .env path, mirroring backend/config/env.py logic."""
    root = Path(__file__).resolve().parents[2]
    for candidate in (root / ".env", root / "backend" / ".env"):
        if candidate.exists():
            return candidate
    return None


def _write_token_to_env(token: str) -> None:
    """Upsert FYERS_ACCESS_TOKEN=<token> in the .env file on disk."""
    env_path = _find_env_file()
    if env_path is None:
        logger.warning("fyers_client: .env file not found — token NOT persisted to disk")
        return
    try:
        text = env_path.read_text(encoding="utf-8")
        key = "FYERS_ACCESS_TOKEN"
        new_line = f"{key}={token}"
        if re.search(rf"^{key}=", text, re.MULTILINE):
            # Replace existing line (with or without a value)
            text = re.sub(rf"^{key}=.*", new_line, text, flags=re.MULTILINE)
        else:
            # Append at end of file
            text = text.rstrip("\n") + f"\n{new_line}\n"
        env_path.write_text(text, encoding="utf-8")
        logger.info("fyers_client: FYERS_ACCESS_TOKEN written to %s", env_path)
    except Exception as exc:
        logger.error("fyers_client: failed to write token to .env: %s", exc)


class FyersClient:
    AUTH_BASE_URL = "https://api-t1.fyers.in/api/v3"
    LOGIN_BASE_URL = "https://api-t1.fyers.in/api/v3/generate-authcode"

    def __init__(
        self,
        app_id: Optional[str] = None,
        secret_key: Optional[str] = None,
        redirect_uri: Optional[str] = None,
        access_token: Optional[str] = None,
        timeout: float = 12.0,
    ) -> None:
        self.app_id = app_id or os.getenv("FYERS_APP_ID", "")
        self.secret_key = secret_key or os.getenv("FYERS_SECRET_KEY", "")
        self.redirect_uri = redirect_uri or os.getenv(
            "FYERS_REDIRECT_URI", "http://localhost:8000/fyers/auth/callback"
        )
        self.access_token = access_token or os.getenv("FYERS_ACCESS_TOKEN", "")
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
                trust_env=False,
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def is_configured(self) -> bool:
        return bool(self.app_id and self.secret_key)

    def resolve_access_token(self, override: Optional[str] = None) -> str:
        return (override or self.access_token or "").strip()

    # ── Step 1: generate the login redirect URL ──────────────────────
    def get_login_url(self) -> str:
        """Return the Fyers OAuth URL the user must visit to get an auth_code."""
        if not self.app_id:
            return ""
        params = {
            "client_id": self.app_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "state": "openterminalui",
        }
        return f"{self.LOGIN_BASE_URL}?{urlencode(params)}"

    # ── Step 2: exchange auth_code for access_token ──────────────────
    async def create_session(self, auth_code: str) -> Dict[str, Any]:
        """Exchange a Fyers auth_code for an access_token."""
        if not self.is_configured or not auth_code:
            return {}
        # Fyers expects SHA-256(app_id:secret_key) as the appIdHash
        app_id_hash = hashlib.sha256(
            f"{self.app_id}:{self.secret_key}".encode()
        ).hexdigest()
        payload = {
            "grant_type": "authorization_code",
            "appIdHash": app_id_hash,
            "code": auth_code,
        }
        try:
            client = await self._http()
            resp = await client.post(
                f"{self.AUTH_BASE_URL}/validate-authcode",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            # Cache in memory and persist to .env on disk
            tok = data.get("access_token") or ""
            if tok:
                self.access_token = tok
                os.environ["FYERS_ACCESS_TOKEN"] = tok
                _write_token_to_env(tok)
            return data
        except Exception as exc:
            logger.error("Fyers session creation failed: %s", exc)
            return {}

    # ── Profile ──────────────────────────────────────────────────────
    async def get_profile(self, access_token: Optional[str] = None) -> Dict[str, Any]:
        tok = self.resolve_access_token(access_token)
        if not tok or not self.app_id:
            return {}
        try:
            client = await self._http()
            resp = await client.get(
                f"{self.AUTH_BASE_URL}/profile",
                headers={"Authorization": f"{self.app_id}:{tok}"},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error("Fyers profile fetch failed: %s", exc)
            return {}
