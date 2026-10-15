"""Маршруты мобильного API из приложения 2.3.1 (декомпиляция index.android.bundle).

Формы запросов — как в ``regionalApi`` приложения; GET-маршруты проверены на
живом сервере, POST с последствиями (ответ учителю, заказ справки) — только здесь.
"""

import datetime
import json

import httpx

from netschoolpy import MobileNetSchool

SITE = "https://sgo.example.ru"


def _mobile(handler) -> tuple[MobileNetSchool, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    ns = MobileNetSchool(SITE, transport=httpx.MockTransport(record))
    ns._user, ns._student_id, ns._school_id = {"id": 42}, 43, 7
    return ns, seen


async def test_answer_text_is_plain_body():
    ns, seen = _mobile(lambda r: httpx.Response(200))
    await ns.set_answer_text(5, "Ответ")
    await ns.set_answer_text(5, "")
    first, cleared = seen
    assert (first.method, first.url.path) == ("POST", "/api/mobile/assignments/5/answer/text")
    assert first.url.params["studentId"] == "43"
    assert first.headers["content-type"].startswith("text/plain")
    assert first.content.decode() == "Ответ" and cleared.content == b""


async def test_answer_files_upload_and_delete():
    ns, seen = _mobile(lambda r: httpx.Response(200, json={"attachmentId": 9}))
    assert await ns.upload_answer_file(5, "решение.pdf", b"PDF", description="  стр. 1  ") == {"attachmentId": 9}
    await ns.delete_answer_file(5, 9)
    upload, delete = seen
    assert upload.url.params["description"] == "стр. 1"
    assert b'name="file"' in upload.content and b"PDF" in upload.content
    assert (delete.method, delete.url.params["attachmentId"]) == ("DELETE", "9")


async def test_distance_meetings_batch_posts_ids():
    ns, seen = _mobile(lambda r: httpx.Response(200, json=[]))
    assert await ns.distance_meetings([1, 2]) == []
    assert await ns.distance_meetings([]) == []
    (batch,) = seen
    assert batch.method == "POST" and json.loads(batch.content) == {"distanceMeetingId": [1, 2]}


async def test_events_paging_and_dates():
    ns, seen = _mobile(lambda r: httpx.Response(200, json={"items": []}))
    await ns.events(page=2, start=datetime.date(2025, 1, 1), end=datetime.date(2025, 1, 31))
    params = seen[0].url.params
    assert (params["page"], params["pageSize"], params["startDate"], params["endDate"]) == ("2", "50", "2025-01-01", "2025-01-31")


async def test_order_parent_reference_body():
    ns, seen = _mobile(lambda r: httpx.Response(200))
    await ns.order_parent_reference(3)
    assert json.loads(seen[0].content) == {"studentId": 43, "schoolId": 7, "referenceTypeId": 3}


async def test_set_push_settings_merges_dict_with_current():
    # Сервер заменяет весь набор, поэтому частичная правка не должна стирать остальные коды.
    current = [{"name": "ДЗ", "code": "Homework", "enabled": True}, {"name": "Оценки", "code": "Result", "enabled": True}]
    ns, seen = _mobile(lambda r: httpx.Response(200, json=current if r.method == "GET" else None))
    await ns.set_push_settings({"Homework": False})
    await ns.set_push_settings([{"code": "Result", "enabled": False}])
    posts = [json.loads(r.content) for r in seen if r.method == "POST"]
    assert posts == [
        [{"code": "Homework", "enabled": False}, {"code": "Result", "enabled": True}],
        [{"code": "Result", "enabled": False}],
    ]


def test_lesson_keeps_distance_meeting_id():
    from netschoolpy.mobile import _lesson_from_mobile

    meeting = {"classmeetingId": 1, "day": "2026-10-05T00:00:00", "startTime": "2026-10-05T08:00:00+03:00",
               "endTime": "2026-10-05T08:40:00+03:00", "distanceMeetingId": 77}
    lesson = _lesson_from_mobile(meeting, [], {})
    assert (lesson.is_distance, lesson.distance_meeting_id) == (True, 77)
