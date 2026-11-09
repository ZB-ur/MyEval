from __future__ import annotations

import threading
from pathlib import Path

from .util import dumps, now, uid


class CancelledError(Exception):
    pass


class RunContext:
    def __init__(self, root, trial_id, experiment_id, timeout_seconds):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.trial_id = trial_id
        self.experiment_id = experiment_id
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()

    @property
    def cancel_requested(self):
        return (self.root / "cancel.requested").exists()

    def check_cancelled(self):
        if self.cancel_requested:
            raise CancelledError("The local execution was asked to stop; upstream completion is unknown.")

    def emit(self, kind, data, **links):
        event = {"id": uid(), "timestamp": now(), "kind": kind, "data": data,
                 **{k: v for k, v in links.items() if k in {"episode", "turn", "stage", "attempt"}}}
        with self._lock, (self.root / "events.jsonl").open("a") as handle:
            handle.write(dumps(event) + "\n")
            handle.flush()
        return event
