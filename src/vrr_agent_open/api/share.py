"""Share mode — what changes when this app is reachable from the public internet.

The workbench was built to run on one engineer's laptop. Two of its defaults are fine
there and wrong the moment a tunnel is pointed at it:

* **Reads are unauthenticated.** `/api/patterns`, `/api/overview`, every trend, lineage
  and audit endpoint answers 200 to anybody. On a laptop that is convenience; on a public
  URL it is the whole dataset, served to whoever has the link.
* **`/api/health` reports internal addresses.** The Postgres host and the MLflow URI are
  useful in the sidebar and are reconnaissance to a stranger.

`VRR_SHARE=1` turns on the public posture:

  - reads require a bearer token unless `VRR_PUBLIC_READS=1` says otherwise, which is the
    right switch for a demo whose data is synthetic and the wrong one for anything else;
  - `/api/health` stops reporting hosts and URIs;
  - a missing `VRR_JWT_SECRET` becomes fatal instead of a warning — an ephemeral key on a
    shared instance signs tokens that die at the next restart, and the operator reads that
    as "the login is broken";
  - the tunnel's own origin is accepted by CORS.

None of this makes the app safe to expose indefinitely. It makes a demo shareable for an
afternoon with the obvious holes closed, and it says so out loud at startup. The
deployment checklist that would replace it — TLS termination you control, a real IdP,
rotating keys, rate limits, an audit sink — is README §12c.
"""
from __future__ import annotations

import os

from fastapi import Depends, HTTPException, Request

from . import auth as AUTH


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


SHARE_MODE = _flag("VRR_SHARE")
# Public reads are opt-IN even inside share mode: the safe default is that turning on
# sharing tightens things, never loosens them.
PUBLIC_READS = _flag("VRR_PUBLIC_READS")


def preflight() -> list[str]:
    """Refuse to serve publicly in a configuration that will embarrass the operator.

    Returns the list of problems; empty means good to go. Called at import so a bad
    configuration fails at startup rather than at the moment a stranger clicks the link.
    """
    problems = []
    if not SHARE_MODE:
        return problems
    if AUTH.SECRET_IS_EPHEMERAL:
        problems.append(
            "VRR_JWT_SECRET is not set. In share mode this is fatal: tokens would be "
            "signed with a per-process key and every restart would silently invalidate "
            "every session. Generate one with "
            "`python -c \"import secrets;print(secrets.token_urlsafe(48))\"` and put it "
            "in .env."
        )
    return problems


def banner() -> str:
    """What gets printed when the app comes up in share mode. Deliberately blunt."""
    reads = ("PUBLIC — anyone with the link can read every pattern, trend and audit"
             if PUBLIC_READS else "require a bearer token")
    return (
        "\n"
        "  ┌─ SHARE MODE ─────────────────────────────────────────────────────────┐\n"
        f"  │  Reads:  {reads:<59}│\n"
        "  │  Writes: always require a token; the role is a signed claim          │\n"
        "  │  Health: internal hosts and URIs redacted                            │\n"
        "  │                                                                      │\n"
        "  │  This is a demo posture, not a deployment. Stop the tunnel when you  │\n"
        "  │  are done — the URL stays live for as long as ngrok is running.      │\n"
        "  └──────────────────────────────────────────────────────────────────────┘\n"
    )


async def guard_reads(request: Request) -> None:
    """Dependency on the read routers. A no-op unless share mode is closing reads.

    Deliberately a router-level dependency rather than a per-endpoint decorator: a new
    read endpoint added later inherits the guard instead of quietly shipping open.
    """
    if not SHARE_MODE or PUBLIC_READS:
        return
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(
            401, "This instance is shared publicly and reads require a sign-in.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Reuse the normal verification so a tampered or expired token fails identically
    # here and on the write path.
    AUTH.decode_token(header.split(" ", 1)[1].strip())


READ_GUARD = [Depends(guard_reads)]


def redact_health(payload: dict) -> dict:
    """Strip internal addresses from /api/health when the app is publicly reachable."""
    if not SHARE_MODE:
        return payload
    out = dict(payload)
    if "postgres" in out:
        pg = dict(out["postgres"])
        pg.pop("host", None)
        out["postgres"] = pg
    if "tracing" in out:
        tr = dict(out["tracing"])
        tr.pop("uri", None)
        out["tracing"] = tr
    out["share_mode"] = {"public_reads": PUBLIC_READS}
    return out
