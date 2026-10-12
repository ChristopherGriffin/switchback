# SwitchbOTA for ESPHome

![switchbot-product-photo](./images/switchbot-product-photo.jpg)

This replaces the stock firmware on the SwitchBot Plug Mini with [ESPHome](https://esphome.io), over Wi-Fi, without opening the case. It works on the W1901400 and the HomeKit W1901401. Both are the same ESP32-C3 hardware.

It is a fork of [kendallgoto/switchbota](https://github.com/kendallgoto/switchbota). The original puts Tasmota 11.1 on the plug and you move to ESPHome from there. This fork writes ESPHome directly, and adds a one-command runner, a server installer, a build step for the ESPHome image, and a script that drives the whole conversion over Bluetooth.

## Read this first

**This is early.** Treat your first plug as an experiment.

The process overwrites the plug's bootloader over Wi-Fi. If the plug loses power at the wrong moment, or is written with something broken, it is bricked. These plugs are welded shut, so recovery means cutting the case open and flashing over the serial pads. Do not unplug or bump a plug while it is converting. Try one plug before you do a box of them.

This is a proof of concept and comes with no warranty.

## What you need

- A SwitchBot Plug Mini, already joined to your Wi-Fi with the SwitchBot app.
- A computer with Bluetooth, on the same network, to trigger the plug.
- A way to point two hostnames at that computer in your DNS (covered below). This is the one piece people get stuck on, so it has its own section.
- The plug and that computer do not have to be on the same subnet, but the plug has to be able to reach it.

## How it works

1. You join the plug to your Wi-Fi with the SwitchBot app, as normal.
2. A small server on your computer pretends to be SwitchBot's update server. Your DNS sends the plug there.
3. A Bluetooth command tells the plug to update. It fetches a tiny flasher from your server, then fetches the ESPHome image and writes it over itself.
4. The plug reboots into ESPHome and appears on your network as `sbplug-xxxxxx`.

## The simple way: run it all from one Windows PC

Your PC does everything: serves the firmware, answers DNS, and talks Bluetooth to the plugs. You need [Node.js](https://nodejs.org), [Python](https://python.org) with ESPHome (`pip install esphome`), and a Bluetooth radio. The script sets up the rest.

Get the code:

```
git clone -b esphome-direct https://github.com/ChristopherGriffin/switchbota.git
cd switchbota
```

**Step 1, one time: point your plugs at this PC for DNS.** Run the script once to see this PC's address:

```powershell
.\switchbota.ps1
```

It prints a line like `This PC is 192.168.1.39` and the DNS instructions. Set that up (see the DNS section below), then stop the script with Ctrl+C.

**Step 2: convert a plug.**

```powershell
.\switchbota.ps1 -Flash
```

This builds the ESPHome image the first time (that takes a while, it downloads a toolchain), starts the server, scans for a plug over Bluetooth, and asks before converting it. A status bar shows each stage. Leave the plug powered until it says the plug is up.

That is the whole thing. Options:

- `.\switchbota.ps1 -Flash -Implant` builds your Wi-Fi name and password into the plug, so it joins your network by itself. By default (without `-Implant`) the plug comes up as its own hotspot and you pick the network through a captive portal. The default needs nothing from you; implant needs your Wi-Fi in `esphome\secrets.yaml`.
- `.\switchbota.ps1 -NoDns` runs the server without the built-in DNS, for when you redirect wohand.com some other way (a Pi-hole, your router, Technitium).
- `.\switchbota.ps1 -Rebuild` rebuilds the image, for after you change a setting.

If a plug does not convert, the plug is unharmed as long as it still shows up in a Bluetooth scan. Leave it powered and try again.

## Point your plugs' DNS at the server

The plug looks up `wohand.com` and `www.wohand.com`. You make those resolve to your server instead of SwitchBot. Pick whichever you can do:

- **Let the server do it.** `switchbota.ps1` runs a DNS server for you. Set your router's DHCP "DNS server" to your PC's address, or set it on just the network the plugs are on. Everything keeps resolving normally; only wohand.com is redirected. This is the simplest if you do not run your own DNS.
- **Pi-hole or AdGuard Home.** Add a local DNS record (Pi-hole: Local DNS, DNS Records) or a DNS rewrite (AdGuard: Filters, DNS rewrites) sending `wohand.com` and `www.wohand.com` to the server's address. Then run with `-NoDns`.
- **Technitium, pfSense, or similar.** Add host overrides for `wohand.com` and `www.wohand.com` pointing at the server, and run with `-NoDns`.

Check it from another device on the network:

```
nslookup www.wohand.com
```

It should answer with your server's address.

## After the plug is on ESPHome

The image this writes is a small bootstrap. It gets the plug on Wi-Fi with an API key and OTA password (in `esphome\secrets.yaml`, generated for you on first run). From there, adopt it in ESPHome or Home Assistant and push your real configuration over OTA. A full plug config for this hardware (relay, button, power monitoring, LEDs) lives in [esphome-miniplug-framework](https://github.com/ChristopherGriffin/esphome-miniplug-framework).

The button on the plug: a short press toggles the relay, and holding it for 10 seconds factory resets. On a portal build, a reset sends it back to its hotspot.

## The other way: a dedicated always-on server

If you would rather run the server on a Linux box (a Pi, a NUC, a VM) and keep it up, use the installer. On Debian or Ubuntu:

```
git clone -b esphome-direct https://github.com/ChristopherGriffin/switchbota.git
sudo sh switchbota/server/install.sh
sudo sh switchbota/server/install.sh build portal
```

It installs Docker if needed, builds the image, and runs the server. Then point your DNS at that box (the DNS section above), and run the Bluetooth flasher from any computer with Bluetooth:

```
python tools/sbplug_batch.py --server http://<that box's address>
```

## If something goes wrong

- **The plug never fetches firmware.** Its DNS is not reaching your server. Check `nslookup www.wohand.com` from a device on the plug's network, and that the plug can reach the server on port 80.
- **The flasher says a port is busy.** Something else on the PC is using port 80 or 53. Close it, or use the dedicated-server path on another box.
- **The plug is not found over Bluetooth.** Make sure it is powered and nearby, and that your computer's Bluetooth is on. A plug already converted will not show up as a SwitchBot device any more.
- **A plug got stuck mid-convert.** As long as it still appears in a Bluetooth scan, it is still on stock firmware and fine. Run the conversion again.
- **It is actually bricked.** It stopped responding during the write. Recovery is opening the case and flashing over the serial pads, which are the four pads inside.

## Credits

Fork of [kendallgoto/switchbota](https://github.com/kendallgoto/switchbota). The hard reverse engineering of the OTA and BLE process is theirs. This fork adds the direct-to-ESPHome path and the tooling around it.

Licensed under the GNU GPL v3, same as upstream.
