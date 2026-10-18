"""9.6.1: код MFA уходит ровно на тот endpoint, который соответствует его типу."""

import httpx
import pytest

from netschoolpy import NetSchool, exceptions

BASE = "https://esia.gosuslugi.ru/aas/oauth2/api/login"


async def _verify(mfa_type, details_key, status=200, body=None):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, json=body or {"redirect_url": "https://sgo.example.ru/ok"})

    login_data = {"mfa_details": {"type": mfa_type, details_key: {"code_length": 6}}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await NetSchool("https://sgo.example.ru")._handle_esia_mfa(
            client, login_data, otp_callback=lambda t, info: "123456",
        )
    return result, seen


@pytest.mark.parametrize(("mfa_type", "details_key", "path"), [
    ("SMS", "otp_details", "otp"),
    ("TTP", "ttp_details", "totp"),
    ("TOTP", "ttp_details", "totp"),
    ("MAX", "otp_max_details", "otp-max"),
])
async def test_code_goes_to_its_own_endpoint_once(mfa_type, details_key, path):
    result, seen = await _verify(mfa_type, details_key)
    assert result == "https://sgo.example.ru/ok"
    assert len(seen) == 1
    assert str(seen[0].url) == f"{BASE}/{path}/verify?code=123456"


async def test_error_on_own_endpoint_is_not_retried_elsewhere():
    with pytest.raises(exceptions.MFAError, match="400"):
        await _verify("MAX", "otp_max_details", status=400, body={"error": "bad"})


async def test_missing_endpoint_names_the_url():
    with pytest.raises(exceptions.MFAError, match="otp-max/verify"):
        await _verify("MAX", "otp_max_details", status=404)
