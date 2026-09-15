"""Redaction contract for text that can reach a model, a terminal or a log.

Every value below is fictional. The point of each case is the pair it asserts: the
secret or personal datum disappears, and the part that makes the error actionable
(status, Deputy's wording, the failing host) survives.
"""

from __future__ import annotations

import time

import pytest

from deputy_mcp.errors import DeputyAPIError
from deputy_mcp.sanitize import REDACTED, SNIPPET_LIMIT, redact, snippet

_ACCESS = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
_REFRESH = "r9q8p7o6n5m4l3k2j1i0h9g8f7e6d5c4"


def test_bearer_header_value_is_redacted() -> None:
    out = redact(f"upstream said: Authorization: Bearer {_ACCESS}")
    assert _ACCESS not in out
    assert "Bearer" in out


def test_json_token_fields_are_redacted_but_error_wording_survives() -> None:
    body = (
        '{"error":{"code":400,"message":"refresh token expired"},'
        f'"access_token":"{_ACCESS}","refresh_token":"{_REFRESH}",'
        '"client_secret":"fictional-client-secret"}'
    )
    out = redact(body)
    for secret in (_ACCESS, _REFRESH, "fictional-client-secret"):
        assert secret not in out
    assert '"code":400' in out
    assert "refresh token expired" in out


def test_form_encoded_authorization_code_and_state_are_redacted() -> None:
    out = redact(
        "grant_type=authorization_code&code=Xy7-fictional-auth-code-123&state=Zq9abcdefghij1234567"
    )
    assert "Xy7-fictional-auth-code-123" not in out
    assert "Zq9abcdefghij1234567" not in out
    assert "grant_type=authorization_code" in out


def test_short_numeric_code_is_kept() -> None:
    assert redact('{"code": 403}') == '{"code": 403}'


def test_calendar_feed_url_path_is_redacted_but_host_is_kept() -> None:
    url = "https://cloud-nine-cafe.eu.deputy.com/exec/ical/k3y5ecr3tfeedtoken/My_Roster.ics"
    out = redact(f"could not fetch {url}")
    assert "k3y5ecr3tfeedtoken" not in out
    assert "cloud-nine-cafe.eu.deputy.com" in out


def test_url_query_and_userinfo_are_dropped() -> None:
    out = redact("GET https://user:pa55word@example.org/api/v1/me?token=abc123&x=1 failed")
    assert "pa55word" not in out
    assert "abc123" not in out
    assert "https://example.org/api/v1/me?" in out


def test_email_addresses_are_redacted() -> None:
    out = redact("Employee alex.rivera@example.com already rostered")
    assert "alex.rivera@example.com" not in out
    assert "already rostered" in out


def test_jwt_and_opaque_tokens_are_redacted_but_numeric_ids_are_kept() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlLWZpY3Rpb25hbA"
    out = redact(f"token {jwt} and opaque {_ACCESS} for roster 900123")
    assert jwt not in out
    assert _ACCESS not in out
    assert "900123" in out


def test_known_secrets_are_masked_whatever_their_shape() -> None:
    out = redact("echo: tiny-secret", secrets=("tiny-secret",))
    assert out == f"echo: {REDACTED}"


def test_our_own_hints_are_left_intact() -> None:
    hint = (
        "Register an app at https://once.deputy.com/my/oauth_clients (redirect "
        "http://localhost:8823/callback), set DEPUTY_OAUTH_CLIENT_ID and "
        "DEPUTY_OAUTH_CLIENT_SECRET, then run 'deputy-mcp login'."
    )
    assert redact(hint) == hint


def test_snippet_collapses_whitespace_strips_control_chars_and_truncates() -> None:
    body = "line one\n\n\tline two\x07 " + "x" * 1000
    out = snippet(body)
    assert out is not None
    assert "\n" not in out and "\x07" not in out
    assert out.startswith("line one line two")
    assert len(out) == SNIPPET_LIMIT + len("...")


def test_snippet_summarises_html_error_pages() -> None:
    assert snippet("<!DOCTYPE html><html><body>502 Bad Gateway</body></html>") == (
        "[HTML error page omitted]"
    )


@pytest.mark.parametrize("body", [None, "", "   \n"])
def test_snippet_of_empty_body_is_none(body: str | None) -> None:
    assert snippet(body) is None


def test_api_error_never_carries_a_raw_secret() -> None:
    err = DeputyAPIError(
        "Deputy API returned HTTP 500.",
        status_code=500,
        body=f'{{"message":"boom","access_token":"{_ACCESS}","email":"jo.murphy@example.com"}}',
    )
    rendered = str(err)
    assert _ACCESS not in rendered
    assert "jo.murphy@example.com" not in rendered
    assert "boom" in rendered


def test_hostile_megabyte_body_is_redacted_in_bounded_time() -> None:
    # Patterns that make the email and opaque-token regexes backtrack badly when they
    # scan a long body; only the head of a body is ever examined.
    for body in ("@" + "ab." * 400_000 + "1", "a" * 1_000_000, "x@" * 500_000):
        started = time.perf_counter()
        out = snippet(body)
        assert time.perf_counter() - started < 1.0
        assert out is not None and out.endswith("...")


def test_known_secret_straddling_the_scan_limit_is_still_masked() -> None:
    secret = "k" * 40
    body = "x " * 598 + secret + " tail"
    out = snippet(body, secrets=(secret,), limit=2000)
    assert out is not None
    assert "kkkkkkkk" not in out
