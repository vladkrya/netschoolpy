"""Тесты мобильного API на подставном транспорте (формы ответов — из захвата приложения 2.0.3)."""

import datetime
import json
from hashlib import md5
from urllib.parse import parse_qs

import httpx
import pytest

from netschoolpy import MobileNetSchool, published_at
from netschoolpy.exceptions import LoginError, SessionExpired
from netschoolpy.mobile import MobileRegion, MobileToken, password_hash

SITE = "https://sgo.example.ru"
MSK = datetime.timezone(datetime.timedelta(hours=3))

USERS = [{
    "id": 42, "nickName": "Иванов Иван", "isStudent": True, "isParent": False, "children": None,
    "organizations": [{"organization": {"id": 7, "name": "Школа"}, "classes": [{"classId": 1, "className": "9б"}], "isActive": True}],
}]
MEETINGS = [
    {"classmeetingId": 2, "assignmentId": [20], "order": 7, "scheduleTimeNumber": 1, "scheduleTimeRelay": 2,
     "day": "2026-09-28T00:00:00", "subjectName": "История", "subjectGroupId": 5,
     "startTime": "2026-09-28T14:10:00+03:00", "endTime": "2026-09-28T14:50:00+03:00",
     "teachers": [{"id": 3, "name": "Петрова"}], "lessonTheme": None, "roomName": "7", "attendance": None,
     "distanceMeetingId": None},
    {"classmeetingId": 1, "assignmentId": [10, 11], "order": 1, "scheduleTimeNumber": 1, "scheduleTimeRelay": 1,
     "day": "2026-09-28T00:00:00", "subjectName": "Алгебра", "subjectGroupId": 4,
     "startTime": "2026-09-28T08:30:00+03:00", "endTime": "2026-09-28T09:10:00+03:00",
     "teachers": [{"id": 1, "name": "Сидорова"}], "lessonTheme": "Дроби", "roomName": "4", "attendance": "Б",
     "distanceMeetingId": None},
]
ASSIGNMENTS = [
    {"classmeetingId": 1, "assignmentId": 10, "assignmentName": "№1", "result": 5.0, "duty": False, "comment": None,
     "assignmentTypeName": "Ответ на уроке", "assignmentTypeAbbr": "О", "weight": 1, "attachmentsExists": False,
     "dueDate": "2026-09-28T08:30:00+03:00", "resultDate": "2026-09-28T12:00:00+03:00",
     "assignmentDate": "2026-09-20T10:00:00+03:00"},
    {"classmeetingId": 1, "assignmentId": 11, "assignmentName": "Стр. 5", "result": None, "duty": False,
     "assignmentTypeName": "Домашнее задание", "assignmentTypeAbbr": "ДЗ", "weight": 1, "attachmentsExists": True,
     "dueDate": "2026-09-29T08:00:00+03:00", "resultDate": None, "assignmentDate": "2026-09-27T02:05:45+03:00"},
    {"classmeetingId": 2, "assignmentId": 20, "assignmentName": "§5", "result": None, "duty": False,
     "assignmentTypeName": "Домашнее задание", "assignmentTypeAbbr": "ДЗ", "weight": 1, "attachmentsExists": False,
     "dueDate": "2026-09-30T08:00:00+03:00", "resultDate": None, "assignmentDate": None},
]


def _form(request: httpx.Request) -> dict:
    return {k: v[0] for k, v in parse_qs(request.content.decode(), keep_blank_values=True).items()}


