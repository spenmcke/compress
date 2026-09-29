"""compress authentication and local credential lifecycle."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
import webbrowser
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from .config import Config


DEVICE_CLIENT_ID = "everest-cli"


class AuthError(RuntimeError):
    """A user-safe authentication failure that never contains a credential."""


def is_compress_endpoint(endpoint: str) -> bool:
    return urlsplit(endpoint).hostname == "api.everestagi.com"


def _write_private(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def verify(token: str, endpoint: str, *, timeout: float = 15.0) -> None:
    """Validate an compress access token without persisting or echoing it."""
    request = Request(
        f"{endpoint.rstrip('/')}/models",
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        error.close()
        if error.code in {401, 403}:
            raise AuthError("compress rejected that access token") from None
        raise AuthError("compress is temporarily unavailable; try again shortly") from None
    except (URLError, TimeoutError, OSError):
        raise AuthError("Could not reach compress; check your connection and try again") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise AuthError("compress returned an invalid response; try again shortly") from None
    models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise AuthError("compress returned an invalid response; try again shortly")


def login(config: Config, token: str, *, timeout: float = 15.0) -> Path:
    """Verify and atomically store an compress access token."""
    clean_token = token.strip()
    if not clean_token:
        raise AuthError("access token must not be blank")
    if config.api_key_file is None:
        raise AuthError("compress is not installed; run the installer first")
    verify(clean_token, config.endpoint, timeout=timeout)
    _write_private(config.api_key_file, clean_token + "\n")
    return config.api_key_file


def _post_json(url: str, payload: dict[str, object], *, timeout: float) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as error:
        try:
            raw = error.read()
        finally:
            error.close()
        if error.code == 404:
            raise AuthError(
                "compress browser login is not available on this server yet"
            ) from None
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = None
        if isinstance(body, dict) and isinstance(body.get("error"), str):
            return body
        if error.code in {401, 403}:
            raise AuthError("compress denied the authorization request") from None
        raise AuthError("compress is temporarily unavailable; try again shortly") from None
    except (URLError, TimeoutError, OSError):
        raise AuthError("Could not reach compress; check your connection and try again") from None
    try:
        body = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise AuthError("compress returned an invalid response; try again shortly") from None
    if not isinstance(body, dict):
        raise AuthError("compress returned an invalid response; try again shortly")
    return body


def device_login(
    config: Config,
    *,
    timeout: float = 15.0,
    announce: Callable[[str], None] = print,
    open_browser: Callable[[str], object] = webbrowser.open,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Path:
    """Authorize in a browser, then atomically store the issued compress credential."""
    if config.api_key_file is None:
        raise AuthError("compress is not installed; run the installer first")
    auth_url = f"{config.endpoint.rstrip('/')}/auth/device"
    issued = _post_json(
        f"{auth_url}/code", {"client_id": DEVICE_CLIENT_ID}, timeout=timeout
    )
    try:
        device_code = str(issued["device_code"])
        user_code = str(issued["user_code"])
        verification_uri = str(issued["verification_uri"])
        verification_uri_complete = str(
            issued.get("verification_uri_complete") or verification_uri
        )
        expires_in = float(issued["expires_in"])
        interval = max(1.0, float(issued.get("interval", 5)))
    except (KeyError, TypeError, ValueError):
        raise AuthError("compress returned an invalid response; try again shortly") from None
    if not device_code or not user_code or not verification_uri or expires_in <= 0:
        raise AuthError("compress returned an invalid response; try again shortly")

    announce(f"Your compress authorization code is: {user_code}")
    announce(f"Open this page to continue: {verification_uri_complete}")
    try:
        open_browser(verification_uri_complete)
    except Exception:
        pass

    deadline = monotonic() + expires_in
    while True:
        if monotonic() >= deadline:
            raise AuthError("compress authorization timed out; run `compress login` again")
        sleep(min(interval, max(0.0, deadline - monotonic())))
        if monotonic() >= deadline:
            raise AuthError("compress authorization timed out; run `compress login` again")
        result = _post_json(
            f"{auth_url}/token",
            {"client_id": DEVICE_CLIENT_ID, "device_code": device_code},
            timeout=timeout,
        )
        access_token = result.get("access_token")
        if isinstance(access_token, str) and access_token.strip():
            _write_private(config.api_key_file, access_token.strip() + "\n")
            return config.api_key_file
        error = result.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5.0
            continue
        if error == "access_denied":
            raise AuthError("compress authorization was denied")
        if error == "expired_token":
            raise AuthError("compress authorization expired; run `compress login` again")
        raise AuthError("compress returned an invalid response; try again shortly")


def logout(config: Config, *, timeout: float = 5.0) -> bool:
    """Remove the locally stored compress credential."""
    if config.api_key_file is None:
        return False
    try:
        token = config.api_key_file.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if token and is_compress_endpoint(config.endpoint):
        request = Request(
            f"{config.endpoint.rstrip('/')}/auth/logout",
            data=b"{}",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout):
                pass
        except (HTTPError, URLError, TimeoutError, OSError):
            pass
    try:
        config.api_key_file.unlink()
    except FileNotFoundError:
        return False
    return True


def credential_is_set(config: Config) -> bool:
    if config.api_key_file is None:
        return False
    try:
        return bool(config.api_key_file.read_text(encoding="utf-8").strip())
    except OSError:
        return False
