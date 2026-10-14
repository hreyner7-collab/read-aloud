"""Check a built .app runs on older Macs: right chip, and nothing needs a newer macOS.

usage: python scripts/check_macos_compat.py "dist/Read Aloud.app" x86_64 11.0
Fails (exit 1) if any executable/library lacks the architecture or requires a
macOS version newer than the given minimum.
"""
import os
import re
import subprocess
import sys


def macho_files(root):
    for d, _, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            if os.path.islink(p):
                continue
            with open(p, "rb") as fh:
                magic = fh.read(4)
            if magic in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xce\xfa\xed\xfe"):
                yield p


def min_os(path, arch):
    out = subprocess.run(["otool", "-arch", arch, "-l", path],
                         capture_output=True, text=True).stdout
    m = re.search(r"cmd LC_BUILD_VERSION.*?minos (\S+)", out, re.S)
    if m:
        return m.group(1)
    m = re.search(r"cmd LC_VERSION_MIN_MACOSX.*?version (\S+)", out, re.S)
    return m.group(1) if m else None


def ver(v):
    return tuple(int(x) for x in v.split("."))


def main(app, arch, limit):
    bad, worst, count = [], "0", 0
    for p in macho_files(app):
        count += 1
        archs = subprocess.run(["lipo", "-archs", p], capture_output=True,
                               text=True).stdout.split()
        rel = os.path.relpath(p, app)
        if arch not in archs:
            bad.append(f"{rel}: missing {arch} (has {' '.join(archs) or '?'})")
            continue
        mo = min_os(p, arch)
        if mo and ver(mo) > ver(worst):
            worst = mo
        if mo and ver(mo) > ver(limit):
            bad.append(f"{rel}: needs macOS {mo}")
    print(f"Checked {count} binaries for {arch}: newest macOS required = {worst} "
          f"(limit {limit})")
    for b in bad:
        print("  PROBLEM:", b)
    if bad:
        print("::error::" + f"{len(bad)} binaries too new for macOS {limit} / {arch}: "
              + " | ".join(bad[:12]))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main(*sys.argv[1:4])
