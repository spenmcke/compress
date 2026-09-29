"""Minimal OpenAI-compatible client for the released cmprs compressor."""

from __future__ import annotations

from dataclasses import dataclass
import errno
import json
import random
import re
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import Config, load_api_key

RETRYABLE_STATUSES = frozenset({502, 503, 504})
SAFE_ERROR_CODES = frozenset({
    "context_length_exceeded", "invalid_request_error", "model_not_found",
    "rate_limit_exceeded", "server_error", "BadRequestError",
})


@dataclass
class RequestState:
    """Counters shared by transport retries and corrective model requests."""

    request_id: str | None = None
    deadline: float | None = None
    attempts: int = 0
    retries: int = 0
    stage: str = "completion"
    deployment_id: str | None = None
    prompt_tokens: int | None = None
    context_limit: int | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True)
class Completion:
    content: str
    usage: dict[str, int]


class CompressorError(RuntimeError):
    """A bounded, telemetry-safe compressor failure.

    Only allowlisted codes and numeric context counts may leave the client.
    URLs, raw response bodies, payloads and exception text are never telemetry.
    """

    def __init__(self, category: str, *, status: int | None = None,
                 upstream_error_code: str | None = None) -> None:
        self.category = category
        self.status = status
        self.retryable = status in RETRYABLE_STATUSES
        self.upstream_error_code = upstream_error_code
        super().__init__(f"compressor request failed ({category})")


def error_reason(error: BaseException) -> str:
    """Return a stable reason suitable for logs without exception details."""
    category = error.category if isinstance(error, CompressorError) else "unexpected"
    if category == "context_limit_exceeded":
        return "compressor_context_limit_exceeded"
    return f"compressor_error:{category}"


def _network_category(error: BaseException) -> str:
    """Classify a network exception without inspecting or recording its text."""
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ConnectionRefusedError) or getattr(
            current, "errno", None
        ) == errno.ECONNREFUSED:
            return "connection_refused"
        if isinstance(current, (TimeoutError, socket.timeout)) or getattr(
            current, "errno", None
        ) == errno.ETIMEDOUT:
            return "timeout"
        if isinstance(current, socket.gaierror):
            return "dns_error"
        reason = getattr(current, "reason", None)
        cause = getattr(current, "__cause__", None)
        if isinstance(reason, BaseException):
            pending.append(reason)
        if isinstance(cause, BaseException):
            pending.append(cause)
    return "connection_error"


def _capture_deployment(headers, state: RequestState) -> None:
    value = headers.get("X-compress-Deployment-ID") if headers is not None else None
    state.deployment_id = None
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{16,64}", value):
        state.deployment_id = value


