#!/usr/bin/env python3
"""Patch Wi-Fi credentials into a prebuilt ESPHome factory image, no rebuild.

The image (esphome/sbplug-direct.yaml) is built with fixed-size placeholder Wi-Fi
credentials. This overwrites them with real ones and fixes the ESP app image's
1-byte checksum and appended SHA-256 so the plug boots the patched firmware.

The maths is verified by recomputing the checksum and hash on the UNPATCHED image
and confirming they already match the bytes in the file (verify_image), so a wrong
algorithm is caught before anything is ever flashed.
"""
import hashlib
import struct

# The direct build fills the Wi-Fi fields with these markers padded out to a fixed
# size. We find each by its prefix and read the slot length up to the string's null
# terminator, so the exact padding count does not matter.
SSID_PREFIX = b"SB_SSID_"
PASS_PREFIX = b"SB_PASS_"
ESP_CHECKSUM_MAGIC = 0xEF
APP_OFFSET = 0x10000  # where the app image sits inside the factory image


def _app_checksum_and_hash(img, app_offset=APP_OFFSET):
    """Return (checksum_byte, checksum_pos, hash_pos or None, computed_sha or None)."""
    if img[app_offset] != 0xE9:
        raise ValueError("no ESP image magic at app offset")
    nseg = img[app_offset + 1]
    hash_appended = img[app_offset + 23]
    off = app_offset + 24
    checksum = ESP_CHECKSUM_MAGIC
    for _ in range(nseg):
        _load, length = struct.unpack_from("<II", img, off)
        off += 8
        checksum ^= _xor(img, off, length)
        off += length
    # pad to a 16-byte boundary; the last byte of that block is the checksum
    pos = off
    while (pos - app_offset + 1) % 16 != 0:
        pos += 1
    checksum_pos = pos
    image_end = checksum_pos + 1
    if hash_appended:
        tmp = bytearray(img[app_offset:image_end])
        tmp[checksum_pos - app_offset] = checksum  # hash is over the image incl. the checksum
        return checksum, checksum_pos, image_end, hashlib.sha256(bytes(tmp)).digest()
    return checksum, checksum_pos, None, None


def _xor(buf, start, length):
    x = 0
    for i in range(start, start + length):
        x ^= buf[i]
    return x


def verify_image(image, app_offset=APP_OFFSET):
    """True if our recomputed checksum/hash match what's already in the image."""
    img = memoryview(image)
    checksum, checksum_pos, hash_pos, sha = _app_checksum_and_hash(img, app_offset)
    if img[checksum_pos] != checksum:
        return False
    if hash_pos is not None and bytes(img[hash_pos:hash_pos + 32]) != sha:
        return False
    return True


def patch_wifi(image, ssid, password, app_offset=APP_OFFSET):
    """Return a copy of image with the placeholder Wi-Fi replaced and checksum/hash fixed."""
    img = bytearray(image)
    for prefix, label, value in ((SSID_PREFIX, "SSID", ssid), (PASS_PREFIX, "password", password)):
        i = img.find(prefix)
        if i < 0:
            raise ValueError(f"{label} placeholder not found; is this the direct build?")
        end = img.find(b"\x00", i)  # the slot runs to the string's null terminator
        if end < 0 or not (len(prefix) <= end - i <= 64):
            raise ValueError(f"{label} placeholder slot looks wrong (length {end - i})")
        slot = end - i
        raw = value.encode("utf-8")
        if len(raw) > slot:
            raise ValueError(f"{label} is too long ({len(raw)} > {slot} bytes for this build)")
        img[i:end] = raw + b"\x00" * (slot - len(raw))

    checksum, checksum_pos, hash_pos, sha = _app_checksum_and_hash(img, app_offset)
    img[checksum_pos] = checksum
    if hash_pos is not None:
        img[hash_pos:hash_pos + 32] = sha
    return bytes(img)


if __name__ == "__main__":
    import sys
    data = open(sys.argv[1], "rb").read()
    print("unpatched image self-check (checksum+hash match file):", verify_image(data))
    patched = patch_wifi(data, "MyNetwork", "hunter2password")
    print("patched image self-check:", verify_image(patched))
    print("size unchanged:", len(patched) == len(data))
    print("placeholders gone:", SSID_PREFIX not in patched and PASS_PREFIX not in patched)
    print("new ssid present:", b"MyNetwork\x00" in patched)
    print("new password present:", b"hunter2password\x00" in patched)
