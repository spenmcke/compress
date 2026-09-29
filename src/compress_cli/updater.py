"""Secure, fail-open updates for the compress CLI."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
from importlib.metadata import version as package_version
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


from .service import DOWNLOAD_BASE_URL


PACKAGE_NAME = "compress-cli"
MANIFEST_URL = f"{DOWNLOAD_BASE_URL}/latest.json"
CHANNEL = "stable"
SCHEMA_VERSION = 1
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
NETWORK_TIMEOUT_SECONDS = 3.0
MAX_MANIFEST_BYTES = 16 * 1024
MAX_WHEEL_BYTES = 50 * 1024 * 1024
REEXEC_ENV = "COMPRESS_UPDATE_REEXEC"
_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class UpdateError(RuntimeError):
    """An update was rejected or could not be installed."""


@dataclass(frozen=True)
class Release:
    version: str
    wheel_path: str
    sha256: str


@dataclass(frozen=True)
class UpdateResult:
    current_version: str
    latest_version: str | None = None
    updated: bool = False
    skipped: bool = False

    @property
    def available(self) -> bool:
        return self.latest_version is not None and _version_key(
            self.latest_version
        ) > _version_key(self.current_version)


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def _version_key(value: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(value)
    if match is None:
        raise UpdateError(f"invalid semantic version: {value!r}")
    return tuple(int(part) for part in match.groups())


def _cache_dir() -> Path:
    configured = os.environ.get("XDG_CACHE_HOME")
    if configured:
        return Path(configured).expanduser() / "compress"
    return Path.home() / ".cache" / "compress"


def _paths() -> tuple[Path, Path]:
    root = _cache_dir()
    return root / "update.lock", root / "update-check.json"


def _require_download_base_url() -> None:
    if not DOWNLOAD_BASE_URL:
        raise UpdateError(
            "updates are not configured; set COMPRESS_DOWNLOAD_BASE_URL"
        )
    parsed = urlsplit(DOWNLOAD_BASE_URL)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise UpdateError(
            "COMPRESS_DOWNLOAD_BASE_URL must be an HTTPS URL without credentials, "
            "a query, or a fragment"
        )


def _read_limited(url: str, limit: int) -> bytes:
    _require_download_base_url()
    parsed = urlsplit(url)
    trusted = urlsplit(DOWNLOAD_BASE_URL)
    trusted_prefix = trusted.path.rstrip("/") + "/"
    if (
        parsed.scheme != trusted.scheme
        or parsed.netloc != trusted.netloc
        or not parsed.path.startswith(trusted_prefix)
        or parsed.username
        or parsed.password
    ):
        raise UpdateError("update URL is outside the trusted download origin")
    request = Request(url, headers={"User-Agent": "compress-cli-updater/1"})
    opener = build_opener(_RejectRedirects)
    try:
        with opener.open(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:
            if response.geturl() != url:
                raise UpdateError("update download redirected unexpectedly")
            content = response.read(limit + 1)
    except HTTPError as error:
        if 300 <= error.code < 400:
            raise UpdateError("update download redirected unexpectedly") from error
        raise UpdateError(f"update server returned HTTP {error.code}") from error
    if len(content) > limit:
        raise UpdateError("update response is too large")
    return content


def _parse_manifest(content: bytes) -> Release:
    try:
        value: Any = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UpdateError("update manifest is not valid UTF-8 JSON") from error
    expected_keys = {"schema_version", "channel", "version", "wheel_path", "sha256"}
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise UpdateError("update manifest has an invalid schema")
    if value["schema_version"] != SCHEMA_VERSION or value["channel"] != CHANNEL:
        raise UpdateError("update manifest has an unsupported schema or channel")
    if not all(isinstance(value[key], str) for key in ("version", "wheel_path", "sha256")):
        raise UpdateError("update manifest fields have invalid types")
    release_version = value["version"]
    _version_key(release_version)
    expected_path = (
        f"v{release_version}/"
        f"compress_cli-{release_version}-py3-none-any.whl"
    )
    if value["wheel_path"] != expected_path:
        raise UpdateError("update manifest contains an invalid release path")
    digest = value["sha256"].lower()
    if _SHA256.fullmatch(digest) is None:
        raise UpdateError("update manifest contains an invalid SHA-256")
    return Release(release_version, expected_path, digest)


def _fetch_release() -> Release:
    return _parse_manifest(_read_limited(MANIFEST_URL, MAX_MANIFEST_BYTES))


def _last_check_is_fresh(path: Path, now: float) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        checked_at = value["checked_at"]
        return isinstance(checked_at, (int, float)) and 0 <= now - checked_at < CHECK_INTERVAL_SECONDS
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _record_check(path: Path, now: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".update-check.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"checked_at": now}, stream)
            stream.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _download_and_install(release: Release) -> None:
    wheel_url = urljoin(f"{DOWNLOAD_BASE_URL}/", release.wheel_path)
    content = _read_limited(wheel_url, MAX_WHEEL_BYTES)
    if hashlib.sha256(content).hexdigest() != release.sha256:
        raise UpdateError("downloaded wheel failed SHA-256 verification")
    uv = shutil.which("uv")
    if uv is None:
        raise UpdateError("uv is required to update compress")
    with tempfile.TemporaryDirectory(prefix="compress-update-") as directory:
        wheel = Path(directory) / Path(release.wheel_path).name
        wheel.write_bytes(content)
        completed = subprocess.run(
            [uv, "tool", "install", "--force", str(wheel)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "uv failed"
            raise UpdateError(f"could not install compress {release.version}: {detail}")


def update(*, install: bool, force: bool = False, current_version: str | None = None) -> UpdateResult:
    """Check for a release and optionally install it while holding the update lock."""
    _require_download_base_url()
    current = current_version or package_version(PACKAGE_NAME)
    _version_key(current)
    lock_path, check_path = _paths()
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open("a+b") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        now = time.time()
        if not force and _last_check_is_fresh(check_path, now):
            return UpdateResult(current_version=current, skipped=True)
        _record_check(check_path, now)
        release = _fetch_release()
        result = UpdateResult(current_version=current, latest_version=release.version)
        if not result.available or not install:
            return result
        _download_and_install(release)
        return UpdateResult(
            current_version=current,
            latest_version=release.version,
            updated=True,
        )


def auto_update() -> bool:
    """Install an available update, returning whether the caller should re-exec."""
    if os.environ.get("COMPRESS_AUTO_UPDATE") == "0" or os.environ.get(REEXEC_ENV) == "1":
        return False
    try:
        return update(install=True).updated
    except Exception as error:
        print(f"compress: automatic update unavailable ({error}); continuing.", file=sys.stderr)
        return False


def reexec(arguments: list[str]) -> None:
    """Replace this process with the freshly installed compress command."""
    os.environ[REEXEC_ENV] = "1"
    invoked = Path(sys.argv[0])
    executable = str(invoked) if invoked.is_absolute() else shutil.which(sys.argv[0])
    if executable is None:
        executable = os.path.abspath(sys.argv[0])
    os.execv(executable, [executable, *arguments])
