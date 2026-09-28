"""Модуль подписки на события реального времени (SignalR / WebSockets)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger(__name__)


class Event(BaseModel):
    """Базовое событие реального времени."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    event_type: str
    timestamp: datetime = Field(default_factory=datetime.now)
    raw_payload: Dict[str, Any] = {}


class NewMarkEvent(Event):
    """Событие: Появление или изменение оценки."""

    event_type: Literal["NEW_MARK"] = "NEW_MARK"
    subject: str
    mark: str
    weight: int = 1
    assignment_name: Optional[str] = None


class NewAnnouncementEvent(Event):
    """Событие: Новое объявление."""

    event_type: Literal["NEW_ANNOUNCEMENT"] = "NEW_ANNOUNCEMENT"
    title: str
    description: str
    author: Optional[str] = None


class NewAssignmentEvent(Event):
    """Событие: Добавлено новое домашнее задание."""

    event_type: Literal["NEW_ASSIGNMENT"] = "NEW_ASSIGNMENT"
    subject: str
    title: str
    due_date: Optional[str] = None


class SignalREventListener:
    """Итератор подписки на события SignalR queueHub."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def listen(self, poll_interval: float = 10.0) -> AsyncGenerator[Event, None]:
        seen_announcements: set[int] = set()
        seen_marks: set[str] = set()

        while True:
            try:
                try:
                    announcements = await self._client.announcements()
                    for ann in announcements:
                        if ann.id not in seen_announcements:
                            seen_announcements.add(ann.id)
                            yield NewAnnouncementEvent(
                                title=ann.title,
                                description=ann.description,
                                # author — объект Author, а событию нужна строка:
                                # раньше здесь падала валидация, и ошибку глотал
                                # except, так что объявления не приходили вовсе.
                                author=ann.author.full_name or ann.author.nickname or None,
                                raw_payload=ann.model_dump(mode="json"),
                            )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.warning("Не удалось получить объявления", exc_info=True)

                try:
                    assigned = await self._client.assigned_marks()
                    for m in assigned:
                        key = f"{m.subject}:{m.topic}:{m.mark_text}:{m.lesson_date}"
                        if key not in seen_marks:
                            seen_marks.add(key)
                            yield NewMarkEvent(
                                subject=m.subject,
                                mark=m.mark_text,
                                assignment_name=m.topic,
                                raw_payload=m.model_dump(mode="json"),
                            )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.warning("Не удалось получить оценки", exc_info=True)

                await asyncio.sleep(poll_interval)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(poll_interval)
