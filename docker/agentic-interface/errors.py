"""Uniform failure reporting for the agentic interface.

Every failure a tool reports is built here so that all of them answer the
same three questions — what was attempted, why it failed (as the backend
reported it, not as we guessed), and what to do next — and so that no
failure is ever downgraded into "no data", "not ready" or an empty list.
A user reading a tool result must always be able to tell a facility that is
quiet from a facility that could not be asked.

Failures are *raised*, as subclasses of the MCP SDK's ``ToolError``, never
returned as strings: the SDK then delivers them on the protocol's own error
channel (``isError: true``), so every client recognises a failure without
parsing prose, while the message still carries the diagnosis. The subclass
says whose fault it was, which is also the metrics label for the call.

Conventions the messages keep:

* they start with ``Error:`` and name the backend in the user's terms
  ("JupyterHub", "gateway 'k8s'", "the monitoring system"), never an
  internal hostname;
* transport failures say ``unreachable``, HTTP failures ``returned HTTP``.
"""

import json
import re
from typing import Any

import httpx
from mcp.server.fastmcp.exceptions import ToolError


class Failure(ToolError):
    """A failed tool call, delivered on MCP's own error channel.

    ``outcome`` is the label recorded in purdue_af_mcp_tool_calls_total.
    """

    outcome = "user_error"


class UserError(Failure):
    """The request itself cannot succeed as made: bad argument, no such
    cluster, a spawn that was never started."""

    outcome = "user_error"


class AuthError(Failure):
    """The credential was refused for the attempted action."""

    outcome = "auth_error"


class UpstreamError(Failure):
    """A backend the tool depends on could not be used."""

    outcome = "upstream_error"


class ServiceFault(Failure):
    """A fault in this service — never the user's, always reportable."""

    outcome = "exception"


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Status → why a backend answered that way, in plain words. Anything else
# 5xx is an internal error of the backend itself.
_STATUS_HINTS = {
    502: "it is down or restarting behind its proxy",
    503: "it is down, restarting, or overloaded",
    504: "it did not answer in time",
}

RETRY_LATER = (
    "Try again in a minute; if it keeps failing, get_facility_health shows "
    "whether the facility itself is degraded."
)


def describe_exception(exc: BaseException) -> str:
    """Plain-language description of a failed outbound request.

    ``str()`` of an httpx timeout is usually empty, and ``ConnectError`` reads
    like an errno dump — neither tells a user what happened.
    """
    if isinstance(exc, httpx.ConnectTimeout):
        return "connection timed out"
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return "timed out waiting for a response"
    if isinstance(exc, httpx.RemoteProtocolError):
        return "the connection was dropped before a response arrived"
    detail = str(exc).strip()
    if isinstance(exc, httpx.ConnectError):
        return f"connection failed ({detail})" if detail else "connection refused"
    name = type(exc).__name__
    return f"{name}: {detail}" if detail else name


def response_detail(resp: httpx.Response, limit: int = 300) -> str:
    """The most useful sentence in an error response body, never raising.

    JSON bodies yield their message field (JupyterHub, Dask Gateway, Loki and
    Prometheus all use one of the keys below); HTML error pages from proxies
    are reduced to their text; anything else is truncated raw text.
    """
    try:
        text = resp.text or ""
    except Exception:
        return ""
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        for key in ("message", "error", "detail", "reason", "error_description"):
            value = payload.get(key)
            if isinstance(value, dict):
                value = value.get("message")
            if isinstance(value, str) and value.strip():
                return _WS_RE.sub(" ", value.strip())[:limit]
    if "<" in text and ">" in text:
        text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()[:limit]


def json_body(resp: httpx.Response) -> Any:
    """Decoded JSON body, or None when the body is not JSON."""
    try:
        return resp.json()
    except ValueError:
        return None


def _finish(message: str, next_step: str) -> str:
    message = message.rstrip(" .") + "."
    return f"{message} {next_step}" if next_step else message


def unreachable(
    service: str, exc: BaseException, *, next_step: str = ""
) -> UpstreamError:
    """``service`` could not be contacted at all."""
    return UpstreamError(
        _finish(
            f"Error: {service} unreachable — {describe_exception(exc)}",
            next_step or RETRY_LATER,
        )
    )


def http_error(
    service: str, resp: httpx.Response, *, action: str = "", next_step: str = ""
) -> UpstreamError:
    """``service`` answered, but with a status the caller could not use.

    Auth and not-found statuses mean something different per backend, so
    callers translate those themselves before falling back to this.
    """
    code = resp.status_code
    message = f"Error: {service} returned HTTP {code}"
    if action:
        message += f" while trying to {action}"
    hint = _STATUS_HINTS.get(code) or (
        "it hit an internal error" if code >= 500 else ""
    )
    if hint:
        message += f" ({hint})"
    detail = response_detail(resp)
    if detail:
        message += f" — {detail}"
    if not next_step and code >= 500:
        next_step = RETRY_LATER
    return UpstreamError(_finish(message, next_step))


def malformed_response(
    service: str, resp: httpx.Response, expected: str
) -> UpstreamError:
    """``service`` answered successfully but not in the shape we rely on."""
    detail = response_detail(resp, limit=120)
    message = (
        f"Error: {service} returned HTTP {resp.status_code} but the response "
        f"was not {expected}"
    )
    if detail:
        message += f" — {detail}"
    return UpstreamError(
        _finish(
            message,
            "This is a fault between the agentic interface and the facility, not "
            "in the request; report it to AF support if it persists.",
        )
    )


def invalid_arguments(tool: str, exc: BaseException) -> UserError:
    """The caller's arguments did not fit the tool's signature."""
    detail = _WS_RE.sub(" ", str(exc)).strip()
    return UserError(
        _finish(
            f"Error: {tool} was called with invalid arguments — {detail}",
            "Check the argument names and types in the tool description and call "
            "it again.",
        )
    )


def unexpected_failure(tool: str, exc: BaseException) -> ServiceFault:
    """An exception no tool handled — a fault in this service, never the user's."""
    detail = str(exc).strip() or type(exc).__name__
    return ServiceFault(
        _finish(
            f"Error: {tool} failed unexpectedly ({type(exc).__name__}: {detail})",
            "This is a fault in the AF agentic interface, not in the request — "
            "report it to AF support with the tool name and the time it happened.",
        )
    )
