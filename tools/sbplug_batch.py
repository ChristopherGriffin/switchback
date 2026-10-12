#!/usr/bin/env python3
"""Convert SwitchBot Plug Minis (W1901400 / W1901401) to ESPHome with switchbota.

Runs on a box with a Bluetooth radio. For each plug it:
  1. finds the plug by its Woan (0x0969) advertisement
  2. sends the BLE command that makes the stock firmware fetch app.bin from the
     switchbota server (the plug must already be on Wi-Fi via the SwitchBot app,
     and wohand.com must resolve to the server)
  3. polls OTA progress over BLE until the plug reboots into switchbota
  4. follows the server's /events feed for that plug's MAC
  5. waits for sbplug-xxxxxx to show up on mDNS
  6. prints a ZPL label

It does one plug per run unless --count says otherwise.

    pip install -r requirements.txt
    python sbplug_batch.py --scan-only
    python sbplug_batch.py
    python sbplug_batch.py --count 10 --yes
"""

import argparse
import asyncio
import csv
import datetime
import json
import socket
import sys
import time
import urllib.request

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

WOAN_COMPANY_ID = 0x0969
SWITCHBOT_SERVICE_DATA = "0000fd3d-0000-1000-8000-00805f9b34fb"
PLUG_MINI_MODELS = ("g", "j")
PLUG_MINI_MFR_LEN = 12  # fallback when the scan response with the model byte is missed

WRITE_CHAR = "cba20002-224d-11e6-9fb8-0002a5d5c51b"
NOTIFY_CHAR = "cba20003-224d-11e6-9fb8-0002a5d5c51b"
CMD_OTA_START = bytes.fromhex("570f0a01")  # followed by the firmware version byte
CMD_OTA_PROGRESS = bytes.fromhex("570f0b")

OTA_HOSTS = ("www.wohand.com", "wohand.com")
ESPHOME_SERVICE = "_esphomelib._tcp.local."
ESPHOME_PREFIX = "sbplug"

# 1.5 x 0.75 inch label at 203 dpi (GK420t): 305 x 152 dots. Text on the left, a QR code
# of the device name on the right. A Code 128 of the name does not fit at a scannable width.
LABEL_ZPL = (
    "^XA^CI28^PW305^LL152^LH0,0"
    "^FO10,16^A0N,30,30^FD{name}^FS"
    "^FO10,58^A0N,22,22^FD{mac}^FS"
    "^FO10,94^A0N,20,20^FD{date}^FS"
    "^FO218,20^BQN,2,3^FDMA,{name}^FS"
    "^XZ"
)


class Failed(Exception):
    pass


def log(message):
    print(f"{time.strftime('%H:%M:%S')}  {message}", flush=True)


def hexs(data):
    return data.hex(" ") if data else "(no reply)"


def mac_offset(mac, delta):
    value = (int(mac.replace(":", ""), 16) + delta) % (1 << 48)
    text = f"{value:012x}"
    return ":".join(text[i:i + 2] for i in range(0, 12, 2))


class Plug:
    def __init__(self, device, adv):
        self.device = device
        mfr = adv.manufacturer_data[WOAN_COMPANY_ID]
        service = adv.service_data.get(SWITCHBOT_SERVICE_DATA)
        self.model = chr(service[0] & 0x7F) if service else None
        self.rssi = adv.rssi
        self.is_plug_mini = self.model in PLUG_MINI_MODELS or (
            self.model is None and len(mfr) == PLUG_MINI_MFR_LEN)
        # macOS hides the real address, but the advertisement carries it
        address = device.address.lower()
        if address.count(":") != 5 and len(mfr) >= 6:
            address = mfr[:6].hex(":")
        self.ble_mac = address
        # ESP32 convention: the Bluetooth MAC is the Wi-Fi station MAC plus 2
        self.wifi_mac = mac_offset(address, -2)
        self.name = f"{ESPHOME_PREFIX}-{self.wifi_mac.replace(':', '')[-6:]}"

    def __str__(self):
        return f"{self.ble_mac} (Wi-Fi {self.wifi_mac}, model {self.model or '?'}, {self.rssi} dBm)"


async def scan(args):
    """Return the Plug Minis in range, strongest signal first."""
    found = await BleakScanner.discover(timeout=args.scan_time, return_adv=True)
    plugs = []
    for device, adv in found.values():
        if WOAN_COMPANY_ID not in adv.manufacturer_data:
            continue
        plug = Plug(device, adv)
        if not plug.is_plug_mini:
            log(f"skipping Woan device that is not a Plug Mini: {plug}")
        elif not args.only or plug.ble_mac == args.only:
            plugs.append(plug)
    return sorted(plugs, key=lambda p: p.rssi, reverse=True)


