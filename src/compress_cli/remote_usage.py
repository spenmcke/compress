"""Fail-open delivery of content-free CLI usage to compress."""

from __future__ import annotations

from datetime import datetime, timezone
from importlib.metadata import version
import hashlib
import json
import os
from pathlib import Path
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .config import Config, load_api_key


EXPECTED_SKIP = {"below_threshold", "below_min_chars", "raw_requested", "uncompressed_mode", "user_cancelled"}
NON_ATTEMPT_REASONS = {"not_bash", "unsupported_response_shape", "command_still_running", "invalid_output"}
ERROR_MARKERS = ("error", "timeout", "network", "invalid", "auth", "payload", "unavailable", "context_limit")


def _client_version() -> str:
    try:
        return version("compress-cli")
    except Exception:
        return "unknown"


def _outcome(event: dict) -> str:
    if event.get("applied"):
        return "applied"
    reason = str(event.get("reason") or "unknown_error").lower()
    if reason in EXPECTED_SKIP:
        return "expected_skip"
    if any(marker in reason for marker in ERROR_MARKERS):
        return "error"
    return "no_compression"


def _pending_path(config: Config) -> Path:
    return config.log_path.with_name("usage-pending.jsonl")


def _enabled(config: Config) -> bool:
    return urlsplit(config.endpoint).hostname == "api.everestagi.com" and bool(
        load_api_key(config)
    )


def _post(config: Config, payload: dict) -> bool:
    token = load_api_key(config)
    if not token:
        return False
    request = Request(
        config.endpoint.rstrip("/") + "/usage",
        data=json.dumps(payload, separators=(",", ":")).encode(), method="POST",
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}",
                 "Content-Type": "application/json", "X-Request-ID": payload["request_id"]},
    )
    try:
        with urlopen(request, timeout=1.5) as response:
            return 200 <= response.status < 300
    except (HTTPError, URLError, TimeoutError, OSError):
        return False


def _append_pending(config: Config, payload: dict) -> None:
    path = _pending_path(config)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
    except OSError:
        pass


def flush_pending(config: Config) -> None:
    path = _pending_path(config)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    remaining = []
    for line in lines[:100]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not _post(config, payload):
            remaining.append(line)
    remaining.extend(lines[100:])
    try:
        if remaining:
            path.write_text("\n".join(remaining) + "\n", encoding="utf-8")
            os.chmod(path, 0o600)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def report_event(config: Config, event: dict) -> None:
    if not _enabled(config):
        return
    if str(event.get("reason") or "").lower() in NON_ATTEMPT_REASONS:
        return
    outcome = _outcome(event)
    if outcome not in {"no_compression", "error"}:
        return
    reason = str(event.get("reason") or "unknown_error").lower()
    if not reason or len(reason) > 128 or not all(character.islower() or character.isdigit() or character in "_.:-" for character in reason):
        reason = "unknown_error"
    payload = {
        "kind": "failure", "request_id": uuid.uuid4().hex,
        "occurred_at": datetime.now(timezone.utc).isoformat(), "reason_code": reason,
        "outcome": outcome, "client_kind": "cli", "client_version": _client_version(),
    }
    flush_pending(config)
    if not _post(config, payload):
        _append_pending(config, payload)


def report_daily(config: Config) -> None:
    if not _enabled(config):
        return
    today = datetime.now(timezone.utc).date().isoformat()
    counts = {"applied": 0, "expected_skip": 0, "no_compression": 0, "error": 0}
    original = returned = saved = 0
    try:
        with config.log_path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                timestamp = datetime.fromtimestamp(float(event.get("timestamp_unix", 0)), timezone.utc)
                if timestamp.date().isoformat() != today or "applied" not in event:
                    continue
                if str(event.get("reason") or "").lower() in NON_ATTEMPT_REASONS:
                    continue
                counts[_outcome(event)] += 1
                before = max(0, int(event.get("original_tokens_estimate", 0) or 0))
                after = max(0, int(event.get("returned_tokens_estimate", event.get("compressed_tokens_estimate", before)) or 0))
                original += before
                returned += after
                saved += max(0, int(event.get("estimated_tokens_saved", event.get("potential_context_tokens_saved", 0)) or 0))
    except OSError:
        return
    identity = hashlib.sha256((str(config.log_path) + today).encode()).hexdigest()[:32]
    payload = {
        "kind": "daily_aggregate", "request_id": identity,
        "occurred_at": datetime.now(timezone.utc).isoformat(), "client_kind": "cli",
        "client_version": _client_version(), **counts,
        "original_tokens_estimate": original, "returned_tokens_estimate": returned,
        "tokens_saved_estimate": saved,
    }
    flush_pending(config)
    _post(config, payload)
