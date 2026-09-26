# Read Aloud

A small floating button that reads out any text you select, using Microsoft's natural-sounding voices (the same ones as Edge and Word's Read Aloud).

**Select text in any app → click the button → it reads it out. Click again to stop.**

- The button stays on top of every window, and you can drag it anywhere.
- Right-click it to change the voice (British, American or Australian, male or female) or the speed.
- Speech starts almost instantly. It keeps a connection to Microsoft ready and plays the audio as it arrives.
- It needs an internet connection. No account or sign-up.

## Download

Go to **[Releases → Latest](../../releases/latest)** and download:

| Computer | File |
|---|---|
| Windows 10 / 11 | `ReadAloud-Windows.exe` |
| Mac with Apple Silicon (M1 or newer) | `ReadAloud-Mac.zip` |
| Mac with an Intel chip (2015–2020), macOS 11 Big Sur or newer | `ReadAloud-Mac-Intel.zip` |

Not sure which Mac you have? Click the Apple menu → **About This Mac**. If it says **Chip: Apple M…**, it's Apple Silicon. If it says **Processor: … Intel**, it's Intel.

## First launch

These builds aren't signed with a paid Apple or Microsoft developer certificate, so both systems show a warning the first time. You only have to do this once.

### Windows
1. Double-click `ReadAloud-Windows.exe`.
2. If you see **"Windows protected your PC"**, click **More info → Run anyway**.
3. The round button appears on the right of your screen. Right-click it and tick **Start with Windows** if you want it there every time.

### Mac
1. Open the zip and drag **Read Aloud** into your **Applications** folder.
2. Open it. If macOS says it **"can't be opened"** or **"Apple could not verify"** it, open **System Settings → Privacy & Security**, scroll down, and click **Open Anyway** next to Read Aloud.
3. macOS will ask you to allow Read Aloud under **Privacy & Security → Accessibility**. Switch it on. It needs this to see the text you've selected.
4. Right-click the button and choose **Open at Login** if you want it there every time.

## Troubleshooting
- **Orange button when clicked:** no text was selected (or, on a Mac, Accessibility permission isn't switched on yet).
- **Red button:** it couldn't reach Microsoft, so check your internet connection.
- **Logs:** Windows `%APPDATA%\Read Aloud\read-aloud.log` · Mac `~/Library/Application Support/Read Aloud/read-aloud.log`

## How it works
`readaloud/engine.py` streams speech from Microsoft's Edge Read Aloud service (via [edge-tts](https://github.com/rany2/edge-tts)) and decodes it as it arrives, using [miniaudio](https://github.com/irmen/pyminiaudio). The button is native on each system: PyObjC on macOS, tkinter + Win32 on Windows. GitHub Actions builds both apps, launches each one to test real speech, and publishes them to Releases.

This project isn't affiliated with Microsoft. It uses the same free online voices as Microsoft Edge.
