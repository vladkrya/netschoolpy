"""Почта, отчёты и справочники 9.4 на подставном транспорте.

Формы запросов — из веб-интерфейса «Сетевого города» 5.56 (почтовый бандл) и
проверки на живом сервере: веб-API ждёт ``{"messageId": [...]}`` в JSON,
мобильное — поля multipart-формы ``messageIds``.
"""

import datetime
import json

import httpx
import pytest

from netschoolpy import MobileNetSchool, NetSchool, RecipientGroup
from netschoolpy.exceptions import NetSchoolError, ServerUnavailable
from netschoolpy.http import HttpSession
from netschoolpy.models import Report

SITE = "https://sgo.example.ru"

REPORT_FORM = {
    "filterSources": [
        {"filterId": "SID", "defaultValue": "583039", "items": [{"value": 583039, "title": "Иванов Иван"}]},
        {"filterId": "MarksType", "defaultValue": "T", "items": [
            {"value": "F", "title": "Итоговые отметки"}, {"value": "T", "title": "Тематические работы"}]},
        {"filterId": "TERMID", "defaultValue": "24588", "items": [
            {"value": -1, "title": "Произвольный интервал"}, {"value": 24588, "title": "1 четверть"}]},
        {"filterId": "period", "defaultValue": "2026-09-01T00:00:00.0000000 - 2026-11-04T00:00:00.0000000",
         "range": {"start": "2026-09-01T00:00:00", "end": "2026-11-04T00:00:00"}},
    ]
}


