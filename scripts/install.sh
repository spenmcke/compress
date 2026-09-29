#!/bin/sh
# compress website bootstrapper for macOS and Linux.

set -eu

: "${HOME:?HOME is not set}"

COMPRESS_VERSION="${COMPRESS_VERSION:-0.1.0}"
UV_VERSION="${UV_VERSION:-0.9.27}"
COMPRESS_PACKAGE="${COMPRESS_PACKAGE:-compress-cli}"
COMPRESS_DOWNLOAD_BASE_URL="${COMPRESS_DOWNLOAD_BASE_URL:-https://raw.githubusercontent.com/spenmcke/compress/refs/heads/main/releases}"
COMPRESS_DOWNLOAD_BASE_URL="${COMPRESS_DOWNLOAD_BASE_URL%/}"
COMPRESS_RELEASE_BASE_URL="${COMPRESS_RELEASE_BASE_URL:-${COMPRESS_DOWNLOAD_BASE_URL}/v${COMPRESS_VERSION}}"
COMPRESS_WHEEL_URL="${COMPRESS_WHEEL_URL:-${COMPRESS_RELEASE_BASE_URL}/compress_cli-${COMPRESS_VERSION}-py3-none-any.whl}"
COMPRESS_WHEEL_SHA256_URL="${COMPRESS_WHEEL_SHA256_URL:-${COMPRESS_WHEEL_URL}.sha256}"
# An override makes it possible to verify release candidates without publishing
# their checksum file. Normal installs download the checksum beside the wheel.
COMPRESS_WHEEL_SHA256="${COMPRESS_WHEEL_SHA256:-}"
UV_INSTALL_URL="${UV_INSTALL_URL:-https://astral.sh/uv/${UV_VERSION}/install.sh}"
COMPRESS_BIN_DIR="${COMPRESS_BIN_DIR:-${HOME}/.local/bin}"
COMPRESS_ENDPOINT="${COMPRESS_ENDPOINT:-https://api.everestagi.com/v1}"
COMPRESS_SKIP_LOGIN="${COMPRESS_SKIP_LOGIN:-0}"

usage() {
    cat <<'EOF'
Usage: install.sh [--uninstall]

Install compress for the current user, or remove it with --uninstall.
No sudo access is required.
The default installation uses the hosted compress release and Everest API.
EOF
}

die() {
    printf 'compress installer: %s\n' "$*" >&2
    exit 1
}

find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
    elif [ -x "${COMPRESS_BIN_DIR}/uv" ]; then
        printf '%s\n' "${COMPRESS_BIN_DIR}/uv"
    else
        return 1
    fi
}

is_our_compress() {
    # Inspect the Python console launcher without running an unknown program.
    [ -f "$1" ] && grep -Eq 'from compress_cli[.]|import compress_cli' "$1"
}

preflight_command() {
    candidate="${COMPRESS_BIN_DIR}/compress"
    if [ -e "$candidate" ] || [ -L "$candidate" ]; then
        is_our_compress "$candidate" || die "an unrelated compress command exists at $candidate. Choose a different COMPRESS_BIN_DIR before installing."
    fi
    existing_compress="$(command -v compress || true)"
    if [ -n "$existing_compress" ] && ! is_our_compress "$existing_compress"; then
        printf 'The new command at %s shares its name with %s; PATH order selects which runs.\n' "$candidate" "$existing_compress"
    fi
}

require_https() {
    case "$2" in
        https://?*) ;;
        *) die "$1 must be an HTTPS URL." ;;
    esac
    case "$2" in
        *'@'*|*'?'*|*'#'*) die "$1 must not contain credentials, a query, or a fragment." ;;
    esac
}

download_artifact() {
    # Do not follow redirects: release artifacts must come from the selected URL.
    status="$(curl --fail --silent --show-error --proto '=https' --tlsv1.2 \
        --write-out '%{http_code}' "$1" --output "$2")" || die "download failed: $1"
    [ "$status" = 200 ] || die "download returned HTTP $status; redirects are not allowed."
}

