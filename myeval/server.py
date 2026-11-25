from __future__ import annotations

import argparse
import base64
import hmac
import io
import json
import os
import signal
import secrets
import stat
import tempfile
import time
import re
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, unquote, quote

from . import __version__
from .catalog import packs, rubric_catalog
from .exporting import attachments, source_attachments, revalidation_attachments, trial_media, read_trial_media
from .runner import Runner
from .store import StalePresentation, Store
from .snapshots import BackupManager
from .decisions import DecisionService
from .campaigns import CampaignService
from .util import ROOT, dumps, now, safe_error, write_json


class EvaluationUnavailable(Exception):
    pass


def _valid_admin_token(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value) is not None


def _load_admin_token(directory):
    """Publish a complete credential, or fail closed without repairing evidence."""
    token_file = directory / "admin.token"
    try:
        token_file.lstat()
    except FileNotFoundError:
        descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".admin-token-")
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(secrets.token_urlsafe(32))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, token_file)
            except FileExistsError:
                pass  # Never replace a credential created by another process.
        finally:
            os.unlink(temporary)
    try:
        descriptor = os.open(token_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= 256:
                raise ValueError("invalid credential file")
            token = handle.read(257).decode("ascii").strip()
        if not _valid_admin_token(token):
            raise ValueError("invalid credential")
    except (OSError, ValueError, UnicodeError):
        raise SystemExit("admin.token 无效或不可安全读取；服务未启动，原文件未覆盖。请由管理员检查并恢复访问码。") from None
    return token


class Handler(BaseHTTPRequestHandler):
    server_version = "MyEval/" + __version__

    def log_message(self, *_):
        # No user content, tokens, or reviewer links in HTTP logs.
        pass

    def reply(self, status, value, content_type="application/json; charset=utf-8", headers=None):
        raw = dumps(value).encode() if content_type.startswith("application/json") else (value.encode() if isinstance(value, str) else value)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def body(self):
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            raise ValueError("请求需使用 application/json")
        length = int(self.headers.get("Content-Length", "0"))
        maximum = 512_000_000 if urlsplit(self.path).path == "/api/imports/traces" else 50_000_000
        if not 0 < length <= maximum:
            raise ValueError("请求体大小不正确")
        result = json.loads(self.rfile.read(length))
        if not isinstance(result, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return result

    def do_GET(self):
        self.route()

    def do_POST(self):
        self.route()

    def route(self):
        try:
            host = self.headers.get("Host", "")
            if host not in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}:
                return self.reply(403, {"error": "仅允许本机访问"})
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
                return self.reply(403, {"error": "不接受跨站请求"})
            return self.dispatch()
        except KeyError:
            self.reply(404, {"error": "记录不存在"})
        except StalePresentation as error:
            self.reply(409, {"error": str(error), "code": "stale_presentation"})
        except EvaluationUnavailable as error:
            self.reply(503, {"error": str(error), "code": "evaluation_unavailable"})
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            self.reply(400, {"error": safe_error(error)})
        except Exception as error:
            self.reply(500, {"error": f"服务处理失败：{type(error).__name__}"})

    def dispatch(self):
        url = urlsplit(self.path)
        path = url.path.strip("/").split("/")
        query = parse_qs(url.query)
        store, runner = self.server.store, self.server.runner
        playbook_access = (self.command == "GET" and path[:3] == ["api", "playbook", "files"]
                           and time.time() < self.server.playbook_token_expires
                           and hmac.compare_digest(query.get("token", [""])[0], self.server.playbook_token))
        if path[0] == "api" and url.path != "/api/health" and path[:2] != ["api", "blind"] and not playbook_access:
            supplied = self.headers.get("X-MyEval-Admin", "")
            if (not _valid_admin_token(supplied) or not _valid_admin_token(self.server.admin_token)
                    or not hmac.compare_digest(supplied, self.server.admin_token)):
                return self.reply(403, {"error": "管理员接口需要本机入口。请打开数据目录中的 open-admin.html；匿名评审入口不含管理员权限。"})
        if self.command == "GET":
            if url.path == "/api/health":
                return self.reply(200, {"status": "ready", "version": __version__, "concurrency": runner.concurrency,
                                       "evaluation_concurrency": getattr(getattr(store, "evaluation_service", None), "concurrency", None),
                                       "evaluation_concurrency_requested": self.server.evaluation_concurrency_requested,
                                       "storage": "SQLite + per-attempt JSON/JSONL", "time": now(), "modes": {"trace_rescore": "supported" if getattr(store, "evaluation_service", None) else "unavailable", "fixed_context_rerun": "adapter_declared", "isolated_full_run": "adapter_declared", "distributed_execution": "unsupported"}})
            if url.path == "/api/catalog":
                return self.reply(200, {"taskpacks": packs(True), "datasets": store.datasets.list_datasets(),
                                       "capabilities": {"multi_configuration": True, "versioned_datasets": True, "source_snapshot": "declared_files_only", "historical_import": True, "local_queue": True, "distributed_execution": False},
                                       "limits": store.queue_settings()["limits"]})
            if url.path == "/api/playbook":
                return self.reply(200, self.playbook_index())
            if path[:3] == ["api", "playbook", "files"] and len(path) >= 4:
                return self.playbook_file(unquote("/".join(path[3:])))
            if url.path == "/api/queue":
                return self.reply(200, store.queue_view())
            if url.path == "/api/datasets":
                return self.reply(200, {"datasets": store.datasets.list_datasets()})
            if path[:2] == ["api", "datasets"]:
                if len(path) == 3:
                    return self.reply(200, store.datasets.get_dataset(path[2]))
                if len(path) in {5, 6} and path[3] == "versions" and (len(path) == 5 or path[5] == "export"):
                    return self.reply(200, store.datasets.get_version(path[2], path[4]))
            if url.path == "/api/imports":
                return self.reply(200, {"imports": store.list_imports()})
            if url.path == "/api/campaigns":
                return self.reply(200, {"campaigns": self.server.campaigns.list_campaigns()})
            if path[:2] == ["api", "campaigns"] and len(path) == 3:
                return self.reply(200, self.server.campaigns.get_campaign(path[2]))
            if url.path == "/api/backups":
                return self.reply(200, {"backups": self.server.backups.list_backups()})
            if url.path == "/api/evaluators":
                service = getattr(store, "evaluation_service", None)
                return self.reply(200, {"evaluators": service.list_evaluators() if service else [], "available": service is not None})
            if url.path == "/api/rubrics":
                if url.query:
                    raise ValueError("量表目录不接受路径或筛选参数")
                try:
                    catalog = rubric_catalog()
                except ImportError:
                    raise EvaluationUnavailable("评分量表校验组件尚未就绪") from None
                return self.reply(200, catalog)
            if url.path == "/api/calibrations":
                return self.reply(200, {"calibrations": self.evaluation().list_calibrations()})
            if url.path == "/api/evaluation-jobs":
                service = self.evaluation()
                return self.reply(200, {"jobs": service.list_jobs(query.get("experiment_id", [None])[0])})
            if path[0] == "api" and len(path) >= 2 and path[1] in {"revalidations", "evaluation-revalidations"}:
                filters = parse_qs(url.query, keep_blank_values=True)
                if len(path) == 2:
                    if set(filters) - {"experiment_id", "original_job_id"} or any(len(v) != 1 for v in filters.values()):
                        raise ValueError("重验收目录仅接受 experiment_id 和 original_job_id 筛选")
                    return self.reply(200, self.server.revalidations.list(filters.get("experiment_id", [None])[0], filters.get("original_job_id", [None])[0]))
                if filters:
                    raise ValueError("重验收详情不接受路径或筛选参数")
                if len(path) == 3:
                    return self.reply(200, self.server.revalidations.get(path[2]))
                if len(path) == 4 and path[3] == "export":
                    raw = self.server.revalidations.export(path[2])
                    return self.reply(200, raw, "application/zip", {"Content-Disposition": f'attachment; filename="{path[2]}.zip"'})
            if url.path == "/api/review-queue":
                return self.reply(200, {"items": self.evaluation().review_queue(query.get("experiment_id", [None])[0])})
            if path[:2] == ["api", "evaluation-jobs"]:
                if len(path) == 3:
                    return self.reply(200, self.evaluation().get_job(path[2]))
                if len(path) == 4 and path[3] == "calibration":
                    return self.reply(200, self.evaluation().calibration_report(path[2]))
            if url.path == "/api/experiments":
                return self.reply(200, {"experiments": store.list_experiments(query.get("project", [None])[0])})
            if path[:2] == ["api", "experiments"] and len(path) == 3:
                return self.reply(200, store.experiment_view(path[2]))
            if path[:2] == ["api", "experiments"] and len(path) == 4:
                if path[3] == "analysis":
                    try:
                        from .evaluation import summarize
                    except ImportError:
                        raise EvaluationUnavailable("独立分析组件尚未就绪，未生成比较结论") from None
                    return self.reply(200, summarize(store.export(path[2])))
                if path[3] == "decisions":
                    return self.reply(200, {"decisions": self.server.decisions.list_decisions(path[2])})
            if path[:2] == ["api", "trials"] and len(path) == 3:
                trial = store.trial(path[2])
                trial["events"] = runner.events(trial)
                trial["media"] = trial_media(trial)
                return self.reply(200, trial)
            if path[:2] == ["api", "trials"] and len(path) == 5 and path[3] == "media":
                raw, mime = read_trial_media(store, store.trial(path[2], False), int(path[4]))
                return self.reply(200, raw, mime)
            if path[:2] == ["api", "export"] and len(path) == 3:
                bundle = store.export(path[2], query.get("case_id", [None])[0])
                for trial in bundle["trials"]:
                    trial["events"] = runner.events(trial)
                files = (attachments(store, bundle["trials"]) + source_attachments(store, bundle["experiment"])
                         + revalidation_attachments(store, bundle))
                bundle["artifact_manifest"] = [{k: v for k, v in f.items() if k not in {"text", "content_base64"}} for f in files]
                if query.get("format") == ["zip"]:
                    stream = io.BytesIO()
                    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
                        archive.writestr("manifest.json", json.dumps(bundle, ensure_ascii=False, indent=2))
                        archive.writestr("README.txt", "MyEval 管理员完整证据导出。manifest.json 可独立读取。trials.jsonl 每行一项 trial（含所有执行尝试、阶段、错误、版本和评价）。reviewer 盲评请使用单独匿名导出。费用 null 表示未知；重复次数不是独立样本。\n")
                        archive.writestr("trials.jsonl", "\n".join(dumps(t) for t in bundle["trials"]) + "\n")
                        archive.writestr("cases.jsonl", "\n".join(dumps(c) for c in bundle["experiment"]["cases"]) + "\n")
                        archive.writestr("configurations.json", json.dumps(bundle["experiment"]["configurations"], ensure_ascii=False, indent=2))
                        archive.writestr("assessments.json", json.dumps(bundle["reviews"], ensure_ascii=False, indent=2))
                        for file in files:
                            if "text" in file:
                                archive.writestr(file["path"], file["text"])
                            elif "content_base64" in file:
                                archive.writestr(file["path"], base64.b64decode(file["content_base64"]))
                    return self.reply(200, stream.getvalue(), "application/zip", {"Content-Disposition": f'attachment; filename="myeval-{path[2]}.zip"'})
                # JSON carries each immutable image blob once; repeated attempts
                # retain their own manifest entry and refer to the same hash.
                seen_media = set()
                portable = []
                for file in files:
                    if "content_base64" in file and file["sha256"] in seen_media:
                        portable.append({k: v for k, v in file.items() if k != "content_base64"} | {"content_ref_sha256": file["sha256"]})
                    else:
                        portable.append(file)
                        if "content_base64" in file:
                            seen_media.add(file["sha256"])
                bundle["artifacts"] = portable
                return self.reply(200, bundle, headers={"Content-Disposition": f'attachment; filename="myeval-{path[2]}.json"'})
            if path[:2] == ["api", "blind"] and len(path) in {3, 4}:
                result = store.blind(path[2])
                headers = {"Content-Disposition": 'attachment; filename="anonymous-review.json"'} if len(path) == 4 and path[3] == "export" else None
                return self.reply(200, result, headers=headers)
            if path[:2] == ["api", "blind"] and len(path) == 7 and path[3] == "pairs" and path[5] == "media":
                trial = store.blind_media_trial(path[2], path[4])
                raw, mime = read_trial_media(store, trial, int(path[6]))
                return self.reply(200, raw, mime)
            files = {"/": "index.html", "/app.js": "app.js", "/view.js": "view.js", "/style.css": "style.css", "/blind.js": "blind.js"}
            file = "blind.html" if url.path.startswith("/blind/") and len(path) == 2 else files.get(url.path)
            if file:
                target = ROOT / "web" / file
                if target.exists():
                    content_type = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}[target.suffix]
                    return self.reply(200, target.read_bytes(), content_type)
        elif self.command == "POST":
            body = self.body()
            if url.path == "/api/experiments":
                return self.reply(201, store.create_experiment(body))
            if url.path == "/api/queue":
                return self.reply(200, store.configure_queue(body))
            if url.path == "/api/datasets/import":
                return self.reply(201, store.datasets.import_version(body))
            if url.path == "/api/imports/traces":
                return self.reply(201, store.import_traces(body))
            if url.path == "/api/assessments/batch":
                return self.reply(201, {"assessments": store.add_assessments(body.get("items"))})
            if url.path == "/api/campaigns":
                return self.reply(201, self.server.campaigns.create_campaign(body))
            if path[:2] == ["api", "campaigns"] and len(path) == 4 and path[3] == "experiments":
                return self.reply(201, self.server.campaigns.add_experiments(path[2], body))
            if url.path == "/api/backups":
                return self.reply(201, self.server.backups.create_backup(body))
            if path[:2] == ["api", "backups"] and len(path) == 4 and path[3] == "restore":
                return self.reply(201, self.server.backups.restore_backup(path[2], body.get("destination")))
            if url.path == "/api/evaluation-jobs":
                return self.reply(201, self.evaluation().create_job(body))
            if path[:2] == ["api", "evaluation-jobs"] and len(path) == 4 and path[3] == "cancel":
                return self.reply(200, self.evaluation().cancel_job(path[2]))
            if path[:2] == ["api", "evaluation-jobs"] and len(path) == 6 and path[3] == "units" and path[5] == "adjudications":
                return self.reply(201, self.evaluation().adjudicate(path[2], path[4], body))
            if path[:2] == ["api", "experiments"] and len(path) == 4:
                if path[3] == "stop":
                    return self.reply(200, store.stop(path[2]))
                if path[3] == "reviewers":
                    return self.reply(201, store.new_reviewer(path[2], body.get("label")))
                if path[3] in {"pause", "resume"}:
                    return self.reply(200, store.pause_experiment(path[2], path[3] == "pause"))
                if path[3] == "decisions":
                    return self.reply(201, self.server.decisions.create_decision(path[2], body))
            if path[:2] == ["api", "trials"] and len(path) == 4:
                if path[3] == "retry":
                    return self.reply(202, store.retry(path[2]))
                if path[3] == "publish":
                    return self.reply(200, runner.publish(path[2]))
                if path[3] == "assessments":
                    return self.reply(201, store.add_assessment(path[2], body))
            if path[:2] == ["api", "blind"] and len(path) == 5:
                if path[4] == "review":
                    return self.reply(201, store.save_review(path[2], path[3], body))
                if path[4] == "reveal":
                    return self.reply(200, store.reveal(path[2], path[3]))
        self.reply(404, {"error": "入口不存在"})

    def evaluation(self):
        service = getattr(self.server.store, "evaluation_service", None)
        if service is None:
            raise EvaluationUnavailable("独立评分组件尚未就绪；当前没有运行评分作业")
        return service

    @staticmethod
    def _playbook_types():
        return {".mp4": "video/mp4", ".webm": "video/webm", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".json": "application/json; charset=utf-8", ".md": "text/plain; charset=utf-8", ".vtt": "text/vtt; charset=utf-8", ".srt": "text/plain; charset=utf-8"}

    def playbook_index(self):
        from .snapshots import _check_path
        root = self.server.store.directory / "playbook"
        if not root.exists():
            return {"available": False, "files": [], "manifest": None, "message": "操作录屏尚未导入本工作区"}
        _check_path(root, self.server.store.directory)
        if time.time() >= self.server.playbook_token_expires:
            self.server.playbook_token = secrets.token_urlsafe(32)
            self.server.playbook_token_expires = time.time() + 4 * 3600
        files = []
        for path in sorted(root.rglob("*")):
            if len(files) >= 2000:
                break
            if path.suffix.lower() not in self._playbook_types() or not path.is_file():
                continue
            try:
                _check_path(path, root)
            except ValueError:
                continue
            relative = path.relative_to(root).as_posix()
            files.append({"path": relative, "bytes": path.stat().st_size, "mime_type": self._playbook_types()[path.suffix.lower()],
                          "url": "/api/playbook/files/" + quote(relative, safe="/") + "?token=" + self.server.playbook_token})
        manifest = None
        manifest_path = root / "manifest.json"
        if manifest_path.is_file():
            _check_path(manifest_path, root)
            if manifest_path.stat().st_size <= 2_000_000:
                manifest = json.loads(manifest_path.read_text())
        return {"available": bool(files), "files": files, "manifest": manifest, "read_token_expires_at": self.server.playbook_token_expires,
                "access_scope": "playbook_files_read_only", "recording_scope": "declared_by_manifest"}

    def playbook_file(self, relative):
        from .snapshots import _safe_relative, _check_path
        root = self.server.store.directory / "playbook"
        path = _check_path(root / _safe_relative(relative), root)
        if not path.is_file() or path.suffix.lower() not in self._playbook_types():
            raise KeyError("操作参考文件不存在")
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        requested = self.headers.get("Range")
        if requested:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
            if not match or not any(match.groups()):
                return self.reply(416, {"error": "不支持该文件范围"}, headers={"Content-Range": f"bytes */{size}"})
            first, last = match.groups()
            start = int(first) if first else max(0, size - int(last))
            end = min(size - 1, int(last)) if first and last else size - 1
            if start > end or start >= size:
                return self.reply(416, {"error": "文件范围超出内容"}, headers={"Content-Range": f"bytes */{size}"})
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", self._playbook_types()[path.suffix.lower()])
        self.send_header("Content-Length", str(max(0, end - start + 1)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    parser = argparse.ArgumentParser(description="MyEval 本机评测与盲评")
    parser.add_argument("--port", type=int, default=8310)
    parser.add_argument("--data-dir", type=Path, default=ROOT / ".local" / "data")
    parser.add_argument("--concurrency", type=int, choices=range(1, 33), default=None)
    parser.add_argument("--evaluation-concurrency", type=int, choices=range(1, 5), default=2,
                        help="独立评分作业并发 1–4，默认 2；不影响产品 trial 并发")
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    # Lock the data directory, not the whole computer. A second instance must not recover live work.
    import fcntl
    lock = (args.data_dir / "server.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("该数据目录已有运行中的 MyEval 服务")
    admin_token = _load_admin_token(args.data_dir)
    store = Store(args.data_dir)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    runner = Runner(store, args.concurrency or store.queue_settings()["concurrency"])
    recovered = runner.recover()
    server.store, server.runner = store, runner
    server.evaluation_concurrency_requested = args.evaluation_concurrency
    server.playbook_token, server.playbook_token_expires = secrets.token_urlsafe(32), time.time() + 4 * 3600
    server.backups, server.decisions, server.campaigns = BackupManager(store), DecisionService(store), CampaignService(store)
    server.revalidations = store.revalidations
    try:
        from .evaluation import EvaluationService
        store.evaluation_service = EvaluationService(store, concurrency=args.evaluation_concurrency)
        # Match the scoring commit lock order (evaluation -> core) so backup
        # cannot capture a partially reconciled pair across the two databases.
        if not hasattr(store.evaluation_service, "backup_lock"):
            store.evaluation_service.backup_lock = store.evaluation_service.lock
    except ImportError:
        # During staged installation the rest of the local workbench remains usable.
        store.evaluation_service = None
    server.admin_token = admin_token
    opener = args.data_dir / "open-admin.html"
    opener.write_text('<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=http://127.0.0.1:' + str(args.port) + '/#access=' + server.admin_token + '"><title>打开本机 MyEval</title><p>正在打开本机管理员工作区…</p>')
    opener.chmod(0o600)
    write_json(args.data_dir / "server.json", {"pid": os.getpid(), "port": args.port, "started_at": now(), "url": f"http://127.0.0.1:{args.port}", "recovered_trials": recovered,
                                             "concurrency": runner.concurrency, "evaluation_concurrency_requested": args.evaluation_concurrency,
                                             "evaluation_concurrency": getattr(store.evaluation_service, "concurrency", None)})
    runner.start()
    stopping = threading.Event()

    def shutdown(*_):
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f"MyEval: http://127.0.0.1:{args.port} (仅本机；数据 {args.data_dir.resolve()})", flush=True)
    print(f"管理员入口：{opener.resolve()}；盲评请使用独立的匿名链接。", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        runner.close()
        if store.evaluation_service is not None:
            store.evaluation_service.close()
        server.server_close()
        store.db.close()
        lock.close()


if __name__ == "__main__":
    main()
