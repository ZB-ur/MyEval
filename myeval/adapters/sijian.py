"""Real Sijian HTTP bridge adapter. No copied business prompts or model simulation."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

VERSION = "sijian-http-v2"
DEFAULT_ROOT = Path.home() / "AiCoding" / "Sijian"
RESOURCE_PATHS = [
    "services/agent/server.py",
    "services/agent/evaluation_profiles.py",
    "services/agent/prompts/boundary-v1.txt",
    "services/agent/prompts/boundary-review-v2.txt",
    "services/agent/schemas/discover-output-v1.json",
    "services/agent/schemas/review-output-v1.json",
]


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _root():
    return Path(os.environ.get("MYEVAL_SIJIAN_ROOT", str(DEFAULT_ROOT))).expanduser().resolve()


def _git(root, *args):
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=5)
        return result.stdout if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def describe(configuration):
    root = _root()
    parameters = configuration.get("parameters", {})
    profile_id = parameters.get("profile", "baseline-v1")
    profile = None
    profiles = []
    profile_error = None
    resources = list(RESOURCE_PATHS)
    try:
        # Only this product-owned module is loaded; configuration never selects a module or path.
        spec = importlib.util.spec_from_file_location("sijian_evaluation_profiles", root / "services/agent/evaluation_profiles.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        profiles = list(module.profile_ids())
        profile = module.describe_profile(profile_id)
        resources += ["services/agent/" + name for name in profile["files"]]
    except (OSError, ValueError, ImportError, SyntaxError, AttributeError, TypeError) as error:
        profile_error = {"code": "unsupported_prompt_profile" if isinstance(error, ValueError) else "profile_source_unavailable",
                         "error_type": type(error).__name__}
    hashes = {name: _hash((root / name).read_bytes()) if (root / name).is_file() else None
              for name in dict.fromkeys(resources)}
    head = _git(root, "rev-parse", "HEAD")
    dirty = _git(root, "diff", "--binary", "HEAD", "--", *hashes)
    return {
        "adapter": VERSION,
        "adapter_sha256": _hash(Path(__file__).read_bytes()),
        "available": bool(profile) and all(hashes.values()),
        "prompt_profile": profile, "profile_error": profile_error,
        "source": {"project_root": str(root), "entrypoint": "services/agent/server.py",
                   "git_head": head.decode().strip() if head else None,
                   "relevant_dirty": bool(dirty), "dirty_sha256": _hash(dirty) if dirty else None,
                   "files": hashes},
        "requested": {"model": parameters.get("model", "gpt-6-sol"),
                      "reasoning_effort": parameters.get("effort", "low"), "prompt_profile": profile_id},
        "effective": {"model": None, "reasoning_effort": None, "provider_model_version": None},
        "capabilities": {"tasks": ["discover", "review"], "prompt_profiles": profiles, "multi_turn": "fixed_history_only",
                         "audio": "unsupported", "cancel": "local_bridge_and_cli_process_group",
                         "provider_cancel_confirmation": "unsupported", "environment_reset": True,
                         "state_snapshot": True, "trace_rescore": "unsupported",
                         "fixed_context_rerun": True, "isolated_full_run": True},
        "mode_definition": "真实桥接的一次发现或复盘调用；固定输入材料与可见历史，独立进程和轨迹目录，不含 iOS 选材/保存旅程。",
        "limitations": ["开发期 Codex CLI 桥接，非生产后端。", "有效模型和推理强度未由供应商回执确认。",
                        "显式取消可回收本机 CLI，供应商计算及计费是否停止未知。", "未覆盖 iOS UI、语音和客户端分批发现。"],
    }


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http(port, path, body=None, request_id=None, timeout=5):
    headers = {"Content-Type": "application/json", "X-Sijian-Run-Mode": "evaluation"}
    if request_id:
        headers["X-Sijian-Request-ID"] = request_id
    request = Request(f"http://127.0.0.1:{port}{path}",
                      data=_json(body).encode() if body is not None else None, headers=headers,
                      method="POST" if body is not None else "GET")
    try:
        with build_opener(ProxyHandler({}), _NoRedirect).open(request, timeout=timeout) as response:
            return response.status, json.loads(response.read(2 * 1024 * 1024))
    except HTTPError as error:
        try:
            value = json.loads(error.read(2 * 1024 * 1024))
        except (ValueError, UnicodeError):
            value = {"error": {"code": "invalid_http_response", "message": "Bridge returned non-JSON."}}
        return error.code, value


def _load(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _observed(metadata):
    effective = metadata.get("effective", {})
    return {"requested": metadata.get("requested", {}),
            "effective": {key: None if value == "unknown" else value for key, value in effective.items()},
            "verification": "Effective fields remain unknown unless present in the product trace.",
            "product_prompt_profile": metadata.get("prompt_profile"),
            "prompt_version": metadata.get("prompt_version"),
            "prompt_template_sha256": metadata.get("prompt_template_sha256"),
            "source_run_id": metadata.get("run_id")}


def _rules(task, request, accepted, metadata, case):
    checks = []

    def add(name, passed, explanation):
        checks.append({"type": "rule", "evaluator": "sijian-observable-rules", "version": "1",
                       "dimension": name, "status": "pass" if passed else "fail",
                       "reason": explanation})

    if accepted:
        if task == "discover":
            candidates = {(m["id"], m["version"]): m["text"] for m in request.get("candidates", [])}
            matches = all(item.get("center_quote") in request["center"]["text"]
                          and item.get("candidate_quote") in candidates.get((item.get("candidate_id"), item.get("candidate_version")), "")
                          for item in accepted.get("relationships", []))
            add("evidence_exact_match", matches, "只检查当前材料版本和引文子串；不判断语义关系是否有价值。")
        else:
            selected = {(m["id"], m["version"]): m["text"] for m in request.get("materials", [])}
            matches = all(c.get("quote") in selected.get((c.get("material_id"), c.get("version")), "")
                          for c in accepted.get("citations", []))
            add("evidence_exact_match", matches, "只检查当前选定材料的版本和引文子串；不证明回答忠实或充分。")
            expected = case.get("expected_behavior", {})
            if isinstance(expected, dict) and expected.get("answer_kind"):
                add("declared_evidence_boundary", accepted.get("answer_kind") == expected["answer_kind"],
                    "仅核对本虚构用例声明的 answer_kind；具体表述仍待人工判断。")
    if "tools_observed" in metadata:
        add("tool_boundary", not metadata["tools_observed"], "仅表示产品过滤事件中未观察到工具调用。")
    return checks


def run(case, configuration, ctx):
    mode = configuration.get("mode", "fixed_context_rerun")
    task = case.get("task")
    source = describe(configuration)
    limitations = source["limitations"]
    empty = {"output": None, "review_output": None, "observed_configuration": {}, "usage": None,
             "cost": None, "assessments": [], "limitations": limitations}
    if mode not in {"fixed_context_rerun", "isolated_full_run"} or task not in {"discover", "review"}:
        return {**empty, "status": "unsupported", "error": {"code": "unsupported_mode_or_task"},
                "review_output": {"状态": "当前适配器不支持此任务或运行模式"}}
    if ctx.cancel_requested:
        ctx.emit("cancel.before_start", {"local_schedule_stopped": True, "model_request_started": False})
        return {**empty, "status": "cancelled", "error": {"code": "cancelled_before_start"},
                "review_output": {"状态": "运行开始前已取消"}}
    parameters = configuration.get("parameters", {})
    model = parameters.get("model", "gpt-6-sol")
    effort = parameters.get("effort", "low")
    profile_id = parameters.get("profile", "baseline-v1")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", model) or effort not in {"low", "medium", "high", "xhigh"}:
        return {**empty, "status": "unsupported", "error": {"code": "unsupported_configuration"},
                "review_output": {"状态": "请求配置不受支持"}}
    if (source.get("profile_error") or {}).get("code") == "unsupported_prompt_profile":
        return {**empty, "status": "unsupported", "error": source["profile_error"],
                "review_output": {"状态": "业务提示词方案不在产品白名单内"}}

    root = Path(ctx.root).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    trace_root = root / "sijian-traces"
    (root / "sijian-provenance.json").write_text(_json(source))
    if not source["available"]:
        return {**empty, "status": "failed", "error": {"code": "product_source_unavailable"},
                "review_output": {"状态": "业务服务源码不可用"}}
    bridge_timeout = max(5, min(300, int(parameters.get("request_timeout_seconds", 120)),
                                max(5, int(ctx.timeout_seconds) - 8)))
    request_id = str(uuid4())
    request = case["input"]
    (root / "sijian-request.json").write_text(_json(request))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = None
    response = None
    http_status = None
    local_failure = None
    cancel_reason = None
    cancel_started = None
    cancel_response = None
    request_started = False
    result_queue = queue.Queue()
    ctx.emit("adapter.start", {"adapter": VERSION, "mode": mode, "source": source,
                               "request_id": request_id}, stage="setup", attempt=1)
    started = time.monotonic()
    deadline = started + max(1, ctx.timeout_seconds - 4)

    def cancel(reason):
        nonlocal cancel_response, cancel_reason, cancel_started
        if cancel_reason is not None:
            return
        cancel_reason = reason
        cancel_started = time.monotonic()
        ctx.emit("cancel.requested", {"reason": reason, "request_id": request_id,
                                      "local_schedule_stopped": True,
                                      "provider_compute_stopped": None, "provider_billing_stopped": None}, stage="cancel", attempt=1)
        try:
            code, value = _http(port, "/cancel", {"request_id": request_id}, timeout=2)
            cancel_response = {"http_status": code, "response": value}
            ctx.emit("cancel.acknowledged", cancel_response, stage="cancel", attempt=1)
        except (OSError, URLError, ValueError) as error:
            cancel_response = {"error_type": type(error).__name__, "status": "unconfirmed"}
            ctx.emit("cancel.unconfirmed", cancel_response, stage="cancel", attempt=1)

    def call():
        try:
            result_queue.put(_http(port, "/" + task, request, request_id, timeout=bridge_timeout + 12))
        except (OSError, URLError, ValueError) as error:
            result_queue.put((None, {"error": {"code": "bridge_transport_error", "message": type(error).__name__}}))

    try:
        command = [sys.executable, str(_root() / "services/agent/server.py"), "--port", str(port),
                   "--model", model, "--effort", effort, "--timeout", str(bridge_timeout),
                   "--trace-root", str(trace_root), "--profile", profile_id]
        with (root / "sijian-service.log").open("wb") as service_log:
            process = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL,
                                       stdout=service_log, stderr=service_log)
        health = None
        ready_deadline = min(deadline, time.monotonic() + 12)
        while time.monotonic() < ready_deadline:
            if ctx.cancel_requested:
                cancel_reason = "user"
                break
            if process.poll() is not None:
                break
            try:
                code, health = _http(port, "/health", timeout=0.4)
                if code == 200 and health.get("contract") == "sijian.agent.v1":
                    break
            except (OSError, URLError, ValueError):
                pass
            time.sleep(0.05)
        if cancel_reason:
            ctx.emit("cancel.before_request", {"model_request_started": False, "local_schedule_stopped": True}, stage="cancel")
        elif not health or health.get("status") != "ready" or process.poll() is not None:
            local_failure = {"code": "bridge_unavailable", "message": "Bridge startup or CLI discovery failed."}
        else:
            ctx.emit("bridge.ready", health, stage="setup", attempt=1)
            ctx.emit("http.request.started", {"endpoint": "/" + task, "request_id": request_id,
                                              "input_sha256": _hash(_json(request).encode()),
                                              "bridge_timeout_seconds": bridge_timeout}, stage=task, attempt=1)
            request_started = True
            threading.Thread(target=call, daemon=True).start()
            while True:
                if ctx.cancel_requested:
                    cancel("user")
                elif time.monotonic() >= deadline:
                    cancel("timeout")
                try:
                    http_status, response = result_queue.get(timeout=0.1)
                    break
                except queue.Empty:
                    if cancel_started and time.monotonic() >= cancel_started + 3:
                        local_failure = {"code": "cancel_terminal_unconfirmed", "message": "No terminal HTTP response before cleanup."}
                        break
                    if process.poll() is not None:
                        local_failure = {"code": "bridge_exited", "message": "Bridge exited before terminal HTTP response."}
                        break
            if response is not None:
                ctx.emit("http.request.finished", {"http_status": http_status, "response": response}, stage=task, attempt=1)
    except (OSError, ValueError) as error:
        local_failure = {"code": "adapter_execution_error", "message": type(error).__name__}
    finally:
        if process and process.poll() is None:
            if request_started and response is None:
                cancel(cancel_reason or "adapter_cleanup")
            process.terminate()
            try:
                process.wait(timeout=3.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
                ctx.emit("bridge.forced_stop", {"signal": "SIGKILL", "provider_compute_stopped": None}, stage="cleanup")
        if process:
            ctx.emit("bridge.process_exited", {"pid": process.pid, "exit_code": process.returncode,
                                               "provider_billing_stopped": None}, stage="cleanup")

    receipt = _load(trace_root / "evaluation" / ".request-ids" / (request_id + ".json"), {})
    run_id = (response or {}).get("run_id") or receipt.get("run_id")
    trace_dir = None
    metadata = {}
    if isinstance(run_id, str) and re.fullmatch(r"[a-fA-F0-9-]{36}", run_id):
        trace_dir = trace_root / "evaluation" / run_id
        metadata = _load(trace_dir / "run.json", {})
        if metadata:
            ctx.emit("product.trace", metadata, stage="trace", attempt=1)
        events_path = trace_dir / "events.jsonl"
        if events_path.is_file():
            for line in events_path.read_text().splitlines():
                event = json.loads(line)
                if event.get("item_type") != "reasoning":
                    ctx.emit("product.event", event, stage=task, attempt=1)
        if metadata.get("process_stop"):
            ctx.emit("model.process_stopped", {**metadata["process_stop"],
                                              "provider_compute_stopped": None,
                                              "provider_billing_stopped": None}, stage="cancel", attempt=1)
    accepted = _load(trace_dir / "result.json") if trace_dir else None
    error = local_failure or (response or {}).get("error") or metadata.get("failure")
    status = "succeeded" if accepted and metadata.get("status") == "completed" else "failed"
    if metadata.get("status") == "cancelled" or cancel_reason:
        status = "timed_out" if cancel_reason == "timeout" else "cancelled"
        # The result acceptance race is a product fact, separate from the user's cancel intent.
        if accepted:
            ctx.emit("cancel.result_race", {"product_result_accepted": True, "user_cancel_intent": True}, stage="cancel")
    elif error and error.get("code") in {"timeout_or_output_limit", "request_timeout"}:
        status = "timed_out"
    output = {"accepted_result": accepted, "http_status": http_status, "response": response,
              "source_run_id": run_id, "request_id": request_id,
              "rejected_model_output": _load(trace_dir / "model-result.json") if trace_dir and not accepted else None,
              "product_status": metadata.get("status"), "cancel": cancel_response,
              "trace_directory": str(trace_dir.relative_to(root)) if trace_dir else None,
              "source": source}
    review_output = {key: value for key, value in accepted.items() if key != "run_id"} if accepted else {
        "状态": {"cancelled": "已取消，未接受模型结果", "timed_out": "运行超时，未接受模型结果",
                 "failed": "运行失败，未接受模型结果"}.get(status, "未交付结果")}
    usage = metadata.get("usage")
    return {"status": status, "output": output, "review_output": review_output,
            "observed_configuration": _observed(metadata), "usage": usage if isinstance(usage, dict) else None,
            "cost": None, "assessments": _rules(task, request, accepted, metadata, case),
            "limitations": limitations, "error": error,
            "timing": {"adapter_duration_ms": round((time.monotonic() - started) * 1000),
                       "product_duration_ms": metadata.get("duration_ms")},
            "artifacts": {"provenance": "sijian-provenance.json", "request": "sijian-request.json",
                          "product_trace": str(trace_dir.relative_to(root)) if trace_dir else None}}
