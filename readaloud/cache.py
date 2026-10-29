"""Spoken-audio cache: RAM always, disk only when the user opts in.

Keys are sha256(voice|rate|text) and files are named after the hash, so no
spoken text ever appears in a file name, a header or the log.  The disk header
holds the voice id, rate, payload size and format only.
"""
import collections
import hashlib
import json
import os
import threading
import time

RAM_BYTES = 64 * 1024 * 1024
DISK_BYTES = 64 * 1024 * 1024
MAGIC = b"RAAC1\n"
KEEP = 40  # seconds per atom at the very most


def cache_key(voice, rate, text):
    return hashlib.sha256(f"{voice}|{rate}|{text}".encode("utf-8")).hexdigest()[:32]


class AudioCache:
    def __init__(self, max_ram=RAM_BYTES, disk_dir=None, disk_enabled=False,
                 max_disk=DISK_BYTES):
        self.max_ram = max_ram
        self.max_disk = max_disk
        self.disk_dir = disk_dir
        self.disk_enabled = bool(disk_enabled and disk_dir)
        self._lock = threading.Lock()
        self._ram = collections.OrderedDict()   # key -> (fmt, payload)
        self._ram_bytes = 0
        self.hits = 0
        self.misses = 0
        if self.disk_enabled:
            try:
                os.makedirs(self.disk_dir, exist_ok=True)
            except OSError:
                self.disk_enabled = False

    # ---------------------------------------------------------------- RAM
    def get_ram(self, key):
        with self._lock:
            item = self._ram.get(key)
            if item is None:
                self.misses += 1
                return None
            self._ram.move_to_end(key)
            self.hits += 1
            return item

    def put_ram(self, key, fmt, payload):
        size = _size(payload)
        if size > self.max_ram:
            return
        with self._lock:
            old = self._ram.pop(key, None)
            if old is not None:
                self._ram_bytes -= _size(old[1])
            self._ram[key] = (fmt, payload)
            self._ram_bytes += size
            while self._ram_bytes > self.max_ram and self._ram:
                _k, (_f, old_payload) = self._ram.popitem(last=False)
                self._ram_bytes -= _size(old_payload)

    def clear_ram(self):
        with self._lock:
            self._ram.clear()
            self._ram_bytes = 0

    @property
    def ram_bytes(self):
        with self._lock:
            return self._ram_bytes

    # ---------------------------------------------------------------- disk
    def set_disk(self, enabled):
        self.disk_enabled = bool(enabled and self.disk_dir)
        if self.disk_enabled:
            try:
                os.makedirs(self.disk_dir, exist_ok=True)
            except OSError:
                self.disk_enabled = False

    def _path(self, key):
        return os.path.join(self.disk_dir, f"a1-{key}.bin")

    def get(self, key):
        """Returns (format, payload) or None.  Deletes corrupt files."""
        hit = self.get_ram(key)
        if hit is not None:
            return hit
        if not self.disk_enabled:
            return None
        path = self._path(key)
        try:
            with open(path, "rb") as f:
                if f.read(len(MAGIC)) != MAGIC:
                    raise ValueError("bad magic")
                head = json.loads(f.readline().decode("utf-8"))
                data = f.read()
        except FileNotFoundError:
            return None
        except (OSError, ValueError, UnicodeDecodeError):
            self._remove(path)
            return None
        try:
            if not isinstance(head, dict):
                raise ValueError("invalid header")
            if head.get("b") != len(data) or head.get("k") != key:
                raise ValueError("size/key mismatch")
            fmt = head.get("f")
            payload = _decode(fmt, data)
        except (ValueError, TypeError):
            self._remove(path)
            return None
        try:
            os.utime(path)                      # LRU by access time
        except OSError:
            pass
        self.put_ram(key, fmt, payload)
        with self._lock:
            self.hits += 1
        return fmt, payload

    def has(self, key):
        """Cheap presence check, used to pick the fast-start audio buffer."""
        with self._lock:
            if key in self._ram:
                return True
        if not self.disk_enabled:
            return False
        return os.path.isfile(self._path(key))

    def pcm_cached(self, key):
        with self._lock:
            item = self._ram.get(key)
            return item is not None and item[0] == "pcm"

    def put(self, key, fmt, payload, pcm=None):
        # RAM keeps decoded frames when available (repeats then skip MP3
        # decoding); disk keeps the compact format.
        if pcm:
            self.put_ram(key, "pcm", pcm)
        else:
            self.put_ram(key, fmt, payload)
        if not self.disk_enabled:
            return
        try:
            self._write(key, fmt, payload)
            self._trim()
        except OSError:
            pass                                 # caching never fails a read

    def _write(self, key, fmt, payload):
        head = json.dumps({"k": key, "f": fmt, "b": _size(payload)}).encode() + b"\n"
        path = self._path(key)
        tmp = f"{path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(MAGIC)
                f.write(head)
                f.write(_encode(fmt, payload))
            os.replace(tmp, path)
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass

    def _trim(self):
        try:
            files = [(os.path.getmtime(p), os.path.getsize(p), p)
                     for p in (os.path.join(self.disk_dir, f)
                               for f in os.listdir(self.disk_dir))
                     if os.path.isfile(p)]
        except OSError:
            return
        total = sum(size for _, size, _ in files)
        for _mtime, size, path in sorted(files):
            if total <= self.max_disk:
                break
            self._remove(path)
            total -= size

    def _remove(self, path):
        try:
            os.remove(path)
        except OSError:
            pass

    def clear_disk(self):
        if not self.disk_dir:
            return 0
        removed = 0
        try:
            names = os.listdir(self.disk_dir)
        except OSError:
            return 0
        for name in names:
            if name.startswith("a1-"):
                self._remove(os.path.join(self.disk_dir, name))
                removed += 1
        return removed

    def disk_bytes(self):
        if not self.disk_dir:
            return 0
        total = 0
        try:
            for name in os.listdir(self.disk_dir):
                p = os.path.join(self.disk_dir, name)
                if name.startswith("a1-") and os.path.isfile(p):
                    total += os.path.getsize(p)
        except OSError:
            pass
        return total


def _size(payload):
    if isinstance(payload, (bytes, bytearray)):
        return len(payload)
    return sum(len(a) for a in payload) * 2


def _encode(fmt, payload):
    if fmt == "mp3":
        return bytes(payload)
    if fmt == "pcm":
        import array
        out = array.array("h")
        for a in payload:
            out.extend(a)
        return out.tobytes()
    raise ValueError(fmt)


def _decode(fmt, data):
    if fmt == "mp3":
        return data
    if fmt == "pcm":
        import array
        a = array.array("h")
        a.frombytes(data[:len(data) // 2 * 2])
        return [a]
    raise ValueError(fmt)


def payload_seconds(payload):
    if isinstance(payload, (bytes, bytearray)):
        return 0
    return sum(len(a) for a in payload) / 24000


def now():
    return time.time()
