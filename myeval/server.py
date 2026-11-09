from __future__ import annotations

import argparse
import hmac
import io
import json
import os
import signal
import secrets
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .catalog import packs
from .exporting import attachments
from .runner import Runner
from .store import StalePresentation, Store
from .util import ROOT, dumps, now, safe_error, write_json


class Handler(BaseHTTPRequestHandler):
    server_version = "MyEval/0.1"

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
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
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
        if not 0 < length <= 1_000_000:
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
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            self.reply(400, {"error": safe_error(error)})
        except Exception as error:
            self.reply(500, {"error": f"服务处理失败：{type(error).__name__}"})

    def dispatch(self):
        url = urlsplit(self.path)
        path = url.path.strip("/").split("/")
        query = parse_qs(url.query)
        store, runner = self.server.store, self.server.runner
        if path[0] == "api" and url.path != "/api/health" and path[:2] != ["api", "blind"]:
            supplied = self.headers.get("X-MyEval-Admin", "")
            if not hmac.compare_digest(supplied, self.server.admin_token):
                return self.reply(403, {"error": "管理员接口需要本机入口。请打开数据目录中的 open-admin.html；匿名评审入口不含管理员权限。"})
        if self.command == "GET":
            if url.path == "/api/health":
                return self.reply(200, {"status": "ready", "version": "0.1.0", "concurrency": runner.concurrency, "storage": "SQLite + per-attempt JSON/JSONL", "time": now(), "modes": {"trace_rescore": "unsupported", "fixed_context_rerun": "adapter_declared", "isolated_full_run": "adapter_declared"}})
            if url.path == "/api/catalog":
                return self.reply(200, {"taskpacks": packs(True)})
            if url.path == "/api/experiments":
                return self.reply(200, {"experiments": store.list_experiments(query.get("project", [None])[0])})
            if path[:2] == ["api", "experiments"] and len(path) == 3:
                return self.reply(200, store.experiment_view(path[2]))
            if path[:2] == ["api", "trials"] and len(path) == 3:
                trial = store.trial(path[2])
                trial["events"] = runner.events(trial)
                return self.reply(200, trial)
            if path[:2] == ["api", "export"] and len(path) == 3:
                bundle = store.export(path[2], query.get("case_id", [None])[0])
                for trial in bundle["trials"]:
                    trial["events"] = runner.events(trial)
                files = attachments(store, bundle["trials"])
                bundle["artifact_manifest"] = [{k: v for k, v in f.items() if k != "text"} for f in files]
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
                    return self.reply(200, stream.getvalue(), "application/zip", {"Content-Disposition": f'attachment; filename="myeval-{path[2]}.zip"'})
                bundle["artifacts"] = files
                return self.reply(200, bundle, headers={"Content-Disposition": f'attachment; filename="myeval-{path[2]}.json"'})
            if path[:2] == ["api", "blind"] and len(path) in {3, 4}:
                result = store.blind(path[2])
                headers = {"Content-Disposition": 'attachment; filename="anonymous-review.json"'} if len(path) == 4 and path[3] == "export" else None
                return self.reply(200, result, headers=headers)
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
            if path[:2] == ["api", "experiments"] and len(path) == 4:
                if path[3] == "stop":
                    return self.reply(200, store.stop(path[2]))
                if path[3] == "reviewers":
                    return self.reply(201, store.new_reviewer(path[2], body.get("label")))
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


def main():
    parser = argparse.ArgumentParser(description="MyEval 本机评测与盲评")
    parser.add_argument("--port", type=int, default=8310)
    parser.add_argument("--data-dir", type=Path, default=ROOT / ".local" / "data")
    parser.add_argument("--concurrency", type=int, choices=[1, 2], default=2)
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    # Lock the data directory, not the whole computer. A second instance must not recover live work.
    import fcntl
    lock = (args.data_dir / "server.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("该数据目录已有运行中的 MyEval 服务")
    store = Store(args.data_dir)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    runner = Runner(store, args.concurrency)
    recovered = runner.recover()
    server.store, server.runner = store, runner
    token_file = args.data_dir / "admin.token"
    if not token_file.exists():
        token_file.write_text(secrets.token_urlsafe(32))
        token_file.chmod(0o600)
    server.admin_token = token_file.read_text().strip()
    opener = args.data_dir / "open-admin.html"
    opener.write_text('<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=http://127.0.0.1:' + str(args.port) + '/#access=' + server.admin_token + '"><title>打开本机 MyEval</title><p>正在打开本机管理员工作区…</p>')
    opener.chmod(0o600)
    write_json(args.data_dir / "server.json", {"pid": os.getpid(), "port": args.port, "started_at": now(), "url": f"http://127.0.0.1:{args.port}", "recovered_trials": recovered})
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
        server.server_close()
        lock.close()


if __name__ == "__main__":
    main()
