"""Portable evidence attachments from explicitly declared, local trial artifacts."""
from __future__ import annotations

import hashlib


def attachments(store, trials):
    result = []
    for trial in trials:
        for attempt in trial.get("attempts", [trial]):
            root = store.attempt_root(attempt).resolve()
            paths = {root / name for name in ["request.json", "result.json", "events.jsonl"]}
            declarations = (attempt.get("result") or {}).get("artifacts", {})
            for relative in declarations.values() if isinstance(declarations, dict) else []:
                if not isinstance(relative, str):
                    continue
                path = (root / relative).resolve()
                if not path.is_relative_to(root):
                    continue
                if path.is_dir():
                    # Product trace contract: only visible business evidence and input resources.
                    paths.update(path / name for name in ["request.json", "prompt.txt", "boundary.txt", "schema.json", "run.json", "result.json", "model-result.json", "events.jsonl"])
                elif path.suffix in {".json", ".jsonl", ".txt"}:
                    paths.add(path)
            for path in sorted(paths):
                if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root):
                    continue
                if path.stat().st_size > 8_000_000:
                    result.append({"trial_id": trial["id"], "attempt": attempt["attempt"], "path": str(path.relative_to(root)), "omitted": "超过 8 MB 附件上限；本机文件仍保留"})
                    continue
                raw = path.read_bytes()
                result.append({"trial_id": trial["id"], "attempt": attempt["attempt"],
                               "path": f"evidence/{trial['id']}/attempt-{attempt['attempt']}/{path.relative_to(root).as_posix()}",
                               "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "text": raw.decode("utf-8")})
    return result
