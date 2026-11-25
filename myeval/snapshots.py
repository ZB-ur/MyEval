"""Local, integrity-checked evidence backups and declared source snapshots.

Backups deliberately exclude process state, administrator authentication and product
runtime databases. Restoring never starts a runner or replaces an existing data set.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
from contextlib import ExitStack
from pathlib import Path, PurePosixPath

from .util import ROOT, digest, now, uid, write_json

EVIDENCE_DIRECTORIES = frozenset({
    "experiments", "datasets", "imports", "source-snapshots", "evaluation",
    "evaluations", "evaluation-jobs", "decisions", "campaigns", "media",
})
DATABASE_NAMES = frozenset({"myeval.sqlite3", "evaluation.sqlite3", "evaluations.sqlite3", "evaluation-jobs.sqlite3"})
EVIDENCE_EXTENSIONS = frozenset({
    ".json", ".jsonl", ".txt", ".md", ".csv", ".tsv", ".html", ".yaml", ".yml",
    ".py", ".swift", ".h", ".hpp", ".c", ".cpp", ".m", ".mm", ".toml",
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic", ".wav", ".mp3", ".m4a", ".mp4", ".webm",
})
SOURCE_EXTENSIONS = frozenset({".py", ".swift", ".h", ".hpp", ".c", ".cpp", ".m", ".mm", ".txt", ".md", ".json", ".yaml", ".yml", ".toml"})
EXCLUDED_NAMES = frozenset({"admin.token", "open-admin.html", "server.json", "server.lock", "auth.json", "config.toml", "cancel.requested"})
EXCLUDED_PARTS = frozenset({"private", "credentials", "secrets", "runtime", "node_modules", "__pycache__", ".git", ".venv"})
ACTIVE_STATES = frozenset({"pending", "queued", "running", "cancelling"})


def _sha_file(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _safe_relative(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("文件路径必须为安全的相对路径")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("文件路径不得逃逸目录")
    return Path(*path.parts)


def _check_path(path, root, *, allow_missing=False):
    """Reject every symlink component, including an apparently safe final target."""
    path, root = Path(path).absolute(), Path(root).absolute()
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ValueError("文件路径超出允许目录") from None
    current = root
    if current.is_symlink():
        raise ValueError("不接受符号链接目录")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("不接受符号链接文件或目录")
        if not allow_missing and not current.exists():
            raise ValueError("声明的文件不存在")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("文件路径超出允许目录")
    return path


def _private_directory(path):
    Path(path).mkdir(parents=True, exist_ok=True, mode=0o700)
    Path(path).chmod(0o700)


def _excluded(relative):
    parts = [part.lower() for part in relative.parts]
    if any(part in EXCLUDED_PARTS or part.startswith(".") for part in parts):
        return True
    name = parts[-1] if parts else ""
    return (name in EXCLUDED_NAMES or bool(re.search(r"(^|[._-])(credentials?|secrets?|api[_-]?keys?|access[_-]?tokens?)([._-]|$)", name))
            or name.endswith(("-wal", "-shm", ".lock", ".token", ".pem", ".key", ".p12")))


def _copy_stable(source, destination):
    """A changing trace is not a successful backup; report it instead of truncating."""
    before = source.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("只支持普通文件")
    _private_directory(destination.parent)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    value, size = hashlib.sha256(), 0
    try:
        with os.fdopen(descriptor, "rb") as src, destination.open("xb") as dst:
            start = os.fstat(src.fileno())
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                dst.write(chunk)
                value.update(chunk)
                size += len(chunk)
            dst.flush()
            os.fsync(dst.fileno())
            end = os.fstat(src.fileno())
        after = source.stat()
        identities = {(s.st_ino, s.st_size, s.st_mtime_ns) for s in (before, start, end, after)}
        if len(identities) != 1 or size != before.st_size:
            raise ValueError("备份期间文件发生变化；请等待作业停止后重试")
        destination.chmod(0o600)
        return {"sha256": value.hexdigest(), "bytes": size}
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


class BackupManager:
    def __init__(self, store):
        self.store = store
        self.root = Path(store.directory).resolve()
        self.directory = self.root / "backups"
        self.lock = threading.RLock()

    def _location(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identifier):
            raise ValueError("备份 ID 格式不正确")
        return _check_path(self.directory / identifier, self.root)

    def _summary(self, manifest, location, files_verified=False):
        return {key: manifest[key] for key in ("id", "schema_version", "created_at", "label", "file_count", "total_bytes", "manifest_hash", "consistency", "exclusions") if key in manifest} | {"location": str(location), "verification": {"manifest": "verified", "files": "verified" if files_verified else "not_checked"}}

    def list_backups(self):
        if not self.directory.exists():
            return []
        _check_path(self.directory, self.root)
        records = []
        for location in sorted(self.directory.iterdir(), reverse=True):
            if location.name.startswith(".tmp-") or location.is_symlink() or not location.is_dir():
                continue
            try:
                manifest = self._read_manifest(location)
                records.append(self._summary(manifest, location))
            except (OSError, ValueError, KeyError):
                records.append({"id": location.name, "status": "invalid", "error": "备份清单缺失或校验失败"})
        return records

    def _check_idle(self):
        active = [t for t in self.store.trials() if t.get("status") in {"running", "cancelling"}]
        if active:
            raise ValueError("有正在执行或取消中的 trial；请暂停调度并等待在途任务结束后备份")
        evaluation = getattr(self.store, "evaluation_service", None)
        if evaluation is not None:
            jobs = evaluation.list_jobs()
            if isinstance(jobs, dict):
                jobs = jobs.get("jobs", [])
            if any(job.get("status") in ACTIVE_STATES for job in jobs):
                raise ValueError("有等待或执行中的评分作业；请结束或取消评分作业后备份")
        return evaluation is not None

    def create_backup(self, body=None):
        body = body or {}
        if not isinstance(body, dict):
            raise ValueError("备份参数必须为对象")
        label = str(body.get("label") or "本机证据备份")[:160]
        with self.lock, ExitStack() as stack:
            # EvaluationService may expose backup_lock to serialize job submission
            # with this operation. Without it the manifest never claims a cross-db transaction.
            evaluation = getattr(self.store, "evaluation_service", None)
            # EvaluationService commits under its RLock then takes Store.lock.
            # Use the same order even for standalone service consumers that did
            # not go through server.main's explicit backup_lock assignment.
            backup_lock = getattr(evaluation, "backup_lock", None) or getattr(evaluation, "lock", None)
            if backup_lock is not None:
                stack.enter_context(backup_lock)
            stack.enter_context(self.store.lock)
            evaluation_checked = self._check_idle()
            _check_path(self.directory, self.root, allow_missing=True)
            _private_directory(self.directory)
            identifier = now().replace(":", "").replace("+", "_").replace(".", "_") + "-" + uid()[:8]
            staging = Path(tempfile.mkdtemp(prefix=".tmp-", dir=self.directory))
            staging.chmod(0o700)
            location = self.directory / identifier
            records, omitted = [], []
            try:
                data = staging / "data"
                _private_directory(data)
                db_target = data / "myeval.sqlite3"
                with sqlite3.connect(db_target) as target:
                    self.store.db.backup(target)
                    if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("SQLite 备份完整性校验失败")
                db_target.chmod(0o600)
                records.append({"path": "myeval.sqlite3", "sha256": _sha_file(db_target), "bytes": db_target.stat().st_size, "kind": "sqlite_snapshot"})
                for source in self._evidence_files(omitted):
                    relative = source.relative_to(self.root)
                    destination = data / relative
                    if source.suffix in {".sqlite3", ".sqlite", ".db"}:
                        _private_directory(destination.parent)
                        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src, sqlite3.connect(destination) as dst:
                            src.backup(dst)
                            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                                raise ValueError("评分数据库完整性校验失败")
                        destination.chmod(0o600)
                        identity = {"sha256": _sha_file(destination), "bytes": destination.stat().st_size}
                        kind = "sqlite_snapshot"
                    else:
                        identity = _copy_stable(source, destination)
                        kind = "evidence"
                    records.append({"path": relative.as_posix(), **identity, "kind": kind})
                self._check_idle()
                manifest = {
                    "schema_version": "myeval.backup.v1", "id": identifier, "created_at": now(), "label": label,
                    "file_count": len(records), "total_bytes": sum(r["bytes"] for r in records), "files": records,
                    "consistency": {"core_database": "sqlite_online_backup", "product_trials_active": False,
                                    "evaluation_jobs_checked": evaluation_checked,
                                    "evaluation_submission_locked": backup_lock is not None,
                                    "filesystem": "stable_read_per_file", "cross_database_transaction": False},
                    "exclusions": ["管理员访问码与入口", "服务锁和进程状态", "凭据命名文件与私有目录", "产品运行 SQLite/缓存/可执行文件", "备份目录本身"],
                    "omitted": omitted,
                    "portability": {"scope": "owned_evidence_and_registered_source_snapshots", "external_media_paths": "not_rewritten_or_copied", "product_dependencies_and_model_weights": "not_included"},
                    "restore": {"replay": False, "requires_empty_destination": True, "administrator_token": "generated_on_first_server_start"},
                }
                manifest["manifest_hash"] = digest(manifest)
                write_json(staging / "manifest.json", manifest)
                (staging / "manifest.json").chmod(0o600)
                os.replace(staging, location)
                return self._summary(manifest, location, files_verified=True)
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise

    def _evidence_files(self, omitted):
        roots = []
        for child in self.root.iterdir():
            if child.name in DATABASE_NAMES - {"myeval.sqlite3"} or child.name in EVIDENCE_DIRECTORIES:
                roots.append(child)
        for root in sorted(roots):
            _check_path(root, self.root)
            paths = [root] if root.is_file() else sorted(root.rglob("*"))
            for path in paths:
                relative = path.relative_to(self.root)
                if path.is_symlink():
                    raise ValueError("证据目录含符号链接；备份已停止")
                if path.is_dir():
                    continue
                _check_path(path, self.root)
                if _excluded(relative):
                    omitted.append({"path": relative.as_posix(), "reason": "excluded_runtime_or_credentials"})
                    continue
                database = (path.name in DATABASE_NAMES and (len(relative.parts) == 1 or relative.parts[0] in {"evaluation", "evaluations", "evaluation-jobs"}))
                if not database and path.suffix.lower() not in EVIDENCE_EXTENSIONS:
                    omitted.append({"path": relative.as_posix(), "reason": "not_declared_evidence_type"})
                    continue
                if not path.is_file():
                    raise ValueError("证据目录含非普通文件")
                yield path

    def _read_manifest(self, location):
        manifest_path = _check_path(location / "manifest.json", self.root)
        if manifest_path.stat().st_size > 20_000_000:
            raise ValueError("备份清单过大")
        manifest = json.loads(manifest_path.read_text())
        if not isinstance(manifest, dict) or manifest.get("schema_version") != "myeval.backup.v1":
            raise ValueError("备份格式不支持")
        original_hash = manifest.get("manifest_hash")
        if digest({k: v for k, v in manifest.items() if k != "manifest_hash"}) != original_hash:
            raise ValueError("备份清单校验失败")
        return manifest

    def restore_backup(self, identifier, destination):
        if not isinstance(destination, (str, Path)) or not str(destination).strip():
            raise ValueError("请指定新的独立恢复目录")
        raw_destination = Path(destination).expanduser().absolute()
        # Check existing ancestors before resolve() can hide a symlink.
        for parent in [raw_destination, *raw_destination.parents]:
            if parent.is_symlink():
                raise ValueError("恢复目标及父目录不得是符号链接")
        destination = raw_destination.resolve()
        if destination == self.root or destination.is_relative_to(self.root) or self.root.is_relative_to(destination):
            raise ValueError("恢复目标必须位于当前数据目录之外")
        if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
            raise ValueError("恢复目标必须为新目录或空目录；不会覆盖已有数据")
        with self.lock:
            location = self._location(identifier)
            manifest = self._read_manifest(location)
            files = manifest.get("files")
            if not isinstance(files, list) or not files:
                raise ValueError("备份没有文件")
            checked, paths = [], set()
            for record in files:
                if not isinstance(record, dict):
                    raise ValueError("备份文件清单不正确")
                relative = _safe_relative(record.get("path"))
                if relative.as_posix() in paths or _excluded(relative):
                    raise ValueError("备份包含重复或禁止恢复的文件")
                paths.add(relative.as_posix())
                source = _check_path(location / "data" / relative, self.root)
                if not source.is_file() or source.stat().st_size != record.get("bytes") or _sha_file(source) != record.get("sha256"):
                    raise ValueError("备份文件校验失败；恢复目标未改变")
                if record.get("kind") == "sqlite_snapshot":
                    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as db:
                        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise ValueError("备份数据库损坏；恢复目标未改变")
                checked.append((source, relative, record))
            if "myeval.sqlite3" not in paths:
                raise ValueError("备份缺少核心数据库")
            # No destination mutation occurs before all input hashes and SQLite files pass.
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=".myeval-restore-", dir=destination.parent))
            staging.chmod(0o700)
            try:
                for source, relative, record in checked:
                    identity = _copy_stable(source, staging / relative)
                    if identity != {"sha256": record["sha256"], "bytes": record["bytes"]}:
                        raise ValueError("恢复期间备份发生变化")
                receipt = {"schema_version": "myeval.restore.v1", "backup_id": identifier,
                           "backup_manifest_hash": manifest["manifest_hash"], "restored_at": now(),
                           "file_count": len(checked), "destination": str(destination), "replayed": False,
                           "administrator_token": "not_restored", "server_started": False}
                write_json(staging / "restore-receipt.json", receipt)
                (staging / "restore-receipt.json").chmod(0o600)
                # rmdir only succeeds for the still-empty destination; racing writes are preserved.
                if destination.exists():
                    destination.rmdir()
                os.rename(staging, destination)
                return receipt
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise


def capture_sources(store, experiment):
    """Archive only the registered adapter's declared source files, never checkout.

    Capture failure is explicit evidence, not permission to execute altered sources.
    The worker still compares current product provenance with the frozen provenance.
    """
    from .catalog import adapter

    project = experiment.get("project")
    identifier = experiment.get("id")
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identifier):
        raise ValueError("实验 ID 无法用于来源快照")
    snapshot_root = Path(store.directory) / "source-snapshots" / identifier
    _check_path(snapshot_root, Path(store.directory), allow_missing=True)
    _private_directory(snapshot_root)
    result = {"schema_version": "myeval.sources.v1", "captured_at": now(), "files": [], "checkout_performed": False,
              "rebuild_status": "not_attempted", "limitations": ["只保留适配器声明的源码及提示词；不包含完整依赖、模型权重、凭据或可运行环境。"]}
    try:
        module = adapter(project)
        root_function = getattr(module, "_root", None)
        registered_root = Path(root_function() if callable(root_function) else getattr(module, "DEFAULT_ROOT")).resolve()
    except (AttributeError, TypeError, ValueError, OSError, ImportError):
        result.update(status="unavailable", reason="registered_source_root_unavailable")
        return result
    total = 0
    for configuration in experiment.get("configurations", []):
        config_id = configuration["id"]
        config_directory = digest(config_id)[:20]
        provenance = configuration.get("provenance") or {}
        declared_root = provenance.get("source_root") or (provenance.get("source") or {}).get("project_root")
        declared_files = provenance.get("source_files_sha256") or (provenance.get("source") or {}).get("files") or {}
        declarations = [("product", name, expected, registered_root) for name, expected in declared_files.items()] if isinstance(declared_files, dict) else []
        declarations.append(("adapter", f"myeval/adapters/{project}.py", provenance.get("adapter_sha256"), ROOT))
        root_matches = isinstance(declared_root, str) and Path(declared_root).expanduser().resolve() == registered_root
        for kind, name, expected, allowed_root in declarations:
            entry = {"configuration_id": config_id, "source_kind": kind, "source_file": name, "expected_sha256": expected}
            try:
                relative = _safe_relative(name)
                if kind == "product" and not root_matches:
                    raise ValueError("declared_root_mismatch")
                if relative.suffix.lower() not in SOURCE_EXTENSIONS or _excluded(relative):
                    raise ValueError("source_type_not_allowed")
                if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                    raise ValueError("source_hash_unavailable")
                source = _check_path(allowed_root / relative, allowed_root)
                if source.stat().st_size > 10_000_000 or total + source.stat().st_size > 100_000_000:
                    raise ValueError("source_snapshot_size_limit")
                target_relative = Path(config_directory) / kind / relative
                target = snapshot_root / target_relative
                if target.exists():
                    _check_path(target, snapshot_root)
                    if _sha_file(target) != expected:
                        raise ValueError("existing_source_snapshot_mismatch")
                    identity = {"sha256": expected, "bytes": target.stat().st_size}
                else:
                    identity = _copy_stable(source, target)
                if identity["sha256"] != expected:
                    target.unlink()
                    raise ValueError("source_changed_since_freeze")
                total += identity["bytes"]
                entry.update(status="captured", **identity,
                             path=(Path("source-snapshots") / identifier / target_relative).as_posix())
            except (ValueError, OSError) as error:
                entry.update(status="unavailable", reason=str(error) if isinstance(error, ValueError) else type(error).__name__)
            result["files"].append(entry)
    succeeded = sum(entry["status"] == "captured" for entry in result["files"])
    result.update(status="captured" if succeeded == len(result["files"]) and succeeded else ("partial" if succeeded else "unavailable"),
                  captured_files=succeeded, total_bytes=total)
    result["manifest_hash"] = digest(result)
    write_json(snapshot_root / "manifest.json", result)
    (snapshot_root / "manifest.json").chmod(0o600)
    return result
