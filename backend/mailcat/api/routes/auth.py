from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from mailcat.api.auth import (
    COOKIE_NAME,
    _token_hash,
    create_session,
    end_session,
    hash_password,
    throttle,
    verify_password,
)
from mailcat.api.deps import current_user, get_db
from mailcat.config import get_config
from mailcat.models import User, UserSession

router = APIRouter(prefix="/api/auth", tags=["auth"])


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1000)


@router.post("/login")
def login(body: Login, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    ip = request.client.host if request.client else "unknown"
    keys = (f"ip:{ip}", f"user:{body.username.lower()}")
    if throttle.blocked(*keys):
        raise HTTPException(429, "too many failed attempts; try again in 15 minutes")
    user = db.scalar(select(User).where(User.username == body.username))
    if not verify_password(user, body.password):
        throttle.fail(*keys)
        raise HTTPException(401, "wrong username or password")
    throttle.reset(*keys)
    token = create_session(db, user, request.headers.get("user-agent"))  # type: ignore[arg-type]
    cfg = get_config()
    response.set_cookie(COOKIE_NAME, token, httponly=True, secure=cfg.cookie_secure, samesite="strict",
                        max_age=cfg.session_hours * 3600, path="/")
    return {"username": user.username}  # type: ignore[union-attr]


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    end_session(db, request.cookies.get(COOKIE_NAME))
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(current_user)) -> dict:
    return {"username": user.username}


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=1000)
    new_password: str = Field(min_length=10, max_length=1000)


@router.post("/password")
def change_password(body: PasswordChange, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(current_user)) -> dict:
    if not verify_password(user, body.current_password):
        raise HTTPException(400, "current password is wrong")
    user.password_hash = hash_password(body.new_password)
    db.add(user)
    # Sign out every other session of this user; the current one stays valid.
    current = _token_hash(request.cookies.get(COOKIE_NAME) or "")
    db.execute(delete(UserSession).where(UserSession.user_id == user.id, UserSession.token_hash != current))
    return {"ok": True}