def _web(handler) -> tuple[NetSchool, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    ns = NetSchool(SITE)
    ns._http._client = httpx.AsyncClient(
        base_url=f"{SITE}/webapi",
        transport=httpx.MockTransport(record),
        event_hooks={"response": [HttpSession._check_status]},
    )
    ns._student_id, ns._school_id = 42, 7
    return ns, seen


def _body(request: httpx.Request):
    return json.loads(request.content)


# ─────────────────────────────── Веб: почта ───────────────────────────────


async def test_web_mail_send_uploads_files_and_posts_guids():
    def handler(request):
        if request.url.path == "/webapi/attachments":
            return httpx.Response(200, json="guid-1")
        if request.url.path == "/webapi/mail/messages":
            return httpx.Response(200, json={"messageId": 77})
        return httpx.Response(404)

    ns, seen = _web(handler)
    mid = await ns.mail_send("Тема", "Текст", ["p1"], cc=["p2"], attachments=[("дз.txt", b"x")])
    assert mid == 77
    upload, send = seen
    assert b'filename="' in upload.content and b"multipart/form-data" in upload.headers["content-type"].encode()
    assert _body(send) == {
        "subject": "Тема", "text": "Текст", "to": ["p1"], "cc": ["p2"], "bcc": [],
        "notify": False, "draft": False, "fileAttachmentGuids": ["guid-1"],
    }


async def test_web_mail_send_requires_recipient():
    ns, seen = _web(lambda r: httpx.Response(500))
    with pytest.raises(NetSchoolError):
        await ns.mail_send("Тема", "Текст", [])
    assert seen == []


@pytest.mark.parametrize(
    ("call", "path", "body"),
    [
        (lambda ns: ns.mail_mark_read([1, 2]), "mark-as", {"messageId": [1, 2], "read": True}),
        (lambda ns: ns.mail_mark_read([1], read=False), "mark-as", {"messageId": [1], "read": False}),
        (lambda ns: ns.mail_delete([1]), "moving", {"messageId": [1]}),
        (lambda ns: ns.mail_delete([1], permanent=True), "delete", {"messageId": [1]}),
        (lambda ns: ns.mail_restore([1]), "restore", {"messageId": [1]}),
    ],
)
async def test_web_mail_actions(call, path, body):
    ns, seen = _web(lambda r: httpx.Response(204))
    await call(ns)
    (request,) = seen
    assert request.url.path == f"/webapi/mail/messages/{path}"
    assert _body(request) == body


async def test_web_mail_list_search_and_order():
    ns, seen = _web(lambda r: httpx.Response(200, json={"rows": [], "page": 1, "totalItems": 0}))
    await ns.mail_list("Sent", search="контрольная", search_field="author", ascending=True)
    body = _body(seen[0])
    assert body["search"] == {"fieldId": "author", "text": "контрольная"}
    assert body["order"] == {"fieldId": "sent", "ascending": True}
    await ns.mail_list()
    assert _body(seen[1])["search"] is None


async def test_web_mail_recipients_group():
    ns, seen = _web(lambda r: httpx.Response(200, json=[{"id": "p1", "name": "Учитель"}]))
    await ns.mail_recipients()
    await ns.mail_recipients(RecipientGroup.TEACHERS)
    assert [r.url.params["group"] for r in seen] == ["1", "4"]


async def test_web_mail_draft():
    draft = {"id": 0, "subject": "Re: Тема", "text": "\r\n> цитата", "to": [{"id": "p1", "name": "Учитель"}],
             "fileAttachments": [], "mailBox": 0}
    ns, seen = _web(lambda r: httpx.Response(200, json=draft))
    result = await ns.mail_draft(5, "forward")
    assert seen[0].url.path == "/webapi/mail/messages/5/edit"
    assert seen[0].url.params["action"] == "forward"
    assert result.subject == "Re: Тема" and [r.id for r in result.to] == ["p1"] and result.cc == []


# ───────────────────────── Веб: отчёты и справочники ─────────────────────────


async def test_reports_list():
    groups = [{"id": "G", "title": "Итоги", "reports": [{"id": "StudentTotal", "title": "Отчёт", "path": ""}]}]
    ns, _ = _web(lambda r: httpx.Response(200, json=groups))
    (group,) = await ns.reports()
    assert group.title == "Итоги" and [r.id for r in group.reports] == ["StudentTotal"]


async def _report_selection(monkeypatch, *args, **kwargs):
    ns, _ = _web(lambda r: httpx.Response(200, json=REPORT_FORM))
    captured = {}

    async def fake_run(report_id, selected, *, timeout=None):
        captured["id"], captured["selected"] = report_id, selected
        return "<table><tr><th>Предмет</th><td>5</td></tr></table>"

    monkeypatch.setattr(ns, "_run_report", fake_run)
    report = await ns.report("StudentAverageMark", *args, **kwargs)
    return report, {s["filterId"]: (s["filterValue"], s["filterText"]) for s in captured["selected"]}


async def test_report_uses_defaults(monkeypatch):
    report, selected = await _report_selection(monkeypatch)
    assert selected == {
        "SID": ("583039", "Иванов Иван"),
        "MarksType": ("T", "Тематические работы"),
        "TERMID": ("24588", "1 четверть"),
        "period": ("2026-09-01T00:00:00.0000000 - 2026-11-04T00:00:00.0000000", ""),
    }
    assert report.tables() == [[["Предмет", "5"]]]


async def test_report_filters_by_title_value_and_dates(monkeypatch):
    _, selected = await _report_selection(
        monkeypatch, {"MarksType": "итоговые отметки", "TERMID": -1},
        start=datetime.date(2026, 9, 1), end=datetime.date(2026, 9, 15),
    )
    assert selected["MarksType"] == ("F", "Итоговые отметки")
    assert selected["TERMID"] == ("-1", "Произвольный интервал")
    assert selected["period"][0] == "2026-09-01T00:00:00.0000000 - 2026-09-15T00:00:00.0000000"


@pytest.mark.parametrize("filters", [{"TERMID": "5 четверть"}, {"SGID": 1}])
async def test_report_rejects_unknown_values(monkeypatch, filters):
    with pytest.raises(NetSchoolError):
        await _report_selection(monkeypatch, filters)


def test_report_tables_parse_nested_markup():
    html = """<table><tr><td>Заголовок</td></tr></table>
    <table class="table-print"><tr><th>№</th><th>Предмет</th></tr>
    <tr><td>1</td><td><b>Алгебра</b>&nbsp;</td></tr><tr></tr></table>"""
    assert Report(id="X", html=html).tables() == [
        [["Заголовок"]],
        [["№", "Предмет"], ["1", "Алгебра"]],
    ]


async def test_assignment_types():
    raw = [{"abbr": "ДЗ", "order": 15, "isActive": True, "id": 3, "name": "Домашнее задание"}]
    ns, seen = _web(lambda r: httpx.Response(200, json=raw))
    (kind,) = await ns.assignment_types(include_inactive=True)
    assert (kind.id, kind.abbr, kind.order, kind.is_active) == (3, "ДЗ", 15, True)
    assert seen[0].url.params["all"] == "true"


# ───────────────────────────── Мобильное API ─────────────────────────────


def _mobile(handler) -> tuple[MobileNetSchool, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    ns = MobileNetSchool(SITE, transport=httpx.MockTransport(record))
    ns._user, ns._student_id = {"id": 42}, 43
    return ns, seen


def _multipart_ids(request: httpx.Request) -> list[str]:
    content = request.content.decode()
    return [
        part.split("\r\n\r\n", 1)[1].split("\r\n", 1)[0]
        for part in content.split("--")
        if 'name="messageIds"' in part
    ]


@pytest.mark.parametrize(
    ("call", "path", "read"),
    [
        (lambda ns: ns.mail_mark_read([1, 2]), "mark-as", "true"),
        (lambda ns: ns.mail_mark_read([1, 2], read=False), "mark-as", "false"),
        (lambda ns: ns.mail_delete([1, 2]), "moving", None),
        (lambda ns: ns.mail_delete([1, 2], permanent=True), "delete", None),
        (lambda ns: ns.mail_restore([1, 2]), "restore", None),
    ],
)
async def test_mobile_mail_actions_send_multipart_ids(call, path, read):
    ns, seen = _mobile(lambda r: httpx.Response(200))
    await call(ns)
    (request,) = seen
    assert request.url.path == f"/api/mobile/mail/messages/{path}"
    assert request.url.params["userId"] == "42"
    assert request.url.params.get("read") == read
    assert _multipart_ids(request) == ["1", "2"]


async def test_mobile_mail_action_without_ids_is_noop():
    ns, seen = _mobile(lambda r: httpx.Response(500))
    await ns.mail_delete([])
    assert seen == []


async def test_mobile_mail_draft_and_server_message():
    def handler(request):
        if request.url.params["action"] == "replyAll":
            return httpx.Response(500, json={"message": "Ученики не могут отвечать всем!", "details": None})
        return httpx.Response(200, json={"id": 0, "subject": "Fw: Тема", "text": "", "to": []})

    ns, _ = _mobile(handler)
    assert (await ns.mail_draft(5, "forward")).subject == "Fw: Тема"
    with pytest.raises(ServerUnavailable, match="Ученики не могут отвечать всем"):
        await ns.mail_draft(5, "replyAll")


async def test_mobile_recipient_and_send_returns_id():
    def handler(request):
        if request.url.path.endswith("/get-by-userid"):
            return httpx.Response(200, json={"id": "p42", "name": "Иванов Иван", "organizationName": "Школа"})
        if request.url.path.endswith("/mail/messages"):
            return httpx.Response(200, json={"messageId": 9, "author": {}, "dateSent": "2026-10-06T21:00:00"})
        return httpx.Response(404)

    ns, seen = _mobile(handler)
    me = await ns.recipient(42)
    assert (me.id, me.name) == ("p42", "Иванов Иван")
    assert seen[0].url.params["userId"] == "42"
    assert await ns.mail_send("Тема", "Текст", [me.id]) == 9


async def test_mobile_period_events_types():
    ns, seen = _mobile(lambda r: httpx.Response(200, json={"events": []}))
    await ns.period_events([5], types=["ResultInfo", "TermTotalInfo"])
    params = seen[0].url.params
    assert params.get_list("Types") == ["ResultInfo", "TermTotalInfo"]
    assert params.get_list("SubjectGroupId") == ["5"]
