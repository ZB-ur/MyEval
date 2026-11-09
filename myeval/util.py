from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"succeeded", "failed", "timed_out", "cancelled", "interrupted", "refused", "unsupported"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def uid():
    return str(uuid.uuid4())


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def safe_error(error):
    text = str(error)[:1500]
    text = re.sub(r"(?i)(bearer\s+|api[_-]?key[=: ]+|sk-)[\w.\-]+", r"\1[redacted]", text)
    return text


def read_events(path):
    events = []
    if path.exists():
        for line in path.read_text(errors="replace").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                # A worker may be interrupted between writes. Preserve valid earlier events.
                pass
    return events
