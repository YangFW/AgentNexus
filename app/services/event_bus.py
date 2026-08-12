from __future__ import annotations

from typing import Any
from app import db


def emit(task_id: str, event_type: str, title: str, content: str = "", data: Any | None = None) -> int:
    return db.insert_event(task_id, event_type, title, content, data or {})
