"""Read the app's self-test result; on failure, report why as a GitHub annotation."""
import json
import sys

try:
    r = json.load(open(sys.argv[1]))
except Exception as e:
    print(f"::error::self-test produced no result ({e!r})")
    sys.exit(1)
log = r.pop("log_tail", "")
print(json.dumps(r, indent=2))
print(log)

issues = []
if not r.get("ok"):
    issues.append("ok=false")
for key, label in (("read", "read"), ("restart", "restart"), ("fallback", "offline fallback")):
    led = r.get(key) or {}
    if led.get("end") != "idle":
        issues.append(f"{label} ended {led.get('end')!r}")
    if not led.get("atoms") or led.get("spoken") != led.get("atoms"):
        issues.append(f"{label} spoke {led.get('spoken')}/{led.get('atoms')} atoms")
    if led.get("partial") or led.get("skipped"):
        issues.append(f"{label} lost {led.get('partial')} partial + {led.get('skipped')} skipped")
    if led.get("warning"):
        issues.append(f"{label} warning: {led.get('warning')}")
if not r.get("atoms_covered"):
    issues.append("ledger did not cover every character")
if not r.get("online_ok"):
    issues.append("online voice failed")
if not r.get("offline_ok"):
    issues.append("offline voice failed")
if not r.get("stop_ok") or float(r.get("stop_seconds", 99)) >= 1.0:
    issues.append(f"stop took {r.get('stop_seconds')} s")
if int(r.get("fallback_network_calls") or 0) <= 0:
    issues.append("offline fallback made no network attempts (test did not exercise it)")
if r.get("error"):
    issues.append("exception")
if issues:
    summary = {k: v for k, v in r.items() if k not in ("error", "read_states",
                                                       "fallback_states", "log_tail")}
    err = (r.get("error") or "").strip().splitlines()[-6:]
    tail = [line for line in log.splitlines()
            if "error" in line.lower() or "fail" in line.lower()][-4:]
    msg = (f"SELFTEST FAILED {json.dumps(summary)} | issues: {'; '.join(issues)}"
           f" | error: {' / '.join(err)} | log: {' / '.join(tail)}")
    print("::error::" + msg.replace("\n", " ")[:3500])
    sys.exit(1)
print("self-test OK: every atom spoken, stop fast, offline fallback exercised")