def _api(request: httpx.Request) -> httpx.Response:
    path = request.url.path.removeprefix("/api/mobile")
    if request.headers.get("Authorization") != "Bearer TOKEN":
        return httpx.Response(401)
    if path == "/users":
        return httpx.Response(200, json=USERS)
    if path == "/classmeetings":
        return httpx.Response(200, json=MEETINGS)
    if path == "/assignments":
        wanted = {int(i) for i in request.url.params.get_list("assignmentId")}
        return httpx.Response(200, json=[a for a in ASSIGNMENTS if a["assignmentId"] in wanted])
    if path == "/attachments":
        return httpx.Response(200, json=[{"attachmentId": 99, "assignmentId": 11, "fileName": "дз.docx"}])
    if path == "/attachments/99":
        return httpx.Response(200, content=b"DOCX")
    if path == "/mail/messages":
        return httpx.Response(200, json={"page": 1, "totalItems": 2, "items": [
            {"id": 1, "subject": "a", "sent": "2026-09-22T16:28:00", "author": {"id": 5, "name": "Учитель"}, "read": False},
            {"id": 2, "subject": "b", "sent": "2026-09-21T16:28:00", "author": {"id": 5, "name": "Учитель"}, "read": True},
        ]})
    if path == "/address-book":
        person = {"id": "p1", "name": "Учитель", "organizationName": None}
        return httpx.Response(200, json=[{"groupName": "Учителя", "recipients": [person],
                                          "subGroups": [{"groupName": "9б", "recipients": [person, {"id": "p2", "name": "Завуч"}]}]}])
    return httpx.Response(404)


async def _client() -> MobileNetSchool:
    ns = MobileNetSchool(SITE, transport=httpx.MockTransport(_api))
    await ns.login_with_token("TOKEN")
    return ns


def test_password_hash_matches_web_client_formula():
    inner = md5("пароль".encode("windows-1251")).hexdigest().encode()
    assert password_hash("пароль", "123") == md5(b"123" + inner).hexdigest()


def test_published_at():
    assert published_at({"assignmentDate": "2026-09-29T09:34:03+03:00"}) == datetime.datetime(2026, 9, 29, 9, 34, 3, tzinfo=MSK)
    assert published_at({}) is None


def test_region_url():
    region = MobileRegion.from_raw({"serverId": "ABC", "name": "X", "url": f"{SITE}/api/mobile"})
    assert region.url == SITE and region.server_id == "abc"


async def test_login_with_token_loads_profile():
    ns = await _client()
    assert [s.id for s in ns.students] == [42]
    assert ns.current_student.group_name == "9б"
    assert await ns.diary_init() == {
        "students": [{"studentId": 42, "nickName": "Иванов Иван", "className": "9б"}],
        "currentStudentId": 42,
    }


async def test_diary_maps_to_web_models():
    ns = await _client()
    diary = await ns.diary(datetime.date(2026, 9, 28))
    (day,) = diary.schedule
    first, second = day.lessons
    assert (first.subject, second.subject) == ("Алгебра", "История")  # по времени, а не по номеру
    assert first.teacher == "Сидорова" and first.topic == "Дроби" and first.absence == "Б"
    assert first.start == datetime.time(8, 30) and second.relay == 2
    mark, homework = first.assignments
    assert mark.mark == 5 and mark.mark_set_date == datetime.date(2026, 9, 28)
    assert homework.deadline == datetime.date(2026, 9, 29)
    assert homework.published_at == datetime.datetime(2026, 9, 27, 2, 5, 45, tzinfo=MSK)
    assert [a.name for a in homework.attachments] == ["дз.docx"]
    assert second.assignments[0].published_at is None
    assert await ns.download_attachment_as_bytes(99) == b"DOCX"


async def test_mail_compat():
    ns = await _client()
    page = await ns.mail_list()
    assert [(m.id, m.read, m.author) for m in page.entries] == [(1, False, "Учитель"), (2, True, "Учитель")]
    assert await ns.mail_unread() == [1]
    assert sorted(r.id for r in await ns.mail_recipients()) == ["p1", "p2"]


