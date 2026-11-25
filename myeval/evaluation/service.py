"""Persistent independent judge jobs. Product execution is never invoked here."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import secrets
import sqlite3
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from ..util import TERMINAL, digest, dumps, now, safe_error, uid, write_json
from . import live_typed
from .calibration import calibration, list_calibrations
from .media import prepare_media
from .schema import SCHEMA_VERSION, judgment_schema, neutral_payload, normalize_pointers, prompt_for, rubric_snapshot, validate_judgment

UNIT_TERMINAL = {"succeeded", "failed", "timed_out", "cancelled", "interrupted", "invalid", "unavailable", "budget_exhausted", "commit_pending"}
EVALUATORS = [
    {"id": "codex-astra-v1", "version": "1.0.0", "name": "Astra 独立评阅", "provider": "codex_cli", "model": "gpt-6-astra", "reasoning_effort": "medium", "vision": True},
    {"id": "codex-sol-v1", "version": "1.0.0", "name": "Sol 独立评阅", "provider": "codex_cli", "model": "gpt-6-sol", "reasoning_effort": "medium", "vision": True},
] + live_typed.evaluator_presets()


def _integer(value, default, low, high, label):
    value = default if value is None else value
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label}必须是 {low}–{high} 范围内的整数")
    return value


def _split(case):
    value = case.get("split", "development")
    return {"dev": "development", "smoke": "development", "regression": "development", "new_holdout": "holdout"}.get(value, value)


def _usage_total(usage):
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if type(total) is int and total >= 0:
        return total
    a = usage.get("input_tokens", usage.get("prompt_tokens"))
    b = usage.get("output_tokens", usage.get("completion_tokens"))
    return a + b if type(a) is int and type(b) is int and a >= 0 and b >= 0 else None


def _implementation():
    root = Path(__file__).parent
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in ("service.py", "schema.py", "media.py", "provider.py",
                         "live_typed.py", "revalidation_schema.py", "../util.py")}


_IMPORTED_IMPLEMENTATION = _implementation()
_IMPORTED_LIVE_PROFILE = live_typed.profile_identity()


class EvaluationService:
    def __init__(self, store, *, autostart=True, concurrency=2):
        self.store = store
        self.concurrency = _integer(concurrency, 2, 1, 4, "评分器并发")
        self.implementation = copy.deepcopy(_IMPORTED_IMPLEMENTATION)
        self.live_profile = copy.deepcopy(_IMPORTED_LIVE_PROFILE)
        self.directory = Path(store.directory).resolve() / "evaluation"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.owner_lock = None
        if autostart:
            self.owner_lock = (self.directory / "worker.lock").open("a+")
            try:
                fcntl.flock(self.owner_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.owner_lock.close()
                raise RuntimeError("此数据目录已有评分调度器，请通过现有服务/API操作") from None
        self.lock = threading.RLock()
        # Every cross-database operation takes evaluation -> Store. BackupManager
        # uses this same lock before it takes Store.lock, preventing ABBA deadlock.
        self.backup_lock = self.lock
        self.db = sqlite3.connect(self.directory / "evaluation.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,experiment_id TEXT,status TEXT,created_at TEXT,body TEXT);
            CREATE TABLE IF NOT EXISTS units(id TEXT PRIMARY KEY,job_id TEXT,status TEXT,ordinal INTEGER,body TEXT);
            CREATE INDEX IF NOT EXISTS units_job ON units(job_id,ordinal);
            CREATE TABLE IF NOT EXISTS adjudications(id TEXT PRIMARY KEY,job_id TEXT,unit_id TEXT,body TEXT);
        """)
        self.db.commit()
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.active = {}
        self.closed = False
        self.closing = False
        if autostart:
            self._recover()
        self.workers = [threading.Thread(target=self._loop, name=f"myeval-independent-judge-{i + 1}", daemon=True)
                        for i in range(self.concurrency)]
        if autostart:
            for worker in self.workers:
                worker.start()

    def _row(self, table, identifier):
        if table not in {"jobs", "units"}:
            raise ValueError("invalid_table")
        row = self.db.execute(f"SELECT body FROM {table} WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise KeyError("评分记录不存在")
        return json.loads(row[0])

    def _save_job(self, job):
        self.db.execute("UPDATE jobs SET status=?,body=? WHERE id=?", (job["status"], dumps(job), job["id"]))

    def _save_unit(self, unit):
        self.db.execute("UPDATE units SET status=?,body=? WHERE id=?", (unit["status"], dumps(unit), unit["id"]))

    def _recover(self):
        with self.lock, self.db:
            affected_jobs = set()
            for row in self.db.execute("SELECT body FROM units WHERE status IN ('committing','commit_pending')").fetchall():
                unit = json.loads(row[0])
                affected_jobs.add(unit["job_id"])
                try:
                    self._persist_assessments(self._row("jobs", unit["job_id"]), unit)
                    unit.update(status="succeeded", error=None, recovery={"phase": "accepted_judgment_commit", "model_replayed": False})
                except Exception as error:
                    unit.update(status="commit_pending", error={"code": "assessment_commit_pending", "message": safe_error(error)})
                self._save_unit(unit)
            for row in self.db.execute("SELECT body FROM units WHERE status='running'").fetchall():
                unit = json.loads(row[0])
                affected_jobs.add(unit["job_id"])
                unit.update(status="interrupted", ended_at=now(), error={"code": "service_restart", "message": "评分进程被中断，未自动重发；可建立新评分作业。"},
                            cancellation={"local": "prior_process_unknown", "upstream": "unknown"},
                            request_sent="unknown" if unit.get("dispatch_intent_at") else False,
                            dispatch_state="interrupted_unknown" if unit.get("dispatch_intent_at") else "not_dispatched")
                self._save_unit(unit)
            for row in self.db.execute("SELECT body FROM jobs").fetchall():
                job = json.loads(row[0])
                if job["id"] not in affected_jobs and job["status"] not in {"running", "queued", "cancelling"}:
                    continue
                if job.get("cancel_requested_at"):
                    for u in self._units(job["id"]):
                        if u["status"] == "queued":
                            u.update(status="cancelled", ended_at=now(), error={"code": "cancelled_before_dispatch", "message": "评分请求未发送"})
                            self._save_unit(u)
                self._refresh(job)

    def _units(self, job_id):
        return [json.loads(r[0]) for r in self.db.execute("SELECT body FROM units WHERE job_id=? ORDER BY ordinal", (job_id,))]

    def _evaluator_snapshot(self, evaluator):
        frozen = copy.deepcopy(evaluator)
        frozen["implementation"] = copy.deepcopy(self.implementation)
        if frozen.get("validation_profile") == live_typed.PROFILE_ID:
            frozen["live_profile"] = copy.deepcopy(self.live_profile)
        return frozen

    def _frozen_live_profile(self, evaluator):
        profile_id = evaluator.get("validation_profile")
        if profile_id is None and evaluator.get("prompt_profile") is None:
            return None  # Legacy v1 jobs retain their original prompt and validator.
        if (profile_id != live_typed.PROFILE_ID
                or evaluator.get("prompt_profile") != live_typed.PROFILE_ID
                or evaluator.get("live_profile") != self.live_profile):
            raise ValueError("冻结的实时评分 profile 与已加载实现不一致，模型请求未发送")
        return evaluator["live_profile"]

    def list_evaluators(self):
        from .provider import available
        availability = available()
        snapshots = [self._evaluator_snapshot(item) for item in EVALUATORS]
        return [item | {"hash": digest(item),
                 "availability": {**availability, "loaded_source_current": self.implementation == _implementation()},
                 "concurrency": {"service_limit": self.concurrency, "maximum": 4},
                 "effective": {"model": "unknown", "reasoning_effort": "unknown"},
                 "limitations": ["AI评阅，不代表真实用户意见。", "不同模型或独立调用不等于统计独立样本。"]} for item in snapshots]

    def list_calibrations(self):
        return list_calibrations()

    def _targets(self, body, experiment):
        explicit = body.get("targets")
        split = body.get("split", "all")
        if split not in {"all", "development", "holdout"}:
            raise ValueError("split 必须为 development、holdout 或 all")
        if explicit is None:
            explicit = [{"trial_id": item["id"]} for item in self.store.trials(experiment["id"]) if split == "all" or _split(item["case"]) == split]
        if not isinstance(explicit, list) or not 1 <= len(explicit) <= 4000:
            raise ValueError("请选择 1–4000 个具体结果")
        targets, seen = [], set()
        for selection in explicit:
            if not isinstance(selection, dict) or not isinstance(selection.get("trial_id"), str):
                raise ValueError("每个 target 需要 trial_id")
            trial = self.store.trial(selection["trial_id"])
            if trial["experiment_id"] != experiment["id"]:
                raise ValueError("评分目标不属于所选实验")
            attempt_number = selection.get("execution_attempt", trial["attempt"])
            if type(attempt_number) is not int:
                raise ValueError("execution_attempt 必须是整数")
            candidates = trial.get("attempts", []) or [trial]
            attempt = next((a for a in candidates if a["attempt"] == attempt_number), None)
            if attempt is None:
                raise ValueError("所选历史 attempt 不存在")
            if attempt["status"] not in TERMINAL:
                raise ValueError("目标仍在执行，进入终态后才能建立评分作业")
            result_hash = digest(attempt.get("result"))
            if selection.get("result_hash") is not None and selection["result_hash"] != result_hash:
                raise ValueError("评分目标 result_hash 已变化")
            identity = (trial["id"], attempt_number)
            if identity in seen:
                raise ValueError("重复选择了同一 attempt")
            seen.add(identity)
            case = copy.deepcopy(attempt.get("case", trial["case"]))
            if split != "all" and _split(case) != split:
                raise ValueError("显式 target 与所选 split 不一致")
            # A full result package is NEVER a fallback for the delivered blind projection.
            result = attempt.get("result") or {}
            archived_media = result.get("media")
            if archived_media is None and isinstance(result.get("review_output"), dict):
                archived_media = result["review_output"].get("media")
            if archived_media is not None and not isinstance(archived_media, list):
                raise ValueError("所选历史 attempt 的 media 快照清单格式错误")
            targets.append({"trial_id": trial["id"], "execution_attempt": attempt_number, "result_hash": result_hash,
                            "case_id": trial["case_id"], "family_id": case.get("family_id", trial["case_id"]),
                            "configuration_id": trial["configuration_id"], "repeat_index": trial["repeat_index"],
                            "status": attempt["status"], "output": copy.deepcopy(result.get("review_output")),
                            "output_projection": "result.review_output", "review_output_hash": digest(result.get("review_output")),
                            "attempt_media": copy.deepcopy(archived_media) if archived_media else None,
                            "attempt_root": str(self.store.attempt_root(attempt)),
                            "case": case, "configuration": copy.deepcopy(attempt.get("configuration", trial["configuration"]))})
        return targets

    def create_job(self, body):
        if self.closed or self.closing:
            raise ValueError("评分服务已关闭")
        if self.implementation != _implementation():
            raise ValueError("评分器文件在服务启动后发生变化，请重启服务再冻结新评分作业")
        if not isinstance(body, dict):
            raise ValueError("评分作业必须是对象")
        evaluator_id = body.get("evaluator_id", "codex-astra-v1")
        evaluator = next((copy.deepcopy(e) for e in EVALUATORS if e["id"] == evaluator_id), None)
        if not evaluator:
            raise ValueError("未知评分器；请选择版本化 evaluator_id")
        evaluator = self._evaluator_snapshot(evaluator)
        live_profile = self._frozen_live_profile(evaluator)
        timeout = _integer(body.get("timeout_seconds"), 180, 5, 1800, "评分超时秒数")
        budget_input = body.get("budget") or {}
        if not isinstance(budget_input, dict):
            raise ValueError("budget 必须是对象")
        budget = {"max_units": _integer(budget_input.get("max_units"), 200, 1, 2000, "最多评分单位"),
                  "max_elapsed_seconds": _integer(budget_input.get("max_elapsed_seconds"), 3600, 5, 86400, "作业最长秒数"),
                  "max_total_tokens": _integer(budget_input.get("max_total_tokens"), 200000, 1, 5000000, "评分token预算")}
        mode = body.get("mode", "individual")
        if mode not in {"individual", "pairwise"}:
            raise ValueError("mode 必须为 individual 或 pairwise")
        anchor_set = None
        if body.get("calibration_id"):
            if body.get("experiment_id") or body.get("targets"):
                raise ValueError("内置校准作业不能同时指定产品实验")
            anchor_set = calibration(body["calibration_id"])
            if mode != "individual" or body.get("rubric") or body.get("rubric_id") not in (None, anchor_set["rubric_id"]):
                raise ValueError("内置校准使用固定量表与 individual 模式")
            rubric = rubric_snapshot(rubric_id=anchor_set["rubric_id"])
            experiment = {"id": None, "project": "calibration", "configurations": [], "pack": {}}
            targets = [{"trial_id": None, "execution_attempt": 1, "result_hash": digest(c["output"]),
                        "case_id": c["id"], "family_id": c["family_id"], "configuration_id": None, "repeat_index": 1,
                        "status": "succeeded", "output": c["output"], "output_projection": "synthetic_calibration_delivery",
                        "review_output_hash": digest(c["output"]), "case": {k: v for k, v in c.items() if k not in {"output", "expected_scores"}},
                        "configuration": {}, "expected_scores": c["expected_scores"]} for c in anchor_set["cases"]]
        else:
            if not isinstance(body.get("experiment_id"), str):
                raise ValueError("评分作业需要 experiment_id 或 calibration_id")
            experiment = self.store.experiment(body["experiment_id"])
            targets = self._targets(body, experiment)
            rubric = rubric_snapshot(body.get("rubric"), body.get("rubric_id"))
        calibration_meta = ({k: v for k, v in anchor_set.items() if k != "cases"} if anchor_set else copy.deepcopy(body.get("calibration")))
        if calibration_meta is not None:
            if not isinstance(calibration_meta, dict) or calibration_meta.get("kind") not in {"internal_ai_anchor", "human_gold"} or not isinstance(calibration_meta.get("provenance"), str) or not calibration_meta["provenance"].strip():
                raise ValueError("校准须声明 kind 与真实来源 provenance；无真人金标不能称 human_gold")
        grouped = []
        if mode == "individual":
            grouped = [{"A": target} for target in targets]
        else:
            config_ids = [c["id"] for c in experiment.get("configurations", [])]
            baseline = experiment.get("baseline_id") or (config_ids[0] if config_ids else None)
            lookup = {}
            for target in targets:
                key = (target["case_id"], target["repeat_index"], target["configuration_id"])
                if key in lookup:
                    raise ValueError("配对作业每个 case/repeat/config 只能选择一个 attempt")
                lookup[key] = target
            used = set()
            for target in targets:
                if target["configuration_id"] == baseline:
                    continue
                base = lookup.get((target["case_id"], target["repeat_index"], baseline))
                if not base:
                    raise ValueError("每个候选结果必须显式包含相同 case/repeat 的基线结果")
                pair = [base, target]
                secrets.SystemRandom().shuffle(pair)
                grouped.append(dict(zip(("A", "B"), pair)))
                used.update(id(t) for t in pair)
            if any(id(t) not in used for t in targets):
                raise ValueError("存在没有候选配对的基线 target；请调整选择")
        if not grouped or len(grouped) > budget["max_units"]:
            raise ValueError("评分单位为空或超过本次 max_units；请明确调整范围或预算")
        job_id = uid()
        created = now()
        job = {"id": job_id, "schema_version": "myeval.evaluation-job.v2", "experiment_id": experiment["id"],
               "project": experiment["project"], "title": str(body.get("title") or (anchor_set["name"] if anchor_set else "独立评分"))[:160],
               "created_at": created, "started_at": None, "ended_at": None, "status": "queued", "mode": mode,
               "split": body.get("split", "all"), "evaluator": evaluator, "evaluator_hash": digest(evaluator),
               "rubric": rubric, "rubric_hash": digest(rubric), "rubric_version": rubric.get("version", rubric["id"]),
               "timeout_seconds": timeout, "budget": budget, "calibration": calibration_meta,
               "concurrency": _integer(body.get("concurrency"), 2, 1, 4, "作业评分并发"),
               "cancel_requested_at": None, "counts": {"queued": len(grouped)},
               "unit_count": len(grouped),
               "usage": {"known_total_tokens": 0, "unknown_units": 0, "model_requests": 0, "cost": None},
               "limitations": ["评分是独立AI判断，需核对证据，不等于真人评价。", "评分耗用与产品执行耗用分开；未知费用不记零。", "token预算基于已完成请求用量，最多当前并发数个在途请求可能超出余额。", "配对评分会受对照对象影响；每个候选使用对应配对内的基线评分。"]}
        if live_profile is not None:
            job.update(operation_kind="model_judgment", validation_profile=copy.deepcopy(live_profile))
        units = []
        for ordinal, sides in enumerate(grouped):
            first = next(iter(sides.values()))
            if any(digest(s["case"]) != digest(first["case"]) for s in sides.values()):
                raise ValueError("配对案例快照不一致")
            media = prepare_media(first["case"], first["configuration"], self.store.directory, experiment["project"],
                                  experiment.get("authorization") or experiment.get("pack", {}).get("authorization"),
                                  attempt_media=first.get("attempt_media"), attempt_root=first.get("attempt_root"))
            for other in list(sides.values())[1:]:
                other_media = prepare_media(other["case"], other["configuration"], self.store.directory, experiment["project"],
                                            experiment.get("authorization") or experiment.get("pack", {}).get("authorization"),
                                            attempt_media=other.get("attempt_media"), attempt_root=other.get("attempt_root"))
                if [m["sha256"] for m in other_media] != [m["sha256"] for m in media]:
                    raise ValueError("两侧冻结媒体身份不一致，不能作为同输入配对评分")
            if media and not evaluator["vision"]:
                raise ValueError("该评分器未声明支持视觉附件")
            payload, removed = neutral_payload(first["case"], sides, media)
            mapping = {label: {k: copy.deepcopy(t[k]) for k in ("trial_id", "execution_attempt", "result_hash", "case_id", "family_id", "configuration_id", "repeat_index", "status", "output_projection", "review_output_hash")} for label, t in sides.items()}
            units.append({"id": uid(), "job_id": job_id, "ordinal": ordinal, "status": "queued", "created_at": created,
                          "started_at": None, "ended_at": None, "case_id": first["case_id"], "family_id": first["family_id"],
                          "split": first["case"].get("split", "development"), "mapping": mapping,
                          "payload": payload, "payload_hash": digest(payload), "removed_metadata_paths": removed,
                          "media": media, "expected_scores": first.get("expected_scores"),
                          "output": None, "error": None, "usage": None, "request_sent": False,
                          "assessment_record_ids": [], "validation_issues": [], "review_flags": []})
            if live_profile is not None:
                units[-1].update(operation_kind="model_judgment", validation_profile=copy.deepcopy(live_profile),
                                 normalized_output=None, typed_evidence=[], transform_log=[], legacy_validation_issues=[])
        with self.lock, self.db:
            if self.closed or self.closing:
                raise ValueError("评分服务已关闭")
            self.db.execute("INSERT INTO jobs VALUES(?,?,?,?,?)", (job_id, experiment["id"], "queued", created, dumps(job)))
            for unit in units:
                self.db.execute("INSERT INTO units VALUES(?,?,?,?,?)", (unit["id"], job_id, unit["status"], unit["ordinal"], dumps(unit)))
        try:
            write_json(self.directory / "jobs" / job_id / "job.json", job)
        except OSError as error:
            with self.lock, self.db:
                latest = self._row("jobs", job_id)
                latest["artifact_error"] = {"code": "job_artifact_write_failed", "type": type(error).__name__, "database_record_preserved": True}
                self._save_job(latest)
        self.wake_event.set()
        return self.get_job(job_id)

    def _refresh(self, job):
        units = self._units(job["id"])
        counts = Counter(u["status"] for u in units)
        usages = [_usage_total(u.get("usage")) for u in units if u.get("request_sent")]
        job["counts"] = dict(counts)
        job["unit_count"] = len(units)
        job["usage"] = {"known_total_tokens": sum(x for x in usages if x is not None),
                        "unknown_units": sum(x is None for x in usages), "model_requests": len(usages),
                        "dispatch_attempts": len(usages), "unknown_dispatch_units": sum(u.get("request_sent") == "unknown" for u in units),
                        "confirmed_model_completions": sum(bool(((u.get("provider") or {}).get("runtime") or {}).get("turn_completed")) for u in units), "cost": None}
        if counts.get("running") or counts.get("committing"):
            job["status"] = "cancelling" if job.get("cancel_requested_at") else "running"
        elif counts.get("queued"):
            job["status"] = "queued"
        else:
            if job.get("cancel_requested_at"):
                job["status"] = "cancelled"
            elif counts.get("budget_exhausted"):
                job["status"] = "budget_exhausted"
            elif counts.get("interrupted") and len(counts) == 1:
                job["status"] = "interrupted"
            elif set(counts) <= {"succeeded"}:
                job["status"] = "succeeded"
            else:
                job["status"] = "completed_with_errors"
            job["ended_at"] = job.get("ended_at") or now()
        self._save_job(job)
        return job

    def get_job(self, identifier):
        with self.lock:
            job = self._row("jobs", identifier)
            job["units"] = self._units(identifier)
            job["adjudications"] = [json.loads(r[0]) for r in self.db.execute("SELECT body FROM adjudications WHERE job_id=? ORDER BY rowid", (identifier,))]
            return job

    def list_jobs(self, experiment_id=None):
        with self.lock:
            rows = self.db.execute("SELECT body FROM jobs WHERE (? IS NULL OR experiment_id=?) ORDER BY created_at DESC", (experiment_id, experiment_id)).fetchall()
        return [{k: v for k, v in json.loads(row[0]).items() if k != "rubric"} for row in rows]

    def cancel_job(self, identifier):
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            job = self._row("jobs", identifier)
            if job["status"] not in {"queued", "running", "cancelling"}:
                return self.get_job(identifier)
            job["cancel_requested_at"] = job.get("cancel_requested_at") or now()
            for unit in self._units(identifier):
                if unit["status"] == "queued":
                    unit.update(status="cancelled", ended_at=now(), error={"code": "cancelled_before_dispatch", "message": "评分请求未发送"},
                                cancellation={"local": "not_started", "upstream": "not_sent"})
                    self._save_unit(unit)
            for active in self.active.values():
                if active["job_id"] == identifier:
                    active["cancel"].set()
            self._refresh(job)
        self.wake_event.set()
        return self.get_job(identifier)

    def _claim(self):
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.stop_event.is_set() or len(self.active) >= self.concurrency:
                return None
            selected = None
            for row in self.db.execute("SELECT body FROM units WHERE status='queued' ORDER BY rowid").fetchall():
                possible = json.loads(row[0])
                possible_job = self._row("jobs", possible["job_id"])
                active_count = sum(a["job_id"] == possible_job["id"] for a in self.active.values())
                if active_count < possible_job.get("concurrency", 2):
                    selected = possible_job, possible
                    break
            if selected is None:
                return None
            job, unit = selected
            if job.get("cancel_requested_at"):
                unit.update(status="cancelled", ended_at=now(), error={"code": "cancelled_before_dispatch", "message": "评分请求未发送"})
                self._save_unit(unit); self._refresh(job)
                return None
            elapsed = 0
            if job.get("started_at"):
                elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(job["started_at"])).total_seconds()
            if elapsed >= job["budget"]["max_elapsed_seconds"] or job["usage"]["known_total_tokens"] >= job["budget"]["max_total_tokens"]:
                for pending in self._units(job["id"]):
                    if pending["status"] == "queued":
                        pending.update(status="budget_exhausted", ended_at=now(), error={"code": "judge_budget_exhausted", "message": "评分预算已达界限，后续请求未发送"})
                        self._save_unit(pending)
                self._refresh(job)
                return None
            job["started_at"] = job.get("started_at") or now()
            unit.update(status="running", started_at=now())
            cancel = threading.Event()
            self.active[unit["id"]] = {"job_id": job["id"], "cancel": cancel}
            self._save_unit(unit); self._refresh(job)
            return job, unit, cancel

    def _loop(self):
        while not self.stop_event.is_set():
            claimed = self._claim()
            if not claimed:
                self.wake_event.wait(.25); self.wake_event.clear()
                continue
            job, unit, cancel = claimed
            try:
                self._execute(job, unit, cancel)
            except Exception as error:
                pending = unit.get("accepted_at") and job.get("experiment_id")
                unit.update(status="commit_pending" if pending else "failed",
                            error={"code": "assessment_commit_pending" if pending else "evaluation_internal_error", "message": safe_error(error)}, ended_at=now())
            finally:
                with self.lock, self.db:
                    if self.stop_event.is_set() and unit["status"] in {"running", "cancelled"} and not self._row("jobs", job["id"]).get("cancel_requested_at"):
                        unit["status"] = "interrupted"
                        unit["error"] = {"code": "service_closed", "message": "评分服务关闭；未自动重发"}
                    self._save_unit(unit)
                    self._refresh(self._row("jobs", job["id"]))
                    self.active.pop(unit["id"], None)
                try:
                    write_json(self.directory / "jobs" / job["id"] / "units" / unit["id"] / "unit.json", unit)
                except OSError as error:
                    with self.lock, self.db:
                        unit["artifact_error"] = {"code": "unit_artifact_write_failed", "type": type(error).__name__, "database_record_preserved": True}
                        self._save_unit(unit)

    def _execute(self, job, unit, cancel):
        from .provider import run_codex
        started = time.monotonic()
        if job["evaluator"].get("implementation") != self.implementation or self.implementation != _implementation():
            unit.update(status="failed", error={"code": "evaluation_source_changed", "message": "评分器来源在冻结后发生变化，模型请求未发送；请创建新作业。"}, ended_at=now())
            return
        live_profile = self._frozen_live_profile(job["evaluator"])
        if any(t["status"] != "succeeded" for t in unit["mapping"].values()):
            unit.update(status="unavailable", error={"code": "product_not_succeeded", "message": "产品至少一侧未成功，未调用评分模型；执行失败仍进入完整分母。"}, ended_at=now())
            return
        if any(s["output"] is None for s in unit["payload"]["sides"].values()):
            unit.update(status="unavailable", error={"code": "missing_review_output", "message": "缺少适配器安全交付投影 review_output，未将完整运行包发送评分器。"}, ended_at=now())
            return
        # Historical attempts are rechecked without looking at any newer delivery.
        for target in unit["mapping"].values():
            if target["trial_id"]:
                current = self.store.trial(target["trial_id"])
                historical = next((a for a in (current.get("attempts") or [current]) if a["attempt"] == target["execution_attempt"]), None)
                if historical is None or digest(historical.get("result")) != target["result_hash"]:
                    unit.update(status="failed", error={"code": "frozen_result_changed", "message": "冻结的历史结果发生变化，评分请求未发送。"}, ended_at=now())
                    return
        media_paths = []
        for item in unit["media"]:
            path = Path(self.store.directory) / item["artifact_path"]
            if path.is_symlink() or not path.resolve().is_relative_to(self.directory) or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                raise ValueError("冻结的评分图片缓存发生变化")
            media_paths.append(path)
        folder = self.directory / "jobs" / job["id"] / "units" / unit["id"]
        schema = judgment_schema(job["rubric"], unit["mapping"])
        prompt = (live_typed.prompt_for if live_profile is not None else prompt_for)(job["rubric"], unit["payload"])
        input_record = {"payload": unit["payload"], "payload_hash": unit["payload_hash"], "rubric_hash": job["rubric_hash"], "media": unit["media"]}
        if live_profile is not None:
            input_record.update(operation_kind="model_judgment", validation_profile=copy.deepcopy(live_profile))
        write_json(folder / "input.json", input_record)
        remaining = job["budget"]["max_elapsed_seconds"] - (datetime.now(timezone.utc) - datetime.fromisoformat(job["started_at"])).total_seconds()
        if remaining <= 0:
            unit.update(status="budget_exhausted", ended_at=now(), error={"code": "judge_budget_exhausted", "message": "材料准备后时间预算已耗尽，模型请求未发送。"})
            return
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if cancel.is_set() or self.stop_event.is_set() or self._row("jobs", job["id"]).get("cancel_requested_at"):
                unit.update(status="cancelled", ended_at=now(), error={"code": "cancelled_before_dispatch", "message": "取消在发送意图之前生效，评分请求未发送。"})
                return
            unit.update(request_sent="unknown", dispatch_intent_at=now(), dispatch_state="launch_intent_recorded")
            self._save_unit(unit)
        watch_done = threading.Event()
        def watch_cancellation():
            while not watch_done.wait(.2):
                with self.lock:
                    requested = self._row("jobs", job["id"]).get("cancel_requested_at")
                if requested or self.stop_event.is_set():
                    cancel.set()
                    return
        watcher = threading.Thread(target=watch_cancellation, name="myeval-judge-cancel", daemon=True)
        watcher.start()
        try:
            provider = run_codex(prompt, schema, job["evaluator"], folder, cancel, max(1, min(job["timeout_seconds"], int(remaining))), media_paths)
        finally:
            watch_done.set()
            watcher.join(timeout=1)
        runtime = provider.get("runtime") or {}
        unit.update(status=provider["status"], output=provider.get("output"), usage=provider.get("usage"), provider=provider,
                    request_sent=True if runtime.get("turn_completed") else "unknown" if runtime.get("process_started") else False,
                    dispatch_state="completed" if runtime.get("turn_completed") else "local_process_started_remote_unknown" if runtime.get("process_started") else "not_dispatched",
                    error=provider.get("error"),
                    ended_at=now(), duration_ms=round((time.monotonic() - started) * 1000))
        if unit["status"] != "succeeded":
            return
        if cancel.is_set():
            unit.update(status="cancelled", error={"code": "cancelled_before_acceptance", "message": "取消后返回的评分未接纳"})
            return
        unit["output"], unit["pointer_normalizations"] = normalize_pointers(unit["output"], unit["payload"])
        if live_profile is None:
            issues = validate_judgment(unit["output"], job["rubric"], unit["payload"])
        else:
            # provider.output is the untouched raw response. Keep a second,
            # explicit normalized snapshot; the typed projection may only add
            # evidence metadata, never revise a score, reason, or preference.
            unit["normalized_output"] = copy.deepcopy(unit["output"])
            projection = live_typed.validate_judgment(unit["output"], job["rubric"], unit["payload"])
            if (digest(projection["original_judgment"]) != digest(unit["normalized_output"])
                    or digest(unit["output"]) != digest(unit["normalized_output"])):
                raise ValueError("实时结构证据验证改变了原判断，拒绝接纳")
            issues = projection["validation_issues"]
            unit.update(typed_evidence=copy.deepcopy(projection["typed_evidence"]),
                        transform_log=copy.deepcopy(projection["transform_log"]),
                        legacy_validation_issues=copy.deepcopy(projection["legacy_validation_issues"]),
                        validation_projection={"schema_version": projection["schema_version"],
                                               "validator": copy.deepcopy(projection["validator"]),
                                               "accepted": projection["accepted"]})
        unit["validation_issues"] = issues
        if issues:
            unit.update(status="invalid", error={"code": "invalid_judgment_evidence", "message": "评分结构或引文核验未通过；保留原评分供复核，未计入模型质量统计。"})
            return
        if unit["output"]["confidence"] == "low":
            unit["review_flags"].append("low_confidence")
        for label, side in unit["output"]["sides"].items():
            for violation in side["hard_violations"]:
                if violation["severity"] in {"major", "critical"} or violation["disputed"]:
                    unit["review_flags"].append({"side": label, "severity": violation["severity"], "disputed": violation["disputed"]})
        with self.lock:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                if cancel.is_set() or self.stop_event.is_set() or self._row("jobs", job["id"]).get("cancel_requested_at"):
                    unit.update(status="cancelled", error={"code": "cancelled_before_acceptance", "message": "取消后返回的评分未接纳"})
                    self._save_unit(unit)
                    return
                unit.update(status="committing" if job["experiment_id"] else "succeeded", accepted_at=now())
                if job["experiment_id"]:
                    unit["assessment_items"] = self._assessment_items(job, unit)
                    unit["assessment_plan_hash"] = digest(unit["assessment_items"])
                self._save_unit(unit)
            if job["experiment_id"] is None:
                return  # Anchors share the same acceptance boundary, without product score records.
            self._persist_assessments(job, unit)
            unit["status"] = "succeeded"

    def _persist_assessments(self, job, unit):
        batch = getattr(self.store, "add_assessments", None)
        if not callable(batch):
            raise ValueError("核心缺少原子幂等 add_assessments，已保存评分但未写入任何评分记录；升级核心后重启可恢复")
        items = unit.get("assessment_items") or self._assessment_items(job, unit)
        if unit.get("assessment_plan_hash") and digest(items) != unit["assessment_plan_hash"]:
            raise ValueError("冻结的评分提交计划哈希不一致，未写入任何记录")
        records = batch(items)
        for item, record in zip(items, records):
            target = item["body"]
            if record.get("execution_attempt") != target["execution_attempt"] or record.get("result_hash") != target["result_hash"]:
                raise ValueError("Store 未按冻结的历史 attempt 身份保存评分，请升级核心接口后新建作业")
        if len(records) != len(items):
            raise ValueError("原子评分批次返回记录数不匹配")
        unit["assessment_record_ids"] = [record["id"] for record in records]

    def _assessment_items(self, job, unit):
        items = []
        for label, side in unit["output"]["sides"].items():
            target = unit["mapping"][label]
            value = {"schema_version": SCHEMA_VERSION, "scores": side["scores"], "score_status": side["score_status"],
                     "hard_violations": side["hard_violations"], "job_id": job["id"], "unit_id": unit["id"],
                     "evaluator_hash": job["evaluator_hash"], "rubric_hash": job["rubric_hash"], "payload_hash": unit["payload_hash"],
                     "evaluator_id": job["evaluator"]["id"], "rubric_id": job["rubric"]["id"],
                     "pointer_normalizations": unit.get("pointer_normalizations", []),
                     "media": unit["media"], "provider_usage": unit.get("usage"), "confidence": unit["output"]["confidence"]}
            if job["evaluator"].get("validation_profile") == live_typed.PROFILE_ID:
                value.update(operation_kind="model_judgment",
                             validation_profile=copy.deepcopy(job["evaluator"]["live_profile"]),
                             prompt_profile=job["evaluator"]["prompt_profile"],
                             typed_evidence=[copy.deepcopy(item) for item in unit.get("typed_evidence", []) if item.get("side") == label],
                             transform_log=[copy.deepcopy(item) for item in unit.get("transform_log", []) if item.get("side") == label])
            if len(unit["mapping"]) == 2:
                other_label = next(k for k in unit["mapping"] if k != label)
                other = unit["mapping"][other_label]
                choice = unit["output"]["preference"]["choice"]
                value["preference"] = {"unit_id": unit["id"], "choice": "self" if choice == label else "other" if choice == other_label else choice,
                                       "reason": unit["output"]["preference"]["reason"], "other_trial_id": other["trial_id"],
                                       "other_attempt": other["execution_attempt"], "other_result_hash": other["result_hash"]}
            items.append({"trial_id": target["trial_id"], "body": {"kind": "model", "evaluator": job["evaluator"]["id"] + "@" + job["evaluator_hash"][:12],
                       "rubric_version": job["rubric"]["id"] + "@" + str(job["rubric_version"]) + "@" + job["rubric_hash"][:12], "reason": side["reason"],
                       "execution_attempt": target["execution_attempt"], "result_hash": target["result_hash"],
                       "evaluation_job_id": job["id"], "evaluator_snapshot": job["evaluator"],
                       "idempotency_key": unit["id"] + ":" + label,
                       "input_hash": unit["payload_hash"], "rubric_hash": job["rubric_hash"],
                       "value": value, "evidence": side["evidence"]}})
        return items

    def review_queue(self, experiment_id=None):
        result = []
        for summary in self.list_jobs(experiment_id):
            job = self.get_job(summary["id"])
            reviewed = {r["unit_id"] for r in job["adjudications"] if r["disposition"] in {"accepted", "rejected"}}
            for unit in job["units"]:
                if unit["status"] in {"failed", "timed_out", "interrupted", "invalid", "commit_pending"} or unit.get("review_flags"):
                    result.append({"job_id": job["id"], "unit_id": unit["id"], "experiment_id": job["experiment_id"],
                                   "case_id": unit["case_id"], "status": unit["status"], "error": unit.get("error"),
                                   "flags": unit.get("review_flags", []), "validation_issues": unit.get("validation_issues", []),
                                   "adjudicated": unit["id"] in reviewed})
        return result

    def adjudicate(self, job_id, unit_id, body):
        if body.get("kind") not in {"human", "acceptance"} or body.get("disposition") not in {"accepted", "rejected", "needs_followup"} or not isinstance(body.get("reason"), str) or not body["reason"].strip():
            raise ValueError("复核需明确 human/acceptance、disposition 和理由")
        with self.lock, self.db:
            unit = self._row("units", unit_id)
            if unit["job_id"] != job_id:
                raise ValueError("评分单位不属于该作业")
            record = {"id": uid(), "job_id": job_id, "unit_id": unit_id, "created_at": now(),
                      "kind": body["kind"], "disposition": body["disposition"], "reason": body["reason"][:10000],
                      "reviewer": str(body.get("reviewer", "本机管理员"))[:100],
                      "note": "仅保存复核意见，不覆盖原评分，也不将无效评分自动改为有效。"}
            self.db.execute("INSERT INTO adjudications VALUES(?,?,?,?)", (record["id"], job_id, unit_id, dumps(record)))
        return record

    def calibration_report(self, job_id):
        job = self.get_job(job_id)
        if not job.get("calibration"):
            raise ValueError("该作业未声明校准来源")
        comparisons = []
        for unit in job["units"]:
            expected = unit.get("expected_scores") or (job["calibration"].get("expected") or {}).get(unit["case_id"], {})
            actual = (((unit.get("output") or {}).get("sides") or {}).get("A") or {}).get("scores", {})
            for dimension, anchor in expected.items():
                value = actual.get(dimension)
                valid = unit["status"] == "succeeded" and type(value) in (int, float)
                comparisons.append({"case_id": unit["case_id"], "dimension": dimension, "expected": anchor,
                                    "actual": value if valid else None, "available": valid,
                                    "matches": value == anchor if valid else None})
        available = [c for c in comparisons if c["available"]]
        return {"job_id": job_id, "kind": job["calibration"]["kind"], "provenance": job["calibration"]["provenance"],
                "comparisons": comparisons, "expected_cells": len(comparisons), "rated_cells": len(available),
                "exact_matches": sum(c["matches"] for c in available), "missing_cells": len(comparisons) - len(available),
                "agreement": sum(c["matches"] for c in available) / len(available) if available else None,
                "limitations": ["描述性规范锚点一致率，不是业务有效性或真实用户一致率。", "分项和重复不构成独立样本；分歧保留。"]}

    def export_experiment(self, experiment_id):
        jobs = [self.get_job(j["id"]) for j in self.list_jobs(experiment_id)]
        return {"schema_version": "myeval.evaluation-export.v2", "jobs": jobs,
                "evaluators": list({j["evaluator_hash"]: j["evaluator"] for j in jobs}.values()),
                "review_queue": self.review_queue(experiment_id), "audience": "administrator_unblinded"}

    def close(self):
        if self.closed:
            return
        self.closing = True
        self.stop_event.set()
        self.wake_event.set()
        with self.lock:
            for active in self.active.values():
                active["cancel"].set()
        deadline = time.monotonic() + 15
        for worker in self.workers:
            if worker.is_alive():
                worker.join(timeout=max(0, deadline - time.monotonic()))
        if any(worker.is_alive() for worker in self.workers):
            raise RuntimeError("评分进程未及时退出；保留数据库连接以防证据丢失")
        with self.lock:
            self.db.close()
        if self.owner_lock:
            fcntl.flock(self.owner_lock.fileno(), fcntl.LOCK_UN)
            self.owner_lock.close()
        self.closed = True
