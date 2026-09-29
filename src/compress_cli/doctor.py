"""Diagnose cmprs configuration and OpenAI-compatible endpoint availability."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import socket
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .config import Config, _find_config, load_api_key, load_config


def _config_sources(cwd: Path) -> dict[str, str]:
    config_path = _find_config(cwd)
    if os.environ.get("COMPRESS_ENDPOINT"):
        endpoint_source = "COMPRESS_ENDPOINT environment variable"
    elif config_path is not None:
        endpoint_source = str(config_path)
    else:
        endpoint_source = "built-in default"
    return {
        "config": str(config_path) if config_path is not None else "built-in defaults",
        "endpoint": endpoint_source,
    }


def _safe_endpoint(endpoint: str) -> tuple[str, str]:
    """Return a printable endpoint and its /models URL, rejecting unsafe shapes."""
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("endpoint must be an http:// or https:// URL with a host")
    if parsed.username or parsed.password:
        raise ValueError("endpoint must not contain credentials; use COMPRESS_API_KEY")
    if parsed.query or parsed.fragment:
        raise ValueError("endpoint must not contain a query string or fragment")
    printable = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
    return printable, f"{printable}/models"


def probe(config: Config, *, timeout: float = 5.0) -> dict[str, Any]:
    """Probe the OpenAI-compatible models endpoint without making a completion."""
    endpoint, models_url = _safe_endpoint(config.endpoint)
    headers = {"Accept": "application/json"}
    api_key = load_api_key(config)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(models_url, headers=headers, method="GET")
    result: dict[str, Any] = {
        "endpoint": endpoint,
        "models_url": models_url,
        "reachable": False,
        "model": config.model,
        "model_available": None,
    }
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        result.update(error=f"HTTP {error.code}", advice=_http_advice(error.code))
        return result
    except URLError as error:
        reason = error.reason
        if isinstance(reason, ConnectionRefusedError):
            detail = "connection refused"
        elif isinstance(reason, socket.timeout):
            detail = "connection timed out"
        else:
            detail = type(reason).__name__ if not isinstance(reason, str) else reason
        result.update(error=detail, advice=_connection_advice(endpoint))
        return result
    except (TimeoutError, socket.timeout):
        result.update(error="connection timed out", advice=_connection_advice(endpoint))
        return result
    except (json.JSONDecodeError, UnicodeDecodeError):
        result.update(
            reachable=True,
            error="/models did not return valid JSON",
            advice="Verify that the endpoint includes the OpenAI-compatible /v1 base path.",
        )
        return result

    result["reachable"] = True
    raw_models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(raw_models, list):
        result.update(
            error="/models response has no data list",
            advice="Verify that this is an OpenAI-compatible API endpoint.",
        )
        return result
    model_ids = sorted(
        str(item["id"])
        for item in raw_models
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    )
    result["available_models"] = model_ids
    result["model_available"] = config.model in model_ids
    if not result["model_available"]:
        result["error"] = "configured model is not advertised by /models"
        result["advice"] = (
            "Set COMPRESS_MODEL (or compressor.model in compress.toml) to a listed model, "
            "or serve the checkpoint under the configured name."
        )
    return result


def _http_advice(status: int) -> str:
    if status in {401, 403}:
        return (
            "Run `compress login`, then retry."
        )
    if status == 404:
        return "Verify that the endpoint includes the OpenAI-compatible /v1 base path."
    return "Check the compressor service logs and endpoint configuration."


def _connection_advice(endpoint: str) -> str:
    host = urlsplit(endpoint).hostname
    if host in {"127.0.0.1", "localhost", "::1"}:
        if importlib.util.find_spec("vllm") is not None and platform.system() == "Linux":
            return "Start the local compressor, then retry."
        return "compress is not configured. Reinstall compress, then retry."
    return "compress is temporarily unavailable. Try again shortly."


def diagnose(cwd: Path, *, timeout: float = 5.0) -> dict[str, Any]:
    sources = _config_sources(cwd)
    config = load_config(cwd)
    result: dict[str, Any] = {
        "ok": False,
        "cwd": str(cwd.resolve()),
        "config_source": sources["config"],
        "endpoint_source": sources["endpoint"],
        "api_key": "set" if load_api_key(config) else "not set",
        "effective": {
            "model": config.model,
            "timeout_seconds": config.timeout_seconds,
            "min_output_tokens": config.min_output_tokens,
            "log_path": str(config.log_path),
        },
    }
    check = probe(config, timeout=timeout)
    result["probe"] = check
    result["ok"] = bool(check["reachable"] and check["model_available"])
    return result


def _print_human(result: dict[str, Any]) -> None:
    check = result.get("probe", {})
    marker = "OK" if result.get("ok") else "ERROR"
    print(f"compress doctor: {marker}")
    print(f"Config: {result['config_source']}")
    print(f"Endpoint source: {result['endpoint_source']}")
    print(f"Endpoint: {check.get('endpoint', 'invalid')}")
    print(f"API key: {result['api_key']}")
    print(f"Model: {result['effective']['model']}")
    print(f"Models endpoint reachable: {'yes' if check.get('reachable') else 'no'}")
    model_available = check.get("model_available")
    model_status = "not checked" if model_available is None else ("yes" if model_available else "no")
    print(f"Configured model available: {model_status}")
    if check.get("available_models"):
        print(f"Advertised models: {', '.join(check['available_models'])}")
    if check.get("error"):
        print(f"Problem: {check['error']}")
    if check.get("advice"):
        print(f"Next step: {check['advice']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    try:
        result = diagnose(args.cwd, timeout=args.timeout)
    except (OSError, ValueError, TypeError) as error:
        result = {
            "ok": False,
            "config_source": str(_find_config(args.cwd) or "built-in defaults"),
            "error": f"{type(error).__name__}: {error}",
            "advice": "Reinstall compress, then retry.",
        }
    if args.as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif "probe" in result:
        _print_human(result)
    else:
        print("compress doctor: ERROR")
        print(f"Problem: {result['error']}")
        print(f"Next step: {result['advice']}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
