"""Token-minimal stdio MCP server for fail-open cmprs compression."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sys
import time
from typing import Any

from .client import complete, error_reason
from .config import Config, load_config
from .protocol import (
    estimate_tokens,
    resolve_focus,
    render_all_content_retry_prompt,
    render_prompt,
    resolve_response,
)
from .telemetry import (
    append_event,
    digest,
    estimate_mcp_framing_tokens,
    make_mcp_compression_event,
)
from .remote_usage import report_event


SERVER_INFO = {"name": "cmprs", "version": "0.1.0"}
TOOL = {
    "name": "compress",
    "description": "Compress focused shell output; fails open.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "output": {"type": "string"},
            "goal": {
                "type": "string",
                "description": "The current user's global task goal. Strongly recommended.",
            },
        },
        "required": ["command", "output"],
        "additionalProperties": False,
    },
}
TOOLS_LIST_RESULT = {"tools": [TOOL]}
RAW_FOCUSES = frozenset({"raw", "verbatim", "uncompressed", "full output"})


@dataclass(frozen=True)
class CompressionResult:
    """Model-safe compression outcome: the text to emit plus a privacy-safe reason."""

    text: str
    applied: bool
    reason: str


def _static_overhead() -> dict[str, int]:
    """Estimate static/call model overhead and non-model MCP wire framing."""
    encoded = json.dumps(TOOLS_LIST_RESULT, separators=(",", ":"))
    empty_call = json.dumps(
        {"name": "compress", "arguments": {"command": "", "output": ""}},
        separators=(",", ":"),
    )
    return {
        "tool_definition_chars": len(encoded),
        "tool_definition_tokens_estimate": estimate_tokens(encoded),
        "call_wrapper_chars": len(empty_call),
        "call_wrapper_tokens_estimate": estimate_tokens(empty_call),
        **estimate_mcp_framing_tokens(
            "compress", argument_names=("command", "output")
        ),
    }


MCP_OVERHEAD = _static_overhead()


def compress_result(
    command: Any,
    output: Any,
    cwd: str | Path | None = None,
    *,
    goal: Any = "",
    transport: str = "mcp",
    min_chars: int = 0,
    _config: Config | None = None,
    target_model: str = "",
) -> CompressionResult:
    """Compress fail-open and report whether the returned text replaced the original.

    ``min_chars`` is a caller-side gate applied before the compressor is contacted, so a
    middleware can skip small results without a network request. The configured
    ``min_output_tokens`` threshold remains authoritative.
    """
    started = time.monotonic()
    original = output if isinstance(output, str) else ""
    clean_command = command if isinstance(command, str) else ""
    clean_goal = goal if isinstance(goal, str) else ""
    focus, prompt_command = resolve_focus(clean_command)
    config = _config or load_config(cwd or Path.cwd())
    original_tokens = estimate_tokens(original)
    overhead = MCP_OVERHEAD if transport == "mcp" else {}
    base_event: dict[str, Any] = {
        "event_type": f"{transport}_compression",
        "transport": transport,
        "token_estimator": "max_lexical_utf8_bytes_div4_v2",
        "command_sha256": digest(clean_command),
        "focus_sha256": digest(focus or ""),
        "goal_sha256": digest(clean_goal),
        "original_chars": len(original),
        "original_tokens_estimate": original_tokens,
        "mcp_overhead": overhead,
    }
    if target_model:
        base_event["target_model"] = target_model

    def finish(
        result: str, *, applied: bool, reason: str, **extra: Any
    ) -> CompressionResult:
        result_tokens = estimate_tokens(result)
        telemetry_event = {
                **make_mcp_compression_event(
                    applied=applied,
                    returned_output="compressed" if applied else "original",
                    original_tokens_estimate=original_tokens,
                    returned_tokens_estimate=result_tokens,
                    compressor_usage=extra.pop("compressor_usage", None),
                    latency_ms=round((time.monotonic() - started) * 1000, 3),
                    reason=reason,
                    mcp_overhead=overhead,
                ),
                **base_event,
                "result_chars": len(result),
                "estimated_tokens_saved": (
                    max(0, original_tokens - result_tokens) if applied else 0
                ),
                "estimated_reduction_ratio": (
                    max(0, original_tokens - result_tokens) / original_tokens
                    if applied and original_tokens
                    else 0.0
                ),
                **extra,
            }
        append_event(config.log_path, telemetry_event)
        report_event(config, telemetry_event)
        return CompressionResult(result, applied, reason)

    if not isinstance(output, str):
        return finish(original, applied=False, reason="invalid_output")
    if focus is None:
        return finish(original, applied=False, reason="missing_focus")
    if focus.casefold() in RAW_FOCUSES:
        return finish(original, applied=False, reason="raw_requested")
    if len(original) < min_chars:
        return finish(original, applied=False, reason="below_min_chars")
    if original_tokens <= config.min_output_tokens:
        return finish(original, applied=False, reason="below_threshold")

    try:
        completion = complete(
            render_prompt(clean_goal[: config.max_goal_chars], focus, prompt_command, original),
            config,
        )
        resolution = resolve_response(completion.content, original)
        retry_count = 0
        first_reason = resolution.reason
        usage = completion.usage
        if resolution.reason == "omits_all_content":
            retry = complete(
                render_all_content_retry_prompt(
                    clean_goal[: config.max_goal_chars], focus, prompt_command, original
                ),
                config,
            )
            retry_count = 1
            usage = {
                key: completion.usage.get(key, 0) + retry.usage.get(key, 0)
                for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            }
            resolution = resolve_response(retry.content, original)
    except Exception as error:
        return finish(
            original,
            applied=False,
            reason=error_reason(error),
        )

    return finish(
        resolution.effective_output,
        applied=not resolution.keep_original,
        reason=resolution.reason,
        compression_kind=resolution.kind,
        compressor_usage=usage,
        retry_count=retry_count,
        first_reason=first_reason,
    )


def compress(
    command: Any,
    output: Any,
    cwd: str | Path | None = None,
    *,
    goal: Any = "",
) -> str:
    """Return compressed output, or the exact original whenever compression is unsafe."""
    return compress_result(command, output, cwd, goal=goal).text


def _result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def dispatch(message: Any) -> dict[str, Any] | None:
    """Dispatch one JSON-RPC message. Notifications intentionally return nothing."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request")
    if "id" not in message:
        return None

    request_id = message["id"]
    method = message.get("method")
    if method == "initialize":
        requested = (message.get("params") or {}).get("protocolVersion")
        protocol_version = requested if isinstance(requested, str) else "2025-06-18"
        return _result(
            request_id,
            {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        )
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, TOOLS_LIST_RESULT)
    if method != "tools/call":
        return _error(request_id, -32601, "Method not found")

    params = message.get("params")
    if not isinstance(params, dict) or params.get("name") != "compress":
        return _error(request_id, -32602, "Invalid params")
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        return _error(request_id, -32602, "Invalid params")
    command = arguments.get("command")
    output = arguments.get("output")
    goal = arguments.get("goal", "")
    if (
        not isinstance(command, str)
        or not isinstance(output, str)
        or not isinstance(goal, str)
    ):
        return _error(request_id, -32602, "Invalid params")

    # Plain text is the smallest standard MCP tool result and is all Codex needs.
    text = compress(command, output, goal=goal)
    return _result(request_id, {"content": [{"type": "text", "text": text}]})


def main() -> None:
    """Serve newline-delimited JSON-RPC over stdin/stdout."""
    for line in sys.stdin:
        try:
            message = json.loads(line)
            response = dispatch(message)
        except Exception:
            # Keep serving after malformed input; never put diagnostics on stdout.
            response = _error(None, -32700, "Parse error")
        if response is not None:
            json.dump(response, sys.stdout, separators=(",", ":"))
            sys.stdout.write("\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
