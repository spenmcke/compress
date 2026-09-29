"""Run-scoped token and cost savings reported by the compress CLI."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import re
import shutil
from pathlib import Path
import sys
import tempfile
import threading
from typing import Any
import uuid

STATE_DIR_ENV = "COMPRESS_STATE_DIR"
RUN_ID_ENV = "COMPRESS_RUN_ID"
RUN_FILE_ENV = "COMPRESS_RUN_FILE"
PRICING_AS_OF = "2026-08-21"


@dataclass(frozen=True)
class Price:
    input: float
    cached_input: float
    output: float
    api_cache_write: float | None = None


# Standard USD rates per million tokens. Codex/Work does not charge for cache writes;
# API-key runs use api_cache_write when the model reports that bucket.
PRICES: dict[str, Price] = {
    "gpt-6-astra": Price(10.0, 1.0, 50.0, 12.5),
    "gpt-5.6-sol": Price(4.0, 0.4, 20.0, 5.0),
    "gpt-5.6-terra": Price(2.0, 0.2, 12.0, 2.5),
    "gpt-5.6-luna": Price(0.2, 0.02, 1.2, 0.25),
    "gpt-5.5": Price(5.0, 0.5, 30.0),
    "gpt-5.4": Price(2.5, 0.25, 15.0),
    "gpt-5.4-mini": Price(0.75, 0.075, 4.5),
    "gpt-5.3-codex": Price(1.75, 0.175, 14.0),
    "gpt-5.2": Price(1.75, 0.175, 14.0),
}


TOKEN_KEYS = (
    "input_uncached",
    "input_cached",
    "input_cache_write",
    "output",
)
AVOIDED_KEYS = (
    "input_uncached",
    "input_cached",
    "input_cache_write",
)


@dataclass(frozen=True)
class RewriteObservation:
    """Estimated model-input tokens removed from one tool result."""

    call_id: str
    saved_tokens: int


def state_dir() -> Path:
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override).expanduser()
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return root / "compress"


def run_path(run_id: str) -> Path:
    return state_dir() / "runs" / f"{run_id}.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_tokens(keys: tuple[str, ...] = TOKEN_KEYS) -> dict[str, int]:
    return {key: 0 for key in keys}


def _model_key(model: Any) -> str:
    value = model if isinstance(model, str) and model else "unknown"
    lowered = value.casefold()
    for key in sorted(PRICES, key=len, reverse=True):
        if lowered == key or re.fullmatch(re.escape(key) + r"-\d{4}-\d{2}-\d{2}", lowered):
            return key
    return value


def extract_usage(response: Any) -> tuple[str, dict[str, int]] | None:
    """Extract mutually exclusive input buckets plus output from a response."""
    if not isinstance(response, dict) or not isinstance(response.get("usage"), dict):
        return None
    usage = response["usage"]
    # Empty/partial usage is not a measured zero. In particular, do not seal a
    # response's accounting before a later event supplies the actual counters.
    if any(type(usage.get(key)) is not int or usage[key] < 0
           for key in ("input_tokens", "output_tokens")):
        return None
    details = usage.get("input_tokens_details")
    details = details if isinstance(details, dict) else {}

    def integer(value: Any) -> int:
        return max(0, value) if isinstance(value, int) and not isinstance(value, bool) else 0

    input_tokens = integer(usage.get("input_tokens"))
    cached = integer(
        details.get("cached_tokens", usage.get("cached_input_tokens"))
    )
    cache_write = integer(
        details.get("cache_write_tokens", usage.get("cache_write_input_tokens"))
    )
    # Be conservative if an upstream reports inconsistent detail fields.
    cached = min(cached, input_tokens)
    cache_write = min(cache_write, input_tokens - cached)
    tokens = {
        "input_uncached": input_tokens - cached - cache_write,
        "input_cached": cached,
        "input_cache_write": cache_write,
        "output": integer(usage.get("output_tokens")),
    }
    return _model_key(response.get("model")), tokens


class RunTracker:
    """Thread-safe aggregate accounting for one proxy/Codex process."""

    def __init__(
        self,
        *,
        cwd: Path,
        billing_mode: str,
        run_id: str | None = None,
        path: Path | None = None,
    ) -> None:
        self.run_id = run_id or str(uuid.uuid4())
        self.path = path or run_path(self.run_id)
        self._lock = threading.Lock()
        self._seen_call_ids: set[str] = set()
        self._seen_response_ids: set[str] = set()
        self._response_observations: dict[str, dict[str, int]] = {}
        now = _now()
        self._data: dict[str, Any] = {
            "schema_version": 1,
            "run_id": self.run_id,
            "integration": "codex",
            "cwd": str(cwd.resolve()),
            "billing_mode": billing_mode,
            "pricing_as_of": PRICING_AS_OF,
            "started_at": now,
            "updated_at": now,
            "finished_at": None,
            "exit_code": None,
            "responses": 0,
            "usage_responses": 0,
            "observed_avoided_tokens_estimate": 0,
            "compressed_results": 0,
            "actual": _empty_tokens(),
            "avoided": _empty_tokens(AVOIDED_KEYS),
            "models": [],
            "by_model": {},
        }
        self._write_locked()

    def _write_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(self._data, sort_keys=True, separators=(",", ":"))
                    + "\n"
                )
            os.replace(temporary, self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _classify_avoided(
        saved: int, *, first: bool, actual: dict[str, int]
    ) -> dict[str, int]:
        result = _empty_tokens(AVOIDED_KEYS)
        if saved <= 0:
            return result
        if not first and actual["input_cached"] > 0:
            result["input_cached"] = saved
            return result
        noncached = actual["input_uncached"] + actual["input_cache_write"]
        if noncached <= 0:
            result["input_uncached"] = saved
            return result
        written = round(saved * actual["input_cache_write"] / noncached)
        result["input_cache_write"] = written
        result["input_uncached"] = saved - written
        return result

    def complete_response(
        self, response: Any, observations: list[RewriteObservation]
    ) -> bool:
        """Record a response; return whether valid usage was newly accounted.

        Each response retains its own rewrites, including repeated history.
        Only rewrites paired with usage enter the session savings fraction.
        Missing usage may be supplied later with the same response ID.
        """
        if not isinstance(response, dict):
            return False
        extracted = extract_usage(response)
        observed_model = _model_key(response.get("model"))
        response_id = response.get("id")
        if not isinstance(response_id, str) or not response_id:
            response_id = str(uuid.uuid4())
        grouped: dict[str, int] = {}
        for observation in observations:
            if observation.saved_tokens > 0:
                grouped[observation.call_id] = (
                    grouped.get(observation.call_id, 0) + observation.saved_tokens
                )
        with self._lock:
            if response_id in self._seen_response_ids:
                return False
            is_new = response_id not in self._response_observations
            if is_new:
                self._response_observations[response_id] = grouped
                self._data["responses"] += 1
                self._data["compressed_results"] += len(grouped)
                self._data["observed_avoided_tokens_estimate"] += sum(grouped.values())
            else:
                grouped = self._response_observations[response_id]
            if observed_model != "unknown" and observed_model not in self._data["models"]:
                self._data["models"].append(observed_model)
            if extracted is None:
                if is_new:
                    self._data["updated_at"] = _now()
                    self._write_locked()
                return False
            model, actual = extracted
            avoided = _empty_tokens(AVOIDED_KEYS)
            for call_id, saved in grouped.items():
                classified = self._classify_avoided(
                    saved, first=call_id not in self._seen_call_ids, actual=actual
                )
                for key in AVOIDED_KEYS:
                    avoided[key] += classified[key]
                self._seen_call_ids.add(call_id)
            self._seen_response_ids.add(response_id)
            # Only pending usage needs the individual observations in memory.
            self._response_observations.pop(response_id)

            self._data["usage_responses"] += 1
            for key in TOKEN_KEYS:
                self._data["actual"][key] += actual[key]
            for key in AVOIDED_KEYS:
                self._data["avoided"][key] += avoided[key]
            by_model = self._data["by_model"].setdefault(
                model,
                {
                    "actual": _empty_tokens(),
                    "avoided": _empty_tokens(AVOIDED_KEYS),
                },
            )
            for key in TOKEN_KEYS:
                by_model["actual"][key] += actual[key]
            for key in AVOIDED_KEYS:
                by_model["avoided"][key] += avoided[key]
            self._data["updated_at"] = _now()
            self._write_locked()
        return True

    def finish(self, exit_code: int) -> None:
        with self._lock:
            now = _now()
            self._data["updated_at"] = now
            self._data["finished_at"] = now
            self._data["exit_code"] = exit_code
            self._write_locked()


def _rate(price: Price, bucket: str, billing_mode: str) -> float:
    if bucket == "input_uncached":
        return price.input
    if bucket == "input_cached":
        return price.cached_input
    if bucket == "input_cache_write":
        if billing_mode == "codex":
            return 0.0
        return price.api_cache_write if price.api_cache_write is not None else price.input
    if bucket == "output":
        return price.output
    raise KeyError(bucket)


def calculate(summary: dict[str, Any]) -> dict[str, Any]:
    actual = summary.get("actual") or {}
    avoided = summary.get("avoided") or {}
    actual_tokens = sum(int(actual.get(key, 0)) for key in TOKEN_KEYS)
    avoided_tokens = sum(int(avoided.get(key, 0)) for key in AVOIDED_KEYS)
    without_tokens = actual_tokens + avoided_tokens
    responses = int(summary.get("responses", 0))
    usage_responses = int(summary.get("usage_responses", responses))
    usage_complete = responses > 0 and usage_responses == responses
    actual_cost = 0.0
    avoided_cost = 0.0
    unknown_models: list[str] = []
    billing_mode = summary.get("billing_mode", "codex")
    for model, values in (summary.get("by_model") or {}).items():
        price = PRICES.get(_model_key(model))
        if price is None:
            unknown_models.append(model)
            continue
        for key in TOKEN_KEYS:
            actual_cost += int(values["actual"].get(key, 0)) * _rate(
                price, key, billing_mode
            ) / 1_000_000
        for key in AVOIDED_KEYS:
            avoided_cost += int(values["avoided"].get(key, 0)) * _rate(
                price, key, billing_mode
            ) / 1_000_000
    return {
        "accounting": "per_response",
        "usage_complete": usage_complete,
        "usage_responses": usage_responses,
        "responses": responses,
        "observed_avoided_tokens_estimate": summary.get(
            "observed_avoided_tokens_estimate", avoided_tokens
        ),
        "actual_tokens": actual_tokens,
        "avoided_tokens_estimate": avoided_tokens,
        "without_compress_tokens_estimate": without_tokens if usage_complete else None,
        # S / (A + S): both sums cover the same responses. Cached input is
        # included in A once; reasoning tokens are already part of output.
        "token_reduction_percent": (
            (100 * avoided_tokens / without_tokens if without_tokens else 0.0)
            if usage_complete else None
        ),
        "actual_cost_usd": None if unknown_models or not usage_complete else actual_cost,
        "avoided_cost_usd_estimate": None if unknown_models or not usage_complete else avoided_cost,
        "without_compress_cost_usd_estimate": (
            None if unknown_models or not usage_complete else actual_cost + avoided_cost
        ),
        "cost_reduction_percent": (
            None
            if unknown_models or not usage_complete or actual_cost + avoided_cost == 0
            else 100 * avoided_cost / (actual_cost + avoided_cost)
        ),
        "unknown_models": unknown_models,
    }


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("run summary must be a JSON object")
    return value


def find_run(cwd: Path) -> tuple[Path, dict[str, Any]]:
    explicit = os.environ.get(RUN_FILE_ENV)
    if explicit:
        path = Path(explicit)
        summary = _load(path)
        return path, summary
    run_id = os.environ.get(RUN_ID_ENV)
    if run_id:
        path = run_path(run_id)
        summary = _load(path)
        return path, summary
    resolved = str(cwd.resolve())
    candidates: list[tuple[str, Path, dict[str, Any]]] = []
    for path in (state_dir() / "runs").glob("*.json"):
        try:
            summary = _load(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if summary.get("cwd") == resolved:
            candidates.append((str(summary.get("updated_at", "")), path, summary))
    if not candidates:
        raise FileNotFoundError("no compress run found for this directory")
    _, path, summary = max(candidates, key=lambda item: item[0])
    return path, summary


def render(summary: dict[str, Any], *, details: bool = False,
           color: bool = False, width: int = 80, unicode: bool = True) -> str:
    from .savings_display import render as render_card

    return render_card(summary, calculate(summary), details=details,
                       color=color, width=width, unicode=unicode)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--details", action="store_true", help="show price sources and accounting details")


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="compress savings")
    add_arguments(parser)
    args = parser.parse_args(argv)
    try:
        path, summary = find_run(Path.cwd())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    if args.as_json:
        print(
            json.dumps(
                {"run_file": str(path), "summary": summary, "totals": calculate(summary)},
                indent=2,
                sort_keys=True,
            )
        )
    else:
        terminal = sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
        encoding = (sys.stdout.encoding or "").lower().replace("-", "")
        print(render(summary, details=args.details,
                     color=terminal and "NO_COLOR" not in os.environ,
                     width=shutil.get_terminal_size((80, 24)).columns,
                     unicode=terminal and encoding in {"utf8", "utf_8"}))
    return 0


def main() -> int:
    arguments = sys.argv[1:]
    if arguments[:1] == ["savings"]:
        arguments = arguments[1:]
    return cli(arguments)


if __name__ == "__main__":
    main()
