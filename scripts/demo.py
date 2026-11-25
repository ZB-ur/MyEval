#!/usr/bin/env python3
"""Start an isolated, offline MyEval demonstration using the normal HTTP API."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener
import uuid
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
MARKER = "demo-workspace.json"
SCHEMA = "myeval.demo-workspace.v1"
PURPOSE = "scripted_offline_demonstration_v1"


def prepare_directory(directory):
    if directory.is_symlink():
        raise ValueError("演示数据目录不能是符号链接。")
    if directory.exists() and not directory.is_dir():
        raise ValueError("演示数据目录必须是目录。")
    marker = directory / MARKER
    if directory.exists() and any(directory.iterdir()):
        if not marker.is_file() or marker.is_symlink():
            raise ValueError("该目录已有非演示数据。请选择新的空目录，不会覆盖或转换现有工作区。")
        declaration = json.loads(marker.read_text(encoding="utf-8"))
        if (not isinstance(declaration, dict) or declaration.get("schema") != SCHEMA
                or declaration.get("project") != "demo"):
            raise ValueError("演示目录标记不匹配；原文件保持不变。")
        database = directory / "myeval.sqlite3"
        if database.exists():
            if database.is_symlink() or not database.is_file():
                raise ValueError("演示数据库必须是普通文件。")
            with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
                if connection.execute("SELECT count(*) FROM experiments WHERE project != 'demo'").fetchone()[0]:
                    raise ValueError("该目录含其他项目实验。请使用新的空演示目录；现有数据保持不变。")
    else:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with marker.open("x", encoding="utf-8") as handle:
            json.dump({"schema": SCHEMA, "project": "demo", "notice": "预编写的虚构演示数据，无模型调用。"},
                      handle, ensure_ascii=False, indent=2)
            handle.write("\n")


def main():
    parser = argparse.ArgumentParser(description="启动 MyEval 虚构办公演示；不需要模型、API 密钥或业务项目。")
    parser.add_argument("--port", type=int, default=8330, help="本机端口，默认 8330")
    parser.add_argument("--data-dir", type=Path, default=ROOT / ".local" / "demo",
                        help="空目录或既有演示目录；默认 .local/demo，不覆盖其他工作区")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.exit(1, "需要 Python 3.11 或更新版本。\n")
    if os.name != "posix":
        parser.exit(1, "当前本地执行器需要 POSIX 进程组；请在 macOS 或 Linux / WSL 中运行。\n")
    if not 1024 <= args.port <= 65535:
        parser.error("端口必须在 1024–65535 之间。")
    # Fail before writing a new directory if another service already owns the port.
    try:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", args.port))
    except OSError:
        parser.exit(1, f"端口 {args.port} 已占用或不可用；请用 --port 指定另一个端口。\n")

    directory = args.data_dir.expanduser().absolute()
    try:
        prepare_directory(directory)
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"无法启动演示：{error}\n")
    directory = directory.resolve()
    import fcntl
    launch_lock = (directory / "demo-launch.lock").open("a+")
    try:
        fcntl.flock(launch_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.exit(1, "该演示目录已有启动进程，请使用现有入口。\n")

    environment = os.environ.copy()
    # Do not inherit the host's optional observability credentials or product roots.
    environment["MYEVAL_LANGFUSE_CONFIG"] = str(directory / (".disabled-observability-" + uuid.uuid4().hex))
    for project in ("POME", "SIJIAN", "SIDELIT"):
        environment[f"MYEVAL_{project}_ROOT"] = str(directory / "unavailable-products" / project.lower())
    environment.pop("MYEVAL_POME_CREDENTIAL_DB", None)
    environment.pop("MYEVAL_SIDELIT_PUBLIC_MEDIA_ROOTS", None)
    process = subprocess.Popen([sys.executable, "-m", "myeval", "--port", str(args.port),
                                "--data-dir", str(directory)], cwd=ROOT, env=environment)

    def stop(_signum, _frame):
        if process.poll() is None:
            process.terminate()

    previous_term = signal.signal(signal.SIGTERM, stop)
    previous_int = signal.signal(signal.SIGINT, stop)
    base = f"http://127.0.0.1:{args.port}"
    client = build_opener(ProxyHandler({}))
    token = None

    def request(path, body=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-MyEval-Admin"] = token
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        with client.open(Request(base + path, data=data, headers=headers), timeout=10) as response:
            return json.load(response)

    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return process.returncode or 1
            try:
                state = json.loads((directory / "server.json").read_text(encoding="utf-8"))
                if state.get("pid") == process.pid and request("/api/health").get("status") == "ready":
                    token = (directory / "admin.token").read_text(encoding="ascii").strip()
                    break
            except (OSError, ValueError, URLError):
                pass
            time.sleep(0.15)
        if token is None:
            raise RuntimeError("本机服务未在 30 秒内就绪；数据已保留。")

        experiments = request("/api/experiments")["experiments"]
        existing = next((item for item in experiments if item.get("project") == "demo"
                         and item.get("purpose") == PURPOSE), None)
        if existing is None:
            pack = json.loads((ROOT / "taskpacks" / "demo.json").read_text(encoding="utf-8"))
            existing = request("/api/experiments", {
                "project": "demo", "taskpack_id": pack["id"],
                "title": "办公回复 · 三方案演示", "purpose": PURPOSE,
                "case_ids": [case["id"] for case in pack["cases"]],
                "baseline_id": pack["configurations"][0]["id"],
                "configuration_ids": [config["id"] for config in pack["configurations"]],
                "repeats": 1, "timeout_seconds": 30,
            })
            print("已创建演示：6 个虚构办公案例 × 3 个预编写方案，包含明确的故障演练。", flush=True)
        else:
            print("已保留既有演示实验与评价；没有重复创建或自动重试。", flush=True)

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and process.poll() is None:
            current = request("/api/experiments/" + existing["id"])
            if current["completed_trials"] == current["total_trials"]:
                print("演示执行已完成：" + json.dumps(current["counts"], ensure_ascii=False), flush=True)
                break
            time.sleep(0.3)
        print("这是预编写演示，不调用模型；耗时仅代表本地执行，用量和费用为未知。", flush=True)
        print(f"打开 {base}；管理员入口保存在演示目录的 open-admin.html。按 Ctrl+C 停止，数据会保留。", flush=True)
        if not args.no_open and process.poll() is None:
            webbrowser.open(base + "/#access=" + token)
        return process.wait()
    except (OSError, ValueError, RuntimeError, URLError) as error:
        print(f"演示启动未完成：{type(error).__name__}。请检查端口或本机服务；原数据已保留。", file=sys.stderr)
        return 1
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
        launch_lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
