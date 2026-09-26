# PyInstaller build recipe for Read Aloud (Windows .exe and macOS .app)
import sys
from PyInstaller.utils.hooks import collect_all

VERSION = "1.0.0"
datas, binaries, hidden = [], [], ["_cffi_backend"]
for pkg in ("miniaudio", "cffi", "edge_tts", "certifi"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hidden += h
if sys.platform == "darwin":
    hidden += ["ServiceManagement", "Quartz", "ApplicationServices", "AppKit", "Foundation"]
else:
    hidden += ["tkinter", "tkinter.font", "tkinter.messagebox"]
    datas += [("assets/icon.ico", "assets")]

a = Analysis(["main.py"], datas=datas, binaries=binaries, hiddenimports=hidden,
             excludes=["numpy", "PIL", "matplotlib"])
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Read Aloud",
              console=False, icon="assets/icon.icns")
    coll = COLLECT(exe, a.binaries, a.datas, name="Read Aloud")
    app = BUNDLE(coll, name="Read Aloud.app", icon="assets/icon.icns",
                 bundle_identifier="com.readaloud.button",
                 info_plist={
                     "CFBundleName": "Read Aloud",
                     "CFBundleDisplayName": "Read Aloud",
                     "CFBundleShortVersionString": VERSION,
                     "CFBundleVersion": VERSION,
                     "LSUIElement": False,          # show in the Dock so it can be quit normally
                     "NSHighResolutionCapable": True,
                     "LSMinimumSystemVersion": "11.0",
                 })
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="ReadAloud",
              console=False, icon="assets/icon.ico", upx=False)
