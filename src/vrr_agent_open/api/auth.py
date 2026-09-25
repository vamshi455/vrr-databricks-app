"""OAuth2 password grant + JWT bearer tokens — who you are, established by the server.

    POST /api/auth/token   username + password  ──▶  signed JWT {sub, role, exp}
    every protected call   Authorization: Bearer <token>  ──▶  claims the client cannot edit

**The hole this closes.** The approval chain is a chain of PEOPLE — draft → analyst → RM
→ site, and only `site` may execute. Before this module the role travelled in the REQUEST
BODY: the server dutifully checked it against the stage, but the caller chose it. A curl
with `{"role": "site"}` executed a valve change. Now `role` is a claim inside a signature
the client cannot forge, minted from a `vrr_agent.app_user` row at login. To act as the
RM you log in as the RM.

Design notes, and their limits, stated plainly:

- **HS256, locally issued.** One secret, no identity provider, no network call — it keeps
  the stack local and free. For real SSO the same `current_user` dependency would verify
  RS256 against an IdP's JWKS instead; nothing downstream changes.
- **Short expiry, no refresh tokens.** 12 h by default: long enough for a shift, and a
  refresh-token rotation scheme is real work that buys nothing on a local workbench.
- **No revocation list.** A stolen token is valid until it expires. Deactivating a user
  (`active = false`) blocks the next LOGIN, not an already-issued token — which is the
  standard stateless-JWT trade and worth knowing before this faces a network.
- **Bcrypt with a per-password salt**, cost from the library default. The plaintext never
  reaches the database, a log line, or a trace.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from .db import execute, query

ALGORITHM = "HS256"
TOKEN_TTL_MINUTES = int(os.environ.get("VRR_JWT_TTL_MINUTES", "720"))    # 12 h

# A per-process random secret when none is configured: the API still works out of the box
# for a local demo, and tokens simply stop being valid across restarts. It must NOT be a
# hard-coded default — a well-known signing key in a public repo is worse than no auth,
# because it looks like security.
_GENERATED = secrets.token_urlsafe(48)
SECRET = os.environ.get("VRR_JWT_SECRET") or _GENERATED
SECRET_IS_EPHEMERAL = not os.environ.get("VRR_JWT_SECRET")

# tokenUrl is what /docs uses to drive the Authorize button.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/auth/token", auto_error=False)


# ------------------------------------------------------------------ passwords ----
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except (ValueError, TypeError):        # malformed hash in the row
        return False


# --------------------------------------------------------------------- tokens ----
def create_access_token(username: str, role: str,
                        minutes: int = TOKEN_TTL_MINUTES) -> tuple[str, int]:
    """Mint a token. Returns (token, expires_in_seconds)."""
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=minutes)
    payload = {"sub": username, "role": role, "iat": now, "exp": expires}
    return jwt.encode(payload, SECRET, algorithm=ALGORITHM), minutes * 60


def decode_token(token: str) -> dict[str, Any]:
    """Verify signature + expiry. Raises 401 with the reason, never a stack trace."""
    try:
        return jwt.decode(token, SECRET, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token expired — sign in again",
                            headers={"WWW-Authenticate": "Bearer"}) from None
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {exc}",
                            headers={"WWW-Authenticate": "Bearer"}) from None


# ---------------------------------------------------------------------- users ----
def get_user(username: str) -> dict | None:
    rows = query("SELECT username, password_hash, role, full_name, active"
                 " FROM vrr_agent.app_user WHERE username = %(u)s", {"u": username})
    return rows[0] if rows else None


def authenticate(username: str, password: str) -> dict | None:
    """Password check. Deliberately returns the same None for 'no such user' and 'wrong
    password' so the endpoint cannot be used to enumerate accounts."""
    user = get_user(username)
    if not user or not user["active"]:
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return user


def touch_last_login(username: str) -> None:
    try:
        execute("UPDATE vrr_agent.app_user SET last_login = now() WHERE username = %(u)s",
                {"u": username})
    except Exception:
        pass                                # a failed audit stamp must not fail the login


def ensure_table() -> None:
    """Idempotent DDL, so a database created before auth existed gains the table."""
    execute("""
        CREATE TABLE IF NOT EXISTS vrr_agent.app_user (
          username text PRIMARY KEY,
          password_hash text NOT NULL,
          role text NOT NULL CHECK (role IN ('analyst','rm','site','data_steward','admin')),
          full_name text, active boolean NOT NULL DEFAULT true,
          created_at timestamptz DEFAULT now(), last_login timestamptz)
    """, {})


def upsert_user(username: str, password: str, role: str,
                full_name: str | None = None) -> None:
    execute("INSERT INTO vrr_agent.app_user (username, password_hash, role, full_name)"
            " VALUES (%(u)s, %(h)s, %(r)s, %(n)s)"
            " ON CONFLICT (username) DO UPDATE SET password_hash = EXCLUDED.password_hash,"
            " role = EXCLUDED.role, full_name = EXCLUDED.full_name, active = true",
            {"u": username, "h": hash_password(password), "r": role, "n": full_name})


# ----------------------------------------------------------------- dependencies ----
def current_user(token: Annotated[str | None, Depends(oauth2_scheme)]) -> dict:
    """The identity behind this request. 401 when absent, expired, or tampered with."""
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "not authenticated — POST /api/auth/token for a bearer token",
                            headers={"WWW-Authenticate": "Bearer"})
    claims = decode_token(token)
    username, role = claims.get("sub"), claims.get("role")
    if not username or not role:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token is missing sub/role",
                            headers={"WWW-Authenticate": "Bearer"})
    return {"username": username, "role": role, "claims": claims}


def require_role(*allowed: str):
    """Dependency factory: the caller's TOKEN role must be one of `allowed`.

    Used for coarse gates. The approval chain needs a finer check — which role may act
    depends on the stage the item currently sits at — so `routes_approvals` compares the
    token role against that stage itself. Either way the role comes from the signature.
    """
    def guard(user: Annotated[dict, Depends(current_user)]) -> dict:
        if user["role"] not in allowed:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"role '{user['role']}' may not do this; requires one of "
                f"{', '.join(allowed)}")
        return user
    return guard


def optional_user(token: Annotated[str | None, Depends(oauth2_scheme)]) -> dict | None:
    """The identity behind this request, or None — never a 401.

    For endpoints that stay readable signed-out but must still not accept a
    client-ASSERTED identity. The chat transcript is the case: it is public to read, and
    whose "cleared" cutoff to apply is a fact about the caller, so it has to come from a
    signature rather than a query parameter. A bad token here reads as anonymous instead
    of erroring, because the endpoint works fine without one.
    """
    if not token:
        return None
    try:
        claims = decode_token(token)
    except HTTPException:
        return None
    username, role = claims.get("sub"), claims.get("role")
    return {"username": username, "role": role, "claims": claims} if username else None


CurrentUser = Annotated[dict, Depends(current_user)]
OptionalUser = Annotated[dict | None, Depends(optional_user)]
