"""
backend/api/routes/fyers.py — Fyers auth endpoints for OpenTerminalUI.

Mirrors backend/api/routes/kite.py.

Endpoints
---------
GET  /fyers/auth/login-url   -> { login_url, configured }
POST /fyers/auth/session     -> { access_token, ... }   (exchange auth_code)
GET  /fyers/auth/callback    -> redirect handler (browser lands here after Fyers login)
GET  /fyers/profile          -> Fyers user profile
"""
from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from backend.core.fyers_client import FyersClient

router = APIRouter()
_fyers = FyersClient()


# ── request/response models ──────────────────────────────────────────

class FyersSessionRequest(BaseModel):
    auth_code: str


# ── helpers ──────────────────────────────────────────────────────────

def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail})


# ── routes ───────────────────────────────────────────────────────────

@router.get("/fyers/auth/login-url", tags=["fyers"])
async def fyers_login_url() -> dict[str, object]:
    """
    Return the Fyers OAuth redirect URL.

    The user must open this URL in a browser, log in, and approve access.
    Fyers then redirects to FYERS_REDIRECT_URI with ?auth_code=XXXX&state=...
    Pass that auth_code to POST /fyers/auth/session.
    """
    if not _fyers.app_id:
        return JSONResponse(
            status_code=400,
            content={"detail": "FYERS_APP_ID is not configured in .env"},
        )
    return {
        "configured": _fyers.is_configured,
        "login_url": _fyers.get_login_url(),
        "redirect_uri": _fyers.redirect_uri,
    }


@router.get("/fyers/auth/callback", tags=["fyers"])
async def fyers_auth_callback(
    auth_code: str = Query(..., alias="auth_code"),
    state: str | None = Query(default=None),
) -> HTMLResponse:
    """
    Browser landing page after the user approves access on Fyers.
    Exchanges the auth_code, writes it to .env automatically, and also
    pushes it to the live adapter so data starts flowing without a restart.
    """
    data = await _fyers.create_session(auth_code)
    token = data.get("access_token", "")
    if not token:
        html = f"""
        <html><body style="font-family:sans-serif;padding:2rem;">
        <h2 style="color:#c0392b;">Fyers auth failed</h2>
        <pre>{data}</pre>
        <p>Check your FYERS_APP_ID and FYERS_SECRET_KEY in .env and try again.</p>
        </body></html>"""
    else:
        # Also push the token into the live FyersAdapter so it works immediately
        # without needing a server restart.
        try:
            from backend.adapters.registry import get_adapter_registry
            registry = get_adapter_registry()
            adapter = registry._instances.get("fyers")
            if adapter is not None:
                adapter.app_id = _fyers.app_id   # ensure app_id is set too
                adapter._fy = None               # force client re-init with new token
            import os
            os.environ["FYERS_ACCESS_TOKEN"] = token
        except Exception:
            pass

        html = f"""
        <html><body style="font-family:sans-serif;padding:2rem;max-width:600px;margin:auto;">
        <h2 style="color:#27ae60;">&#10003; Fyers connected</h2>
        <p style="color:#555;">Token has been saved to your <code>.env</code> file automatically
        and is active immediately — <strong>no restart needed</strong>.</p>
        <hr style="border:none;border-top:1px solid #eee;margin:1.2rem 0;">
        <p style="font-size:13px;color:#888;">Token (also in .env as <code>FYERS_ACCESS_TOKEN</code>):</p>
        <pre style="background:#f4f4f4;padding:1rem;border-radius:4px;font-size:12px;word-break:break-all;">{token}</pre>
        <p style="font-size:12px;color:#aaa;">Valid until end of today's trading session.</p>
        </body></html>"""
    return HTMLResponse(content=html)


@router.post("/fyers/auth/session", tags=["fyers"])
async def fyers_create_session(payload: FyersSessionRequest) -> dict[str, object]:
    """
    Exchange a Fyers auth_code for an access_token.

    Call this with the auth_code received at your redirect_uri.
    The returned access_token is valid until the end of the trading day —
    save it as FYERS_ACCESS_TOKEN in .env and restart the server.
    """
    if not _fyers.is_configured:
        return _error(400, "FYERS_APP_ID and FYERS_SECRET_KEY are not configured in .env")
    data = await _fyers.create_session(payload.auth_code)
    if not data or not data.get("access_token"):
        return _error(502, f"Failed to create Fyers session: {data.get('message', 'unknown error')}")
    return data


@router.get("/fyers/profile", tags=["fyers"])
async def fyers_profile() -> dict[str, object]:
    """Return the Fyers user profile using the configured access token."""
    if not _fyers.app_id:
        return _error(400, "FYERS_APP_ID is not configured in .env")
    tok = _fyers.resolve_access_token()
    if not tok:
        return _error(401, "No access token — set FYERS_ACCESS_TOKEN in .env or call /fyers/auth/session first")
    data = await _fyers.get_profile(tok)
    if not data:
        return _error(502, "Failed to fetch Fyers profile")
    return data
