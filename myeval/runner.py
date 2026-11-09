from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .context import RunContext
from .util import ROOT, TERMINAL, now, read_events, safe_error, write_json


class Runner:
    def __init__(self, store, concurrency=2):
        self.store = store
        self.concurrency = concurrency
        self.stopping = threading.Event()
        self.active = {}
        self.lock = threading.Lock()
        self.threads = []

    def recover(self):
        # Only touch our own recorded workers, and confirm their command before using a PID.
        for trial in self.store.trials():
            if trial["status"] not in {"running", "cancelling"}:
                continue
            pid = trial.get("worker_pid")
            root = self.store.attempt_root(trial)
            (root / "cancel.requested").touch(exist_ok=True) if root.exists() else None
            if isinstance(pid, int):
                processes = subprocess.run(["ps", "-axo", "pid=,pgid=,command="], capture_output=True, text=True).stdout.splitlines()
                owned = []
                for line in processes:
                    parts = line.strip().split(None, 2)
                    if len(parts) == 3 and parts[1] == str(pid) and str(root) in parts[2]:
                        owned.append(parts)
                if owned:
                    self._signal(pid, signal.SIGTERM)
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        try:
                            os.killpg(pid, 0)
                        except ProcessLookupError:
                            break
                        time.sleep(0.1)
                    self._signal(pid, signal.SIGKILL)
        return self.store.recover()

    def start(self):
        for index in range(self.concurrency):
            thread = threading.Thread(target=self.loop, name=f"myeval-runner-{index}", daemon=True)
            self.threads.append(thread)
            thread.start()

    def close(self):
        self.stopping.set()
        with self.lock:
            active = list(self.active.items())
        for identifier, process in active:
            trial = self.store.trial(identifier, False)
            (self.store.attempt_root(trial) / "cancel.requested").touch()
        for thread in self.threads:
            thread.join(timeout=15)
        with self.lock:
            remaining = list(self.active.values())
        for process in remaining:
            self._signal(process.pid, signal.SIGKILL)

    def loop(self):
        while not self.stopping.is_set():
            trial = self.store.claim()
            if trial:
                try:
                    self.execute(trial)
                except Exception as error:
                    self.store.update_trial(trial["id"], expected_attempt=trial["attempt"], status="failed", ended_at=now(), result={"status": "failed", "error": {"code": "runner_error", "message": safe_error(error)}, "usage": None, "cost": None, "review_output": {"说明": "执行器未能完成交付，请管理员查看证据"}})
            else:
                # Stopped-before-dispatch and recovered terminal trials also belong
                # in the observability denominator. One bounded delivery attempt;
                # failed/unconfigured receipts require explicit operator resync.
                unpublished = next((t for t in self.store.trials() if t["status"] in TERMINAL and t.get("platform") is None), None)
                if unpublished:
                    self.publish(unpublished["id"], unpublished["attempt"])
                else:
                    self.stopping.wait(0.25)

    @staticmethod
    def _signal(pid, sig):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass

    def execute(self, trial):
        root = self.store.attempt_root(trial)
        root.mkdir(parents=True, exist_ok=True)
        write_json(root / "request.json", trial)
        ctx = RunContext(root, trial["id"], trial["experiment_id"], trial["timeout_seconds"])
        ctx.emit("execution.dispatched", {"execution_attempt": trial["attempt"], "timeout_seconds": trial["timeout_seconds"]})
        started = time.monotonic()
        # Child output is not a trace source: keep provider errors/secrets off generic logs.
        process = subprocess.Popen([sys.executable, "-m", "myeval.worker", str(root / "request.json")], cwd=ROOT,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        with self.lock:
            self.active[trial["id"]] = process
        self.store.update_trial(trial["id"], worker_pid=process.pid)
        stop_reason, stop_time, term_sent, kill_sent = None, None, False, False
        while process.poll() is None:
            elapsed = time.monotonic() - started
            if stop_reason is None:
                if elapsed >= trial["timeout_seconds"]:
                    stop_reason = "timeout"
                elif ctx.cancel_requested:
                    stop_reason = "user_cancel"
                elif self.stopping.is_set():
                    stop_reason = "server_shutdown"
                if stop_reason:
                    stop_time = time.monotonic()
                    (root / "cancel.requested").touch()
                    ctx.emit("execution.stop_requested", {"reason": stop_reason, "upstream_completion": "unknown"})
            if stop_time is not None:
                waited = time.monotonic() - stop_time
                if waited >= 8 and not term_sent:
                    self._signal(process.pid, signal.SIGTERM)
                    term_sent = True
                    ctx.emit("execution.process_interrupt_sent", {"signal": "SIGTERM", "upstream_completion": "unknown"})
                if waited >= 11 and not kill_sent:
                    self._signal(process.pid, signal.SIGKILL)
                    kill_sent = True
                    ctx.emit("execution.process_interrupt_sent", {"signal": "SIGKILL", "upstream_completion": "unknown"})
            time.sleep(0.15)
        # The worker may crash while its product server is still alive. The group ID
        # belongs to this execution, even when its leader has already exited.
        try:
            os.killpg(process.pid, 0)
            ctx.emit("execution.descendant_cleanup", {"signal": "SIGTERM", "process_group": process.pid})
            self._signal(process.pid, signal.SIGTERM)
            cleanup_deadline = time.monotonic() + 3
            while time.monotonic() < cleanup_deadline:
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            self._signal(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        with self.lock:
            self.active.pop(trial["id"], None)
        result_path = root / "result.json"
        result = json.loads(result_path.read_text()) if result_path.exists() else None
        # Serialize stop-marker observation and terminal persistence with Store.stop.
        # A concurrent HTTP stop either precedes this block or sees a terminal trial.
        with self.store.lock:
            cancellation = None
            late_cancel_intent = stop_reason is None and ctx.cancel_requested
            if late_cancel_intent:
                # A terminal product result can win the race before the poll loop sees
                # the user's stop marker. Keep the terminal status AND the stop intent.
                stop_reason = "user_cancel"
                ctx.emit("execution.cancel_intent_after_terminal", {"product_terminal_available": result is not None,
                                                                   "upstream_completion": "unknown"})
            if stop_reason:
                cancellation = {"reason": stop_reason, "phase": "cooperative_terminal" if result else "process_interrupted",
                                "process_exit_code": process.returncode, "sigterm_sent": term_sent, "sigkill_sent": kill_sent, "upstream": "unknown"}
                if late_cancel_intent:
                    cancellation["phase"] = "terminal_before_cancel_observation" if result else "worker_exited_before_cancel_observation"
            if not result:
                status = "timed_out" if stop_reason == "timeout" else ("interrupted" if stop_reason else "failed")
                result = {"status": status, "error": {"code": "worker_interrupted" if stop_reason else "worker_exit", "message": "本地进程结束，未取得确认的产品终态；已保留阶段轨迹"},
                          "usage": None, "cost": None, "review_output": {"说明": "未获得确认的交付结果"}, "limitations": ["供应商是否停止计算和计费未知。"]}
            if stop_reason == "timeout":
                result["terminal_before_runner_timeout"] = result.get("status")
                result["status"] = "timed_out"
                result.setdefault("limitations", []).append("达到 runner 时间上限；随后取得的输出仅作为迟到证据保留。")
            status = result["status"]
            if cancellation and status == "succeeded":
                cancellation["phase"] = "completed_before_cancel_confirmation"
            ctx.emit("execution.terminal", {"status": status, "process_exit_code": process.returncode, "cancellation": cancellation})
            self.store.update_trial(trial["id"], status=status, result=result, cancellation=cancellation, ended_at=now(), duration_ms=round((time.monotonic() - started) * 1000))
        self.publish(trial["id"], trial["attempt"])

    def events(self, trial):
        events = []
        for number in range(1, trial["attempt"] + 1):
            attempt = {**trial, "attempt": number}
            events.extend({**event, "execution_attempt": number} for event in read_events(self.store.attempt_root(attempt) / "events.jsonl"))
        return events

    def publish(self, identifier, attempt=None):
        trial = self.store.trial(identifier)
        if attempt is not None and trial["attempt"] != attempt:
            trial = next(t for t in trial["attempts"] if t["attempt"] == attempt)
        if trial["status"] not in TERMINAL:
            return {"status": "pending", "error": "终态后才能同步该尝试"}
        try:
            from .observability import publish_trial
            outcome = publish_trial(trial, [e for e in self.events(trial) if e["execution_attempt"] == trial["attempt"]], self.store.directory)
        except ImportError:
            outcome = {"status": "unconfigured", "error": "观测平台尚未配置；本地证据已保存"}
        except Exception as error:
            outcome = {"status": "failed", "error": safe_error(error)}
        self.store.update_trial(identifier, expected_attempt=trial["attempt"], platform=outcome)
        return outcome
