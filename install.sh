#!/bin/sh
# Install CryoPilot on Linux x86_64:
#
#   curl -fsSL https://cryopilot.vercel.app/install.sh | sh
#
# Downloads the CryoPilot binary from kiharalab.org, checks its SHA-256, keeps it
# in ~/.local/share/cryopilot/, puts the cryopilot command in ~/.local/bin, and
# adds ~/.local/bin to PATH in your shell's startup file if it is not there yet.
# Run it again to upgrade: it replaces the program and removes older versions'
# programs. The new version removes their unpacked files when it starts. Once
# CryoPilot is installed, cryopilot update does all of this too.
# In a terminal it first asks where each part goes and whether to change PATH;
# press Enter to keep the default. Without a terminal, or with --yes, it uses
# the defaults without asking:
#
#   curl -fsSL https://cryopilot.vercel.app/install.sh | sh -s -- --yes
#
# Settings (environment variables), which are also the defaults it offers:
#   CRYOPILOT_VERSION        a version such as 0.2.0 (default: the version below)
#   CRYOPILOT_INSTALL_DIR    where the binary is kept (default: ~/.local/share/cryopilot)
#   CRYOPILOT_BIN_DIR        where the cryopilot command goes (default: ~/.local/bin)
#   CRYOPILOT_CACHE          where the first start unpacks the program (default: ~/.cache/cryopilot)
#   CRYOPILOT_NO_MODIFY_PATH set to 1 to leave your shell startup files alone
#   CRYOPILOT_NO_ANALYTICS   set to 1 to leave this install out of the install count
#
# When an install finishes, it tells cryopilot.vercel.app the version and whether
# this is a new install or an upgrade, so that we can count installs. It waits at
# most 5 seconds and the install does not depend on it.
#
# Everything runs inside main(), so a download cut short runs nothing.

set -eu

LATEST=0.3.7
DOWNLOADS=https://kiharalab.org/CryoPilot/app
INSTALL_COUNT=https://cryopilot.vercel.app/api/installs

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

count_install() {  # count_install VERSION KIND
    [ "${CRYOPILOT_NO_ANALYTICS:-0}" = 1 ] && return 0
    if command -v curl >/dev/null 2>&1; then
        curl -fsS -m 5 -o /dev/null -d "version=$1&kind=$2" "$INSTALL_COUNT" >/dev/null 2>&1 || :
    elif command -v wget >/dev/null 2>&1; then
        wget -q -T 5 -t 1 -O /dev/null --post-data="version=$1&kind=$2" "$INSTALL_COUNT" >/dev/null 2>&1 || :
    fi
    return 0
}

# Questions are read from the terminal, not from standard input: under
# curl | sh, standard input is this script.
can_ask() { (: </dev/tty) 2>/dev/null; }

ask() {  # ask QUESTION DEFAULT: prints the answer, or DEFAULT for an empty one
    printf '%s [%s]: ' "$1" "$2" >/dev/tty
    IFS= read -r reply </dev/tty || reply=
    reply=$(printf '%s' "$reply" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
    printf '%s' "${reply:-$2}"
}

confirm() {  # confirm QUESTION: succeeds for yes, the default
    while :; do
        printf '%s [Y/n]: ' "$1" >/dev/tty
        IFS= read -r reply </dev/tty || return 1
        case $reply in
            '' | [Yy] | [Yy][Ee][Ss]) return 0 ;;
            [Nn] | [Nn][Oo]) return 1 ;;
        esac
    done
}

