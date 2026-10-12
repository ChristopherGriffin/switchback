#!/usr/bin/env python3
"""switchbota, all in one.

The firmware server, a DNS responder for wohand.com, and the Bluetooth flasher in
one program, so it can be shipped as a single switchbota.exe with no git, no Node,
no ESPHome and no Python for the person running it.

    switchbota_app.py                 serve + DNS, scan and convert one plug
    switchbota_app.py --count 10      a batch, asking before each
    switchbota_app.py --no-dns        server only (you redirect wohand.com elsewhere)

It serves app.bin and payload.bin from next to this file (server/bin in the repo,
or bundled into the exe). Point your plugs' DNS at this PC and it does the rest.
"""

import argparse
import asyncio
import datetime
import hashlib
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import sbplug_batch as sb

OTA_HOSTS = ("wohand.com", "www.wohand.com")


def data_dir():
    # PyInstaller unpacks bundled files to _MEIPASS; in the repo they live in server/bin
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent / "server" / "bin"


def local_ip():
    # the address other machines reach us on, found without sending anything
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class LiveFeed:
    """Stands in for the HTTP /events feed, filled in process by the HTTP handler."""

    def __init__(self):
        self._events = []
        self._lock = threading.Lock()
        self.seq = 0

    def add(self, event, ip, mac, path, **extra):
        with self._lock:
            self.seq += 1
            entry = dict(seq=self.seq, time=datetime.datetime.now().isoformat(), event=event,
                         ip=ip, mac=mac, path=path, **extra)
            self._events.append(entry)
        print(f"{time.strftime('%H:%M:%S')}  server: {event} from {ip}"
              + (f" after {extra['seconds']}s" if "seconds" in extra else ""), flush=True)

    def poll(self):
        with self._lock:
            out, self._events = self._events, []
            return out


class Firmware:
    def __init__(self):
        self.app = (data_dir() / "app.bin").read_bytes()
        self.payload = (data_dir() / "payload.bin").read_bytes()
        self.md5 = hashlib.md5(self.payload).hexdigest()


def make_handler(fw, feed):
    def mac_of(handler):
        raw = handler.headers.get("X-Device-MAC", "")
        parts = raw.lower().replace("-", ":").split(":")
        if len(parts) == 6 and all(len(p) in (1, 2) and all(c in "0123456789abcdef" for c in p) for p in parts):
            return ":".join(p.zfill(2) for p in parts)
        return None

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, body, content_type="application/octet-stream", close=False):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if close:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            ip = self.client_address[0]
            mac = mac_of(self)
            path = self.path.split("?")[0]
            if path == "/payload.md5":
                feed.add("payload_md5", ip, mac, path)
                self._send(f"{fw.md5} {len(fw.payload)}\n".encode(), "text/plain")
            elif path == "/payload.bin":
                started = time.monotonic()
                feed.add("payload_start", ip, mac, path, size=len(fw.payload))
                self._send(fw.payload, close=True)
                feed.add("payload_done", ip, mac, path, seconds=round(time.monotonic() - started))
            else:
                if path == "/app.bin":
                    feed.add("flip", ip, mac, path)
                elif path.endswith(".bin"):
                    feed.add("stage1", ip, mac, path)
                self._send(fw.app)

    return Handler


