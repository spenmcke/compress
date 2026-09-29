"""Privacy-conscious JSONL telemetry."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _tokens_for_json(value: Any) -> int:
    """Estimate tokens in compact JSON at four UTF-8 bytes per token."""
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return math.ceil(len(encoded) / 4)


def estimate_mcp_framing_tokens(
    tool_name: str,
    *,
    argument_names: tuple[str, ...] = ("focus", "output"),
) -> dict[str, int]:
    """Estimate MCP protocol/field framing, excluding argument and result content.

    The estimate intentionally serializes empty values. This counts JSON-RPC/tool field
    names and punctuation without claiming that the raw shell or compressed output is
    MCP overhead. It is an auditable approximation, not a provider token count.
    """
    request = {
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": {name: "" for name in argument_names}},
    }
    response = {"result": {"content": [{"type": "text", "text": ""}]}}
    request_tokens = _tokens_for_json(request)
    response_tokens = _tokens_for_json(response)
    return {
        "estimator_version": 1,
        "request_tokens_estimate": request_tokens,
        "response_tokens_estimate": response_tokens,
        "total_tokens_estimate": request_tokens + response_tokens,
    }


def make_mcp_compression_event(
    *,
    applied: bool,
    returned_output: str,
    original_tokens_estimate: int,
    returned_tokens_estimate: int,
    compressor_usage: dict[str, Any] | None,
    latency_ms: float,
    reason: str,
    mcp_overhead: dict[str, Any],
    **metadata: Any,
) -> dict[str, Any]:
    """Build the shared, fail-open MCP compression telemetry schema.

    Savings are potential only when compressed output is returned to Codex. Whether that
    output actually replaces model context must be established from paired Codex rollout
    counters; an MCP server cannot observe that boundary itself.
    """
    if returned_output not in {"compressed", "original"}:
        raise ValueError("returned_output must be 'compressed' or 'original'")
    original = max(0, int(original_tokens_estimate))
    returned = max(0, int(returned_tokens_estimate))
    compression_returned = applied and returned_output == "compressed"
    gross_saved = max(0, original - returned) if compression_returned else 0
    # Only the small generated call wrapper is plausibly additional Codex context.
    # JSON-RPC framing is local transport, while the tool definition is static and
    # normally prefix-cached; keep both visible but do not charge either per call.
    call_overhead = max(
        0, int(mcp_overhead.get("call_wrapper_tokens_estimate", 0) or 0)
    )
    return {
        "event_type": "mcp_compression",
        "transport": "mcp",
        "applied": bool(applied),
        "returned_output": returned_output,
        "original_tokens_estimate": original,
        "returned_tokens_estimate": returned,
        "potential_context_tokens_saved": gross_saved,
        "potential_context_tokens_saved_after_call_overhead": max(
            0, gross_saved - call_overhead
        ),
        "mcp_overhead": mcp_overhead,
        "compressor_usage": compressor_usage or {},
        "latency_ms": max(0.0, float(latency_ms)),
        "reason": reason,
        **metadata,
    }


def append_event(path: Path, event: dict[str, Any]) -> None:
    """Append one compact event; telemetry failure must never break the hook."""
    record = {"schema_version": 1, "timestamp_unix": time.time(), **event}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        pass