async def test_session_roundtrip_and_expiry():
    ns = await _client()
    exported = ns.export_session()
    assert json.loads(exported)["kind"] == "mobile"

    restored = MobileNetSchool(SITE, transport=httpx.MockTransport(_api))
    await restored.import_session(exported)
    assert restored.current_student.id == 42

    with pytest.raises(SessionExpired):
        await restored.import_session(json.dumps({"version": 1, "access_token": "x", "cookies": {}}))

    stale = MobileNetSchool(SITE, transport=httpx.MockTransport(_api))
    with pytest.raises(SessionExpired):
        await stale.login_with_token("OLD")
    assert await ns.is_alive()


async def test_password_login_runs_app_flow(monkeypatch):
    monkeypatch.setenv("NETSCHOOL_MOBILE_CLIENT_SECRET", "cs")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(f"{request.method} {request.url.host}{path}")
        if path == "/webapi/auth/login-state":
            return httpx.Response(200, json="state")
        if path == "/webapi/auth/getdata":
            return httpx.Response(200, json={"lt": "1", "ver": "2", "salt": "777"})
        if path == "/webapi/auth/login":
            form = _form(request)
            assert form["pw2"] == password_hash("secret", "777") and form["loginState"] == "state"
            assert form["scid"] == "7" and form["mobile"] == "1"
            return httpx.Response(200, json={"at": "AT", "entryPoint": "/app/school/main"})
        if path == "/webapi/mysettings/mobile/pincode":
            assert request.headers["at"] == "AT"
            return httpx.Response(200, json={"userCode": "123456"})
        if path == "/connect/token":
            form = _form(request)
            assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
            assert (form["device_code"], form["client_id"], form["client_secret"]) == ("123456", "parent-mobile", "cs")
            return httpx.Response(200, json={"access_token": "TOKEN", "refresh_token": "R", "expires_in": 5184000})
        if path.startswith("/api/mobile"):
            return _api(request)
        return httpx.Response(200, text="")

    ns = MobileNetSchool(SITE, transport=httpx.MockTransport(handler))
    await ns.login("user", "secret", 7)
    assert isinstance(ns.token, MobileToken) and not ns.token.expires_soon()
    assert calls.index("POST sgo.example.ru/webapi/auth/logout") < calls.index("POST identity.ir-tech.ru/connect/token")


async def test_password_login_conflict(monkeypatch):
    monkeypatch.setenv("NETSCHOOL_MOBILE_CLIENT_SECRET", "cs")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/webapi/auth/login-state":
            return httpx.Response(200, json="s")
        if request.url.path == "/webapi/auth/getdata":
            return httpx.Response(200, json={"lt": "1", "ver": "2", "salt": "3"})
        if request.url.path == "/webapi/auth/login":
            return httpx.Response(409, json={"message": "Неправильный пароль"})
        return httpx.Response(200, text="")

    with pytest.raises(LoginError, match="Неправильный пароль"):
        await MobileNetSchool(SITE, transport=httpx.MockTransport(handler)).login("u", "p", 7)


async def test_login_without_client_secret(monkeypatch):
    monkeypatch.delenv("NETSCHOOL_MOBILE_CLIENT_SECRET", raising=False)
    from netschoolpy.mobile import exchange_code

    with pytest.raises(LoginError, match="client_secret"):
        await exchange_code("1")


async def test_web_client_mobile_token_uses_pincode(monkeypatch):
    from netschoolpy import NetSchool, mobile

    ns = NetSchool(SITE)
    seen = {}

    async def fake_get(path, **kw):
        seen["path"] = path
        return httpx.Response(200, json={"subjectId": "s", "userCode": "555"})

    async def fake_exchange(code, **kw):
        seen["code"] = code
        return MobileToken(access_token="T", expires_at=datetime.datetime.now(datetime.timezone.utc))

    monkeypatch.setattr(ns, "_authed_get", fake_get)
    monkeypatch.setattr(mobile, "exchange_code", fake_exchange)
    token = await ns.mobile_token(client_secret="cs")
    assert token.access_token == "T"
    assert seen == {"path": "mysettings/mobile/pincode", "code": "555"}
    await ns._http.close()
