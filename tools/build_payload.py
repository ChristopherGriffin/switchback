#!/usr/bin/env python3
"""Build the ESPHome image that switchbota writes to the plug and stage it for the server.

    python tools/build_payload.py portal
        The plug comes up as a hotspot named sbplug-xxxxxx with a captive portal,
        and whoever sets it up picks the network.

    python tools/build_payload.py implant
        The Wi-Fi name and password from esphome/secrets.yaml are built in, so the
        plug joins that network by itself.

Run it with the Python that has ESPHome installed. It refuses to build until every
secret the chosen variant needs is filled in, because whatever is in secrets.yaml
is written to every plug.
"""

import argparse
import base64
import binascii
import hashlib
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ESPHOME_DIR = ROOT / "esphome"
BUILD_DIR = ESPHOME_DIR / ".esphome" / "build" / "sbplug" / ".pioenvs" / "sbplug"
PAYLOAD = ROOT / "server" / "bin" / "payload.bin"
WRITE_SIZE = 0x171000  # keep in step with espressif/main/include/config.h

REQUIRED = {
    "portal": ("api_encryption_key", "ota_password"),
    "implant": ("api_encryption_key", "ota_password", "wifi_ssid", "wifi_password"),
}


def check_secrets(variant):
    import yaml  # comes with ESPHome

    path = ESPHOME_DIR / "secrets.yaml"
    if not path.exists():
        sys.exit(f"{path} is missing. Copy secrets.yaml.example to secrets.yaml and fill it in.")
    secrets = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    problems = []
    for name in REQUIRED[variant]:
        value = secrets.get(name)
        if value is None or not str(value).strip():
            problems.append(f"{name} is blank")
    key = str(secrets.get("api_encryption_key") or "").strip()
    if key:
        try:
            valid = len(base64.b64decode(key, validate=True)) == 32
        except binascii.Error:
            valid = False
        if not valid:
            problems.append("api_encryption_key is not 32 bytes of base64")
    if problems:
        sys.exit(f"{path} is not ready for the {variant} build:\n  " + "\n  ".join(problems))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("variant", choices=sorted(REQUIRED), help="which payload to build")
    variant = parser.parse_args().variant

    if importlib.util.find_spec("esphome") is None:
        sys.exit(f"ESPHome is not installed for {sys.executable}. Run this with the Python that has it.")
    check_secrets(variant)

    # Remove the old images first so a stale one can never be staged
    for name in ("firmware.bin", "firmware.factory.bin"):
        (BUILD_DIR / name).unlink(missing_ok=True)
    result = subprocess.run([sys.executable, "-m", "esphome", "compile", f"sbplug-{variant}.yaml"], cwd=ESPHOME_DIR)
    image = BUILD_DIR / "firmware.factory.bin"
    if result.returncode != 0 or not image.exists():
        sys.exit("ESPHome build failed, nothing was staged.")

    size = image.stat().st_size
    if size > WRITE_SIZE:
        sys.exit(f"The factory image is {size:,} bytes and the flasher can only write {WRITE_SIZE:,}. Nothing was staged.")
    PAYLOAD.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(image, PAYLOAD)
    md5 = hashlib.md5(PAYLOAD.read_bytes()).hexdigest()
    print(f"\nStaged the {variant} payload at {PAYLOAD}")
    print(f"  {size:,} bytes, {WRITE_SIZE - size:,} bytes of headroom, md5 {md5}")
    print("  Restart the switchbota server so it picks this up.")
    if variant == "portal":
        print("  Run tools/sbplug_batch.py with --portal, since these plugs will not join Wi-Fi by themselves.")


if __name__ == "__main__":
    main()