class EventFeed:
    """The server's /events endpoint, read incrementally."""

    def __init__(self, server):
        self.server = server.rstrip("/")
        self.seq = 0

    def get(self, path):
        with urllib.request.urlopen(self.server + path, timeout=5) as response:
            return response.read().decode()

    def poll(self):
        feed = json.loads(self.get(f"/events?since={self.seq}"))
        self.seq = feed["seq"]
        return feed["events"]


class PlugEvents:
    """Picks one plug's events out of the feed and logs them as they arrive."""

    def __init__(self, feed, plug):
        self.feed = feed
        self.mac = plug.wifi_mac
        self.ips = set()
        self.seen = []

    async def update(self):
        try:
            events = await asyncio.to_thread(self.feed.poll)
        except (OSError, ValueError) as error:
            log(f"server event feed unavailable: {error}")
            return
        for event in events:
            # The stock firmware's request has no MAC unless the server's ARP table knows it
            unattributed = event["mac"] is None and (event["ip"] in self.ips or event["event"] == "stage1")
            if event["mac"] != self.mac and not unattributed:
                continue
            self.ips.add(event["ip"])
            self.seen.append(event["event"])
            detail = f" after {event['seconds']}s" if "seconds" in event else ""
            log(f"server: {event['event']} from {event['ip']}{detail}")
            if event["event"] == "payload_md5" and "payload_start" in self.seen:
                log("server: the flasher is retrying, leave the plug powered")

    def saw(self, *names):
        return any(name in self.seen for name in names)


def preflight(args, feed):
    """Check the server and DNS before any plug is told to do anything."""
    try:
        md5, size = feed.get("/payload.md5").split()
        feed.poll()
    except (OSError, ValueError) as error:
        raise Failed(f"switchbota server at {feed.server} is not answering: {error}")
    log(f"server payload: {size} bytes, md5 {md5}")

    if args.skip_dns_check:
        return
    server_host = feed.server.split("//")[-1].split("/")[0].split(":")[0]
    server_ips = {info[4][0] for info in socket.getaddrinfo(server_host, 80, socket.AF_INET)}
    for host in OTA_HOSTS:
        try:
            ips = {info[4][0] for info in socket.getaddrinfo(host, 80, socket.AF_INET)}
        except OSError as error:
            raise Failed(f"{host} does not resolve: {error}")
        if not ips & server_ips:
            raise Failed(f"{host} resolves to {sorted(ips)}, not the server ({sorted(server_ips)}). "
                         "Fix the DNS override, or pass --skip-dns-check if only this box sees it that way.")
    log(f"DNS: {', '.join(OTA_HOSTS)} point at {server_host}")


async def command(client, replies, data):
    while not replies.empty():
        replies.get_nowait()
    await client.write_gatt_char(WRITE_CHAR, data, response=False)
    try:
        return await asyncio.wait_for(replies.get(), timeout=5)
    except asyncio.TimeoutError:
        return None


async def run_stage1(plug, tracker, args):
    """Start the stock OTA and follow it until the plug reboots into switchbota."""
    replies = asyncio.Queue()
    async with BleakClient(plug.device, timeout=30) as client:
        await client.start_notify(NOTIFY_CHAR, lambda _, data: replies.put_nowait(bytes(data)))
        reply = await command(client, replies, CMD_OTA_START + bytes([args.fw_byte]))
        log(f"OTA start reply: {hexs(reply)}")

        started = time.monotonic()
        progress = None
        while time.monotonic() - started < args.stage1_timeout:
            await asyncio.sleep(2)
            await tracker.update()
            if tracker.saw("flip", "payload_md5"):
                return
            if not tracker.saw("stage1") and time.monotonic() - started > 60:
                raise Failed("the plug never fetched firmware from the server. Check that it is on Wi-Fi, "
                             "that its DNS sends wohand.com to the server, and that --fw-byte differs "
                             "from the firmware version it already runs.")
            try:
                reply = await command(client, replies, CMD_OTA_PROGRESS)
            except (BleakError, OSError):
                reply = None
            if not client.is_connected:
                log("BLE link dropped, the plug is rebooting")
                return
            if reply and len(reply) >= 2 and reply[1] != progress:
                progress = reply[1]
                log(f"stage 1: {progress}%  [{hexs(reply)}]")
            if progress is not None and progress >= 100:
                return
    raise Failed("stage 1 did not finish in time")


