"""Тесты клиента «Госуслуги Моя школа» на подставном транспорте."""

import asyncio
import datetime as dt
import json

import httpx
import pytest

from myschoolpy import MySchool, exceptions, interval_id, week_intervals

SID = "b63e8aad-0000-0000-0000-b3026ba5cbd9"

STUDENT = {
    "student_id": SID, "student_first_name": "Иван", "student_last_name": "Иванов",
    "student_middle_name": "Иванович", "student_region": "77", "student_region_active": True,
    "student_oid": 1, "mdm_id": "m", "trusted": True, "disabled": False, "permission_mark": True,
}
LESSON = {
    "lesson_id": "1", "subject_name": "Физика", "start_datetime": "2026-10-28 08:00:00.0",
    "end_datetime": "2026-10-28 08:45:00.0", "room": None, "lastname": "Петрова",
    "firstname": "Анна", "patronymic": "Сергеевна", "is_distant": False, "skipping_id": None,
}
MARK = {
    "marks_id": "9", "subject_name": "Алгебра", "mark_value1": "5", "mark_value2": None,
    "mark_date": "2026-10-27", "weight": None, "create_datetime": "2026-10-27 12:00:00.0",
}
HW = {
    "homeworks_id": "h1", "subject_name": "Физика", "description": "§12",
    "issue_date": "2026-10-26", "plan_ready_date": "2026-10-29 00:00:00.0", "materials": [],
}


def make(handler, **kwargs) -> MySchool:
    ms = MySchool(not_ready_delay=0, **kwargs)
    ms._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://www.gosuslugi.ru"
    )
    return ms


def datamart_handler(rows_by_type, *, not_ready_first=0):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/datamart"):
            calls["n"] += 1
            if calls["n"] <= not_ready_first:
                return httpx.Response(202, json=[])
            body = json.loads(request.content)
            return httpx.Response(200, json=[
                {"interval_id": b["interval_id"], "obj_type": b["obj_type"],
                 "student_id": b["student_id"], "data_status": "snapshot",
                 "data": rows_by_type[b["obj_type"]]}
                for b in body
            ])
        if "customservices" in request.url.path:
            return httpx.Response(200, json={"homeworks": [
                {"interval_id": "442026", "homework_id": "h1", "completed": True}]})
        return httpx.Response(404)

    handler.calls = calls
    return handler


def test_interval_id_is_iso_week():
    assert interval_id(dt.date(2026, 10, 28)) == "442026"
    assert interval_id(dt.date(2026, 9, 7)) == "372026"
    assert interval_id(dt.date(2027, 1, 1)) == "532026"


def test_week_intervals_cover_span():
    assert week_intervals(dt.date(2026, 10, 28), dt.date(2026, 11, 3)) == ["442026", "452026"]


async def test_students_parses_aliases():
    def handler(request):
        assert request.url.params["role"] == "student"
        return httpx.Response(200, json=[STUDENT])

    async with make(handler) as ms:
        [student] = await ms.students()
    assert student.id == SID and student.full_name == "Иванов Иван Иванович"


async def test_lessons_filtered_and_typed():
    handler = datamart_handler({"student_lessons": [
        LESSON, {**LESSON, "lesson_id": "2", "start_datetime": "2026-11-05 08:00:00.0"}]})
    async with make(handler) as ms:
        lessons = await ms.lessons(SID, dt.date(2026, 10, 28))
    assert [x.id for x in lessons] == ["1"]
    assert lessons[0].teacher == "Петрова Анна Сергеевна"
    assert lessons[0].start == dt.datetime(2026, 10, 28, 8, 0)


async def test_marks_value_and_number():
    async with make(datamart_handler({"student_marks": [MARK]})) as ms:
        [mark] = await ms.marks(SID, dt.date(2026, 10, 26))
    assert mark.value == "5" and mark.number == 5 and mark.date == dt.date(2026, 10, 27)


async def test_homeworks_merge_completed_flag():
    async with make(datamart_handler({"student_homeworks_materials": [HW]})) as ms:
        [hw] = await ms.homeworks(SID, dt.date(2026, 10, 28))
    assert hw.description == "§12" and hw.completed is True
    assert hw.plan_ready_date == dt.date(2026, 10, 29)


async def test_datamart_retries_while_202():
    handler = datamart_handler({"student_marks": [MARK]}, not_ready_first=2)
    async with make(handler) as ms:
        await ms.marks(SID, dt.date(2026, 10, 26))
    assert handler.calls["n"] == 3


async def test_datamart_not_ready_raises():
    handler = datamart_handler({"student_marks": []}, not_ready_first=99)
    async with make(handler, not_ready_retries=1) as ms:
        with pytest.raises(exceptions.DataNotReady):
            await ms.marks(SID, dt.date(2026, 10, 26))


@pytest.mark.parametrize("status, exc", [
    (401, exceptions.SessionExpired), (403, exceptions.AccessDenied),
    (404, exceptions.NotFound), (500, exceptions.ServerUnavailable),
])
async def test_http_errors_mapped(status, exc):
    async with make(lambda r: httpx.Response(status)) as ms:
        with pytest.raises(exc):
            await ms.current_year()


async def test_login_with_cookies_checks_session():
    seen = {}

    def handler(request):
        seen["cookie"] = request.headers.get("cookie")
        return httpx.Response(200, json={"current_year": "2026"})

    async with make(handler) as ms:
        await ms.login_with_cookies("a=1; b=2")
        assert "a=1" in seen["cookie"] and "b=2" in seen["cookie"]
        assert json.loads(ms.export_session()) == {"a": "1", "b": "2"}

    async with make(lambda r: httpx.Response(401)) as ms:
        with pytest.raises(exceptions.SessionExpired):
            await ms.login_with_cookies({"a": "1"})


async def test_empty_cookie_string_rejected():
    async with make(lambda r: httpx.Response(200, json={})) as ms:
        with pytest.raises(exceptions.MySchoolError):
            await ms.login_with_cookies("   ")


async def test_keepalive_reports_cookies_then_expiry():
    calls = {"n": 0}
    updates, expired = [], []

    def handler(request):
        calls["n"] += 1
        # 1-й запрос — проверка входа, 2-й — продление, дальше сессию «отозвали».
        if calls["n"] >= 3:
            return httpx.Response(401)
        return httpx.Response(200, json={"current_year": "2026"},
                              headers={"set-cookie": "a=2; Path=/"} if calls["n"] == 2 else {})

    ms = make(handler, keepalive_interval=0.01, on_session_update=updates.append,
              on_session_expired=lambda: expired.append(True))
    async with ms:
        await ms.login_with_cookies({"a": "1"})
        for _ in range(100):
            if expired:
                break
            await asyncio.sleep(0.01)
    assert expired == [True]
    assert json.loads(updates[0]) == {"a": "2"}


async def test_keepalive_stops_on_close():
    ms = make(lambda r: httpx.Response(200, json={"current_year": "2026"}), keepalive_interval=60)
    await ms.login_with_cookies({"a": "1"})
    task = ms._keepalive_task
    await ms.close()
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()
