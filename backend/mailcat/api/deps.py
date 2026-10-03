from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from mailcat.api.auth import COOKIE_NAME, user_for_token
from mailcat.db import new_session
from mailcat.models import User


def get_db() -> Iterator[Session]:
    session = new_session()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = user_for_token(db, request.cookies.get(COOKIE_NAME))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "login required")
    return user