async def wait_for_esphome(plug, tracker, args):
    """Follow stage 2 on the server until the plug announces itself as ESPHome."""
    zeroconf = AsyncZeroconf()
    try:
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            await tracker.update()
            info = AsyncServiceInfo(ESPHOME_SERVICE, f"{plug.name}.{ESPHOME_SERVICE}")
            if await info.async_request(zeroconf.zeroconf, 2000):
                return info
    finally:
        await zeroconf.async_close()
    raise Failed(f"{plug.name} never appeared on mDNS. Leave the plug powered and check the server log.")


def print_label(printer, name, wifi_mac):
    host, _, port = printer.partition(":")
    zpl = LABEL_ZPL.format(name=name, mac=wifi_mac.upper(), date=datetime.date.today().isoformat())
    with socket.create_connection((host, int(port or 9100)), timeout=10) as connection:
        connection.sendall(zpl.encode("ascii"))


async def convert(plug, feed, args):
    await asyncio.to_thread(feed.poll)  # skip anything already in the feed
    tracker = PlugEvents(feed, plug)

    log(f"converting {plug}, expecting {plug.name}")
    await run_stage1(plug, tracker, args)
    log("stage 1 done, waiting for switchbota to write the payload. Do not unplug.")
    info = await wait_for_esphome(plug, tracker, args)

    address = ", ".join(info.parsed_addresses())
    reported = (info.properties.get(b"mac") or b"").decode()
    log(f"{plug.name} is up at {address}")
    if reported and reported != plug.wifi_mac.replace(":", ""):
        log(f"warning: it reports MAC {reported}, expected {plug.wifi_mac}")

    with open(args.log, "a", newline="") as handle:
        csv.writer(handle).writerow(
            [datetime.datetime.now().isoformat(timespec="seconds"), plug.name, plug.wifi_mac, plug.ble_mac, address])
    if not args.no_print:
        try:
            print_label(args.printer, plug.name, plug.wifi_mac)
            log(f"label sent to {args.printer}")
        except OSError as error:
            log(f"label not printed: {error}")


async def run(args):
    if args.print_test:
        print_label(args.printer, f"{ESPHOME_PREFIX}-000000", "00:00:00:00:00:00")
        return
    if args.scan_only:
        for plug in await scan(args):
            log(f"{plug} -> {plug.name}")
        return

    feed = EventFeed(args.server)
    preflight(args, feed)
    handled = set()
    converted = 0
    while converted < args.count:
        plugs = [plug for plug in await scan(args) if plug.ble_mac not in handled]
        if not plugs:
            log("no more Plug Minis in range")
            break
        plug = plugs[0]
        handled.add(plug.ble_mac)
        if not args.yes:
            answer = await asyncio.to_thread(input, f"Convert {plug}? [y/N] ")
            if answer.strip().lower() != "y":
                continue
        await convert(plug, feed, args)
        converted += 1
    log(f"converted {converted} plug(s)")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", default="http://rando.symtus.com", help="switchbota server base URL")
    parser.add_argument("--count", type=int, default=1, help="how many plugs to convert this run (default 1)")
    parser.add_argument("--only", type=str.lower, help="only touch the plug with this BLE MAC")
    parser.add_argument("--yes", action="store_true", help="do not ask before each plug")
    parser.add_argument("--scan-only", action="store_true", help="list the Plug Minis in range and exit")
    parser.add_argument("--scan-time", type=float, default=10, help="seconds per BLE scan")
    parser.add_argument("--fw-byte", type=lambda text: int(text, 16), default=0x0C,
                        help="version byte for the OTA start command, hex. 0C is v1.2, 0D v1.3, 0E v1.4. "
                             "It has to differ from what the plug runs now.")
    parser.add_argument("--stage1-timeout", type=float, default=300, help="seconds allowed for the stock OTA")
    parser.add_argument("--timeout", type=float, default=600, help="seconds allowed from stage 1 to mDNS")
    parser.add_argument("--skip-dns-check", action="store_true", help="do not check wohand.com from this box")
    parser.add_argument("--printer", default="LABEL-PRINTER.symtus.com:9100", help="ZPL printer host:port")
    parser.add_argument("--no-print", action="store_true", help="skip the label")
    parser.add_argument("--print-test", action="store_true", help="print a sample label and exit")
    parser.add_argument("--log", default="sbplug_converted.csv", help="CSV file that converted plugs are appended to")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except Failed as error:
        log(f"FAILED: {error}")
        sys.exit(1)
    except KeyboardInterrupt:
        log("interrupted. A plug that is mid-flash carries on by itself, leave it powered.")
        sys.exit(130)


if __name__ == "__main__":
    main()