def start_http(fw, feed, port):
    server = ThreadingHTTPServer(("", port), make_handler(fw, feed))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def start_dns(ip, port, upstream="8.8.8.8"):
    """Answer A queries for wohand.com with our IP, forward everything else."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", port))

    def qname(data):
        labels, i = [], 12
        while i < len(data) and data[i] != 0:
            n = data[i]
            labels.append(data[i + 1:i + 1 + n].decode("ascii", "replace"))
            i += n + 1
        return ".".join(labels), i + 1  # i+1 is the byte after the name's null terminator

    def serve():
        while True:
            try:
                data, addr = sock.recvfrom(512)
            except OSError:
                return
            try:
                name, after = qname(data)
                qtype = int.from_bytes(data[after:after + 2], "big")
                if qtype == 1 and name.lower().endswith("wohand.com"):
                    header = data[:2] + b"\x81\x80" + data[4:6] + b"\x00\x01" + b"\x00\x00\x00\x00"
                    answer = b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04" + socket.inet_aton(ip)
                    sock.sendto(header + data[12:after + 4] + answer, addr)
                else:
                    fwd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    fwd.settimeout(3)
                    try:
                        fwd.sendto(data, (upstream, 53))
                        reply, _ = fwd.recvfrom(512)
                        sock.sendto(reply, addr)
                    except OSError:
                        pass
                    finally:
                        fwd.close()
            except Exception:
                pass

    threading.Thread(target=serve, daemon=True).start()
    return sock


async def run(args):
    fw = Firmware()
    ip = args.ip or local_ip()
    print(f"switchbota ready. This PC is {ip}")
    print(f"  payload: {len(fw.payload)} bytes, md5 {fw.md5}")

    feed = LiveFeed()
    try:
        start_http(fw, feed, args.http_port)
    except OSError as error:
        print(f"Could not bind port {args.http_port} ({error}). Close whatever is using it, or run as administrator.")
        return
    if not args.no_dns:
        try:
            start_dns(ip, args.dns_port)
            print(f"  DNS is answering on this PC (port {args.dns_port}).")
            print(f"  Point your plugs' network DNS at {ip} so they reach this server.")
        except OSError as error:
            print(f"  Could not start the DNS server ({error}). Redirect wohand.com to {ip} yourself, "
                  "or close whatever is using port 53.")
    else:
        print(f"  DNS off. Redirect {', '.join(OTA_HOSTS)} to {ip} in your own DNS.")

    if args.serve_only:
        print("\nServing. Press Ctrl+C to stop.")
        while True:
            await asyncio.sleep(3600)

    handled = set()
    converted = 0
    while converted < args.count:
        plugs = [p for p in await sb.scan(args) if p.ble_mac not in handled]
        if not plugs:
            print("No more Plug Minis in range.")
            break
        plug = plugs[0]
        handled.add(plug.ble_mac)
        if not args.yes:
            if (await asyncio.to_thread(input, f"Convert {plug}? [y/N] ")).strip().lower() != "y":
                continue
        await sb.convert(plug, feed, args)
        converted += 1
    print(f"Converted {converted} plug(s).")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--count", type=int, default=1, help="how many plugs to convert this run")
    p.add_argument("--only", type=str.lower, help="only the plug with this BLE MAC")
    p.add_argument("--yes", action="store_true", help="do not ask before each plug")
    p.add_argument("--serve-only", action="store_true", help="run the server and DNS, do not flash")
    p.add_argument("--no-dns", action="store_true", help="do not run the DNS server")
    p.add_argument("--ip", help="this PC's address to hand out (default: detected)")
    p.add_argument("--domain", help="DNS suffix such as .lan, to find plugs across VLANs")
    p.add_argument("--printer", help="ZPL printer host:port for labels")
    p.add_argument("--scan-time", type=float, default=10.0)
    p.add_argument("--fw-byte", type=lambda t: int(t, 16), default=0x0C)
    p.add_argument("--stage1-timeout", type=float, default=900.0)
    p.add_argument("--timeout", type=float, default=600.0)
    p.add_argument("--log", default="sbplug_converted.csv")
    p.add_argument("--txt", default="sbplug_flashed.txt", help="numbered text log of flashed plugs, both MACs, in flash order")
    p.add_argument("--http-port", type=int, default=80)
    p.add_argument("--dns-port", type=int, default=53)
    p.add_argument("--check", action="store_true", help="load the bundled firmware and exit (no network)")
    args = p.parse_args()
    if args.check:
        fw = Firmware()
        print(f"firmware source: {data_dir()}")
        print(f"app.bin: {len(fw.app)} bytes")
        print(f"payload.bin: {len(fw.payload)} bytes, md5 {fw.md5}")
        return
    # convert() expects these names
    args.portal = True
    args.skip_dns_check = True
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
