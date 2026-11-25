"""Owned local episode files, read-back snapshots and objective artifact checks.

Descriptor-relative operations reject symlinks. This is not a process sandbox.
"""
from __future__ import annotations

import hashlib
import errno
import json
import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath


class UnsafePath(ValueError):
    pass


def _parts(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise UnsafePath("use a nonempty relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise UnsafePath("absolute paths, traversal and empty components are forbidden")
    return path.parts


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode()


def _hash(value):
    return hashlib.sha256(value).hexdigest()


class EpisodeWorkspace:
    MARKER = ".myeval-episode-workspace.json"

    def __init__(self, root, *, max_file_bytes=8 * 1024 * 1024, max_total_bytes=64 * 1024 * 1024, max_files=1000):
        if any(type(v) is not int or v < 1 for v in (max_file_bytes, max_total_bytes, max_files)):
            raise ValueError("workspace limits must be positive integers")
        self.root = Path(root).absolute()
        if ".." in self.root.parts:
            raise UnsafePath("workspace root cannot contain traversal")
        self.max_file_bytes, self.max_total_bytes, self.max_files = max_file_bytes, max_total_bytes, max_files
        # Walk every ancestor by descriptor: O_NOFOLLOW on only the final path
        # would still allow an ancestor symlink to escape the requested root.
        self._fd = os.open(self.root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in self.root.parts[1:]:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=self._fd)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self._fd)
                os.close(self._fd)
                self._fd = child
        except OSError as error:
            self.close()
            if error.errno in (errno.ENOTDIR, errno.ELOOP):
                raise UnsafePath("workspace ancestors must be directories, not symbolic links") from None
            raise
        try:
            existing = os.listdir(self._fd)
            if self.MARKER not in existing:
                if existing:
                    raise UnsafePath("refusing to adopt a nonempty unowned directory")
                self._write(self.MARKER, _json({"schema": "myeval.episode-workspace.v1", "id": str(uuid.uuid4())}))
            marker = json.loads(self._read(self.MARKER))
            if marker.get("schema") != "myeval.episode-workspace.v1" or not isinstance(marker.get("id"), str):
                raise UnsafePath("invalid workspace ownership marker")
            self.identifier = marker["id"]
            for name in ("live", "snapshots", "reset-history"):
                with self._directory((name,), create=True):
                    pass
            self.recovery = self._recover_reset()
        except Exception:
            self.close()
            raise

    def close(self):
        if getattr(self, "_fd", None) is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def _directory(self, parts, create=False):
        if self._fd is None:
            raise ValueError("workspace is closed")
        fd = os.dup(self._fd)
        try:
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except OSError as error:
                    if error.errno in (errno.ENOTDIR, errno.ELOOP):
                        raise UnsafePath("non-directory or symbolic link in path") from None
                    raise
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)

    def _read(self, name):
        parts = _parts(name)
        with self._directory(parts[:-1]) as parent:
            try:
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            except OSError as error:
                if error.errno == errno.ELOOP:
                    raise UnsafePath("symbolic links are forbidden") from None
                raise
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise UnsafePath("only regular files are supported")
                if info.st_size > self.max_file_bytes:
                    raise ValueError("file exceeds workspace byte limit")
                data = handle.read(self.max_file_bytes + 1)
                if len(data) > self.max_file_bytes:
                    raise ValueError("file exceeds workspace byte limit")
                return data

    def _write(self, name, data):
        if not isinstance(data, bytes) or len(data) > self.max_file_bytes:
            raise ValueError("write requires bytes within the file byte limit")
        parts = _parts(name)
        with self._directory(parts[:-1], create=True) as parent:
            try:
                existing = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(existing.st_mode):
                    raise UnsafePath("cannot replace a symlink or non-regular file")
            except FileNotFoundError:
                pass
            temp = ".write-" + uuid.uuid4().hex
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                try:
                    os.unlink(temp, dir_fd=parent)
                except FileNotFoundError:
                    pass

    def write(self, relative_path, data):
        _parts(relative_path)
        data = data.encode() if isinstance(data, str) else data
        self._write("live/" + relative_path, data)
        observed = self.read(relative_path)
        return {"path": relative_path, "sha256": _hash(observed), "bytes": len(observed)}

    def read(self, relative_path):
        _parts(relative_path)
        return self._read("live/" + relative_path)

    def _files(self, prefix):
        result, total = {}, 0

        def walk(directory, components):
            nonlocal total
            for name in sorted(os.listdir(directory)):
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                parts = components + (name,)
                if stat.S_ISDIR(info.st_mode):
                    with self._directory(_parts(prefix) + parts) as child:
                        walk(child, parts)
                elif stat.S_ISREG(info.st_mode):
                    data = self._read(prefix + "/" + "/".join(parts))
                    total += len(data)
                    if len(result) >= self.max_files or total > self.max_total_bytes:
                        raise ValueError("workspace snapshot limit exceeded")
                    result["/".join(parts)] = data
                else:
                    raise UnsafePath("workspace contains a symbolic link or non-regular file")

        with self._directory(_parts(prefix)) as directory:
            walk(directory, ())
        return result

    def snapshot(self, label):
        if not isinstance(label, str) or not label.strip() or len(label) > 200:
            raise ValueError("snapshot label must contain 1–200 characters")
        files = self._files("live")
        identifier = uuid.uuid4().hex
        manifest = {"schema": "myeval.episode-snapshot.v1", "workspace_id": self.identifier,
                    "id": identifier, "label": label,
                    "files": [{"path": path, "sha256": _hash(data), "bytes": len(data)} for path, data in files.items()]}
        for path, data in files.items():
            self._write(f"snapshots/{identifier}/files/{path}", data)
        manifest["manifest_sha256"] = _hash(_json(manifest))
        self._write(f"snapshots/{identifier}/manifest.json", _json(manifest))
        return manifest

    def _recover_reset(self):
        try:
            pending = json.loads(self._read(".reset-pending.json"))
        except FileNotFoundError:
            return None
        reset_id = pending.get("id")
        if not isinstance(reset_id, str) or len(reset_id) != 32 or any(c not in "0123456789abcdef" for c in reset_id):
            raise UnsafePath("invalid reset recovery journal")
        staging, previous = ".reset-" + reset_id, "reset-history/" + reset_id
        names = os.listdir(self._fd)
        # __init__ creates live if it was missing. A completed first rename is
        # identified by its preserved previous state, not that directory alone.
        with self._directory(("reset-history",)) as history:
            old_saved = reset_id in os.listdir(history)
        if old_saved and staging in names:
            if self._files("live"):
                raise UnsafePath("interrupted reset has conflicting live files")
            os.rmdir("live", dir_fd=self._fd)
            os.rename(staging, "live", src_dir_fd=self._fd, dst_dir_fd=self._fd)
            action = "completed_interrupted_reset"
        elif old_saved:
            action = "reset_was_already_completed"
        else:
            action = "kept_previous_live_state"
        os.unlink(".reset-pending.json", dir_fd=self._fd)
        return {"id": reset_id, "action": action, "previous_state": previous if old_saved else None}

    def reset(self, snapshot_id):
        if not isinstance(snapshot_id, str) or len(snapshot_id) != 32 or any(c not in "0123456789abcdef" for c in snapshot_id):
            raise ValueError("invalid snapshot ID")
        manifest = json.loads(self._read(f"snapshots/{snapshot_id}/manifest.json"))
        expected = manifest.pop("manifest_sha256", None)
        if expected != _hash(_json(manifest)) or manifest.get("workspace_id") != self.identifier:
            raise ValueError("snapshot manifest identity or hash mismatch")
        if manifest.get("id") != snapshot_id or not isinstance(manifest.get("files"), list):
            raise ValueError("invalid snapshot manifest")
        files = {}
        for entry in manifest["files"]:
            path = entry["path"]
            _parts(path)
            if path in files:
                raise ValueError("duplicate snapshot path")
            data = self._read(f"snapshots/{snapshot_id}/files/{path}")
            if _hash(data) != entry["sha256"] or len(data) != entry["bytes"]:
                raise ValueError("snapshot content hash mismatch")
            files[path] = data
        if len(files) > self.max_files or sum(map(len, files.values())) > self.max_total_bytes:
            raise ValueError("snapshot limit exceeded")
        # Refuse suspect current content before preserving it. No user directory is deleted.
        self._files("live")
        reset_id = uuid.uuid4().hex
        staging = ".reset-" + reset_id
        with self._directory((staging,), create=True):
            pass
        for path, data in files.items():
            self._write(staging + "/" + path, data)
        previous = "reset-history/" + reset_id
        self._write(".reset-pending.json", _json({"id": reset_id, "snapshot_id": snapshot_id}))
        with self._directory(("reset-history",)) as history:
            os.rename("live", reset_id, src_dir_fd=self._fd, dst_dir_fd=history)
            try:
                os.rename(staging, "live", src_dir_fd=self._fd, dst_dir_fd=self._fd)
            except Exception:
                os.rename(reset_id, "live", src_dir_fd=history, dst_dir_fd=self._fd)
                raise
        os.unlink(".reset-pending.json", dir_fd=self._fd)
        observed = self._files("live")
        return {"snapshot_id": snapshot_id, "manifest_sha256": expected, "previous_state": previous,
                "files": [{"path": path, "bytes": len(data), "sha256": _hash(data)} for path, data in observed.items()],
                "readback_matches": observed == files}

    def verify(self, expectations):
        if not isinstance(expectations, list):
            raise ValueError("artifact expectations must be a list")
        results = []
        for spec in expectations:
            if not isinstance(spec, dict) or set(spec) - {"path", "kind", "expected", "pointer"}:
                raise ValueError("invalid artifact expectation")
            path, kind = spec.get("path"), spec.get("kind", "exists")
            _parts(path)
            if kind in ("sha256", "text_exact", "json_pointer") and "expected" not in spec:
                raise ValueError("this artifact check requires an expected value")
            result = {"path": path, "kind": kind, "status": "fail", "sha256": None, "bytes": None}
            try:
                data = self.read(path)
                result.update(sha256=_hash(data), bytes=len(data))
                if kind == "exists":
                    passed = True
                elif kind == "sha256":
                    passed = _hash(data) == spec.get("expected")
                elif kind == "text_exact":
                    passed = data.decode("utf-8") == spec.get("expected")
                elif kind == "json_pointer":
                    value, pointer = json.loads(data), spec.get("pointer", "")
                    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
                        raise ValueError("JSON Pointer must be empty or begin with /")
                    if pointer:
                        for key in pointer[1:].split("/"):
                            key = key.replace("~1", "/").replace("~0", "~")
                            if isinstance(value, list):
                                if not key.isascii() or not key.isdigit() or (len(key) > 1 and key.startswith("0")):
                                    raise ValueError("invalid JSON array index")
                                value = value[int(key)]
                            else:
                                value = value[key]
                    passed = _json(value) == _json(spec["expected"])
                else:
                    result.update(status="unsupported", reason="unknown_artifact_check")
                    results.append(result)
                    continue
                result.update(status="pass" if passed else "fail", reason="matched" if passed else "value_mismatch")
            except FileNotFoundError:
                result["reason"] = "file_missing"
            except UnsafePath:
                result["reason"] = "unsafe_path"
            except (UnicodeError, json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
                result["reason"] = "invalid_artifact_or_pointer"
            results.append(result)
        return {"schema": "myeval.artifact-verification.v1", "workspace_id": self.identifier,
                "checks": results, "passed": sum(r["status"] == "pass" for r in results),
                "failed": sum(r["status"] == "fail" for r in results),
                "unsupported": sum(r["status"] == "unsupported" for r in results),
                "scope": "local_file_facts_only"}
