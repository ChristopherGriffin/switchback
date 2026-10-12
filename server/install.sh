#!/bin/sh
# Sets up the switchbota server on a Debian or Ubuntu box. Everything runs in Docker,
# so the only things installed on the host are Docker, git and curl.
#
#   sh install.sh                  install or update, then start the server
#   sh install.sh build portal     build the ESPHome payload on this box, then restart
#   sh install.sh build implant
#
# Run it as root. It is safe to run again at any time.
set -eu

REPO="${SWITCHBOTA_REPO:-https://github.com/ChristopherGriffin/switchbota.git}"
BRANCH="${SWITCHBOTA_BRANCH:-esphome-direct}"
DIR="${SWITCHBOTA_DIR:-/opt/switchbota}"

# The flasher that gets written to the plug first. It is pinned by checksum because
# it is the program that overwrites the bootloader.
APP_BIN_URL="https://github.com/ChristopherGriffin/switchbota/releases/download/esphome-v0.1.0/app.bin"
APP_BIN_SHA256="dc99cacdfe1a46debfdb66d971c44673ed957d8ebe8331f430e9016bbe5cbc56"

ESPHOME_IMAGE="ghcr.io/esphome/esphome:stable"

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

need_tools() {
    if ! command -v git >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
        command -v apt-get >/dev/null 2>&1 || die "git and curl are needed. Install them and run this again."
        say "Installing git and curl"
        apt-get update -qq
        apt-get install -y -qq git curl ca-certificates
    fi
    if ! docker compose version >/dev/null 2>&1; then
        say "Installing Docker with Docker's own install script"
        curl -fsSL https://get.docker.com | sh
    fi
}

fetch_repo() {
    if [ -d "$DIR/.git" ]; then
        say "Updating $DIR"
        git -C "$DIR" pull -q --ff-only
    else
        say "Cloning into $DIR"
        git clone -q -b "$BRANCH" "$REPO" "$DIR"
    fi
}

fetch_app_bin() {
    app="$DIR/server/bin/app.bin"
    [ -f "$app" ] && return
    say "Downloading the flasher (app.bin)"
    curl -fsSL -o "$app.tmp" "$APP_BIN_URL"
    if ! echo "$APP_BIN_SHA256  $app.tmp" | sha256sum -c - >/dev/null 2>&1; then
        rm -f "$app.tmp"
        die "app.bin did not match its checksum. Nothing was installed."
    fi
    mv "$app.tmp" "$app"
}

ensure_secrets() {
    secrets="$DIR/esphome/secrets.yaml"
    [ -f "$secrets" ] && return
    cp "$DIR/esphome/secrets.yaml.example" "$secrets"
    chmod 600 "$secrets"
}

start_server() {
    cd "$DIR/server"
    if [ ! -f bin/payload.bin ]; then
        ensure_secrets
        cat <<EOF

The server is installed, but there is no payload for it to serve yet, so it
has not been started. Next:

  1. Fill in $DIR/esphome/secrets.yaml
  2. Run:  sh $DIR/server/install.sh build portal
     (or "build implant" to build your Wi-Fi details into the plugs)
EOF
        return
    fi

    say "Starting the server"
    docker compose up -d
    tries=0
    until docker compose logs 2>/dev/null | grep -q "Server listening"; do
        tries=$((tries + 1))
        if [ "$tries" -gt 60 ]; then
            docker compose logs --tail 20
            die "the server did not come up. Its log is above."
        fi
        sleep 2
    done
    docker compose logs --no-log-prefix 2>/dev/null | grep -E "^(app|payload)\.bin" | tail -2

    address="$(ip route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p')"
    cat <<EOF

The server is running on port 80.

Now point both wohand.com and www.wohand.com at ${address:-this box} in your
network's DNS, then check from another computer:

  curl http://www.wohand.com/payload.md5

That should print a checksum and a size.
EOF
}

build_payload() {
    variant="${1:-}"
    case "$variant" in
        portal|implant) ;;
        *) die "say which payload to build: portal or implant" ;;
    esac
    if [ ! -f "$DIR/esphome/secrets.yaml" ]; then
        ensure_secrets
        die "fill in $DIR/esphome/secrets.yaml first, then run this again"
    fi

    say "Building the $variant payload in Docker. The first build downloads a lot and can take 15 minutes."
    docker run --rm \
        -v "$DIR:/work" -w /work \
        -v switchbota-platformio:/root/.platformio \
        --entrypoint python3 "$ESPHOME_IMAGE" tools/build_payload.py "$variant"

    cd "$DIR/server"
    if [ -n "$(docker compose ps -q 2>/dev/null)" ]; then
        say "Restarting the server so it serves the new payload"
        docker compose restart
        sleep 3
        docker compose logs --no-log-prefix --tail 3
    else
        start_server
    fi
}

[ "$(id -u)" = 0 ] || die "run this as root, or with sudo"

case "${1:-install}" in
    install)
        need_tools
        fetch_repo
        fetch_app_bin
        start_server
        ;;
    build)
        [ -d "$DIR/.git" ] || die "run 'sh install.sh' once first"
        build_payload "${2:-}"
        ;;
    *)
        die "usage: sh install.sh [build portal|implant]"
        ;;
esac
