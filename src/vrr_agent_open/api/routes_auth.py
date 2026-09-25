"""Sign in, and find out who the server thinks you are.

`POST /api/auth/token` is the OAuth2 *password grant*: form-encoded `username` and
`password` in, a bearer token out. The form encoding is not a style choice — it is what
`OAuth2PasswordRequestForm` and the Authorize button in `/docs` both speak, so the
generated docs become a working client.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from . import auth as A

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/token")
def token(form: Annotated[OAuth2PasswordRequestForm, Depends()]) -> dict:
    """Exchange credentials for a JWT carrying `sub` and `role`.

    The failure message is identical for an unknown user and a wrong password — a
    login endpoint that distinguishes them is an account-enumeration oracle.
    """
    user = A.authenticate(form.username, form.password)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "incorrect username or password",
                            headers={"WWW-Authenticate": "Bearer"})
    access_token, expires_in = A.create_access_token(user["username"], user["role"])
    A.touch_last_login(user["username"])
    return {"access_token": access_token, "token_type": "bearer",
            "expires_in": expires_in, "role": user["role"],
            "username": user["username"], "full_name": user.get("full_name")}


@router.get("/me")
def me(user: A.CurrentUser) -> dict:
    """The decoded claims. The UI shows WHO YOU ARE from this rather than from a
    dropdown you picked — that dropdown was the bug this whole module removes."""
    return {"username": user["username"], "role": user["role"],
            "expires_at": user["claims"].get("exp")}
