"""compress command-line interface."""

from __future__ import annotations

import argparse
from importlib.metadata import version
import os
from pathlib import Path
import shutil
import subprocess
import sys

from . import auth, doctor, proxy, savings, updater
from .config import Config, load_config
from .install import InstallError, config_dir, install, uninstall


PACKAGE_NAME = "compress-cli"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="compress")
    parser.add_argument("--version", action="version", version=f"compress {version(PACKAGE_NAME)}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    savings_parser = subparsers.add_parser("savings", help="show savings for this run")
    savings.add_arguments(savings_parser)

    login_parser = subparsers.add_parser("login", help="log in to compress")
    login_parser.add_argument("--token-stdin", action="store_true", help=argparse.SUPPRESS)
    login_parser.add_argument("--timeout", type=float, default=15.0, help=argparse.SUPPRESS)

    subparsers.add_parser("logout", help="log out of compress on this computer")

    install_parser = subparsers.add_parser("install", help="configure compress for Codex")
    install_parser.add_argument("--shell", choices=("auto", "zsh", "bash"), default="auto")
    install_parser.add_argument("--endpoint", help=argparse.SUPPRESS)
    install_parser.add_argument(
        "--token-stdin", action="store_true", help=argparse.SUPPRESS
    )
    install_parser.add_argument("--force", action="store_true")

    uninstall_parser = subparsers.add_parser("uninstall", help="remove compress")
    uninstall_parser.add_argument("--purge", action="store_true")
    uninstall_parser.add_argument("--keep-cli", action="store_true", help=argparse.SUPPRESS)

    doctor_parser = subparsers.add_parser("doctor", help="check the compress connection")
    doctor_parser.add_argument("--timeout", type=float, default=5.0)
    doctor_parser.add_argument("--json", action="store_true", dest="as_json")

    update_parser = subparsers.add_parser("update", help="update compress to the latest release")
    update_parser.add_argument(
        "--check", action="store_true", help="check for an update without installing it"
    )

    return parser


def _remove_cli() -> None:
    uv = shutil.which("uv")
    if uv is None:
        print(
            f"compress integration removed. Remove the {PACKAGE_NAME} package with its installer.",
            file=sys.stderr,
        )
        return
    completed = subprocess.run(
        [uv, "tool", "uninstall", PACKAGE_NAME],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        print(
            f"compress integration was removed, but the CLI package remains: {detail}",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "_codex":
        if updater.auto_update():
            try:
                updater.reexec(arguments)
            except OSError as error:
                print(f"compress: could not restart after updating ({error}); continuing.", file=sys.stderr)
        global_config = config_dir() / "config.toml"
        if global_config.exists():
            os.environ.setdefault("COMPRESS_CODEX_CONFIG", str(global_config))
        config = load_config(Path.cwd())
        if auth.is_compress_endpoint(config.endpoint) and not auth.credential_is_set(config):
            print("compress is not logged in. Run: compress login", file=sys.stderr)
            return 1
        proxy.launch(arguments[1:])
        return 0
    parser = _parser()
    args = parser.parse_args(arguments)

    if args.command == "savings":
        options = ["--json"] if args.as_json else []
        if args.details:
            options.append("--details")
        return savings.cli(options)
    if args.command == "login":
        global_config = config_dir() / "config.toml"
        if not global_config.exists():
            parser.error("compress is not installed; run the installer first")
        os.environ.setdefault("COMPRESS_CODEX_CONFIG", str(global_config))
        try:
            config = load_config(Path.cwd())
            if args.token_stdin:
                auth.login(config, sys.stdin.read().strip(), timeout=args.timeout)
            else:
                auth.device_login(config, timeout=args.timeout)
        except auth.AuthError as error:
            parser.error(str(error))
        print("Logged in to compress.")
        return 0
    if args.command == "logout":
        global_config = config_dir() / "config.toml"
        if global_config.exists():
            os.environ.setdefault("COMPRESS_CODEX_CONFIG", str(global_config))
        removed = auth.logout(load_config(Path.cwd()))
        print("Logged out of compress." if removed else "compress is already logged out.")
        return 0
    if args.command == "doctor":
        global_config = config_dir() / "config.toml"
        if global_config.exists():
            os.environ.setdefault("COMPRESS_CODEX_CONFIG", str(global_config))
        forwarded = ["--timeout", str(args.timeout)]
        if args.as_json:
            forwarded.append("--json")
        return doctor.main(forwarded)
    if args.command == "update":
        try:
            result = updater.update(install=not args.check, force=True)
        except Exception as error:
            print(f"compress update failed: {error}", file=sys.stderr)
            return 1
        if not result.available:
            print(f"compress {result.current_version} is up to date.")
        elif args.check:
            print(
                f"compress {result.latest_version} is available "
                f"(installed: {result.current_version})."
            )
        else:
            print(f"compress was updated to {result.latest_version}.")
        return 0
    if args.command == "install":
        if shutil.which("codex") is None:
            parser.error("codex is not on PATH; install Codex before configuring compress")
        token = sys.stdin.read().strip() if args.token_stdin else None
        try:
            result = install(
                shell=args.shell,
                endpoint=args.endpoint,
                api_key=token,
                force=args.force,
                bin_dir=Path(sys.argv[0]).resolve().parent,
            )
        except (InstallError, OSError) as error:
            parser.error(str(error))
        print(f"compress is installed for {result.shell}.")
        print(f"Open a new terminal or run: source {result.rc_path}")
        credentials = config_dir() / "credentials"
        if not auth.credential_is_set(Config(api_key_file=credentials)):
            print("Next: run compress login")
        return 0
    if args.command == "uninstall":
        removed = uninstall(purge=args.purge)
        print("compress integration removed.")
        if not args.purge:
            print("Configuration and savings history were preserved; use --purge to remove them.")
        if removed.rc_paths:
            print("Open a new terminal, or run: unset -f codex")
        if not args.keep_cli:
            _remove_cli()
        return 0
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
