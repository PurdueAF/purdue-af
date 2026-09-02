"""Tests for errors.py and shared.prom_query — the one failure vocabulary.

What matters here is not the exact prose but the contract every tool relies
on: a failure names the backend, carries the backend's own reason, ends with
a next step, and is never confused with an empty-but-successful answer.
"""

import errors
import httpx
import pytest
import respx
import shared

PROM = "http://prom:9090"


def _resp(status, *, text=None, json=None, headers=None):
    return httpx.Response(
        status,
        text=text,
        json=json,
        headers=headers,
        request=httpx.Request("GET", "http://x/"),
    )


# ── describe_exception ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "exc, expected",
    [
        (httpx.ConnectTimeout(""), "connection timed out"),
        (httpx.ReadTimeout(""), "timed out waiting for a response"),
        (httpx.PoolTimeout(""), "timed out waiting for a response"),
        (httpx.ConnectError(""), "connection refused"),
        (
            httpx.ConnectError("[Errno 111] refused"),
            "connection failed ([Errno 111] refused)",
        ),
        (
            httpx.RemoteProtocolError("x"),
            "the connection was dropped before a response arrived",
        ),
        (RuntimeError("odd"), "RuntimeError: odd"),
        (RuntimeError(""), "RuntimeError"),
    ],
)
def test_describe_exception_never_yields_an_empty_reason(exc, expected):
    assert errors.describe_exception(exc) == expected


# ── response_detail ───────────────────────────────────────────────────────────


def test_response_detail_prefers_the_backends_message_field():
    assert errors.response_detail(_resp(422, json={"message": "one cluster only"})) == (
        "one cluster only"
    )
    assert (
        errors.response_detail(_resp(400, json={"error": "bad query"})) == "bad query"
    )
    assert (
        errors.response_detail(
            _resp(500, json={"error": {"message": "nested", "code": 7}})
        )
        == "nested"
    )


def test_response_detail_strips_html_and_truncates():
    html = "<html><body><h1>503 Service Unavailable</h1><p>hub   restarting</p></body></html>"
    assert errors.response_detail(_resp(503, text=html)) == (
        "503 Service Unavailable hub restarting"
    )
    assert len(errors.response_detail(_resp(500, text="x" * 1000), limit=50)) == 50


def test_response_detail_handles_empty_and_non_json_bodies():
    assert errors.response_detail(_resp(500, text="")) == ""
    assert (
        errors.response_detail(_resp(500, text="plain  words\n here"))
        == "plain words here"
    )
    assert errors.json_body(_resp(200, text="not json")) is None
    assert errors.json_body(_resp(200, json={"a": 1})) == {"a": 1}


# ── message builders ──────────────────────────────────────────────────────────


def test_unreachable_names_service_reason_and_next_step():
    out = errors.unreachable("gateway 'k8s'", httpx.ConnectError("down"))
    assert out.startswith(
        "Error: gateway 'k8s' unreachable — connection failed (down)."
    )
    assert out.endswith(errors.RETRY_LATER)

    custom = errors.unreachable(
        "JupyterHub API", httpx.ReadTimeout(""), next_step="Nothing changed."
    )
    assert custom == (
        "Error: JupyterHub API unreachable — timed out waiting for a response. "
        "Nothing changed."
    )


def test_http_error_explains_5xx_and_includes_the_body():
    out = errors.http_error(
        "JupyterHub API", _resp(503, text="hub down"), action="start the session"
    )
    assert out.startswith(
        "Error: JupyterHub API returned HTTP 503 while trying to start the session "
        "(it is down, restarting, or overloaded) — hub down."
    )
    assert out.endswith(errors.RETRY_LATER)

    out = errors.http_error("Loki", _resp(500, text=""))
    assert out.startswith("Error: Loki returned HTTP 500 (it hit an internal error).")


def test_http_error_4xx_has_no_retry_advice_by_default():
    out = errors.http_error("gateway 'k8s'", _resp(418, json={"message": "teapot"}))
    assert out == "Error: gateway 'k8s' returned HTTP 418 — teapot."


def test_malformed_response_and_argument_and_bug_messages():
    out = errors.malformed_response(
        "gateway 'k8s'", _resp(201, text="<html>oops</html>"), "a cluster record"
    )
    assert "returned HTTP 201 but the response was not a cluster record — oops" in out
    assert "report it to AF support" in out

    out = errors.invalid_arguments(
        "scale_dask_cluster", ValueError("n_workers\n  must be int")
    )
    assert out.startswith(
        "Error: scale_dask_cluster was called with invalid arguments — n_workers must be int."
    )
    assert "argument names and types" in out

    out = errors.unexpected_failure("create_dask_cluster", KeyError("name"))
    assert out.startswith(
        "Error: create_dask_cluster failed unexpectedly (KeyError: 'name')."
    )
    assert "not in the request" in out
    assert "AF support" in out


# ── shared.prom_query ─────────────────────────────────────────────────────────


@respx.mock
async def test_prom_query_distinguishes_problems_from_empty_results():
    async with httpx.AsyncClient() as client:
        route = respx.get(f"{PROM}/api/v1/query")

        route.mock(side_effect=httpx.ConnectError("down"))
        rows, problem = await shared.prom_query(client, PROM, "up")
        assert rows == [] and problem == "is unreachable — connection failed (down)"

        route.respond(422, json={"status": "error", "error": "bad_data: parse error"})
        rows, problem = await shared.prom_query(client, PROM, "up")
        assert rows == [] and problem == "returned HTTP 422 — bad_data: parse error"

        route.respond(200, text="<html>login</html>")
        rows, problem = await shared.prom_query(client, PROM, "up")
        assert rows == [] and "not a query result" in problem

        route.respond(200, json={"data": {"result": []}})
        assert await shared.prom_query(client, PROM, "up") == ([], None)

        route.respond(
            200, json={"data": {"result": [{"metric": {}, "value": [0, "2"]}, "junk"]}}
        )
        rows, problem = await shared.prom_query(client, PROM, "up")
        assert rows == [{"metric": {}, "value": [0, "2"]}] and problem is None
        assert shared.prom_scalar(rows) == 2.0
        assert shared.prom_vector(rows) == [({}, 2.0)]


def test_prom_scalar_and_vector_tolerate_malformed_rows():
    assert shared.prom_scalar([]) is None
    assert shared.prom_scalar([{"value": [0, "nope"]}]) is None
    assert shared.prom_vector(
        [{"value": [0, "nope"]}, {"metric": {"a": "b"}, "value": [0, "1"]}]
    ) == [({"a": "b"}, 1.0)]
