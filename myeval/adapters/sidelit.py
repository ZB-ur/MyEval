"""Sidelit shared native Critique runtime. No prompt or inference reimplementation."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import time
from uuid import uuid4

from ..datasets import validate_source

VERSION = "sidelit-native-v1"
PROFILES = ("author-schema-v1", "evidence-grounded-v1", "intent-conditioned-v1")
PROTOCOL_V1 = "sidelit-critique-eval-v1"
PROTOCOL_V2 = "sidelit-critique-eval-v2"
PROFILES_V2 = ("author-schema-v1", "score-free-observation-v1", "score-free-evidence-first-v1")
BUILD_V2 = ".acceptance/myeval-scorefree-20261118/build"
DEFAULT_ROOT = Path.home() / "AiCoding" / "Sidelit"
BUILD = ".acceptance/myeval-v2-20261118/build"
BUILD_CATALOG = {
    "legacy-frozen-20261118": (PROTOCOL_V1, BUILD),
    "legacy-compat-20261118": (PROTOCOL_V1, ".acceptance/myeval-scorefree-20261118/legacy-compat-build"),
    "scorefree-20261118": (PROTOCOL_V2, BUILD_V2),
}
DEFAULT_BUILD_IDS = {PROTOCOL_V1: "legacy-frozen-20261118", PROTOCOL_V2: "scorefree-20261118"}
FILES = ("POC/App/CritiqueRuntime.swift", "POC/Shared/ModelResourceLease.swift",
         "POC/NativeCritique/Bridge/SCritique.cpp", "POC/NativeCritique/Bridge/SCritique.h",
         "POC/NativeCritique/Evaluation/main.swift", "POC/NativeCritique/Evaluation/build.sh")
LIMITATIONS = ["Mac 原生共用模型调用，不代表 iPhone、相机、快门或能耗验收。",
               "三个已见公开来源仅属开发材料；重复固定 seed 不构成独立样本。",
               "权重完整哈希在 native 加载时核验；目录描述仅记录元数据。",
               "权重验证会预读文件，不能称磁盘冷启动；RSS 是进程生命周期峰值。",
               "模型自评分不是质量裁判；自动规则仅检查运行与结构。"]


def _root():
    return Path(os.environ.get("MYEVAL_SIDELIT_ROOT", DEFAULT_ROOT)).expanduser().resolve()


def _sha(path):
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _select_build(configuration):
    """Resolve a registered build ID, never a caller-provided path or command."""
    params = configuration.get("parameters", {})
    if not isinstance(params, dict) or set(params) - {"profile_id", "protocol_version", "build_id"}:
        raise ValueError("unsupported_configuration_parameter")
    protocol = params.get("protocol_version", PROTOCOL_V1)
    if not isinstance(protocol, str) or protocol not in DEFAULT_BUILD_IDS:
        raise ValueError("unsupported_protocol")
    build_id = params.get("build_id", DEFAULT_BUILD_IDS[protocol])
    if not isinstance(build_id, str) or build_id not in BUILD_CATALOG:
        raise ValueError("unsupported_build_id")
    selected_protocol, build = BUILD_CATALOG[build_id]
    if selected_protocol != protocol:
        raise ValueError("build_protocol_mismatch")
    v2 = protocol == PROTOCOL_V2
    return {"id": build_id, "protocol": protocol, "directory": build,
            "binary": "sidelit-critique-v2" if v2 else "sidelit-critique",
            "library": "libSCritiqueV2.dylib" if v2 else "libSCritique.dylib"}


def describe(configuration):
    root = _root()
    try:
        selected = _select_build(configuration)
    except ValueError as error:
        # No user-selected filesystem path is read on a rejected selection.
        return {"adapter": VERSION, "adapter_sha256": _sha(Path(__file__)), "available": False,
                "error": {"code": str(error)}, "requested": {"build_selection": "rejected"},
                "effective": {"profile_id": None, "model": None},
                "source": {"project_root": str(root), "files": {}, "build_matches_sources_and_artifacts": False},
                "limitations": LIMITATIONS}
    protocol = selected["protocol"]
    v2 = protocol == PROTOCOL_V2
    build = selected["directory"]
    binary_name, library_name = selected["binary"], selected["library"]
    binary = root / build / binary_name
    entrypoint = "POC/NativeCritique/EvaluationV2/main.swift" if v2 else FILES[4]
    paths = set((*FILES[:4], "POC/Shared/CritiqueContractsV2.swift", entrypoint,
                 "POC/NativeCritique/EvaluationV2/build.sh")) if v2 else set(FILES)
    hashes = {p: _sha(root / p) for p in sorted(paths)}
    runtime = (root / FILES[0]).read_text() if (root / FILES[0]).exists() else ""
    model_dir = Path(os.environ.get("SIDELIT_CRITIQUE_MODEL_DIR", root / "POC/Models/Photography-Critique-Q4_K_M")).resolve()
    models = []
    for name, expected in re.findall(r'\("([^\"]+\.gguf)", "([a-f0-9]{64})"\)', runtime):
        p = model_dir / name
        stat = p.stat() if p.is_file() else None
        models.append({"name": name, "expected_sha256": expected, "bytes": stat.st_size if stat else None,
                       "mtime_ns": stat.st_mtime_ns if stat else None, "hash_verified_at_describe": False})
    manifests = {str(p.relative_to(root)): _sha(p) for p in (root / build).glob("*manifest*.json")}
    libraries = {str(p.relative_to(root)): _sha(p) for p in (root / build).glob("*.dylib")}
    runtime_dir = root / "POC/SpecialistComparison/.runtime/llama-b12110"
    runtime_libraries = {p.name: _sha(p) for p in sorted(runtime_dir.glob("*.dylib"))}
    try:
        manifest = json.loads((root / build / "manifest.json").read_text())
    except (OSError, ValueError):
        manifest = {}
    build_matches = (bool(manifest) and manifest.get("protocol_version") == protocol
                     and manifest.get("source_hashes") == hashes
                     and manifest.get("artifacts") == {binary_name: _sha(binary), library_name: _sha(root / build / library_name)}
                     and manifest.get("runtime_libraries") == runtime_libraries)
    params = configuration.get("parameters", {})
    return {"adapter": VERSION, "adapter_sha256": _sha(Path(__file__)),
            "available": protocol in {PROTOCOL_V1, PROTOCOL_V2} and build_matches and all(hashes.values()) and binary.is_file() and bool(models) and all(m["bytes"] for m in models),
            "source": {"project_root": str(root), "git_head": None, "files": hashes,
                       "build_id": selected["id"], "build_directory": build,
                       "entrypoint": entrypoint, "binary_sha256": _sha(binary),
                       "build_manifests": manifests, "build_matches_sources_and_artifacts": build_matches,
                       "native_libraries": libraries, "runtime_libraries": runtime_libraries,
                       "model_directory": str(model_dir), "models": models},
            "requested": {"protocol_version": protocol, "build_id": selected["id"],
                          "profile_id": params.get("profile_id"), "model": "Photography-Critique-Q4_K_M"},
            "effective": {"profile_id": None, "model": None},
            "environment": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()},
            "capabilities": {"tasks": ["photography_critique"], "fixed_context_rerun": True,
                             "media": "licensed_public_local_image", "cancel": "native_marker_then_process_group",
                             "physical_camera": "unsupported", "ios_native_inference": "unsupported",
                             "device_energy": "unsupported", "multi_turn": "unsupported"},
            "limitations": LIMITATIONS}


def _media(case, root):
    source = validate_source(case, project="sidelit")
    if source["type"] != "licensed_public":
        raise ValueError("Sidelit 本轮只接受逐图有许可的公开材料")
    value = case["input"]
    path = Path(value["image_path"]).expanduser()
    path = (root / path).resolve() if not path.is_absolute() else path.resolve()
    allowed = [root / "POC/Resources/Samples"]
    # Additional public manifests can be explicitly admitted without opening private media.
    allowed += [Path(p).expanduser().resolve() for p in os.environ.get("MYEVAL_SIDELIT_PUBLIC_MEDIA_ROOTS", "").split(os.pathsep) if p]
    if not any(path.is_relative_to(folder.resolve()) for folder in allowed):
        raise ValueError("公开图片路径未在允许的媒体目录内")
    if not path.is_file() or path.stat().st_size > 30 * 1024 * 1024:
        raise ValueError("图片缺失或超过 30 MiB")
    expected = value.get("image_sha256")
    if not isinstance(expected, str) or not re.fullmatch("[a-f0-9]{64}", expected):
        raise ValueError("图片须声明 image_sha256")
    if source.get("media_sha256") != expected or _sha(path) != expected:
        raise ValueError("图片哈希与来源/冻结输入不一致")
    return path, source


def _artifacts(out, result=None):
    """Declare only existing attempt-local evidence; never export external paths."""
    candidates = {"native_request": "native-request.json", "native_result": "native-result.json",
                  "media_manifest": "media-manifest.json", "native_events": "native/native-events.jsonl"}
    if result is None:
        try:
            result = json.loads((out / "native-result.json").read_text())
        except (OSError, ValueError):
            result = {}
    if isinstance(result, dict) and isinstance(result.get("native_run_path"), str):
        candidates["native_run"] = result["native_run_path"]
    if isinstance(result, dict) and isinstance(result.get("native_run_paths"), list):
        for index, path in enumerate(result["native_run_paths"]):
            if isinstance(path, str):
                candidates[f"native_run_{index}"] = path
    # A later stage can be interrupted before the envelope is saved. Keep already
    # persisted stage runs as partial evidence, without manufacturing final output.
    for index, path in enumerate(sorted((out / "native" / "runs").glob("*.json"))):
        candidates[f"partial_native_run_{index}"] = str(path)
    for index, path in enumerate(sorted((out / "media").glob("*"))):
        candidates[f"public_image_{index}"] = str(path)
    artifacts = {}
    for name, value in candidates.items():
        path = Path(value)
        path = (out / path).resolve() if not path.is_absolute() else path.resolve()
        if path.is_relative_to(out) and path.is_file():
            artifacts[name] = str(path.relative_to(out))
    return artifacts


def run(case, configuration, ctx):
    root, out = _root(), Path(ctx.root).resolve()
    provenance = describe(configuration)
    empty = {"output": None, "review_output": None, "usage": None, "cost": None,
             "observed_configuration": provenance, "assessments": [], "limitations": LIMITATIONS}
    def terminal(status, code):
        return {**empty, "status": status, "error": {"code": code}, "artifacts": _artifacts(out)}
    params = configuration.get("parameters", {})
    try:
        selected = _select_build(configuration)
    except ValueError as error:
        return terminal("unsupported", str(error))
    protocol = selected["protocol"]
    v2 = protocol == PROTOCOL_V2
    build, binary_name = selected["directory"], selected["binary"]
    if (case.get("task") != "photography_critique" or configuration.get("mode") != "fixed_context_rerun"
            or params.get("profile_id") not in (PROFILES_V2 if v2 else PROFILES)):
        return terminal("unsupported", "unsupported_mode_task_or_profile")
    if configuration.get("provenance", provenance) != provenance:
        return terminal("failed", "source_changed_since_freeze")
    if ctx.cancel_requested:
        return terminal("cancelled", "cancelled_before_start")
    if not provenance["available"]:
        return terminal("failed", "native_build_or_models_unavailable" if provenance["source"]["build_matches_sources_and_artifacts"] else "native_build_stale_or_missing")
    try:
        image_path, source = _media(case, root)
    except (KeyError, ValueError, OSError) as error:
        ctx.emit("media.rejected", {"reason": str(error)})
        return terminal("failed", "public_media_validation_failed")
    intent = case["input"].get("intent", "")
    if not v2 and (not isinstance(intent, str) or len(intent.encode("utf-8")) > 800 or "<|" in intent or "<__" in intent or "\x00" in intent
            or (params["profile_id"] == "intent-conditioned-v1" and not intent.strip())):
        return terminal("failed", "invalid_intent")
    if v2:
        product_request = case["input"].get("product_request")
        if not isinstance(product_request, dict) or not isinstance(intent, str):
            return terminal("failed", "invalid_product_request")
        # Only explicit caller fields cross the boundary; product owns validation and routing.
        if set(product_request) != {"purpose", "intent", "response_language", "rating_request"}:
            return terminal("failed", "invalid_product_request")
        if not isinstance(product_request.get("intent"), dict) or product_request["intent"].get("text") != intent:
            return terminal("failed", "intent_source_mismatch")
    started = time.monotonic()
    deadline = started + max(1, ctx.timeout_seconds - 7)
    lock_path = root / ".acceptance/myeval-v2-20261118/native-model.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lease:
        ctx.emit("native.queue", {"serialization": "flock", "lock": str(lock_path)})
        while True:
            if ctx.cancel_requested:
                return terminal("cancelled", "cancelled_while_queued")
            if time.monotonic() >= deadline:
                return terminal("timed_out", "timeout_while_queued")
            try:
                fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(.15)
        # Source/media may change during a wait; verify immediately before native launch.
        if describe(configuration) != provenance:
            return terminal("failed", "source_changed_since_freeze")
        if _sha(image_path) != case["input"]["image_sha256"]:
            return terminal("failed", "media_changed_since_freeze")
        media_dir = out / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        snapshot = media_dir / (case["input"]["image_sha256"] + image_path.suffix.lower())
        image_bytes = image_path.read_bytes()
        if len(image_bytes) > 30 * 1024 * 1024 or hashlib.sha256(image_bytes).hexdigest() != case["input"]["image_sha256"]:
            return terminal("failed", "media_changed_since_freeze")
        snapshot.write_bytes(image_bytes)
        media = [{"path": str(snapshot.relative_to(out)), "sha256": case["input"]["image_sha256"],
                  "license": source["license"], "attribution": source["attribution"], "source_url": source["url"],
                  "type": "image", "transformation": "none", "bytes": len(image_bytes)}]
        empty["media"] = media
        request_id = str(uuid4())
        marker, result_path = out / "native.cancel", out / "native-result.json"
        request = {"image_path": str(snapshot), "image_sha256": case["input"]["image_sha256"],
                   "profile_id": params["profile_id"], "intent": intent, "request_id": request_id,
                   "output_dir": str(out / "native"), "cancel_path": str(marker),
                   "timeout_seconds": min(600, max(1, int(deadline - time.monotonic()))), "parent_pid": os.getpid()}
        if v2:
            request["product_request"] = product_request
        _write(out / "native-request.json", request)
        _write(out / "media-manifest.json", {"source": source, "input": case["input"], "resolved_path": str(image_path), "media": media})
        env = os.environ.copy()
        env["SIDELIT_CRITIQUE_MODEL_DIR"] = provenance["source"]["model_directory"]
        env["SIDELIT_CRITIQUE_RUN_DIR"] = str(out / "native")
        process = None
        def stop(sig):
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    pass
        def interrupted(signum, frame):
            marker.touch()
            stop(signal.SIGKILL)
            raise SystemExit(128 + signum)
        previous = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, interrupted)
        reason, stop_at, signals = None, None, []
        try:
            with (out / "native-stdout.log").open("wb") as stdout, (out / "native-stderr.log").open("wb") as stderr:
                process = subprocess.Popen(["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)(deny network*)",
                                            str(root / build / binary_name), "--request", str(out / "native-request.json"),
                                            "--result", str(result_path)], cwd=root, env=env, stdin=subprocess.DEVNULL,
                                           stdout=stdout, stderr=stderr, start_new_session=True, pass_fds=(lease.fileno(),))
                ctx.emit("native.started", {"pid": process.pid, "request_id": request_id,
                                             "profile_id": params["profile_id"], "mode": "mac_native_critique"})
                while process.poll() is None:
                    now = time.monotonic()
                    if reason is None and (ctx.cancel_requested or now >= deadline):
                        reason = "cancelled" if ctx.cancel_requested else "timed_out"
                        stop_at = now
                        marker.touch()
                        ctx.emit("native.cancel_requested", {"reason": reason, "native_acknowledged": False})
                    if stop_at is not None:
                        for delay, sig in ((3, signal.SIGTERM), (5, signal.SIGKILL)):
                            if now - stop_at >= delay and sig.name not in signals:
                                stop(sig)
                                signals.append(sig.name)
                                ctx.emit("native.forced_stop", {"signal": sig.name, "native_acknowledged": False})
                    time.sleep(.1)
        finally:
            stop(signal.SIGKILL)
            if process is not None:
                process.wait(timeout=3)
            signal.signal(signal.SIGTERM, previous)
        ctx.emit("native.exited", {"exit_code": process.returncode, "signals": signals})
        try:
            result = json.loads(result_path.read_text())
        except (OSError, ValueError):
            return terminal(reason or "failed", "native_result_unavailable")
        if result.get("protocol_version") != protocol or result.get("request_id") != request_id:
            return terminal("failed", "native_protocol_mismatch")
        native_runs = result.get("native_runs", []) if v2 else [result.get("native_run") or {}]
        native = native_runs[-1] if native_runs else {}

        status = result.get("status", "failed")
        if status == "completed":
            status = "succeeded"
        if status not in {"succeeded", "failed", "cancelled", "timed_out", "unsupported", "refused"}:
            status = "failed"
        if reason:
            status = reason
        elif process.returncode != 0 and status == "succeeded":
            status = "failed"
        parsed = result.get("parsed_report")
        if (status == "succeeded" or (v2 and status == "refused")) and (not parsed or not native_runs or any(r.get("status") != 0 for r in native_runs)):
            status = "failed"
        if v2:
            # No repaired/partial business object on failed delivery; raw stays in output/artifacts.
            review = {"report": parsed if status in {"succeeded", "refused"} else None,
                      "intent": intent, "status": status, "media": media}
            def total(key):
                values = [r.get(key) for r in native_runs]
                return sum(values) if values and all(isinstance(x, (int, float)) for x in values) else None
            # A load failure can have consumed work without producing a CritiqueRun.
            complete_metrics = status in {"succeeded", "refused"}
            load_states_known = all(isinstance((r.get("configuration") or {}).get("modelWasLoadedBeforeRequest"), bool)
                                    for r in native_runs)
            loads = [r.get("loadMilliseconds") for r in native_runs
                     if (r.get("configuration") or {}).get("modelWasLoadedBeforeRequest") is False]
            usage = {"prompt_tokens": total("promptTokens"), "completion_tokens": total("generatedTokens"),
                     "native_calls": len(native_runs) if complete_metrics else None,
                     "recorded_native_runs": len(native_runs),
                     "scheduled_stages": result.get("scheduled_stages"),
                     "expected_stages": (result.get("profile") or {}).get("expected_stages"),
                     "metrics_scope": "complete_returned_calls" if complete_metrics else "recorded_runs_only_unrecorded_work_unknown",
                     "wall_seconds": time.monotonic() - started,
                     "end_to_end_ms": total("endToEndMilliseconds"),
                     "prompt_ms": total("promptMilliseconds"), "generation_ms": total("generationMilliseconds"),
                     "load_ms": sum(loads) if complete_metrics and load_states_known and loads and all(isinstance(x, (int, float)) for x in loads) else None,
                     "process_lifetime_peak_rss_bytes": max((r["processLifetimePeakRSSBytes"] for r in native_runs if isinstance(r.get("processLifetimePeakRSSBytes"), (int, float))), default=None)}
        else:
            review = {"report": parsed, "raw_output": native.get("rawOutput"), "intent": intent, "status": status, "media": media}
            usage = {"prompt_tokens": native.get("promptTokens"), "completion_tokens": native.get("generatedTokens"),
                     "wall_seconds": time.monotonic() - started, "end_to_end_ms": native.get("endToEndMilliseconds"),
                     "process_lifetime_peak_rss_bytes": native.get("processLifetimePeakRSSBytes")}
        native_cancellation = result.get("cancellation") or {}
        native_acknowledged = native_cancellation.get("cooperative_acknowledged") is True
        ctx.emit("native.result", {"status": status, "native_status": native.get("status"),
                                   "native_acknowledged_cancel": native_acknowledged,
                                   "cancellation": native_cancellation,
                                   "parsed_report_available": parsed is not None, "schema_status": result.get("schema_status"),
                                   "semantic_disposition": result.get("semantic_disposition"), "resource_released": result.get("resource_released")})
        return {**empty, "status": status, "output": result, "artifacts": _artifacts(out, result),
                "review_output": review,
                "observed_configuration": {"requested": provenance["requested"], "effective": result.get("profile"),
                                           "native_model": {k: native.get(k) for k in ("modelRevision", "languageSHA256", "projectorSHA256", "runtimeCommit")}},
                "usage": usage,
                "error": result.get("error"), "cancellation": {**native_cancellation, "requested": reason, "signals": signals,
                    "native_acknowledged": native_acknowledged, "local_process_exited": True,
                    "resource_released": result.get("resource_released")},
                "assessments": [{"type": "rule", "evaluator": "sidelit-native-contract", "version": "1",
                    "dimension": "native_delivery", "status": "pass" if status == "succeeded" and parsed else "fail",
                    "reason": "仅检查 native 终态及产品结构解析，不判断照片事实或审美质量。",
                    **({"schema_status": result.get("schema_status"), "semantic_disposition": result.get("semantic_disposition"),
                        "coverage": "insufficient_evidence" if result.get("semantic_disposition") == "insufficient_evidence" else ("refused" if status == "refused" else "requires_independent_review")} if v2 else {})}]}