find_compress() {
    if [ -x "${COMPRESS_BIN_DIR}/compress" ] && is_our_compress "${COMPRESS_BIN_DIR}/compress"; then
        printf '%s\n' "${COMPRESS_BIN_DIR}/compress"
    elif command -v compress >/dev/null 2>&1 && is_our_compress "$(command -v compress)"; then
        command -v compress
    else
        return 1
    fi
}

# Match the shell files and conflicts checked by the CLI before downloading or
# installing anything. A pre-existing compress-managed block is safe to update.
preflight_shell() {
    shell_name="${SHELL##*/}"
    case "$shell_name" in
        zsh|bash) ;;
        *)
            if [ "$(uname -s)" = Darwin ]; then
                shell_name=zsh
            else
                shell_name=bash
            fi
            ;;
    esac
    if [ "$shell_name" = zsh ]; then
        rc_path="${HOME}/.zshrc"
    else
        rc_path="${HOME}/.bashrc"
    fi
    [ -f "$rc_path" ] || return 0

    if grep -Eq '^[[:space:]]*(function[[:space:]]+)?codex[[:space:]]*\([[:space:]]*\)[[:space:]]*\{' "$rc_path" \
        || grep -Eq '^[[:space:]]*alias[[:space:]]+codex=' "$rc_path" \
        || grep -Eq '^[[:space:]]*(source|\.)[[:space:]]+.*codex-cmprs\.sh([[:space:]]|$)' "$rc_path"; then
        die "$rc_path already defines or sources a codex wrapper. Remove that line or wrapper, then rerun this installer. No compress package was installed."
    fi
}

remove_tool() {
    compress_bin="$(find_compress || true)"
    uv_bin="$(find_uv || true)"

    if [ -n "$compress_bin" ]; then
        is_our_compress "$compress_bin" || die "refusing to run unrelated compress command at $compress_bin."
        "$compress_bin" uninstall --keep-cli || die "compress could not remove its shell integration."
    else
        printf '%s\n' "compress is not on PATH; continuing with package removal."
    fi

    if [ -n "$uv_bin" ] && "$uv_bin" tool list | awk '{print $1}' | grep -Fx "$COMPRESS_PACKAGE" >/dev/null 2>&1; then
        if "$uv_bin" tool uninstall "$COMPRESS_PACKAGE"; then
            printf '%s\n' "compress was uninstalled. Saved credentials and usage history were preserved."
        else
            die "The compress package could not be removed with uv."
        fi
    elif [ -n "$compress_bin" ] && [ -z "$uv_bin" ]; then
        printf '%s\n' "compress shell integration was removed, but uv was not found; remove the package manually."
    else
        printf '%s\n' "compress package removal is complete. Saved credentials and usage history were preserved."
    fi
}

install_uv() {
    command -v curl >/dev/null 2>&1 || die "curl is required to install uv."

    uv_script="${tmp_dir}/uv-install.sh"
    printf 'Installing uv %s for the current user...\n' "$UV_VERSION"
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --tlsv1.2 "$UV_INSTALL_URL" --output "$uv_script"
    UV_INSTALL_DIR="$COMPRESS_BIN_DIR" sh "$uv_script" --no-modify-path

    [ -x "${COMPRESS_BIN_DIR}/uv" ] || die "uv installed, but ${COMPRESS_BIN_DIR}/uv was not found."
    printf '%s\n' "${COMPRESS_BIN_DIR}/uv"
}

