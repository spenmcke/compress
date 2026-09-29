"""Loopback Responses proxy that compresses text observations before model ingestion."""

from __future__ import annotations

import argparse
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Callable
from urllib.parse import urlsplit

from .config import Config, load_config
from .mcp_server import RAW_FOCUSES, CompressionResult, compress_result
from .protocol import estimate_tokens, resolve_focus
from .savings import RUN_FILE_ENV, RUN_ID_ENV, RewriteObservation, RunTracker, extract_usage
from .telemetry import append_event, digest, make_mcp_compression_event


API_UPSTREAM = "https://api.openai.com/v1"
CHATGPT_UPSTREAM = "https://chatgpt.com/backend-api/codex"
MIN_OUTPUT_CHARS = 2048
RESULT_CACHE_ENTRIES = 256
RESULT_CACHE_BYTES = 16 * 1024 * 1024
CACHE_SCHEMA_VERSION = 1
SHELL_FUNCTIONS = frozenset({"exec", "exec_command", "write_stdin"})
TEXT_FIELDS = frozenset({"text", "content", "output", "stdout", "stderr", "result", "body"})
HOP_BY_HOP = frozenset(
    {
        "connection",
        "content-length",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
FIRST_PARTY_UPSTREAMS = frozenset({"api.openai.com", "chatgpt.com"})
LOOPBACK_UPSTREAMS = frozenset({"127.0.0.1", "::1", "localhost"})


@dataclass(frozen=True)
class CallContext:
    name: str
    payload: str


class CallRegistry:
    """Bounded, thread-safe association between Responses call ids and calls."""

    def __init__(self, limit: int = 4096) -> None:
        self._limit = limit
        self._calls: OrderedDict[str, CallContext] = OrderedDict()
        self._goal = ""
        self._lock = threading.Lock()

    def goal(self, latest: str = "") -> str:
        """Carry the user's task across Responses requests with incremental input."""
        with self._lock:
            if latest:
                self._goal = latest
            return self._goal

    def put(self, call_id: Any, context: CallContext) -> None:
        if not isinstance(call_id, str) or not call_id:
            return
        with self._lock:
            self._calls[call_id] = context
            self._calls.move_to_end(call_id)
            while len(self._calls) > self._limit:
                self._calls.popitem(last=False)

    def get(self, call_id: Any) -> CallContext | None:
        if not isinstance(call_id, str):
            return None
        with self._lock:
            context = self._calls.get(call_id)
            if context is not None:
                self._calls.move_to_end(call_id)
            return context


CacheKey = tuple[str, ...]


@dataclass
class _Flight:
    event: threading.Event
    result: CompressionResult | None = None
    error: BaseException | None = None


class CompressionCache:
    """Bounded result cache with one in-flight compressor call per exact input."""

    def __init__(
        self,
        max_entries: int = RESULT_CACHE_ENTRIES,
        max_bytes: int = RESULT_CACHE_BYTES,
    ) -> None:
        if max_entries < 0 or max_bytes < 0:
            raise ValueError("cache limits must be non-negative")
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._entries: OrderedDict[CacheKey, CompressionResult] = OrderedDict()
        self._entry_bytes: dict[CacheKey, int] = {}
        self._bytes = 0
        self._flights: dict[CacheKey, _Flight] = {}
        self._lock = threading.Lock()

    def _store(self, key: CacheKey, result: CompressionResult) -> None:
        size = len(result.text.encode("utf-8", errors="surrogatepass"))
        if not self._max_entries or not self._max_bytes or size > self._max_bytes:
            return
        previous = self._entries.pop(key, None)
        if previous is not None:
            self._bytes -= self._entry_bytes.pop(key)
        self._entries[key] = result
        self._entry_bytes[key] = size
        self._bytes += size
        while len(self._entries) > self._max_entries or self._bytes > self._max_bytes:
            oldest, _ = self._entries.popitem(last=False)
            self._bytes -= self._entry_bytes.pop(oldest)

    def resolve(
        self,
        key: CacheKey,
        compute: Callable[[], CompressionResult],
        *,
        wait_timeout: float | None = None,
    ) -> tuple[CompressionResult, str]:
        """Return a cached/shared/computed result; cache successful compression only."""
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                self._entries.move_to_end(key)
                return cached, "cache"
            flight = self._flights.get(key)
            if flight is None:
                flight = _Flight(threading.Event())
                self._flights[key] = flight
                leader = True
            else:
                leader = False

        if not leader:
            if not flight.event.wait(wait_timeout):
                raise TimeoutError("timed out waiting for in-flight compression")
            if flight.error is not None:
                raise flight.error
            if flight.result is None:
                raise RuntimeError("compression flight completed without a result")
            return flight.result, "single_flight"

        try:
            result = compute()
            if not isinstance(result, CompressionResult):
                raise TypeError("compressor returned an invalid result")
        except BaseException as error:
            with self._lock:
                flight.error = error
                self._flights.pop(key, None)
                flight.event.set()
            raise
        store_error: BaseException | None = None
        with self._lock:
            try:
                flight.result = result
                if result.applied and isinstance(result.text, str):
                    self._store(key, result)
            except BaseException as error:
                flight.result = None
                flight.error = error
                store_error = error
            finally:
                self._flights.pop(key, None)
                flight.event.set()
        if store_error is not None:
            raise store_error
        return result, "computed"


def _call_context(item: Any) -> tuple[str, CallContext] | None:
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    call_id = item.get("call_id")
    name = item.get("name")
    if item_type == "custom_tool_call":
        payload = item.get("input")
    elif item_type == "function_call":
        payload = item.get("arguments")
    else:
        return None
    if not all(isinstance(value, str) and value for value in (call_id, name, payload)):
        return None
    return call_id, CallContext(name=name, payload=payload)


def record_response_event(event: Any, registry: CallRegistry) -> None:
    """Remember completed tool calls observed in a streamed Responses result."""
    if not isinstance(event, dict):
        return
    item = event.get("item") if event.get("type") == "response.output_item.done" else None
    found = _call_context(item)
    if found is not None:
        registry.put(*found)


def record_response_json(response: Any, registry: CallRegistry) -> None:
    if not isinstance(response, dict):
        return
    for item in response.get("output") or []:
        found = _call_context(item)
        if found is not None:
            registry.put(*found)


def _scan_js_string(source: str, start: int) -> str | None:
    quote = source[start]
    index = start + 1
    result: list[str] = []
    while index < len(source):
        char = source[index]
        if char == quote:
            return "".join(result)
        if char == "\\" and index + 1 < len(source):
            following = source[index + 1]
            escapes = {"n": "\n", "r": "\r", "t": "\t"}
            result.append(escapes.get(following, following))
            index += 2
            continue
        result.append(char)
        index += 1
    return None


def _js_literal_string(source: str, start: int) -> tuple[str, int] | None:
    """Read a quoted JS string without interpreting expressions or templates."""
    quote = source[start]
    if quote not in {"'", '"'}:
        return None
    index = start + 1
    result: list[str] = []
    escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f",
               "v": "\v", "\\": "\\", "'": "'", '"': '"', "/": "/"}
    while index < len(source):
        char = source[index]
        if char == quote:
            return "".join(result), index + 1
        if ord(char) < 32:
            return None
        if char == "\\":
            index += 1
            if index >= len(source):
                return None
            char = source[index]
            if char in escapes:
                result.append(escapes[char])
            elif char in {"u", "x"}:
                width = 4 if char == "u" else 2
                digits = source[index + 1:index + 1 + width]
                if len(digits) != width or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                    return None
                result.append(chr(int(digits, 16)))
                index += width
            else:
                return None
        else:
            result.append(char)
        index += 1
    return None


def _js_literal_arguments(source: str, start: int) -> dict[str, Any] | None:
    """Accept only flat literal argument objects; reject dynamic or nested syntax."""
    index = start + 1
    arguments: dict[str, Any] = {}
    while index < len(source):
        while index < len(source) and source[index].isspace():
            index += 1
        if index >= len(source):
            return None
        if source[index] == "}":
            return arguments
        if source[index] in {"'", '"'}:
            parsed = _js_literal_string(source, index)
            if parsed is None:
                return None
            key, index = parsed
        else:
            match = re.match(r"[A-Za-z_$][A-Za-z0-9_$]*", source[index:])
            if match is None:
                return None
            key = match.group()
            index += len(key)
        if key in arguments:
            return None
        while index < len(source) and source[index].isspace():
            index += 1
        if index >= len(source) or source[index] != ":":
            return None
        index += 1
        while index < len(source) and source[index].isspace():
            index += 1
        if index >= len(source):
            return None
        if source[index] in {"'", '"'}:
            parsed = _js_literal_string(source, index)
            if parsed is None:
                return None
            value, index = parsed
        else:
            match = re.match(r"(?:true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)", source[index:])
            if match is None:
                return None
            value = json.loads(match.group())
            index += len(match.group())
        arguments[key] = value
        while index < len(source) and source[index].isspace():
            index += 1
        if index < len(source) and source[index] == "}":
            return arguments
        if index >= len(source) or source[index] != ",":
            return None
        index += 1
    return None


def _inline_exec_command(payload: str) -> str | None:
    """Extract a literal command from an inline code-mode exec_command call."""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\btools\.exec_command\s*\(", payload):
        index = match.end()
        while index < len(payload) and payload[index].isspace():
            index += 1
        if index >= len(payload) or payload[index] != "{":
            continue
        try:
            arguments, _ = decoder.raw_decode(payload, index)
        except json.JSONDecodeError:
            arguments = _js_literal_arguments(payload, index)
        if not isinstance(arguments, dict):
            continue
        command = arguments.get("cmd") or arguments.get("command")
        if isinstance(command, str):
            return command
    return None


def extract_command(context: CallContext) -> str | None:
    """Recover the focused shell command from a direct or code-mode tool call."""
    if context.name not in SHELL_FUNCTIONS:
        return None
    payload = context.payload
    if context.name == "exec":
        match = re.search(r"\b(?:const|let|var)\s+cmd\s*=\s*([`\"'])", payload)
        if match is not None:
            command = _scan_js_string(payload, match.start(1))
            if command is not None:
                return command
        command = _inline_exec_command(payload)
        if command is not None:
            return command
        focus_match = re.search(
            r"(?m)^\s*(#\s*(?:compress|compress-compress|cmprs[-_ ]focus|context[-_ ]focus(?:[-_ ]question)?)\s*:[^\r\n]+)",
            payload,
            re.IGNORECASE,
        )
        if focus_match is not None:
            without_focus = payload[: focus_match.start(1)] + payload[focus_match.end(1) :]
            return f"{focus_match.group(1).strip()}\n{without_focus}"
        return None
    try:
        arguments = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(arguments, dict):
        return None
    command = arguments.get("cmd") or arguments.get("command")
    return command if isinstance(command, str) else None


def _eligible_command(context: CallContext | None) -> str | None:
    if context is None:
        return None
    command = extract_command(context)
    if command is None:
        if context.name == "exec_command" or (
            context.name == "exec" and "tools.exec_command" in context.payload
        ):
            return None
        # Tool arguments are data, not a shell program. Encode them on one line so
        # an embedded focus comment cannot accidentally change the policy.
        detail = json.dumps(context.payload[:4096], ensure_ascii=True)
        command = (
            "# compress: Which exact evidence in this tool result is needed for the next action?\n"
            f"{context.name} {detail}"
        )
    focus, _ = resolve_focus(command)
    if focus is None or focus.casefold() in RAW_FOCUSES:
        return None
    return command


Compressor = Callable[..., CompressionResult]


def _config_fingerprint(config: Config) -> str:
    """Hash result-affecting configuration without retaining credentials or paths."""
    values = {
        "schema": CACHE_SCHEMA_VERSION,
        "endpoint": config.endpoint,
        "model": config.model,
        "timeout_seconds": config.timeout_seconds,
        "min_output_tokens": config.min_output_tokens,
        "max_goal_chars": config.max_goal_chars,
        "temperature": config.temperature,
        "top_p": config.top_p,
        "top_k": config.top_k,
        "min_p": config.min_p,
        "presence_penalty": config.presence_penalty,
        "repetition_penalty": config.repetition_penalty,
        "retry_503_seconds": config.retry_503_seconds,
    }
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _cache_key(
    text: str,
    command: str,
    cwd: Path,
    compressor: Compressor,
    config: Config,
    goal: str = "",
) -> CacheKey:
    identity = getattr(compressor, "__qualname__", compressor.__class__.__qualname__)
    return (
        str(CACHE_SCHEMA_VERSION),
        str(cwd.resolve()),
        _config_fingerprint(config),
        f"{getattr(compressor, '__module__', compressor.__class__.__module__)}.{identity}",
        str(id(compressor)),
        str(MIN_OUTPUT_CHARS),
        str(len(command)),
        hashlib.sha256(command.encode("utf-8", errors="surrogatepass")).hexdigest(),
        hashlib.sha256(goal.encode("utf-8", errors="surrogatepass")).hexdigest(),
        str(len(text)),
        hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest(),
    )


def _record_cache_hit(
    *,
    config: Config,
    command: str,
    original: str,
    result: CompressionResult,
    source: str,
    latency_ms: float,
) -> None:
    append_event(
        config.log_path,
        {
            **make_mcp_compression_event(
                applied=True,
                returned_output="compressed",
                original_tokens_estimate=estimate_tokens(original),
                returned_tokens_estimate=estimate_tokens(result.text),
                compressor_usage={},
                latency_ms=latency_ms,
                reason="cache_hit",
                mcp_overhead={},
                event_type="proxy_cache_hit",
                transport="proxy",
            ),
            "cache_source": source,
            "command_sha256": digest(command),
            "original_sha256": digest(original),
            "original_chars": len(original),
            "result_chars": len(result.text),
        },
    )


def _compress_text(
    text: str,
    command: str,
    cwd: Path,
    compressor: Compressor,
    cache: CompressionCache | None = None,
    goal: str = "",
) -> tuple[str, bool]:
    if len(text) < MIN_OUTPUT_CHARS:
        return text, False
    try:
        if cache is None:
            result = compressor(
                command,
                text,
                cwd,
                transport="proxy",
                min_chars=MIN_OUTPUT_CHARS,
                goal=goal,
            )
        else:
            config = load_config(cwd)
            cache_started = time.monotonic()

            def compute() -> CompressionResult:
                kwargs: dict[str, Any] = {
                    "transport": "proxy",
                    "min_chars": MIN_OUTPUT_CHARS,
                    "goal": goal,
                }
                if compressor is compress_result:
                    kwargs["_config"] = config
                return compressor(command, text, cwd, **kwargs)

            result, source = cache.resolve(
                _cache_key(text, command, cwd, compressor, config, goal),
                compute,
                wait_timeout=max(
                    1.0,
                    (2 * config.timeout_seconds) + 5.0,
                ),
            )
            if source != "computed" and result.applied:
                _record_cache_hit(
                    config=config,
                    command=command,
                    original=text,
                    result=result,
                    source=source,
                    latency_ms=round((time.monotonic() - cache_started) * 1000, 3),
                )
    except Exception:
        return text, False
    if not result.applied or not isinstance(result.text, str):
        return text, False
    return result.text, True


def _rewrite_text(
    text: str,
    command: str,
    cwd: Path,
    compressor: Compressor,
    cache: CompressionCache | None = None,
    goal: str = "",
) -> tuple[str, bool]:
    try:
        nested = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return _compress_text(text, command, cwd, compressor, cache, goal)
    if not isinstance(nested, dict) or not isinstance(nested.get("output"), str):
        return text, False
    if nested.get("session_id") is not None and nested.get("exit_code") is None:
        return text, False
    compressed, applied = _compress_text(
        nested["output"], command, cwd, compressor, cache, goal
    )
    if not applied:
        return text, False
    nested["output"] = compressed
    return json.dumps(nested, ensure_ascii=False, separators=(",", ":")), True


def _rewrite_output(
    output: Any,
    command: str,
    cwd: Path,
    compressor: Compressor,
    cache: CompressionCache | None = None,
    goal: str = "",
) -> tuple[Any, bool]:
    if isinstance(output, str):
        return _rewrite_text(output, command, cwd, compressor, cache, goal)
    if not isinstance(output, list):
        return output, False
    changed = False
    rewritten: list[Any] = []
    for item in output:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            rewritten.append(item)
            continue
        text, applied = _rewrite_text(item["text"], command, cwd, compressor, cache, goal)
        if not applied:
            rewritten.append(item)
            continue
        updated = dict(item)
        updated["text"] = text
        rewritten.append(updated)
        changed = True
    return rewritten if changed else output, changed


def _rewrite_observation(
    value: Any,
    command: str,
    cwd: Path,
    compressor: Compressor,
    cache: CompressionCache | None,
    goal: str,
    *,
    text_field: bool = True,
) -> tuple[Any, bool]:
    """Rewrite model-visible text while keeping structured results and media intact."""
    if isinstance(value, str):
        if not text_field or value.startswith("data:"):
            return value, False
        try:
            nested = json.loads(value)
        except (ValueError, TypeError):
            return _compress_text(value, command, cwd, compressor, cache, goal)
        if not isinstance(nested, (dict, list)):
            return _compress_text(value, command, cwd, compressor, cache, goal)
        rewritten, changed = _rewrite_observation(
            nested, command, cwd, compressor, cache, goal
        )
        if not changed:
            return value, False
        return json.dumps(rewritten, ensure_ascii=False, separators=(",", ":")), True
    if isinstance(value, list):
        changed = False
        result = []
        for item in value:
            rewritten, applied = _rewrite_observation(
                item, command, cwd, compressor, cache, goal,
                text_field=isinstance(item, str) or isinstance(item, (dict, list)),
            )
            result.append(rewritten)
            changed |= applied
        return (result if changed else value), changed
    if not isinstance(value, dict):
        return value, False
    # An active session is still producing output; wait for the completed result.
    if value.get("session_id") is not None and value.get("exit_code") is None:
        return value, False
    changed = False
    result = dict(value)
    for key, item in value.items():
        if key not in TEXT_FIELDS:
            continue
        rewritten, applied = _rewrite_observation(
            item, command, cwd, compressor, cache, goal,
            text_field=True,
        )
        if applied:
            result[key] = rewritten
            changed = True
    return (result if changed else value), changed


def _task_goal(body: dict[str, Any]) -> str:
    """Take the most recent user text available in the model request."""
    for item in reversed(body.get("input", [])):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str):
            return content[:4000]
        if isinstance(content, list):
            parts = [part.get("text") for part in content if isinstance(part, dict)]
            return "\n".join(part for part in parts if isinstance(part, str))[:4000]
    return ""


def rewrite_responses_request(
    body: Any,
    registry: CallRegistry,
    cwd: Path,
    *,
    compressor: Compressor = compress_result,
    cache: CompressionCache | None = None,
    observations: list[RewriteObservation] | None = None,
) -> tuple[Any, int]:
    """Return a copied request with completed text observations compressed."""
    if not isinstance(body, dict) or not isinstance(body.get("input"), list):
        return body, 0
    request_calls: dict[str, CallContext] = {}
    for item in body["input"]:
        found = _call_context(item)
        if found is not None:
            request_calls[found[0]] = found[1]
            registry.put(*found)

    changed = 0
    goal = registry.goal(_task_goal(body))
    rewritten_input: list[Any] = []
    for item in body["input"]:
        if not isinstance(item, dict) or item.get("type") not in {
            "custom_tool_call_output",
            "function_call_output",
        }:
            rewritten_input.append(item)
            continue
        call_id = item.get("call_id")
        context = request_calls.get(call_id) or registry.get(call_id)
        command = _eligible_command(context)
        if command is None:
            rewritten_input.append(item)
            continue
        if context is not None and extract_command(context) is not None:
            output, applied = _rewrite_output(
                item.get("output"), command, cwd, compressor, cache, goal
            )
        else:
            output, applied = _rewrite_observation(
                item.get("output"), command, cwd, compressor, cache, goal
            )
        if not applied:
            rewritten_input.append(item)
            continue
        updated = dict(item)
        updated["output"] = output
        rewritten_input.append(updated)
        changed += 1
        if observations is not None and isinstance(call_id, str):
            original_tokens = estimate_tokens(
                json.dumps(item.get("output"), ensure_ascii=False, separators=(",", ":"))
            )
            returned_tokens = estimate_tokens(
                json.dumps(output, ensure_ascii=False, separators=(",", ":"))
            )
            observations.append(
                RewriteObservation(call_id, max(0, original_tokens - returned_tokens))
            )

    if not changed:
        return body, 0
    rewritten = dict(body)
    rewritten["input"] = rewritten_input
    return rewritten, changed


def _upstream_url(base_url: str, request_path: str) -> tuple[str, str, int | None, str]:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("upstream must be an absolute HTTP(S) URL")
    suffix = request_path
    if suffix.startswith("/v1/"):
        suffix = suffix[3:]
    elif suffix == "/v1":
        suffix = ""
    path = f"{parsed.path.rstrip('/')}/{suffix.lstrip('/')}"
    if not path.startswith("/"):
        path = f"/{path}"
    return parsed.scheme, parsed.hostname, parsed.port, path


def validate_upstream(base_url: str) -> str:
    parsed = urlsplit(base_url)
    _upstream_url(base_url, "/v1/responses")
    first_party = parsed.scheme == "https" and parsed.hostname in FIRST_PARTY_UPSTREAMS
    local_test = parsed.hostname in LOOPBACK_UPSTREAMS
    if not first_party and not local_test:
        raise ValueError("upstream must be first-party HTTPS or a loopback test server")
    return base_url.rstrip("/")


class ResponseAccounting:
    """Pair one HTTP request's rewrites with its response metadata and usage."""

    def __init__(self, tracker: RunTracker, observations: list[RewriteObservation],
                 model: Any = None) -> None:
        self.tracker = tracker
        self.observations = observations
        self.response: dict[str, Any] = {"model": model}
        self.accounting_id: str | None = None
        self.terminal = False
        self.usage_recorded = False

    def _save(self) -> None:
        if self.accounting_id is None:
            self.accounting_id = self.response.get("id") or str(uuid.uuid4())
        try:
            recorded = self.tracker.complete_response(
                {**self.response, "id": self.accounting_id}, self.observations
            )
            self.usage_recorded |= recorded
        except Exception as error:
            self._log("proxy_accounting_error", error_class=type(error).__name__)

    def _log(self, event_type: str, **metadata: Any) -> None:
        append_event(self.tracker.path.with_suffix(".events.jsonl"), {
            "event_type": event_type, "transport": "proxy",
            "run_id": self.tracker.run_id, "response_id": self.accounting_id,
            **metadata,
        })

    def observe(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if not isinstance(kind, str) or kind not in {
            "response.created", "response.completed", "response.incomplete",
            "response.failed", "response.usage",
        }:
            return
        response = event.get("response")
        response = response if isinstance(response, dict) else {}
        for key in ("id", "model"):
            value = response.get(key)
            if isinstance(value, str) and value:
                self.response[key] = value
        if not self.response.get("id") and isinstance(event.get("response_id"), str):
            self.response["id"] = event["response_id"]
        # Preserve valid usage if a later terminal event contains null/empty usage.
        for candidate in (response, event):
            if extract_usage(candidate) is not None:
                self.response["usage"] = candidate["usage"]
                break
        if kind in {"response.completed", "response.incomplete", "response.failed"}:
            self.terminal = True
        if self.terminal:
            self._save()

    def finish(self) -> None:
        # A disconnected/unterminated stream must not silently look like zero
        # usage. Retain its rewrites, but leave the session fraction unavailable.
        self._save()
        if not self.usage_recorded:
            self._log("proxy_accounting_incomplete", reason=(
                "usage_missing" if self.terminal else "terminal_missing"
            ))


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        upstream: str,
        cwd: Path,
        registry: CallRegistry | None = None,
        tracker: RunTracker | None = None,
    ) -> None:
        super().__init__(address, ProxyHandler)
        self.upstream = validate_upstream(upstream)
        self.cwd = cwd
        self.registry = registry or CallRegistry()
        self.compression_cache = CompressionCache()
        self.tracker = tracker


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: ProxyServer

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        self._proxy()

    def do_POST(self) -> None:  # noqa: N802
        self._proxy()

    def _body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        return self.rfile.read(length) if length > 0 else b""

    def _proxy(self) -> None:
        original = self._body()
        payload = original
        observations: list[RewriteObservation] = []
        request_model = None
        request_streaming = False
        is_responses_request = (
            self.command == "POST"
            and self.path.split("?", 1)[0].endswith("/responses")
        )
        if is_responses_request and not self.headers.get("Content-Encoding"):
            try:
                decoded = json.loads(original)
                if isinstance(decoded, dict):
                    request_model = decoded.get("model")
                    request_streaming = decoded.get("stream") is True
                rewritten, changed = rewrite_responses_request(
                    decoded,
                    self.server.registry,
                    self.server.cwd,
                    cache=self.server.compression_cache,
                    observations=observations,
                )
                if changed:
                    payload = json.dumps(
                        rewritten, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                payload = original
                observations.clear()

        try:
            scheme, host, port, path = _upstream_url(self.server.upstream, self.path)
            connection_type = (
                http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
            )
            connection = connection_type(host, port=port, timeout=330)
            headers = {
                name: value
                for name, value in self.headers.items()
                if name.casefold() not in HOP_BY_HOP
                and name.casefold() not in {"host", "accept-encoding"}
            }
            headers["Accept-Encoding"] = "identity"
            if payload:
                headers["Content-Length"] = str(len(payload))
            connection.request(self.command, path, body=payload or None, headers=headers)
            upstream = connection.getresponse()
        except Exception:
            self._error(502, "upstream request failed")
            return

        accounting = None
        if is_responses_request and 200 <= upstream.status < 300 and self.server.tracker:
            accounting = ResponseAccounting(self.server.tracker, observations, request_model)

        content_type = upstream.getheader("Content-Type", "")
        try:
            self.send_response(upstream.status, upstream.reason)
            for name, value in upstream.getheaders():
                if name.casefold() not in HOP_BY_HOP:
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if "text/event-stream" in content_type or (
                request_streaming and not content_type
            ):
                data_lines: list[bytes] = []

                def record_event() -> None:
                    if not data_lines:
                        return
                    data = b"\n".join(data_lines)
                    data_lines.clear()
                    try:
                        event = json.loads(data)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        return
                    record_response_event(event, self.server.registry)
                    if accounting is not None:
                        accounting.observe(event)

                for line in iter(upstream.readline, b""):
                    if line.startswith(b"data:"):
                        data_lines.append(line[5:].strip())
                    elif not line.strip():
                        record_event()
                    self.wfile.write(line)
                    self.wfile.flush()
                record_event()
            else:
                response_body = upstream.read()
                if "application/json" in content_type:
                    try:
                        response_json = json.loads(response_body)
                        record_response_json(response_json, self.server.registry)
                        if accounting is not None:
                            accounting.observe({"type": "response.completed",
                                                "response": response_json})
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        pass
                self.wfile.write(response_body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if accounting is not None:
                accounting.finish()
            upstream.close()
            connection.close()

    def _error(self, status: int, message: str) -> None:
        body = json.dumps({"error": {"message": message}}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True


def detect_upstream() -> str:
    explicit = os.environ.get("COMPRESS_CODEX_UPSTREAM")
    if explicit:
        return validate_upstream(explicit)
    try:
        status = subprocess.run(
            ["codex", "login", "status"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("could not determine Codex login mode") from error
    message = f"{status.stdout}\n{status.stderr}".casefold()
    if status.returncode == 0 and "using chatgpt" in message:
        return CHATGPT_UPSTREAM
    if status.returncode == 0 and "using an api key" in message:
        return API_UPSTREAM
    raise RuntimeError(
        "could not determine Codex login mode; set COMPRESS_CODEX_UPSTREAM explicitly"
    )


def codex_arguments(port: int, user_arguments: list[str]) -> list[str]:
    provider = {
        "name": "OpenAI",
        "base_url": f"http://127.0.0.1:{port}/v1",
        "wire_api": "responses",
        "requires_openai_auth": True,
        "supports_websockets": False,
        "supports_standalone_web_search": True,
    }
    # JSON is valid TOML for each scalar, but an inline TOML table uses equals signs.
    provider_toml = "{" + ",".join(
        f"{key}={json.dumps(value)}" for key, value in provider.items()
    ) + "}"
    return [
        "codex",
        "-c",
        f"model_providers.compress_proxy={provider_toml}",
        "-c",
        'model_provider="compress_proxy"',
        "-c",
        "features.enable_request_compression=false",
        "-c",
        "features.hooks=false",
        *user_arguments,
    ]


def launch(user_arguments: list[str] | None = None) -> None:
    """Run Codex through an ephemeral loopback proxy and clean it up on exit."""
    try:
        upstream = detect_upstream()
    except RuntimeError as error:
        print(f"compress: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    billing_mode = "codex" if upstream == CHATGPT_UPSTREAM else "api"
    try:
        tracker = RunTracker(cwd=Path.cwd(), billing_mode=billing_mode)
    except OSError:
        tracker = None
    server = ProxyServer(
        ("127.0.0.1", 0), upstream, Path.cwd(), tracker=tracker
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    arguments = codex_arguments(
        port, sys.argv[1:] if user_arguments is None else user_arguments
    )
    child_environment = os.environ.copy()
    if tracker is not None:
        child_environment[RUN_ID_ENV] = tracker.run_id
        child_environment[RUN_FILE_ENV] = str(tracker.path)
    returncode = 130
    try:
        completed = subprocess.run(arguments, check=False, env=child_environment)
        returncode = completed.returncode
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        if tracker is not None:
            try:
                tracker.finish(returncode)
            except OSError:
                pass
    raise SystemExit(returncode)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--upstream", default=None)
    args = parser.parse_args()
    upstream = args.upstream or detect_upstream()
    server = ProxyServer(("127.0.0.1", args.port), upstream, Path.cwd())
    print(
        f"compress proxy listening on http://127.0.0.1:{args.port}/v1",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--launch":
        del sys.argv[1]
        launch()
    else:
        main()
