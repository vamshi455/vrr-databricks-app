"""Create `vrr_agent.app_user` and seed the three approval-chain accounts.

The approval chain is analyst → RM → site, so a demo needs one account per role — that
is the only way to *show* that the chain is enforced: sign in as the analyst, watch the
RM step refuse; sign in as the RM, watch it go through.

Passwords come from `VRR_DEMO_PASSWORD` (default `vrr-demo`) and are bcrypt-hashed before
they touch the database. These are DEMO accounts with a published default — the banner
below says so, because a seeded credential that nobody remembers is seeded forever.

Run: `make users`   ·   change one: `make users p=somethingbetter`
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, "src")

from vrr_agent_open.api import auth as A
from vrr_agent_open.config import load_config

CFG = load_config()
PASSWORD = os.environ.get("VRR_DEMO_PASSWORD", "vrr-demo")

DEMO_USERS = [
    ("analyst.demo", "analyst", "Ana Lyst — reviews drafts, first sign-off"),
    ("rm.demo", "rm", "Reservoir Manager — second sign-off"),
    ("site.demo", "site", "Site Engineer — the ONLY role that may execute"),
    ("steward.demo", "data_steward", "Data Steward — owns DATA_ARTIFACT items"),
]


if __name__ == "__main__":
    password = sys.argv[1] if len(sys.argv) > 1 else PASSWORD
    A.ensure_table()
    for username, role, full_name in DEMO_USERS:
        A.upsert_user(username, password, role, full_name)
        print(f"  ✓ {username:<14} role={role}")

    print(f"\n{len(DEMO_USERS)} accounts seeded in {CFG.pg_dsn.split('@')[-1]}")
    print(f"password for all of them: {password!r}")
    if password == "vrr-demo":
        print("\n⚠️  That is the PUBLISHED default. Fine for a local demo; change it with")
        print("   `make users p=<something>` before this is reachable from anywhere else.")
    if A.SECRET_IS_EPHEMERAL:
        print("\n⚠️  VRR_JWT_SECRET is not set, so tokens are signed with a random")
        print("   per-process key: every API restart invalidates every issued token.")
        print("   Set one in .env to keep sessions across restarts:")
        print("   python3 -c \"import secrets; print(secrets.token_urlsafe(48))\"")
