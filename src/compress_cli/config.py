"""Configuration loading with environment-variable overrides."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class Config:
    endpoint: str = "http://127.0.0.1:8001/v1"
    model: str = "cmprs"
    timeout_seconds: float = 120.0
    min_output_tokens: int = 512
    max_goal_chars: int = 4000
    temperature: float = 0.6
    top_p: float = 0.95
    top_k: int = 20
    min_p: float = 0.0
    presence_penalty: float = 0.0
    repetition_penalty: float = 1.0
    log_path: Path = Path(".codex/compress/events.jsonl")
    api_key_file: Path | None = None
    retry_503_seconds: float = 0.0
    # Customer gateway policy; legacy CLI retries remain opt-in and unchanged.
    retry_http_attempts: int = 0


def _find_config(cwd: Path) -> Path | None:
    explicit = os.environ.get("COMPRESS_CODEX_CONFIG")
    if explicit:
        return Path(explicit).expanduser().resolve()
    for directory in (cwd, *cwd.parents):
        candidate = directory / "compress.toml"
        if candidate.is_file():
            return candidate
    return None


def load_config(cwd: str | Path, *, config_path: Path | None = None) -> Config:
    """Load compress.toml and supported environment overrides."""
    cwd_path = Path(cwd).resolve()
    config_path = config_path or _find_config(cwd_path)
    data: dict = {}
    base = cwd_path
    if config_path is not None:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
        base = config_path.parent

    compressor = data.get("compressor", {})
    logging = data.get("logging", {})
    raw_log_path = os.environ.get(
        "COMPRESS_CODEX_LOG_PATH", str(logging.get("path", ".codex/compress/events.jsonl"))
    )
    log_path = Path(raw_log_path).expanduser()
    if not log_path.is_absolute():
        log_path = base / log_path

    raw_api_key_file = os.environ.get(
        "COMPRESS_API_KEY_FILE", compressor.get("api_key_file")
    )
    api_key_file = None
    if raw_api_key_file:
        api_key_file = Path(str(raw_api_key_file)).expanduser()
        if not api_key_file.is_absolute():
            api_key_file = base / api_key_file

    return Config(
        endpoint=os.environ.get(
            "COMPRESS_ENDPOINT", str(compressor.get("endpoint", Config.endpoint))
        ).rstrip("/"),
        model=os.environ.get("COMPRESS_MODEL", str(compressor.get("model", Config.model))),
        timeout_seconds=float(
            os.environ.get(
                "COMPRESS_TIMEOUT_SECONDS",
                compressor.get("timeout_seconds", Config.timeout_seconds),
            )
        ),
        min_output_tokens=int(
            os.environ.get(
                "COMPRESS_MIN_OUTPUT_TOKENS",
                compressor.get("min_output_tokens", Config.min_output_tokens),
            )
        ),
        max_goal_chars=int(compressor.get("max_goal_chars", Config.max_goal_chars)),
        temperature=float(compressor.get("temperature", Config.temperature)),
        top_p=float(compressor.get("top_p", Config.top_p)),
        top_k=int(compressor.get("top_k", Config.top_k)),
        min_p=float(compressor.get("min_p", Config.min_p)),
        presence_penalty=float(
            compressor.get("presence_penalty", Config.presence_penalty)
        ),
        repetition_penalty=float(
            compressor.get("repetition_penalty", Config.repetition_penalty)
        ),
        log_path=log_path.resolve(),
        api_key_file=api_key_file.resolve() if api_key_file else None,
        retry_503_seconds=float(
            os.environ.get(
                "COMPRESS_RETRY_503_SECONDS",
                compressor.get("retry_503_seconds", Config.retry_503_seconds),
            )
        ),
    )


def load_api_key(config: Config) -> str | None:
    """Load a bearer token without including it in the configuration object."""
    environment_key = os.environ.get("COMPRESS_API_KEY")
    if environment_key:
        return environment_key
    if config.api_key_file is None:
        return None
    try:
        key = config.api_key_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return key or None
