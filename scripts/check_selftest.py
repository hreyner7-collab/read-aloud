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
if r.get("ok"):
    sys.exit(0)
summary = {k: v for k, v in r.items() if k != "error"}
err = (r.get("error") or "").strip().splitlines()[-6:]
tail = [l for l in log.splitlines() if "error" in l.lower() or "fail" in l.lower()][-4:]
msg = f"SELFTEST FAILED {json.dumps(summary)} | error: {' / '.join(err)} | log: {' / '.join(tail)}"
print("::error::" + msg.replace("\n", " ")[:3500])
sys.exit(1)
