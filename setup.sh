#!/usr/bin/env bash
# Install or upgrade Synapse (Linux, macOS).
#
#   curl -LsSf https://github.com/synapseorch-ai/synapse-ai/releases/latest/download/setup.sh | bash
#   curl … | bash -s -- --yes            # ask nothing, take every default
#
# This only fetches two things quietly — uv, and the installer that ships with
# the same release — and hands over to it. The installer draws the steps, asks
# its questions and ends with the link; everything else goes to
# ~/.synapse/logs/install.log.
#
# For testing: SYNAPSE_INSTALLER=<a local synapse-install.pyz>,
# SYNAPSE_PACKAGE=<a local wheel>, SYNAPSE_HOME=<another home>.

set -euo pipefail

# Everything is inside main, called on the last line: a download cut short
# runs nothing rather than half a script.
main() {
    local home="${SYNAPSE_HOME:-$HOME/.synapse}"
    local release="https://github.com/synapseorch-ai/synapse-ai/releases/latest/download"
    local url="${SYNAPSE_INSTALLER_URL:-$release/synapse-install.pyz}"
    local uv_version="0.11.8"
    local log="$home/logs/install.log"

    case "$(uname -s)" in
        Linux|Darwin) ;;
        *) fail "This installer is for Linux and macOS. On Windows, in PowerShell:" \
                "irm $release/setup.ps1 | iex" ;;
    esac
    mkdir -p "$home/runtime" "$home/logs"
    printf '\n===== %s setup.sh =====\n' "$(date '+%Y-%m-%d %H:%M:%S')" >>"$log"

    # One line while there is nothing to draw yet; the installer's first frame
    # clears it (no newline on purpose).
    if [ -t 1 ]; then printf '\n  Getting the Synapse installer…'; fi

    # uv: one already on this computer, or a private copy that edits no
    # shell profile and nothing on PATH.
    local uv
    uv="$(command -v uv || true)"
    if [ -z "$uv" ] && [ -x "$home/runtime/uv/uv" ]; then uv="$home/runtime/uv/uv"; fi
    if [ -z "$uv" ]; then
        fetch "https://astral.sh/uv/$uv_version/install.sh" >"$home/runtime/uv-install.sh" \
            || fail "Could not download uv. Check the internet connection, then try again."
        if ! UV_UNMANAGED_INSTALL="$home/runtime/uv" sh "$home/runtime/uv-install.sh" >>"$log" 2>&1; then
            fail "Could not install uv (details in $log)."
        fi
        rm -f "$home/runtime/uv-install.sh"
        uv="$home/runtime/uv/uv"
    fi

    # The installer: this release's, checked against its published checksum.
    local pyz="${SYNAPSE_INSTALLER:-}"
    if [ -z "$pyz" ]; then
        pyz="$home/runtime/synapse-install.pyz"
        fetch "$url" >"$pyz.part" || fail "Could not download the installer. Check the internet connection, then try again."
        local expected actual
        expected="$(fetch "$url.sha256" | awk '{print $1}')" || fail "Could not download the installer's checksum."
        actual="$(sha256 "$pyz.part")"
        if [ -n "$actual" ] && [ "$actual" != "$expected" ]; then
            rm -f "$pyz.part"
            fail "The installer did not match its published checksum. Nothing was installed."
        fi
        mv "$pyz.part" "$pyz"
    fi

    "$uv" python install 3.12 --quiet >>"$log" 2>&1 \
        || fail "uv could not get Python (details in $log)."
    # Hand over. The installer reads answers from the terminal itself, never
    # from stdin — which under `curl | bash` is this script.
    exec "$uv" run --no-project --quiet --python 3.12 python "$pyz" --uv "$uv" "$@" </dev/null
}

fetch() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --retry 3 "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- "$1"
    else
        fail "Neither curl nor wget is installed; install one, then try again."
    fi
}

sha256() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
    else printf ''
    fi
}

fail() {
    [ -t 1 ] && printf '\r\033[2K'
    printf '\n'
    for line in "$@"; do printf '  %s\n' "$line"; done
    printf '\n'
    exit 1
}

main "$@"
