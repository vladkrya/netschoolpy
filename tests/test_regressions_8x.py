"""Регрессии 8.0: модели разошлись с форматом SGO и с собственным клиентом."""

import asyncio
import datetime
import sys

import httpx
import pytest

from netschoolpy import exceptions
from netschoolpy.client import NetSchool, _ask
from netschoolpy.events import SignalREventListener
from netschoolpy.models import (
    AssignedMark,
    Announcement,
    Diary,
    Message,
    ReportingPeriod,
    ShortSchool,
    Student,
)

DIARY = {
    "weekStart": "2026-09-28T00:00:00",
    "weekEnd": "2026-10-04T00:00:00",
    "weekDays": [
        {
            "date": "2026-09-29T00:00:00",
            "lessons": [
                {
                    "day": "2026-09-29T00:00:00",
                    "startTime": "08:00",
                    "endTime": "08:40",
                    "room": "12, каб. русского",
                    "number": 1,
                    "subjectName": "Русский язык",
                    "assignments": [
                        {
                            "id": 7,
                            "typeId": 3,
                            "assignmentName": "Выучить союзы\nстр. 78",
                            "weight": 2,
                            "dueDate": "2026-09-29T00:00:00",
                            "mark": {"mark": 5, "dutyMark": False},
                        }
                    ],
                }
            ],
        }
    ],
}


def test_student_id_comes_from_student_id_field():
    assert Student.from_raw({"studentId": 200, "nickName": "Иванова"}).id == 200


def test_diary_keeps_sgo_fields_and_8x_names():
    diary = Diary.from_raw(DIARY, {3: {"name": "Домашнее задание", "abbr": "ДЗ"}})
    assert diary.schedule is diary.days
    day = diary.days[0]
    assert day.date == datetime.date(2026, 9, 29)
    task = day.lessons[0].assignments[0]
    assert task.deadline == datetime.date(2026, 9, 29)
    assert task.weight == 2
    assert task.title.startswith("Выучить") and task.type_name == "Домашнее задание"


def test_ical_uses_local_time_and_escapes_text():
    ical = Diary.from_raw(DIARY, {3: {"name": "ДЗ"}}).to_ical()
    assert "DTSTART:20260929T080000\r\n" in ical
    assert "Z\r\nDTEND" not in ical
    # Перевод строки и запятая в тексте не ломают файл.
    assert "LOCATION:12\\, каб. русского" in ical
    assert "союзы\\nстр. 78" in ical


def test_models_that_client_constructs_have_from_raw():
    assert ShortSchool.from_raw({"name": "Школа", "id": 5}).id == 5
    message = Message.from_raw(
        {"id": 1, "subject": "Тема", "sent": "2026-09-01T10:51:34.99", "author": {"id": 2, "name": "Учитель"}}
    )
    assert message.sent.microsecond == 990000
    period = ReportingPeriod.from_filter_source({"value": "12", "title": "1 четверть"})
    assert (period.id, period.name) == (12, "1 четверть")


def test_ask_refuses_without_terminal(monkeypatch):
    class NoTty:
        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdin", NoTty())
    with pytest.raises(exceptions.MFAError):
        _ask("Код: ", need="код подтверждения")


async def test_import_session_network_error_is_not_session_expired(monkeypatch):
    ns = NetSchool("https://sgo.example.ru")

    async def down(*args, **kwargs):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(ns._http, "get", down)
    with pytest.raises(exceptions.ServerUnavailable):
        await ns.import_session('{"access_token": "t", "cookies": {}}')
    await ns._http.close()


async def test_event_listener_yields_announcements_and_marks():
    class Client:
        async def announcements(self):
            return [
                Announcement.from_raw(
                    {
                        "id": 1,
                        "name": "Собрание",
                        "description": "В 18:00",
                        "postDate": "2026-09-01T10:00:00",
                        "author": {"id": 3, "fio": "Иванова А. А.", "nickName": "Иванова"},
                    }
                )
            ]

        async def assigned_marks(self):
            return [
                AssignedMark(
                    subject="Физика",
                    assignment_type="Контрольная",
                    topic="Законы Ньютона",
                    lesson_date=datetime.date(2026, 9, 1),
                    mark_set_date=datetime.date(2026, 9, 2),
                    mark_text="5",
                    numeric_mark=5,
                    special_mark_code=None,
                    special_mark_description=None,
                )
            ]

    events = SignalREventListener(Client()).listen(poll_interval=0)
    first = await asyncio.wait_for(events.__anext__(), 1)
    second = await asyncio.wait_for(events.__anext__(), 1)
    await events.aclose()
    assert first.author == "Иванова А. А."
    assert (second.subject, second.mark, second.assignment_name) == ("Физика", "5", "Законы Ньютона")