verify_wheel() {
    wheel_path=$1
    expected_sha256="$COMPRESS_WHEEL_SHA256"
    if [ -z "$expected_sha256" ]; then
        checksum_path="${tmp_dir}/compress-wheel.sha256"
        download_artifact "$COMPRESS_WHEEL_SHA256_URL" "$checksum_path"
        expected_sha256="$(awk 'NR == 1 {print $1}' "$checksum_path")"
    fi
    expected_sha256="$(printf '%s' "$expected_sha256" | tr 'A-F' 'a-f')"
    case "$expected_sha256" in
        *[!0-9a-fA-F]*|'') die "the compress release checksum is invalid." ;;
    esac
    [ "${#expected_sha256}" -eq 64 ] || die "the compress release checksum is invalid."

    if command -v sha256sum >/dev/null 2>&1; then
        actual_sha256="$(sha256sum "$wheel_path" | awk '{print $1}')"
    elif command -v shasum >/dev/null 2>&1; then
        actual_sha256="$(shasum -a 256 "$wheel_path" | awk '{print $1}')"
    else
        die "sha256sum or shasum is required to verify the compress release."
    fi

    [ "$actual_sha256" = "$expected_sha256" ] || die "compress wheel checksum verification failed."
}

case "${1:-}" in
    "")
        ;;
    --uninstall)
        [ "$#" -eq 1 ] || die "--uninstall does not accept additional arguments."
        remove_tool
        exit 0
        ;;
    -h|--help)
        usage
        exit 0
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac

[ "$#" -le 1 ] || die "unexpected arguments"
case "$(uname -s)" in
    Darwin|Linux)
        ;;
    *)
        die "only macOS and Linux are supported."
        ;;
esac

case "$(uname -m)" in
    arm64|aarch64|x86_64|amd64)
        ;;
    *)
        die "unsupported CPU architecture: $(uname -m)"
        ;;
esac

[ -n "$COMPRESS_DOWNLOAD_BASE_URL" ] || die "set COMPRESS_DOWNLOAD_BASE_URL to the HTTPS release location."
[ -n "$COMPRESS_ENDPOINT" ] || die "set COMPRESS_ENDPOINT to the service's HTTPS API endpoint."
require_https COMPRESS_DOWNLOAD_BASE_URL "$COMPRESS_DOWNLOAD_BASE_URL"
require_https COMPRESS_ENDPOINT "$COMPRESS_ENDPOINT"
require_https COMPRESS_WHEEL_URL "$COMPRESS_WHEEL_URL"
require_https COMPRESS_WHEEL_SHA256_URL "$COMPRESS_WHEEL_SHA256_URL"
require_https UV_INSTALL_URL "$UV_INSTALL_URL"
command -v curl >/dev/null 2>&1 || die "curl is required."
command -v codex >/dev/null 2>&1 || die "install Codex before enabling compress integration."
preflight_command
preflight_shell

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/compress-install.XXXXXX")"
trap 'rm -rf "$tmp_dir"' EXIT HUP INT TERM

mkdir -p "$COMPRESS_BIN_DIR"
uv_bin="$(find_uv || true)"
if [ -z "$uv_bin" ]; then
    install_uv
    uv_bin="$(find_uv || true)"
fi
[ -n "$uv_bin" ] || die "uv was installed, but its command could not be found."
wheel_path="${tmp_dir}/compress_cli-${COMPRESS_VERSION}-py3-none-any.whl"

printf 'Downloading compress %s...\n' "$COMPRESS_VERSION"
download_artifact "$COMPRESS_WHEEL_URL" "$wheel_path"
verify_wheel "$wheel_path"

printf '%s\n' "Installing compress for the current user..."
UV_TOOL_BIN_DIR="$COMPRESS_BIN_DIR" "$uv_bin" tool install --force "$wheel_path"

compress_bin="${COMPRESS_BIN_DIR}/compress"
[ -x "$compress_bin" ] || die "compress installed, but its command was not found in ${COMPRESS_BIN_DIR}."
"$compress_bin" install --endpoint "$COMPRESS_ENDPOINT"

printf '\n%s\n' "compress ${COMPRESS_VERSION} is installed."
if [ "$COMPRESS_SKIP_LOGIN" = "1" ]; then
    printf '%s\n' "Next, run: compress login"
else
    printf '%s\n' "Starting browser login. If it does not open, follow the URL shown below."
    "$compress_bin" login || die "compress was installed, but login did not complete. Run 'compress login' to retry."
fi
printf '%s\n' "Open a new terminal, then run: codex"
printf '%s\n' "To remove compress later: compress uninstall"
