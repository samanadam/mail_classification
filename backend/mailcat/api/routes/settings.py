from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from mailcat import settings_store
from mailcat.api.deps import current_user, get_db
from mailcat.models import User

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
def get_settings(db: Session = Depends(get_db), _: User = Depends(current_user)) -> dict:
    return {"values": settings_store.get_all(db),
            "specs": {k: asdict(v) for k, v in settings_store.SPECS.items()}}


@router.put("")
def put_settings(body: dict[str, Any], db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    # Validate everything before writing anything.
    try:
        clean = {k: settings_store.validate(k, v) for k, v in body.items()}
    except settings_store.SettingError as exc:
        raise HTTPException(400, str(exc)) from exc
    for key, value in clean.items():
        settings_store.set_value(db, key, value, user.username)
    return {"values": settings_store.get_all(db)}
