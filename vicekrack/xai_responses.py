"""Step 50: bounded xAI Responses transport (POST https://api.x.ai/v1/responses).

Used by the Grok research request (with the web_search tool) and the Grok script writer. Both ask
for structured JSON output (text.format json_schema). Official reference: docs.x.ai, Responses API,
Search Tools and Structured Outputs (October 2026). The output text is the `output_text` content of
the `message` item in `output`.

One request, fixed URL, no redirects, no ambient proxies, no retries. The key is read from
XAI_API_KEY at call time and sent only in the Authorization header; it is never returned, logged or
stored. Failures are fixed codes only:

  xai_rejected           the API answered 4xx: the request was refused, nothing was generated
  xai_not_sent           the connection was never made (DNS failure, refused)
  xai_transport_failed   anything else (timeout, reset, 5xx, oversize): the outcome is UNKNOWN and
                         the caller must treat the request as possibly billed
"""

import json
import os
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from jsonschema import Draft202012Validator

from .errors import NetworkError

RESPONSES_URL = "https://api.x.ai/v1/responses"
REJECTED_STATUSES = (400, 401, 403, 404, 413, 422, 429)
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class XAIRejected(NetworkError):
    def __init__(self, http_status):
        super().__init__("xai_rejected", f"xAI refused the request (HTTP {http_status}); nothing was generated.")
        self.http_status = http_status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def credentials_configured():
    return bool(os.environ.get("XAI_API_KEY", "").strip())


def transport(body, *, timeout_seconds):
    """Send one Responses request. Returns the decoded JSON response object."""
    key = os.environ.get("XAI_API_KEY", "").strip()
    if not key:
        raise NetworkError("missing_xai_credential", "Set XAI_API_KEY in the window that starts ViceKrack.")
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json", "Accept": "application/json"}
    deadline = time.monotonic() + timeout_seconds
    try:
        request = Request(RESPONSES_URL, data=json.dumps(body).encode(), headers=headers, method="POST")
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                raise ValueError
            chunks, total = [], 0
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError
                chunk = response.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise ValueError
        document = json.loads(b"".join(chunks).decode("utf-8"))
        if not isinstance(document, dict):
            raise ValueError
        return document
    except HTTPError as error:
        status = error.code
        try:
            error.close()                             # the body is never read: it may echo the request
        except Exception:
            pass
        if status in REJECTED_STATUSES:
            raise XAIRejected(status) from None
        raise NetworkError("xai_transport_failed", "The xAI request failed; its outcome is unknown.") from None
    except URLError as error:
        if isinstance(error.reason, (socket.gaierror, ConnectionRefusedError)):
            raise NetworkError("xai_not_sent", "Could not connect to xAI; the request was not sent.") from None
        raise NetworkError("xai_transport_failed", "The xAI request failed; its outcome is unknown.") from None
    except NetworkError:
        raise
    except Exception:
        raise NetworkError("xai_transport_failed", "The xAI request failed; its outcome is unknown.") from None


def structured_body(*, model, instructions, payload, schema, schema_name, max_output_tokens, tools=None):
    """The exact request body (no key) for a structured-output Responses call."""
    body = {"model": model, "store": False, "max_output_tokens": max_output_tokens,
            "input": [{"role": "system", "content": instructions},
                      {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            "text": {"format": {"type": "json_schema", "name": schema_name, "strict": True, "schema": schema}}}
    if tools:
        body["tools"] = tools
    return body


def parse_structured(response, schema):
    """(result, usage) from a completed Responses object, validated against schema."""
    if response.get("status") == "incomplete":
        raise NetworkError("xai_incomplete", "Grok could not finish within its output limit.")
    if response.get("status") not in (None, "completed"):
        raise NetworkError("xai_invalid_response", "Grok did not return a completed response.")
    chunks = []
    for item in response.get("output") or []:
        if isinstance(item, dict) and item.get("type") == "message":
            for content in item.get("content") or []:
                if isinstance(content, dict) and content.get("type") == "refusal":
                    raise NetworkError("xai_refusal", "Grok declined the request.")
                if isinstance(content, dict) and content.get("type") == "output_text" and isinstance(content.get("text"), str):
                    chunks.append(content["text"])
    try:
        result = json.loads("".join(chunks))
        if not Draft202012Validator(schema).is_valid(result):
            raise ValueError
    except (ValueError, TypeError):
        raise NetworkError("xai_invalid_response", "Grok's answer did not match the requested format.") from None
    return result, usage_of(response)


def usage_of(response):
    """Token and tool counts reported by the API (missing values become 0)."""
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}

    def number(value):
        return value if isinstance(value, int) and value >= 0 else 0

    tools = usage.get("server_side_tool_usage_details") or usage.get("server_side_tool_usage") or {}
    searches = 0
    if isinstance(tools, dict):
        searches = sum(number(v) for k, v in tools.items() if "search" in str(k))
    return {"input_tokens": number(usage.get("input_tokens")), "output_tokens": number(usage.get("output_tokens")),
            "search_calls": searches}


def cost_usd(usage, prices):
    """Approximate cost from reported usage and the configured price table (never billed by us)."""
    return round(usage["input_tokens"] / 1e6 * prices["input_per_million"]
                 + usage["output_tokens"] / 1e6 * prices["output_per_million"]
                 + usage["search_calls"] / 1000 * prices["web_search_per_thousand"], 4)


class XAIScriptProvider:
    """The Grok script writer for the Creator (adapter "xai"): one structured request, no tools."""

    def __init__(self, transport_fn=None, timeout_seconds=120):
        self.transport_fn, self.timeout_seconds = transport_fn, timeout_seconds
        self.last_usage = None

    def generate_structured(self, *, instructions, payload, schema, schema_name, model):
        if not isinstance(model, str) or not model.strip():
            raise NetworkError("missing_model", "Set the Creator's execution.model for xAI.")
        body = structured_body(model=model, instructions=instructions, payload=payload, schema=schema,
                               schema_name=schema_name, max_output_tokens=3000)
        response = (self.transport_fn or transport)(body, timeout_seconds=self.timeout_seconds)
        result, self.last_usage = parse_structured(response, schema)
        return result
