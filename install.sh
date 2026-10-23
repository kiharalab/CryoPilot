#!/bin/sh
# Install CryoPilot on Linux x86_64:
#
#   curl -fsSL https://raw.githubusercontent.com/kiharalab/CryoPilot/main/install.sh | sh
#
# Downloads the newest release binary from GitHub, checks its SHA-256, puts it
# in ~/.local/share/cryopilot/, links it as ~/.local/bin/cryopilot, and adds
# ~/.local/bin to PATH in your shell's startup file if it is not there yet.
#
# Settings (environment variables):
#   CRYOPILOT_VERSION        a release tag such as v0.1.0 (default: the newest release)
#   CRYOPILOT_INSTALL_DIR    where the binary is kept (default: ~/.local/share/cryopilot)
#   CRYOPILOT_BIN_DIR        where the cryopilot command goes (default: ~/.local/bin)
#   CRYOPILOT_NO_MODIFY_PATH set to 1 to leave your shell startup files alone
#   GITHUB_TOKEN             needed while the repository is private (or be signed in to gh)
#
# Everything runs inside main(), so a download cut short runs nothing.

set -eu

REPO=kiharalab/CryoPilot
API=https://api.github.com/repos/$REPO

say() { printf '%s\n' "$*"; }
die() { printf 'cryopilot install: %s\n' "$*" >&2; exit 1; }

fetch() {  # fetch URL OUTPUT [ACCEPT]
    accept=${3:-application/vnd.github+json}
    if command -v curl >/dev/null 2>&1; then
        if [ -n "${GITHUB_TOKEN:-}" ]; then
            curl -fsSL -H "Authorization: Bearer $GITHUB_TOKEN" -H "Accept: $accept" -o "$2" "$1"
        else
            curl -fsSL -H "Accept: $accept" -o "$2" "$1"
        fi
    elif command -v wget >/dev/null 2>&1; then
        if [ -n "${GITHUB_TOKEN:-}" ]; then
            wget -q --header="Authorization: Bearer $GITHUB_TOKEN" --header="Accept: $accept" -O "$2" "$1"
        else
            wget -q --header="Accept: $accept" -O "$2" "$1"
        fi
    else
        die "curl or wget is required."
    fi
}

# Print "<asset id> <name>" for each file of the chosen release, from the GitHub API JSON.
release_assets() {
    tr -d '\n' < "$1" | sed 's/},/}\n/g' | sed -n 's/.*"url": *"[^"]*\/releases\/assets\/\([0-9]*\)".*"name": *"\([^"]*\)".*/\1 \2/p'
}

main() {
    [ "$(uname -s)" = Linux ] || die "the CryoPilot binary is for Linux. On macOS, install from source (see the README)."
    [ "$(uname -m)" = x86_64 ] || die "the CryoPilot binary is for x86_64; this computer is $(uname -m)."

    install_dir=${CRYOPILOT_INSTALL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/cryopilot}
    bin_dir=${CRYOPILOT_BIN_DIR:-$HOME/.local/bin}
    work=$(mktemp -d)
    trap 'rm -rf "$work"' EXIT

    # Private repositories need a token; borrow the GitHub CLI's if it is signed in.
    if [ -z "${GITHUB_TOKEN:-}" ] && command -v gh >/dev/null 2>&1; then
        GITHUB_TOKEN=$(gh auth token 2>/dev/null || true)
    fi

    if [ -n "${CRYOPILOT_VERSION:-}" ]; then
        url=$API/releases/tags/$CRYOPILOT_VERSION
    else
        url=$API/releases?per_page=1  # newest first, pre-releases included
    fi
    fetch "$url" "$work/release.json" || die "cannot read the CryoPilot releases from GitHub. While the repository is private, set GITHUB_TOKEN or sign in with gh."
    tag=$(sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' "$work/release.json" | head -n 1)
    [ -n "$tag" ] || die "no CryoPilot release found."

    release_assets "$work/release.json" > "$work/assets"
    binary=$(awk '$2 ~ /^cryopilot-.*-linux-x86_64$/ {print $2; exit}' "$work/assets")
    [ -n "$binary" ] || die "release $tag has no Linux x86_64 binary."
    binary_id=$(awk -v n="$binary" '$2 == n {print $1; exit}' "$work/assets")
    sum_id=$(awk -v n="$binary.sha256" '$2 == n {print $1; exit}' "$work/assets")

    say "Downloading CryoPilot $tag ($binary)..."
    fetch "$API/releases/assets/$binary_id" "$work/$binary" application/octet-stream || die "the download failed."
    if [ -n "$sum_id" ] && command -v sha256sum >/dev/null 2>&1; then
        fetch "$API/releases/assets/$sum_id" "$work/$binary.sha256" application/octet-stream || die "the checksum download failed."
        (cd "$work" && sha256sum -c "$binary.sha256" >/dev/null 2>&1) || die "the download is damaged (checksum mismatch). Run the installer again."
    fi

    mkdir -p "$install_dir" "$bin_dir"
    chmod 755 "$work/$binary"
    mv -f "$work/$binary" "$install_dir/$binary"
    ln -sf "$install_dir/$binary" "$bin_dir/cryopilot"
    say "Installed $install_dir/$binary"
    say "Linked $bin_dir/cryopilot"

    case ":$PATH:" in
        *":$bin_dir:"*) on_path=1 ;;
        *) on_path=0 ;;
    esac
    if [ "$on_path" = 0 ] && [ "${CRYOPILOT_NO_MODIFY_PATH:-0}" != 1 ]; then
        case ${SHELL:-} in
            */zsh) rc=$HOME/.zshrc ;;
            */bash) rc=$HOME/.bashrc ;;
            *) rc=$HOME/.profile ;;
        esac
        line="export PATH=\"$bin_dir:\$PATH\"  # added by the CryoPilot installer"
        if ! grep -Fqs "$line" "$rc"; then
            printf '\n%s\n' "$line" >> "$rc"
            say "Added $bin_dir to PATH in $rc"
        fi
        say ""
        say "Open a new terminal, or run:  export PATH=\"$bin_dir:\$PATH\""
    fi

    say ""
    say "Start CryoPilot with:  cryopilot"
    say "The first start unpacks the program (about 670 MB) into ~/.cache/cryopilot/."
}

main "$@"