def _decode(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as error:
        raise CompressorError("invalid_json") from error
    except (UnicodeDecodeError, AttributeError, RecursionError) as error:
        raise CompressorError("invalid_response") from error


def _http_error(error: HTTPError, state: RequestState) -> CompressorError:
    status = error.code if isinstance(error.code, int) and 100 <= error.code <= 599 else None
    _capture_deployment(error.headers, state)
    code = None
    try:
        # Read only a bounded 400 body and export recognized codes, never text.
        if status == 400:
            parsed = _decode(error.read(16_384))
            detail = parsed.get("error", parsed) if isinstance(parsed, dict) else {}
            if isinstance(detail, dict):
                for candidate in (detail.get("code"), detail.get("type")):
                    if isinstance(candidate, str) and candidate in SAFE_ERROR_CODES:
                        code = candidate
                        break
    except Exception:
        pass
    finally:
        error.close()
    category = "context_limit_exceeded" if code == "context_length_exceeded" else (
        f"http_status_{status}" if status is not None else "http_error"
    )
    return CompressorError(category, status=status, upstream_error_code=code)


def _post(url: str, body: dict, headers: dict, deadline: float, state: RequestState):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CompressorError("timeout")
    request = Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    state.deployment_id = None
    with urlopen(request, timeout=remaining) as response:
        _capture_deployment(getattr(response, "headers", None), state)
        raw = response.read()
    return _decode(raw)


def complete(prompt: str, config: Config, *, api_key: str | None = None,
             state: RequestState | None = None, preflight: bool = False,
             max_output_tokens: int | None = None,
             request_id: str | None = None) -> Completion:
    """Call an OpenAI-compatible chat-completions endpoint."""
    body = {
        "model": config.model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": config.temperature,
        "top_p": config.top_p,
        "top_k": config.top_k,
        "min_p": config.min_p,
        "presence_penalty": config.presence_penalty,
        "repetition_penalty": config.repetition_penalty,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    state = state if state is not None else RequestState()
    if request_id is not None:
        state.request_id = request_id
    if max_output_tokens is not None:
        body["max_tokens"] = max_output_tokens
        state.max_output_tokens = max_output_tokens
    if preflight and (type(max_output_tokens) is not int or max_output_tokens < 1):
        raise ValueError("preflight requires a positive output token budget")
    headers = {"Content-Type": "application/json"}
    if state.request_id is not None:
        headers["X-Request-ID"] = state.request_id
        # Proxies may replace the standard ID; keep our end-to-end ID separate.
        headers["X-compress-Request-ID"] = state.request_id
        # Keep tokenization and generation on the same worker when possible.
        headers["Modal-Session-ID"] = state.request_id
    api_key = api_key if api_key is not None else load_api_key(config)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    started_at = time.monotonic()
    request_deadline = started_at + config.timeout_seconds
    if state.deadline is not None:
        request_deadline = min(request_deadline, state.deadline)
    state.deadline = request_deadline
    retry_deadline = min(
        request_deadline, started_at + max(0.0, config.retry_503_seconds)
    )
    while True:
        try:
            if request_deadline <= time.monotonic():
                raise CompressorError("timeout")
            state.attempts += 1
            state.deployment_id = None
            state.prompt_tokens = state.context_limit = None
            if preflight:
                state.stage = "preflight"
                endpoint = config.endpoint.rstrip("/")
                root = endpoint[:-3] if endpoint.endswith("/v1") else endpoint
                tokens = _post(root + "/tokenize", {
                    "model": config.model, "messages": body["messages"],
                    "chat_template_kwargs": body["chat_template_kwargs"],
                    "add_generation_prompt": True, "add_special_tokens": False,
                }, headers, request_deadline, state)
                count = tokens.get("count") if isinstance(tokens, dict) else None
                limit = tokens.get("max_model_len") if isinstance(tokens, dict) else None
                if type(count) is not int or count < 0 or type(limit) is not int or limit < 1:
                    raise CompressorError("invalid_response")
                state.prompt_tokens, state.context_limit = count, limit
                if count + max_output_tokens > limit:
                    raise CompressorError("context_limit_exceeded")
            state.stage = "completion"
            parsed = _post(config.endpoint.rstrip("/") + "/chat/completions",
                           body, headers, request_deadline, state)
            break
        except HTTPError as error:
            failure = _http_error(error, state)
            now = time.monotonic()
            if failure.retryable and state.retries < config.retry_http_attempts:
                delay = (0.25 if state.retries == 0 else 1.0) * random.uniform(0.8, 1.2)
                if now + delay >= request_deadline:
                    raise failure from None
                state.retries += 1
                time.sleep(delay)
                continue
            # Do not stack the legacy retry window with the bounded API policy.
            if config.retry_http_attempts == 0 and failure.status == 503 and now < retry_deadline:
                time.sleep(min(2.0, retry_deadline - now))
                continue
            raise failure from None
        except (URLError, OSError, TimeoutError) as error:
            raise CompressorError(_network_category(error)) from error

    try:
        content = parsed["choices"][0]["message"]["content"]
        if parsed["choices"][0].get("finish_reason") == "length":
            raise CompressorError("incomplete_response")
    except (KeyError, IndexError, TypeError) as error:
        raise CompressorError("invalid_response") from error
    if not isinstance(content, str) or not content.strip():
        raise CompressorError("invalid_response")
    usage_raw = parsed.get("usage") or {}
    try:
        usage = {
            key: int(usage_raw.get(key, 0) or 0)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        }
    except (AttributeError, TypeError, ValueError) as error:
        raise CompressorError("invalid_response") from error
    return Completion(content=content.strip(), usage=usage)
