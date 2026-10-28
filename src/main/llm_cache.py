import fcntl
import hashlib
import json
import os
import threading
from pathlib import Path

PATH = Path(os.getenv("LLM_CACHE_PATH", Path(__file__).resolve().parents[2] / "llm_cache.jsonl"))
ENABLED = os.getenv("LLM_CACHE", "on") != "off"

_lock = threading.Lock()
_entries: dict | None = None


def key(req: dict) -> str:
    return hashlib.sha256(json.dumps(req, sort_keys=True, default=str).encode()).hexdigest()


def _load() -> dict:
    global _entries
    if _entries is None:
        _entries = {}
        if PATH.exists():
            for line in PATH.read_text().splitlines():
                if line.strip():
                    e = json.loads(line)
                    _entries[e["key"]] = e["response"]
    return _entries


def get(k: str):
    with _lock:
        return _load().get(k)


def put(k: str, req: dict, response: dict):
    line = json.dumps(dict(key=k, request=req, response=response), sort_keys=True, default=str)
    with _lock:
        _load()[k] = response
        # xdist runs many worker processes against the same file
        with open(PATH, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(line + "\n")
            fcntl.flock(f, fcntl.LOCK_UN)
