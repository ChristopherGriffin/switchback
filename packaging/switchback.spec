# PyInstaller spec for switchback.exe (one file, console, asks for admin).
#
# Needs these present before building:
#   server/bin/app.bin     the flasher (from the GitHub release, or built in espressif/)
#   dist/payload.bin       the ship firmware (python tools/build_payload.py ship -o dist/payload.bin)
#
# Build:  pyinstaller packaging/switchback.spec
# Output: dist/switchback.exe

import os
from PyInstaller.utils.hooks import collect_all

root = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [
    (os.path.join(root, "server", "bin", "app.bin"), "."),
    (os.path.join(root, "dist", "payload.bin"), "."),         # portal build
    (os.path.join(root, "dist", "payload-direct.bin"), "."),  # patchable build for direct mode
]
binaries = []
hiddenimports = ["sbplug_batch", "wifi_patch"]

# bleak and zeroconf pull in platform backends (WinRT on Windows) that PyInstaller
# does not always find on its own.
for pkg in ("bleak", "zeroconf", "winrt"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

a = Analysis(
    [os.path.join(root, "tools", "switchback.py")],
    pathex=[os.path.join(root, "tools")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "esphome", "platformio"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="switchback",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    # Ports 53 and 80 bind without admin on Windows, so no forced UAC prompt. The
    # first run shows the normal one-time Windows Firewall allow instead.
    icon=os.path.join(root, "packaging", "switchback.ico") if os.path.exists(os.path.join(root, "packaging", "switchback.ico")) else None,
)
