# Read Aloud

A small floating button that reads out any text you select, using Microsoft's natural-sounding voices (the same ones as Edge and Word's Read Aloud).

**Select text in any app → click the button → it reads it out. Click again to stop.**

- The button stays on top of every window, and you can drag it anywhere.
- Right-click it to change the voice (British, American or Australian, male or female) or the speed.
- **Speech streams as audio arrives**, sentence by sentence, without waiting for the whole passage. Decoded audio is kept in RAM so repeat reads can skip the network and decoder. The website publishes callback timings separately from audible latency; no 33× or 100× audible-speed improvement is claimed. Playback itself is always real time: a two-minute passage takes two minutes to speak.
- No account or sign-up.
- **Built-in offline voice.** If Microsoft's voices can't be reached (no internet, or Microsoft changes something), the button switches to an offline voice ([Kokoro](https://huggingface.co/hexgrad/Kokoro-82M)) that runs on your own computer. You can also pick **Voice → Offline voice only**: the menu title then says "offline only", a read in progress stops so the next click uses it, and the button turns orange with a prompt if the 177 MB voice hasn't downloaded yet. The offline voice downloads once in the background the first time you open the app, and the menu shows its progress.
- **Nothing is ever dropped in silence.** Every sentence is written down in a ledger as spoken, partially heard or skipped. If part of a read was lost, the button says so and the right-click menu tells you which part — it does not quietly stop.

## Download

Use the public **[Read Aloud download website](https://read-aloud-kvtn6ncvzmt.qoder.website)**. No account is required.

| Computer | Version 1.0.14 |
|---|---|
| Windows 10 / 11, 64-bit | `ReadAloud-1.0.14.exe` |
| Mac with Apple Silicon (M1 or newer) | `ReadAloud-Mac.zip` (macOS 11+) |
| Mac with an Intel chip | `ReadAloud-Mac-Intel.zip` (macOS 11+) |

Both Mac builds are produced and exercised on Apple's macOS runners: the packaged app launches, reads real speech online and offline, falls back to the offline voice, quits from the Dock, and only one button runs at a time. They are unsigned and not notarized and were not tried on a physical Mac before release. The in-app update feed publishes only the Windows build, so Mac updates are manual downloads from the website.

Not sure which Mac you have? Click the Apple menu → **About This Mac**. If it says **Chip: Apple M…**, it's Apple Silicon. If it says **Processor: … Intel**, it's Intel.

## Updates

Read Aloud checks the public website's manifest shortly after launch and every six hours while open. With **Updates → Update automatically** enabled, it also registers one daily noon check: a least-privilege Windows task or a per-user Mac LaunchAgent. The daily check only stages an update; the app installs it when idle or at its next launch. HTTPS origin, file size and SHA-256 are checked before installation. Disabling automatic updates removes the daily task.

On Windows the download is a one-time installer: it copies itself to `%LOCALAPPDATA%\Programs\Read Aloud\ReadAloud.exe`, adds a Start-menu shortcut, and starts that copy. Updates replace that single file in place, so there is only ever one Read Aloud and the file you downloaded can be deleted. A newer download updates the installed copy the same way — you never need to keep it.

Read Aloud 1.0.13 and older checked a download path that no longer exists, so Windows users coming from those versions need one manual download of 1.0.14; from 1.0.14 on, updates are automatic.

On a Mac, macOS may ask you to switch Read Aloud back on under **Privacy & Security → Accessibility** after an update, because each build is unsigned.

Older builds using the previous GitHub update feed need the website download once to move to this feed. Offline model repair is separate: the app rechecks its fingerprint in the background, replaces corrupt bytes, and retries load failures without deleting a valid model.

## First launch

These builds aren't signed with a paid Apple or Microsoft developer certificate, so both systems show a warning the first time. You only have to do this once.

### Windows
1. Double-click `ReadAloud-1.0.14.exe`.
2. If you see **"Windows protected your PC"**, click **More info → Run anyway**.
3. The round button appears on the right of your screen. The download has installed the app in your user folder and added **Read Aloud** to the Start menu; you can delete the downloaded file and start it from there next time.
4. Right-click the button and tick **Start with Windows** if you want it there every time.

### Mac
1. Open the zip and drag **Read Aloud** into your **Applications** folder.
2. Open it. If macOS says it **"can't be opened"** or **"Apple could not verify"** it, open **System Settings → Privacy & Security**, scroll down, and click **Open Anyway** next to Read Aloud.
3. macOS will ask you to allow Read Aloud under **Privacy & Security → Accessibility**. Switch it on. It needs this to see the text you've selected.
4. Right-click the button and choose **Open at Login** if you want it there every time.

## What the button means

| Colour | Meaning | What to do |
|---|---|---|
| Dark grey | idle | select text and click it |
| Dark grey, spinning | grabbing your selection or loading | wait a moment |
| Blue, with a stop sign | speaking | click it again to stop |
| Orange | nothing was selected (or, on a Mac, Accessibility isn't on yet) | select some text |
| Orange, down-arrow | Offline voice only is on but the 177 MB voice isn't downloaded | right-click → Voice → **Download offline voice** |
| Red | this read failed | right-click: the menu shows the reason and **Try again** |

If part of a read was lost the read still ends calmly: on Windows a small orange dot stays on the button, and on both systems the tooltip and the top of the right-click menu show the reason (or "Part of the last read was skipped.") until the next read. Nothing is a 1.6-second flash you have to catch.

## Text and audio privacy

- Online synthesis sends selected text to Microsoft. Offline-only synthesis stays local, although update checks and model repair can still use the network. The last passage is held in memory for Try again; selected words are not written to the log.
- Cached audio is audible content, not encrypted data. Hashes avoid plaintext filenames but are not a guarantee of secrecy.
- Spoken **audio** is kept in RAM (up to 64 MB) so that re-reading the same passage is nearly instant. Disk storage is **off by default**. If you want it, right-click → **Audio cache** → **Keep recent audio on disk**: the menu says what it does, the files are the audio the app already made — never your text — with hashed names, in your AppData / Application Support folder, capped at 64 MB, oldest-first, and **Clear audio cache** removes everything. Cache trouble can never fail a read.
- The log (`read-aloud.log`) records one line per read: how many sentences, timings, and offsets of anything lost. Never the words themselves.

## Troubleshooting
- **Orange button when clicked:** no text was selected (or, on a Mac, Accessibility permission isn't switched on yet).
- **Turned on Offline voice only but expected it to apply to something already reading:** switching the voice stops the read in progress, so click the button again. The menu title shows "· offline only" when it is on, and the next read is local. If the offline voice isn't downloaded yet, the button turns orange with a down-arrow and the menu offers **Download offline voice**.
- **Red button:** it couldn't finish the read. Right-click the button — the menu shows the reason and **Try again**, **Use offline voice** or **Download offline voice**. If Microsoft answered but the passage was long, part of the last read may have been skipped; the button then shows a warning instead of an error.
- **It stops in the middle of a long read:** it no longer does this silently. If audio is lost, the read ends with a warning naming the sentence, and a stalled connection is retried once per fallback (Microsoft → edge-tts → offline) before giving up.
- **First read is slow:** fresh cloud speech depends on Microsoft's service and your connection. A fully cached read makes no synthesis request, but hardware and buffering still affect audible start. See the website's versioned benchmark for measured callback timings, not a guaranteed latency.
- **Offline voice timing:** measured first sound on this machine (ASUS FX516PE laptop, CPU only) is about **1.0 s** for a short passage, and the offline path is used automatically whenever Microsoft's voices can't answer.
- **How much memory:** 20 minutes of continuous online reading measured flat at **59–60 MB** working set (peak 61.6 MB across 38 reads). The packaged Windows app sits at **70–83 MB** on the online path, and about **660 MB** once the offline voice has synthesised something — the 177 MB model costs roughly that much again in onnxruntime's working buffers, and it stays loaded for the rest of the session. On a small laptop, that is the difference between the two paths.
- **Logs:** Windows `%APPDATA%\Read Aloud\read-aloud.log` · Mac `~/Library/Application Support/Read Aloud/read-aloud.log`

## How it works

A read is split into **sentence atoms** that exactly cover your text (a hard cap of 320 characters, first atom ≤100 so the first sound comes sooner). Each atom is streamed and decoded **as its packets arrive**, so sound starts before the sentence is finished downloading, and the ledger records the outcome of every atom: `SPOKEN`, `PARTIAL` (with the exact offset), `SKIPPED` or `CANCELLED`.

If an atom fails, the app walks a fixed ladder — warm Microsoft connection → edge-tts → offline Kokoro — one retry per rung, with a 60-second circuit breaker after repeated failures so a bad path isn't retried for every sentence. One websocket pool (max 4 sockets, hedged after 0.6 s) keeps the first sound fast; playback uses one live audio device at a time, with a watchdog that rebuilds a dead device and resumes 50 ms before the last frame it heard.

`readaloud/atoms.py` splits, `readaloud/session.py` runs one read and owns the ledger, `readaloud/player.py` is the streaming PCM device, `readaloud/engine.py` holds the connections and the ladder, and the button itself is native on each system (PyObjC on macOS, tkinter + Win32 on Windows). GitHub Actions builds both apps, runs the test suite, launches each one to test real speech, and publishes them to Releases.

This project isn't affiliated with Microsoft. It uses the same free online voices as Microsoft Edge.
