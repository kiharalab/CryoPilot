#!/bin/sh
# Install CryoPilot on Linux x86_64:
#
#   curl -fsSL https://cryopilot.vercel.app/install.sh | sh
#
# Downloads the CryoPilot binary from kiharalab.org, checks its SHA-256, puts it
# in ~/.local/share/cryopilot/, links it as ~/.local/bin/cryopilot, and adds
# ~/.local/bin to PATH in your shell's startup file if it is not there yet.
#
# Settings (environment variables):
#   CRYOPILOT_VERSION        a version such as 0.1.0 (default: the version below)
#   CRYOPILOT_INSTALL_DIR    where the binary is kept (default: ~/.local/share/cryopilot)
#   CRYOPILOT_BIN_DIR        where the cryopilot command goes (default: ~/.local/bin)
#   CRYOPILOT_NO_MODIFY_PATH set to 1 to leave your shell startup files alone
#
# Everything runs inside main(), so a download cut short runs nothing.

set -eu

LATEST=0.2.0
DOWNLOADS=https://kiharalab.org/CryoPilot/app

say() { printf '%s\n' "$*"; }
die() { printf 'cryopilot install: %s\n' "$*" >&2; exit 1; }

fetch() {  # fetch URL OUTPUT
    if command -v curl >/dev/null 2>&1; then
        curl -fL --progress-bar -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$2" "$1"
    else
        die "curl or wget is required."
    fi
}

main() {
    [ "$(uname -s)" = Linux ] || die "the CryoPilot binary is for Linux."
    [ "$(uname -m)" = x86_64 ] || die "the CryoPilot binary is for x86_64; this computer is $(uname -m)."

    version=${CRYOPILOT_VERSION:-$LATEST}
    version=${version#v}
    binary=cryopilot-$version-linux-x86_64
    url=$DOWNLOADS/v$(printf '%s' "$version" | tr . _)/$binary
    install_dir=${CRYOPILOT_INSTALL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/cryopilot}
    bin_dir=${CRYOPILOT_BIN_DIR:-$HOME/.local/bin}
    work=$(mktemp -d)
    trap 'rm -rf "$work"' EXIT

    say "Downloading CryoPilot $version ($binary, about 265 MB)..."
    fetch "$url" "$work/$binary" || die "the download from $url failed."
    if command -v sha256sum >/dev/null 2>&1; then
        fetch "$url.sha256" "$work/$binary.sha256" 2>/dev/null || die "the checksum download failed."
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
    say "The first start unpacks the program (about 730 MB) into ~/.cache/cryopilot/."
}

main "$@"