absolute() {  # absolute PATH: PATH with a leading ~ expanded, made absolute
    case $1 in
        '~') path=$HOME ;;
        '~/'*) path=$HOME/${1#??} ;;
        /*) path=$1 ;;
        *) path=$PWD/$1 ;;
    esac
    [ "$path" = / ] || path=${path%/}
    printf '%s' "$path"
}

short() {  # short PATH: PATH with your home folder written as ~
    case $1 in
        "$HOME") printf '~' ;;
        "$HOME"/*) printf '~/%s' "${1#"$HOME"/}" ;;
        *) printf '%s' "$1" ;;
    esac
}

writable() {  # writable FOLDER: FOLDER, or the part of it that exists, is a folder you can write to
    dir=$1
    while [ ! -e "$dir" ]; do dir=$(dirname "$dir"); done
    [ -d "$dir" ] && [ -w "$dir" ]
}

ask_folder() {  # ask_folder QUESTION DEFAULT: prints a folder you can write to
    while :; do
        folder=$(absolute "$(ask "$1" "$(short "$2")")")
        if writable "$folder"; then
            printf '%s' "$folder"
            return 0
        fi
        printf 'You cannot write to %s. Choose another folder.\n' "$folder" >/dev/tty
    done
}

quote() {  # quote TEXT: TEXT in single quotes, for a shell script
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

main() {
    questions=1
    for arg in "$@"; do
        case $arg in
            -y | --yes) questions=0 ;;
            *) die "unknown option $arg. Use --yes to install with the defaults and no questions." ;;
        esac
    done
    [ "$questions" = 1 ] && can_ask || questions=0

    [ "$(uname -s)" = Linux ] || die "the CryoPilot binary is for Linux."
    [ "$(uname -m)" = x86_64 ] || die "the CryoPilot binary is for x86_64; this computer is $(uname -m)."

    version=${CRYOPILOT_VERSION:-$LATEST}
    version=${version#v}
    binary=cryopilot-$version-linux-x86_64
    # The same name for every version, so that an upgrade replaces the program.
    program=cryopilot-linux-x86_64
    url=$DOWNLOADS/v$(printf '%s' "$version" | tr . _)/$binary
    install_dir=$(absolute "${CRYOPILOT_INSTALL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/cryopilot}")
    bin_dir=$(absolute "${CRYOPILOT_BIN_DIR:-$HOME/.local/bin}")
    default_cache=$(absolute "${XDG_CACHE_HOME:-$HOME/.cache}/cryopilot")
    cache_dir=$(absolute "${CRYOPILOT_CACHE:-$default_cache}")
    if [ "${CRYOPILOT_NO_MODIFY_PATH:-0}" = 1 ]; then modify_path=0; else modify_path=1; fi
    case ${SHELL:-} in
        */zsh) rc=$HOME/.zshrc ;;
        */bash) rc=$HOME/.bashrc ;;
        *) rc=$HOME/.profile ;;
    esac

    if [ "$questions" = 1 ]; then
        say "This installs CryoPilot $version. Press Enter to keep the answer in brackets."
        say ""
        install_dir=$(ask_folder "Folder for the program (about 280 MB)" "$install_dir")
        bin_dir=$(ask_folder "Folder for the cryopilot command" "$bin_dir")
        cache_dir=$(ask_folder "Folder that the first start unpacks into (about 780 MB)" "$cache_dir")
    fi

    case ":$PATH:" in
        *":$bin_dir:"*) on_path=1 ;;
        *) on_path=0 ;;
    esac
    line="export PATH=\"$bin_dir:\$PATH\"  # added by the CryoPilot installer"
    if grep -Fqs "$line" "$rc"; then in_rc=1; else in_rc=0; fi
    if [ "$questions" = 1 ]; then
        if [ "$on_path" = 0 ] && [ "$in_rc" = 0 ] && [ "$modify_path" = 1 ]; then
            if confirm "Add $(short "$bin_dir") to PATH in $(short "$rc")?"; then modify_path=1; else modify_path=0; fi
        fi
        if [ "$on_path" = 1 ]; then
            path_plan="already on PATH"
        elif [ "$in_rc" = 1 ]; then
            path_plan="on PATH through $(short "$rc")"
        elif [ "$modify_path" = 1 ]; then
            path_plan="added to PATH in $(short "$rc")"
        else
            path_plan="not added to PATH"
        fi
        say ""
        say "CryoPilot $version will be installed like this:"
        say "  Program:  $(short "$install_dir/$program")"
        say "  Command:  $(short "$bin_dir/cryopilot") ($path_plan)"
        say "  Unpacks:  $(short "$cache_dir")/ (on the first start)"
        say ""
        if ! confirm "Install now?"; then
            say "Nothing was installed."
            exit 0
        fi
        say ""
    fi

    mkdir -p "$install_dir" "$bin_dir" || die "cannot create $install_dir or $bin_dir."
    # Next to the program, not in /tmp, which may be too small: the last step is
    # then one rename on the same disk. Earlier installs that were killed left these.
    rm -rf "$install_dir"/.install.*
    work=$(mktemp -d "$install_dir/.install.XXXXXX") || die "cannot write to $install_dir."
    trap 'rm -rf "$work"' EXIT

    say "Downloading CryoPilot $version ($binary, about 280 MB)..."
    fetch "$url" "$work/$binary" || die "the download from $url failed."
    if command -v sha256sum >/dev/null 2>&1; then
        fetch "$url.sha256" "$work/$binary.sha256" 2>/dev/null || die "the checksum download failed."
        (cd "$work" && sha256sum -c "$binary.sha256" >/dev/null 2>&1) || die "the download is damaged (checksum mismatch). Run the installer again."
    fi

    chmod 755 "$work/$binary"
    mv -f "$work/$binary" "$install_dir/$program"
    say "Installed $(short "$install_dir/$program")"
    if [ -e "$bin_dir/cryopilot" ]; then kind=upgrade; else kind=new; fi
    # Before 0.3.5 every version kept its own copy here.
    rm -f "$install_dir"/cryopilot-[0-9]*-linux-x86_64
    # Remove the old command first: writing through a link would overwrite the binary.
    rm -f "$bin_dir/cryopilot"
    if [ "$cache_dir" = "$default_cache" ]; then
        ln -s "$install_dir/$program" "$bin_dir/cryopilot"
        say "Linked $(short "$bin_dir/cryopilot")"
    else
        # The command is a small script that tells the binary where to unpack.
        cat > "$bin_dir/cryopilot" <<EOF
#!/bin/sh
# Starts CryoPilot. Written by the CryoPilot installer. The first start unpacks
# the program into the folder below unless CRYOPILOT_CACHE is set.
CRYOPILOT_CACHE=\${CRYOPILOT_CACHE:-$(quote "$cache_dir")}
export CRYOPILOT_CACHE
exec $(quote "$install_dir/$program") "\$@"
EOF
        chmod 755 "$bin_dir/cryopilot"
        say "Wrote $(short "$bin_dir/cryopilot")"
    fi

    command=cryopilot
    if [ "$on_path" = 0 ]; then
        if [ "$in_rc" = 1 ] || [ "$modify_path" = 1 ]; then
            if [ "$in_rc" = 0 ]; then
                printf '\n%s\n' "$line" >> "$rc"
                say "Added $(short "$bin_dir") to PATH in $(short "$rc")"
            fi
            say ""
            say "Open a new terminal, or run:  export PATH=\"$bin_dir:\$PATH\""
        else
            command=$bin_dir/cryopilot
            say ""
            say "$(short "$bin_dir") is not on your PATH, so start CryoPilot by its full path."
        fi
    fi

    count_install "$version" "$kind"

    say ""
    say "Start CryoPilot with:  $command"
    say "The first start unpacks the program (about 780 MB) into $(short "$cache_dir")/."
    if [ "$kind" = upgrade ]; then
        say "A CryoPilot that is already running keeps the old version until you restart it."
    fi
}

main "$@"
