# Read Aloud

A small floating button that reads out any text you select, using Microsoft's natural-sounding voices (the same ones as Edge and Word's Read Aloud).

**Select text in any app → click the button → it reads it out. Click again to stop.**

- The button stays on top of every window, and you can drag it anywhere.
- Right-click it to change the voice (British, American or Australian, male or female) or the speed.
- Speech starts almost instantly. It keeps a connection to Microsoft ready and plays the audio as it arrives.
- No account or sign-up.
- **Built-in offline voice.** If Microsoft's voices can't be reached (no internet, or Microsoft changes something), the button switches to an offline voice ([Kokoro](https://huggingface.co/hexgrad/Kokoro-82M)) that runs on your own computer. You can also pick **Voice → Offline voice only**. The offline voice downloads once (177 MB) in the background the first time you open the app, and the menu shows its progress.

## Download

Go to **[Releases → Latest](../../releases/latest)** and download:

| Computer | File |
|---|---|
| Windows 10 / 11 | `ReadAloud-Windows.exe` |
| Mac with Apple Silicon (M1 or newer) | `ReadAloud-Mac.zip` |
| Mac with an Intel chip (2015–2020), macOS 11 Big Sur or newer | `ReadAloud-Mac-Intel.zip` |

Not sure which Mac you have? Click the Apple menu → **About This Mac**. If it says **Chip: Apple M…**, it's Apple Silicon. If it says **Processor: … Intel**, it's Intel.

## Updates

Read Aloud **updates itself**. It checks for a new version when it opens and every few hours after that, downloads it in the background, checks it against the release's fingerprints (`SHA256SUMS.txt`), and swaps it in the next time it isn't speaking. The button disappears for a second and comes back in the same spot. Right-click the button to see your version, **Check for updates** now, or switch off **Update automatically**.

On a Mac, macOS may ask you to switch Read Aloud back on under **Privacy & Security → Accessibility** after an update, because each build is unsigned.

A daily check on GitHub makes sure Microsoft's voices still work. When the library the app relies on (edge-tts) gets a fix, a new version is built, tested on Windows and both kinds of Mac, and published automatically.

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
- **Red button:** it couldn't reach Microsoft and the offline voice isn't downloaded yet. Check your internet connection.
- **Offline voice is slow to start on older computers.** It makes the speech on your own machine. Expect 1–2 seconds on an older laptop and well under a second on newer ones.
- **Logs:** Windows `%APPDATA%\Read Aloud\read-aloud.log` · Mac `~/Library/Application Support/Read Aloud/read-aloud.log`

## How it works
`readaloud/engine.py` streams speech from Microsoft's Edge Read Aloud service (via [edge-tts](https://github.com/rany2/edge-tts)) and decodes it as it arrives, using [miniaudio](https://github.com/irmen/pyminiaudio). The button is native on each system: PyObjC on macOS, tkinter + Win32 on Windows. GitHub Actions builds both apps, launches each one to test real speech, and publishes them to Releases.

This project isn't affiliated with Microsoft. It uses the same free online voices as Microsoft Edge.
