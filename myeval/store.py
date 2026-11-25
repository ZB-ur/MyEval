from __future__ import annotations

import json
import math
import hashlib
import base64
import secrets
import sqlite3
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .catalog import case_family, freeze
from .performance import aggregate_performance, measure_attempt
from .util import TERMINAL, digest, dumps, now, read_events, uid, write_json


BLIND_CASE_FIELDS = (
    "name", "task", "family_id", "variant", "split", "input", "initial_state", "source",
    "expected_behavior", "allowed_facts", "forbidden_inferences", "turn_focus",
    "expected_background_texts", "allowed_background_texts", "expected_history_ids", "summary_forbidden_fragments",
)


_BLIND_METADATA_KEYS = {
    "configuration", "configurations", "configuration_id", "configuration_name", "model", "model_id", "provider",
    "prompt", "system_prompt", "developer_prompt", "provenance", "source_root", "project_root", "root",
    "path", "paths", "filename", "file_name", "local_file", "trial_id", "experiment_id", "trace_id", "request_id",
    "authorization", "authorization_hash", "api_key", "access_token", "usage", "cost", "latency_ms", "duration_ms",
    "raw_response", "reasoning_content", "thinking_content", "hidden_reasoning", "chain_of_thought",
}


def _neutral_projection(value):
    """Strip declared metadata keys recursively; preserve every remaining string."""
    if isinstance(value, list):
        return [_neutral_projection(item) for item in value]
    if isinstance(value, dict):
        return {key: _neutral_projection(item) for key, item in value.items()
                if key.lower() not in _BLIND_METADATA_KEYS and not key.lower().endswith(("_path", "_paths"))}
    return value


def _blind_case(value):
    return _neutral_projection(value)


def _blind_output(value):
    return _neutral_projection(value)


class StalePresentation(ValueError):
    pass


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._performance_cache = {}
        self.db = sqlite3.connect(self.directory / "myeval.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS experiments(id TEXT PRIMARY KEY, project TEXT, created_at TEXT, body TEXT);
            CREATE TABLE IF NOT EXISTS trials(id TEXT PRIMARY KEY, experiment_id TEXT REFERENCES experiments(id), status TEXT, body TEXT);
            CREATE TABLE IF NOT EXISTS attempts(trial_id TEXT REFERENCES trials(id), number INTEGER, body TEXT, PRIMARY KEY(trial_id,number));
            CREATE TABLE IF NOT EXISTS reviewers(token TEXT PRIMARY KEY, experiment_id TEXT REFERENCES experiments(id), label TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS presentations(id TEXT PRIMARY KEY, token TEXT REFERENCES reviewers(token), case_id TEXT, repeat_index INTEGER, left_id TEXT, right_id TEXT);
            CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY, presentation_id TEXT UNIQUE REFERENCES presentations(id), body TEXT);
            CREATE TABLE IF NOT EXISTS assessment_records(id TEXT PRIMARY KEY, trial_id TEXT REFERENCES trials(id), body TEXT);
            CREATE TABLE IF NOT EXISTS assessment_keys(job_id TEXT, idempotency_key TEXT, trial_id TEXT REFERENCES trials(id), record_id TEXT REFERENCES assessment_records(id), payload_hash TEXT, PRIMARY KEY(job_id,idempotency_key));
            CREATE TABLE IF NOT EXISTS settings(id TEXT PRIMARY KEY, body TEXT);
            CREATE TABLE IF NOT EXISTS imports(id TEXT PRIMARY KEY, created_at TEXT, body TEXT);
            CREATE TABLE IF NOT EXISTS historical_reviews(id TEXT PRIMARY KEY, experiment_id TEXT REFERENCES experiments(id), body TEXT);
            CREATE TABLE IF NOT EXISTS historical_evidence(id TEXT PRIMARY KEY, experiment_id TEXT REFERENCES experiments(id), body TEXT);
            CREATE INDEX IF NOT EXISTS trials_experiment_status ON trials(experiment_id,status);
            CREATE INDEX IF NOT EXISTS trials_status ON trials(status);
            CREATE INDEX IF NOT EXISTS assessments_trial ON assessment_records(trial_id);
            CREATE INDEX IF NOT EXISTS reviewers_experiment ON reviewers(experiment_id);
            CREATE INDEX IF NOT EXISTS presentations_token ON presentations(token);
        """)
        self.db.commit()

    @property
    def datasets(self):
        from .datasets import DatasetRegistry
        if not hasattr(self, "_datasets"):
            self._datasets = DatasetRegistry(self)
        return self._datasets

    @property
    def revalidations(self):
        from .receipts import RevalidationReceipts
        if not hasattr(self, "_revalidations"):
            self._revalidations = RevalidationReceipts(self.directory)
        return self._revalidations

    def queue_settings(self):
        defaults = {"paused": False, "concurrency": 2,
                    "limits": {"max_trials_per_experiment": 5000, "max_queued_trials": 20000,
                               "max_timeout_seconds": 86400, "max_repeats": 20,
                               "max_attempts_per_trial": 3, "max_concurrency": 32}}
        with self.lock:
            row = self.db.execute("SELECT body FROM settings WHERE id='queue'").fetchone()
        if row:
            defaults.update(json.loads(row[0]))
        return defaults

    def configure_queue(self, body):
        if not isinstance(body, dict) or any(k not in {"paused", "concurrency", "limits"} for k in body):
            raise ValueError("队列设置仅接受 paused、concurrency、limits")
        with self.lock, self.db:
            settings = self.queue_settings()
            if "paused" in body:
                if type(body["paused"]) is not bool:
                    raise ValueError("paused 必须为布尔值")
                settings["paused"] = body["paused"]
            limits = body.get("limits", {})
            if not isinstance(limits, dict) or set(limits) - set(settings["limits"]):
                raise ValueError("未知队列限制")
            hard_bounds = {"max_trials_per_experiment": (2, 50000), "max_queued_trials": (2, 100000),
                           "max_timeout_seconds": (5, 604800), "max_repeats": (1, 100),
                           "max_attempts_per_trial": (1, 20), "max_concurrency": (1, 32)}
            for key, value in limits.items():
                low, high = hard_bounds[key]
                if type(value) is not int or not low <= value <= high:
                    raise ValueError(f"{key} 必须为 {low}–{high} 的整数")
                settings["limits"][key] = value
            concurrency = body.get("concurrency", settings["concurrency"])
            if type(concurrency) is not int or not 1 <= concurrency <= settings["limits"]["max_concurrency"]:
                raise ValueError("并发必须为 1–max_concurrency 的整数，最大 32")
            settings["concurrency"] = concurrency
            settings["updated_at"] = now()
            self.db.execute("INSERT OR REPLACE INTO settings VALUES('queue',?)", (dumps(settings),))
        return self.queue_view()

    def queue_view(self):
        with self.lock:
            counts = dict(self.db.execute("SELECT status,count(*) FROM trials GROUP BY status").fetchall())
            active = [json.loads(r[0]) for r in self.db.execute("SELECT body FROM trials WHERE status IN ('running','cancelling')")]
        return {**self.queue_settings(), "counts": counts,
                "active": [{k: t.get(k) for k in ("id", "experiment_id", "configuration_id", "status", "started_at", "timeout_seconds")} for t in active],
                "experiments": [{k: e.get(k) for k in ("id", "title", "project", "status", "counts", "queue", "budget")} for e in self.list_experiments() if e["counts"].get("queued") or e["counts"].get("running") or e["counts"].get("cancelling")],
                "dispatch": "local_process_queue", "limitations": ["本机有界并发，不是分布式调度。", "已发出的供应商请求可能继续执行和计费；成本与 token 门槛按已观测值阻止后续调度。"]}

    def _row(self, table, identifier):
        row = self.db.execute(f"SELECT body FROM {table} WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise KeyError("记录不存在")
        return json.loads(row[0])

    def experiment(self, identifier):
        with self.lock:
            return self._row("experiments", identifier)

    def trial(self, identifier, attempts=True):
        with self.lock:
            result = self._row("trials", identifier)
            if attempts:
                result["attempts"] = [json.loads(r[0]) for r in self.db.execute(
                    "SELECT body FROM attempts WHERE trial_id=? ORDER BY number", (identifier,))]
                result["assessment_records"] = [json.loads(r[0]) for r in self.db.execute(
                    "SELECT body FROM assessment_records WHERE trial_id=?", (identifier,))]
                for record in result["assessment_records"]:
                    record["current_result"] = (record.get("execution_attempt") == result["attempt"]
                        and record.get("result_hash") == digest(result.get("result")))
            return result

    def trials(self, experiment_id=None):
        with self.lock:
            rows = self.db.execute("SELECT body FROM trials WHERE experiment_id=? ORDER BY rowid", (experiment_id,)) if experiment_id else self.db.execute("SELECT body FROM trials ORDER BY rowid")
            return [json.loads(r[0]) for r in rows]

    def _save_trial(self, value):
        self.db.execute("UPDATE trials SET status=?,body=? WHERE id=?", (value["status"], dumps(value), value["id"]))
        self.db.execute("INSERT OR REPLACE INTO attempts VALUES(?,?,?)", (value["id"], value["attempt"], dumps(value)))

    def update_trial(self, identifier, expected_attempt=None, **fields):
        with self.lock, self.db:
            value = self._row("trials", identifier)
            if expected_attempt is not None and value["attempt"] != expected_attempt:
                row = self.db.execute("SELECT body FROM attempts WHERE trial_id=? AND number=?", (identifier, expected_attempt)).fetchone()
                if not row:
                    raise KeyError("执行尝试不存在")
                historical = json.loads(row[0])
                historical.update(fields)
                self.db.execute("UPDATE attempts SET body=? WHERE trial_id=? AND number=?", (dumps(historical), identifier, expected_attempt))
            else:
                value.update(fields)
                self._save_trial(value)
        with self.lock:
            write_json(self.directory / "experiments" / value["experiment_id"] / "trials" / identifier / "trial.json", self.trial(identifier))
        return value

    def create_experiment(self, body):
        limits = self.queue_settings()["limits"]
        repeats = body.get("repeats", 1)
        timeout = body.get("timeout_seconds", 300)
        if type(repeats) is not int or not 1 <= repeats <= limits["max_repeats"]:
            raise ValueError(f"每配置重复次数为 1–{limits['max_repeats']}；重复不增加独立样本数")
        if type(timeout) is not int or not 5 <= timeout <= limits["max_timeout_seconds"]:
            raise ValueError(f"每 trial 超时必须在 5–{limits['max_timeout_seconds']} 秒内")
        if type(body.get("start_paused", False)) is not bool:
            raise ValueError("start_paused 必须为布尔值")
        selected_pack = None
        case_ids = body.get("case_ids")
        if body.get("dataset_id"):
            selected_pack = self.datasets.resolve_pack(body["dataset_id"], body.get("dataset_version_id"))
            split = body.get("split", "all")
            if split not in {"development", "holdout", "all"}:
                raise ValueError("split 必须为 development、holdout 或 all")
            split_ids = {c["id"] for c in selected_pack["cases"] if split == "all" or c.get("split", "development") == split}
            case_ids = case_ids if case_ids is not None else [c["id"] for c in selected_pack["cases"] if c["id"] in split_ids]
            if not isinstance(case_ids, list) or set(case_ids) - split_ids:
                raise ValueError("案例不属于所选数据集划分")
        if not isinstance(case_ids, list):
            raise ValueError("缺少案例选择")
        pack, cases, configs = freeze(body.get("project") or (selected_pack or {}).get("project"), case_ids,
                                     body.get("baseline_id"), body.get("candidate_id"),
                                     None if selected_pack else body.get("taskpack_id"),
                                     configuration_ids=body.get("configuration_ids"), pack_override=selected_pack,
                                     authorization=body.get("authorization"))
        total = len(cases) * repeats * len(configs)
        if total > limits["max_trials_per_experiment"]:
            raise ValueError(f"本机当前单实验上限 {limits['max_trials_per_experiment']} 个 trial；可在队列限制中显式调整")
        budget = body.get("budget") or {}
        if not isinstance(budget, dict) or set(budget) - {"max_trials", "max_attempts", "max_elapsed_seconds", "max_known_cost", "max_total_tokens", "max_concurrency"}:
            raise ValueError("实验预算字段不正确")
        for key, value in budget.items():
            if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{key} 必须为正的有限数值")
            if key != "max_known_cost" and type(value) is not int:
                raise ValueError(f"{key} 必须为整数")
        budget = {"max_trials": limits["max_trials_per_experiment"],
                  "max_attempts": total * limits["max_attempts_per_trial"], **budget}
        if total > budget["max_trials"] or total > budget["max_attempts"]:
            raise ValueError("计划 trial 数超过本实验预算；未创建或发送请求")
        if budget.get("max_concurrency", 1) > limits["max_concurrency"]:
            raise ValueError("实验并发超过本机队列上限")
        experiment = {"id": uid(), "created_at": now(), "project": pack["project"], "taskpack_id": pack["id"],
                      "title": str(body.get("title") or pack["name"])[:160], "purpose": str(body.get("purpose") or "synthetic_evaluation")[:200],
                      "pack": pack, "cases": cases, "configurations": configs, "repeats": repeats,
                      "timeout_seconds": timeout, "stop_requested_at": None, "schema_version": "myeval.v2",
                      "baseline_id": configs[0]["id"], "configuration_ids": [c["id"] for c in configs],
                      "candidate_ids": [c["id"] for c in configs[1:]],
                      "dataset": pack.get("dataset"), "split": body.get("split", "all"),
                      "budget": budget, "queue": {"paused": body.get("start_paused", False)},
                      "authorization": body.get("authorization") or pack.get("authorization")}
        from .snapshots import capture_sources
        experiment["source_snapshot"] = capture_sources(self, experiment)
        experiment["snapshot_hash"] = digest(experiment)
        with self.lock, self.db:
            queued = self.db.execute("SELECT count(*) FROM trials WHERE status IN ('queued','running','cancelling')").fetchone()[0]
            if queued + total > limits["max_queued_trials"]:
                raise ValueError("计划运行数超过本机待执行队列上限；未创建或发送请求")
            self.db.execute("INSERT INTO experiments VALUES(?,?,?,?)", (experiment["id"], experiment["project"], experiment["created_at"], dumps(experiment)))
            for case in cases:
                for repeat in range(1, repeats + 1):
                    # Shuffle launch order too; left/right assignment is separately randomized.
                    order = list(configs)
                    secrets.SystemRandom().shuffle(order)
                    for configuration in order:
                        trial = {"id": uid(), "experiment_id": experiment["id"], "project": pack["project"],
                                 "case_id": case["id"], "configuration_id": configuration["id"], "repeat_index": repeat,
                                 "attempt": 1, "status": "queued", "queued_at": now(), "started_at": None,
                                 "ended_at": None, "duration_ms": None, "case": case, "configuration": configuration,
                                 "case_hash": digest(case), "configuration_hash": digest(configuration),
                                 "timeout_seconds": timeout, "result": None, "cancellation": None, "platform": None,
                                 "experiment_snapshot_hash": experiment["snapshot_hash"], "authorization": experiment.get("authorization")}
                        self.db.execute("INSERT INTO trials VALUES(?,?,?,?)", (trial["id"], experiment["id"], trial["status"], dumps(trial)))
                        self.db.execute("INSERT INTO attempts VALUES(?,?,?)", (trial["id"], 1, dumps(trial)))
            self._new_reviewer(experiment["id"], "评审 1")
        write_json(self.directory / "experiments" / experiment["id"] / "experiment.json", experiment)
        return self.experiment_view(experiment["id"])

    def _new_reviewer(self, experiment_id, label):
        token = secrets.token_urlsafe(24)
        self.db.execute("INSERT INTO reviewers VALUES(?,?,?,?)", (token, experiment_id, label, now()))
        groups = {}
        experiment = self.experiment(experiment_id)
        baseline_id = experiment.get("baseline_id", experiment["configurations"][0]["id"])
        for trial in self.trials(experiment_id):
            groups.setdefault((trial["case_id"], trial["repeat_index"]), {})[trial["configuration_id"]] = trial["id"]
        pairs = []
        for (case_id, repeat), ids in groups.items():
            if baseline_id not in ids:
                continue
            for candidate_id, identifier in ids.items():
                if candidate_id == baseline_id:
                    continue
                pair = [ids[baseline_id], identifier]
                secrets.SystemRandom().shuffle(pair)
                pairs.append((uid(), token, case_id, repeat, *pair))
        secrets.SystemRandom().shuffle(pairs)
        self.db.executemany("INSERT INTO presentations VALUES(?,?,?,?,?,?)", pairs)
        return {"token": token, "label": label, "url": "/blind/" + token}

    def new_reviewer(self, experiment_id, label):
        with self.lock, self.db:
            self.experiment(experiment_id)
            count = self.db.execute("SELECT count(*) FROM reviewers WHERE experiment_id=?", (experiment_id,)).fetchone()[0]
            if count >= 20:
                raise ValueError("本实验已有 20 个评审席位")
            return self._new_reviewer(experiment_id, str(label or f"评审 {count + 1}")[:80])

    def stop(self, experiment_id):
        with self.lock, self.db:
            experiment = self.experiment(experiment_id)
            experiment["stop_requested_at"] = now()
            self.db.execute("UPDATE experiments SET body=? WHERE id=?", (dumps(experiment), experiment_id))
            for trial in self.trials(experiment_id):
                if trial["status"] == "queued":
                    trial.update(status="cancelled", ended_at=now(), cancellation={"phase": "not_dispatched", "upstream": "not_sent"},
                                 result={"status": "cancelled", "error": {"code": "stopped_before_dispatch", "message": "调度已停止，本 trial 未发送产品请求"}, "cost": None, "usage": None,
                                         "review_output": {"说明": "尚未开始即已停止"}})
                    self._save_trial(trial)
                elif trial["status"] in {"running", "cancelling"}:
                    trial.update(status="cancelling", cancellation={"phase": "requested", "requested_at": now(), "upstream": "unknown"})
                    self._save_trial(trial)
                    root = self.attempt_root(trial)
                    root.mkdir(parents=True, exist_ok=True)
                    (root / "cancel.requested").touch()
        return self.experiment_view(experiment_id)

    def pause_experiment(self, experiment_id, paused=True):
        if type(paused) is not bool:
            raise ValueError("paused 必须为布尔值")
        with self.lock, self.db:
            experiment = self.experiment(experiment_id)
            if not paused and experiment.get("stop_requested_at"):
                raise ValueError("已停止实验不能直接恢复；请明确重试未成功的 trial，保留旧尝试")
            if experiment.get("origin", {}).get("kind") == "historical_import":
                raise ValueError("导入的历史记录不能调度；请使用版本化任务集新建实验")
            experiment["queue"] = {**experiment.get("queue", {}), "paused": paused, "updated_at": now()}
            self.db.execute("UPDATE experiments SET body=? WHERE id=?", (dumps(experiment), experiment_id))
        return self.experiment_view(experiment_id)

    def attempt_root(self, trial):
        return self.directory / "experiments" / trial["experiment_id"] / "trials" / trial["id"] / f"attempt-{trial['attempt']}"

    def claim(self):
        with self.lock, self.db:
            settings = self.queue_settings()
            if settings["paused"]:
                return None
            active = self.db.execute("SELECT count(*) FROM trials WHERE status IN ('running','cancelling')").fetchone()[0]
            if active >= settings["concurrency"]:
                return None
            # Only examine one pending item per experiment, avoiding a large queued
            # body scan. No distributed worker ownership is implied by this lock.
            rows = self.db.execute("SELECT body FROM trials WHERE rowid IN (SELECT min(rowid) FROM trials WHERE status='queued' GROUP BY experiment_id) ORDER BY rowid").fetchall()
            for row in rows:
                trial = json.loads(row[0])
                experiment = self.experiment(trial["experiment_id"])
                if experiment.get("queue", {}).get("paused") or experiment.get("stop_requested_at"):
                    continue
                budget = experiment.get("budget", {})
                running = self.db.execute("SELECT count(*) FROM trials WHERE experiment_id=? AND status IN ('running','cancelling')", (experiment["id"],)).fetchone()[0]
                if running >= budget.get("max_concurrency", settings["concurrency"]):
                    continue
                usage = self.budget_usage(experiment["id"])
                exceeded = [key for key, field in (("max_attempts", "started_attempts"), ("max_elapsed_seconds", "elapsed_seconds"),
                             ("max_known_cost", "known_cost"), ("max_total_tokens", "known_total_tokens"))
                            if key in budget and usage[field] >= budget[key]]
                if exceeded:
                    self._exhaust_budget(experiment, exceeded, usage)
                    continue
                trial.update(status="running", started_at=now())
                self._save_trial(trial)
                return trial
            return None

    def budget_usage(self, experiment_id):
        with self.lock:
            rows = self.db.execute("SELECT a.body FROM attempts a JOIN trials t ON t.id=a.trial_id WHERE t.experiment_id=?", (experiment_id,)).fetchall()
        attempts = [json.loads(r[0]) for r in rows]
        started = [a for a in attempts if a.get("started_at")]
        earliest = min((a["started_at"] for a in started), default=None)
        elapsed = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(earliest)).total_seconds()) if earliest else 0
        known_cost, known_tokens, missing_cost, missing_tokens = 0, 0, 0, 0
        for attempt in started:
            result = attempt.get("result") or {}
            cost = result.get("cost")
            cost = cost.get("amount") if isinstance(cost, dict) else cost
            if type(cost) in {int, float} and math.isfinite(cost):
                known_cost += cost
            else:
                missing_cost += 1
            usage = result.get("usage")
            usage = usage if isinstance(usage, dict) else {}
            tokens = usage.get("total_tokens")
            if tokens is None:
                input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
                output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
                if type(input_tokens) in {int, float} and type(output_tokens) in {int, float}:
                    tokens = input_tokens + output_tokens
            if type(tokens) in {int, float} and math.isfinite(tokens):
                known_tokens += tokens
            else:
                missing_tokens += 1
        return {"started_attempts": len(started), "elapsed_seconds": round(elapsed, 3), "known_cost": known_cost,
                "known_total_tokens": known_tokens, "cost_unknown_attempts": missing_cost,
                "tokens_unknown_attempts": missing_tokens,
                "measurement": "observed_lower_bound", "in_flight_may_exceed_limit": True}

    def _exhaust_budget(self, experiment, exceeded, usage):
        experiment["queue"] = {**experiment.get("queue", {}), "budget_exhausted_at": now(), "exceeded": exceeded, "usage_at_stop": usage}
        self.db.execute("UPDATE experiments SET body=? WHERE id=?", (dumps(experiment), experiment["id"]))
        for trial in self.trials(experiment["id"]):
            if trial["status"] != "queued":
                continue
            trial.update(status="cancelled", ended_at=now(), cancellation={"phase": "budget_before_dispatch", "upstream": "not_sent"},
                         result={"status": "cancelled", "error": {"code": "budget_exhausted", "message": "实验预算达到限制，未发送该项产品请求", "limits": exceeded},
                                 "usage": None, "cost": None, "review_output": {"说明": "预算限制停止后续调度"}})
            self._save_trial(trial)

    def retry(self, identifier):
        with self.lock, self.db:
            trial = self.trial(identifier, False)
            if trial["status"] not in TERMINAL - {"succeeded"}:
                raise ValueError("只允许重试未成功的终态 trial")
            maximum = self.queue_settings()["limits"]["max_attempts_per_trial"]
            if trial["attempt"] >= maximum:
                raise ValueError(f"本 trial 已达 {maximum} 次执行尝试上限；请检查证据后建立新实验")
            reviewed = self.db.execute("SELECT 1 FROM reviews r JOIN presentations p ON p.id=r.presentation_id WHERE p.left_id=? OR p.right_id=?", (identifier, identifier)).fetchone()
            if reviewed:
                raise ValueError("该配对已有评价；为保留评价对应的证据，请建立新实验")
            experiment = self.experiment(trial["experiment_id"])
            if experiment.get("origin", {}).get("kind") == "historical_import":
                raise ValueError("导入的历史轨迹只能评价，不能重放；请用版本化任务集新建实验")
            if experiment.get("queue", {}).get("budget_exhausted_at"):
                raise ValueError("实验已达到冻结预算；请基于当前证据另建实验")
            experiment["stop_requested_at"] = None
            self.db.execute("UPDATE experiments SET body=? WHERE id=?", (dumps(experiment), experiment["id"]))
            trial.update(attempt=trial["attempt"] + 1, status="queued", queued_at=now(), started_at=None, ended_at=None,
                         duration_ms=None, result=None, cancellation=None, platform=None)
            self._save_trial(trial)
        return trial

    def recover(self):
        recovered = []
        for trial in self.trials():
            # claim() persists running before a worker can be dispatched. Queued
            # items have never crossed that boundary: keep their attempt and
            # persisted experiment/global pause controls intact across restart.
            if trial["status"] in {"running", "cancelling"}:
                result_path = self.attempt_root(trial) / "result.json"
                if result_path.exists():
                    try:
                        result = json.loads(result_path.read_text())
                        if result.get("status") in TERMINAL:
                            self.update_trial(trial["id"], status=result["status"], ended_at=result.get("recorded_at", now()), result=result,
                                              recovery={"phase": "recovered_terminal_artifact", "replayed": False})
                            recovered.append(trial["id"])
                            continue
                    except (OSError, ValueError):
                        pass
                self.update_trial(trial["id"], status="interrupted", ended_at=now(),
                                  result={"status": "interrupted", "error": {"code": "server_restart", "message": "已领取的执行因服务重启中断，未自动重放；可查看旧尝试并明确重试"},
                                          "usage": None, "cost": None, "review_output": {"说明": "执行被服务重启中断，未确认交付"}},
                                  cancellation={"phase": "restart_recovery", "request_sent": "unknown", "upstream": "unknown"})
                recovered.append(trial["id"])
        return recovered

    def reviewers(self, experiment_id):
        with self.lock:
            return [{"token": r[0], "label": r[1], "url": "/blind/" + r[0]} for r in self.db.execute("SELECT token,label FROM reviewers WHERE experiment_id=?", (experiment_id,))]

    def performance(self, experiment_id, trials=None):
        trials = self.trials(experiment_id) if trials is None else trials
        selected = {t["id"] for t in trials}
        with self.lock:
            attempts = [json.loads(r[0]) for r in self.db.execute(
                "SELECT a.body FROM attempts a JOIN trials t ON t.id=a.trial_id WHERE t.experiment_id=? ORDER BY t.rowid,a.number",
                (experiment_id,))]
        attempts = [a for a in attempts if a["id"] in selected]
        measurements = []
        for attempt in attempts:
            path = self.attempt_root(attempt) / "events.jsonl"
            try:
                stat = path.stat()
                file_version = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                file_version = None
            key = (attempt["id"], attempt["attempt"])
            version = (file_version, digest({k: attempt.get(k) for k in ("status", "started_at", "ended_at", "duration_ms")} |
                                           {"usage": (attempt.get("result") or {}).get("usage")}))
            with self.lock:
                cached = self._performance_cache.get(key)
            if cached and cached[0] == version:
                measurement = cached[1]
            else:
                try:
                    events = read_events(path)
                    available = file_version is not None
                except OSError:
                    events, available = [], False
                measurement = measure_attempt(attempt, events, events_available=available)
                with self.lock:
                    self._performance_cache[key] = (version, measurement)
                    if len(self._performance_cache) > 2048:
                        self._performance_cache.pop(next(iter(self._performance_cache)))
            measurements.append(measurement)
        return aggregate_performance(trials, measurements)

    def experiment_view(self, experiment_id, include_performance=True):
        experiment = self.experiment(experiment_id)
        trials = self.trials(experiment_id)
        counts = dict(Counter(t["status"] for t in trials))
        state = self._experiment_state(experiment, counts)
        case_count = len(experiment["cases"])
        family_count = len({case_family(case) for case in experiment["cases"]})
        summary = {k: experiment[k] for k in ["id", "title", "project", "created_at", "purpose", "repeats", "timeout_seconds", "stop_requested_at", "snapshot_hash"]}
        # Legacy independent_cases now aliases families; old packs without family_id retain their counts.
        summary.update(status=state, counts=counts, total_trials=len(trials), taskpack_id=experiment["pack"]["id"], case_count=case_count,
                       family_count=family_count, independent_cases=family_count,
                       completed_trials=sum(counts.get(s, 0) for s in TERMINAL), taskpack={"id": experiment["pack"]["id"], "name": experiment["pack"]["name"], "version": experiment["pack"]["version"]},
                       configurations=experiment["configurations"], reviewers=self.reviewers(experiment_id))
        summary.update({k: experiment.get(k) for k in ("dataset", "split", "budget", "queue", "origin", "source_snapshot")})
        summary["baseline_id"] = experiment.get("baseline_id", experiment["configurations"][0]["id"])
        summary["configuration_ids"] = experiment.get("configuration_ids", [c["id"] for c in experiment["configurations"]])
        summary["candidate_ids"] = [c for c in summary["configuration_ids"] if c != summary["baseline_id"]]
        summary["budget_usage"] = self.budget_usage(experiment_id)
        summary["trials"] = [{k: t.get(k) for k in ["id", "case_id", "configuration_id", "repeat_index", "attempt", "status", "started_at", "ended_at", "duration_ms", "cancellation", "platform"]} | {"case_name": t["case"]["name"], "family_id": case_family(t["case"]), "error": (t.get("result") or {}).get("error")} for t in trials]
        summary["comparison"] = self.comparison(experiment_id)
        summary["rubric"] = experiment["pack"].get("rubric", [])
        if include_performance:
            summary["performance"] = self.performance(experiment_id, trials)
        return summary

    def list_experiments(self, project=None):
        with self.lock:
            experiments = [json.loads(r[0]) for r in self.db.execute("SELECT body FROM experiments WHERE (? IS NULL OR project=?) ORDER BY created_at DESC", (project, project))]
            grouped = self.db.execute("SELECT t.experiment_id,t.status,count(*) FROM trials t JOIN experiments e ON e.id=t.experiment_id WHERE (? IS NULL OR e.project=?) GROUP BY t.experiment_id,t.status", (project, project)).fetchall()
            reviews = self.db.execute("SELECT v.experiment_id,r.body FROM reviews r JOIN presentations p ON p.id=r.presentation_id JOIN reviewers v ON v.token=p.token JOIN experiments e ON e.id=v.experiment_id WHERE (? IS NULL OR e.project=?)", (project, project)).fetchall()
            assessments = dict(self.db.execute("SELECT t.experiment_id,count(*) FROM assessment_records a JOIN trials t ON t.id=a.trial_id GROUP BY t.experiment_id").fetchall())
        counts, review_counts = {}, {}
        for identifier, status, count in grouped:
            counts.setdefault(identifier, {})[status] = count
        for identifier, raw in reviews:
            record = json.loads(raw)
            counter = review_counts.setdefault(identifier, {"human": Counter(), "acceptance": Counter()})
            counter.setdefault(record["kind"], Counter())[record["choice"]] += 1
        result = []
        for experiment in experiments:
            identifier = experiment["id"]
            statuses = counts.get(identifier, {})
            families = len({case_family(c) for c in experiment["cases"]})
            summary = {k: experiment.get(k) for k in ("id", "title", "project", "created_at", "purpose", "repeats", "timeout_seconds", "stop_requested_at", "snapshot_hash", "dataset", "split", "budget", "queue", "origin")}
            summary.update(status=self._experiment_state(experiment, statuses), counts=statuses,
                           total_trials=sum(statuses.values()), completed_trials=sum(statuses.get(s, 0) for s in TERMINAL),
                           case_count=len(experiment["cases"]), family_count=families, independent_cases=families,
                           taskpack_id=experiment["pack"]["id"],
                           taskpack={k: experiment["pack"].get(k) for k in ("id", "name", "version")},
                           baseline_id=experiment.get("baseline_id", experiment["configurations"][0]["id"]),
                           configuration_ids=[c["id"] for c in experiment["configurations"]],
                           configuration_count=len(experiment["configurations"]),
                           comparison={"reviews": {k: dict(v) for k, v in review_counts.get(identifier, {"human": {}, "acceptance": {}}).items()},
                                       "model_assessments": assessments.get(identifier, 0), "scope": "list_summary"})
            result.append(summary)
        return result

    @staticmethod
    def _experiment_state(experiment, counts):
        if counts.get("running") or counts.get("cancelling"):
            return "running"
        if counts.get("queued"):
            return "paused" if experiment.get("queue", {}).get("paused") else "running"
        return "completed_with_issues" if any(count and status != "succeeded" for status, count in counts.items()) else "completed"

    def blind(self, token):
        from .exporting import trial_media
        with self.lock:
            reviewer = self.db.execute("SELECT * FROM reviewers WHERE token=?", (token,)).fetchone()
            if not reviewer:
                raise KeyError("评审入口不存在")
            experiment = self.experiment(reviewer["experiment_id"])
            presentations = self.db.execute("SELECT * FROM presentations WHERE token=? ORDER BY rowid", (token,)).fetchall()
            pairs = []
            for presentation in presentations:
                left, right = self.trial(presentation["left_id"], False), self.trial(presentation["right_id"], False)
                review = self.db.execute("SELECT body FROM reviews WHERE presentation_id=?", (presentation["id"],)).fetchone()
                ready = left["status"] in TERMINAL and right["status"] in TERMINAL
                identity = self._review_identity(left, right)
                revision = digest({"presentation_id": presentation["id"], "identity": identity})
                # Strict allow-list projection: no configuration, trial IDs, provenance, paths, usage, or traces.
                case = left["case"]
                media = [{**item, "url": f"/api/blind/{token}/pairs/{presentation['id']}/media/{item['index']}"} for item in trial_media(left)]
                pairs.append({"id": presentation["id"], "case": _blind_case({k: case[k] for k in BLIND_CASE_FIELDS if k in case}), "media": media,
                              "repeat_index": presentation["repeat_index"], "ready": ready, "revision": revision,
                              "A": {"status": left["status"], "output": _blind_output((left.get("result") or {}).get("review_output"))} if ready else None,
                              "B": {"status": right["status"], "output": _blind_output((right.get("result") or {}).get("review_output"))} if ready else None,
                              "review": self._public_review(json.loads(review[0])) if review else None})
            return {"schema_version": "myeval.blind.v2", "projection_version": "neutral-case-and-delivered-output.v2",
                    "reviewer_label": reviewer["label"], "project": experiment["project"],
                    "taskpack_name": experiment["pack"]["name"], "rubric": experiment["pack"].get("rubric", []), "pairs": pairs,
                    "completed": sum(p["review"] is not None for p in pairs), "total": len(pairs),
                    "case_count": len(experiment["cases"]), "family_count": len({case_family(case) for case in experiment["cases"]}),
                    "limitations": ["本地盲评控制配置偏见；不能阻止已看过管理员结果的人根据内容识别。"]}

    def blind_media_trial(self, token, presentation_id):
        with self.lock:
            row = self.db.execute("SELECT left_id FROM presentations WHERE token=? AND id=?", (token, presentation_id)).fetchone()
            if row is None:
                raise KeyError("匿名配对不属于该评审席位")
            return self.trial(row[0], False)

    @staticmethod
    def _review_identity(left, right):
        return {side: {"trial_id": trial["id"], "execution_attempt": trial["attempt"],
                       "status": trial["status"], "result_hash": digest(trial.get("result"))}
                for side, trial in [("A", left), ("B", right)]}

    @staticmethod
    def _public_review(record):
        return {key: record[key] for key in ["id", "choice", "reason", "kind", "dimensions", "created_at", "rubric_version", "presentation_revision"] if key in record}

    def save_review(self, token, presentation_id, body):
        if body.get("choice") not in {"A", "B", "tie", "unable"}:
            raise ValueError("请选择 A、B、平局或无法判断")
        reason = str(body.get("reason") or "").strip()
        if not 1 <= len(reason) <= 10000:
            raise ValueError("请填写 1–10000 字评价理由")
        kind = body.get("kind", "human")
        if kind not in {"human", "acceptance"}:
            raise ValueError("评价类型不正确")
        dimensions = body.get("dimensions", {})
        if not isinstance(dimensions, dict) or any(v not in {"A", "B", "tie", "unable"} for v in dimensions.values()):
            raise ValueError("分项评价格式不正确")
        with self.lock, self.db:
            pair = next((p for p in self.blind(token)["pairs"] if p["id"] == presentation_id), None)
            if not pair:
                raise KeyError("配对不存在")
            if body.get("revision") != pair["revision"]:
                raise StalePresentation("配对结果或页面版本已变化，这份评价尚未保存。请重新载入配对，核对当前结果后重新评价。")
            if not pair["ready"]:
                raise ValueError("两侧执行均进入终态后才能评价")
            if pair["review"]:
                raise ValueError("评价已保存；使用新的评审席位保留另一份独立评价")
            presentation = self.db.execute("SELECT left_id,right_id FROM presentations WHERE id=? AND token=?", (presentation_id, token)).fetchone()
            identity = self._review_identity(self.trial(presentation["left_id"], False), self.trial(presentation["right_id"], False))
            record = {"id": uid(), "choice": body["choice"], "reason": reason, "kind": kind, "dimensions": dimensions, "created_at": now(), "rubric_version": "taskpack-snapshot",
                      "presentation_revision": pair["revision"], "result_identity": identity}
            self.db.execute("INSERT INTO reviews VALUES(?,?,?)", (record["id"], presentation_id, dumps(record)))
        return self._public_review(record)

    def reveal(self, token, presentation_id):
        with self.lock:
            row = self.db.execute("SELECT p.* FROM presentations p JOIN reviews r ON p.id=r.presentation_id WHERE p.token=? AND p.id=?", (token, presentation_id)).fetchone()
            if not row:
                raise ValueError("保存该配对评价后才能揭示配置")
            return {side: {"configuration": self.trial(row[column], False)["configuration"], "trial_id": row[column]} for side, column in [("A", "left_id"), ("B", "right_id")]}

    def comparison(self, experiment_id):
        trials = self.trials(experiment_id)
        experiment = self.experiment(experiment_id)
        baseline_id = experiment.get("baseline_id", experiment["configurations"][0]["id"])
        configs = {}
        for trial in trials:
            key = trial["configuration_id"]
            group = configs.setdefault(key, {"name": trial["configuration"]["name"], "trials": 0, "statuses": Counter(), "attempt_statuses": Counter(), "duration_ms": [], "rules": Counter(), "rule_dimensions": {}, "unknown_cost_trials": 0, "known_cost_total": 0, "human_wins": 0, "acceptance_wins": 0})
            group["trials"] += 1
            group["statuses"][trial["status"]] += 1
            for execution in self.trial(trial["id"])["attempts"]:
                group["attempt_statuses"][execution["status"]] += 1
            if trial.get("duration_ms") is not None:
                group["duration_ms"].append(trial["duration_ms"])
            result = trial.get("result") or {}
            cost = result.get("cost")
            amount = cost.get("amount") if isinstance(cost, dict) else cost
            if isinstance(amount, (int, float)):
                group["known_cost_total"] += amount
            else:
                group["unknown_cost_trials"] += 1
            for assessment in result.get("assessments", []):
                group["rules"][assessment.get("status", "unknown")] += 1
                group["rule_dimensions"].setdefault(assessment.get("dimension", "unnamed"), Counter())[assessment.get("status", "unknown")] += 1
        with self.lock:
            rows = self.db.execute("SELECT r.body,p.left_id,p.right_id,p.case_id,p.repeat_index FROM reviews r JOIN presentations p ON p.id=r.presentation_id JOIN reviewers v ON p.token=v.token WHERE v.experiment_id=?", (experiment_id,)).fetchall()
            model_rows = self.db.execute("SELECT a.trial_id,a.body FROM assessment_records a JOIN trials t ON t.id=a.trial_id WHERE t.experiment_id=?", (experiment_id,)).fetchall()
        model_dimensions = []
        current_trials = {t['id']: t for t in trials}
        model_groups, group_dimensions = {}, {}
        for row in model_rows:
            record, trial = json.loads(row[1]), current_trials[row[0]]
            # Earlier-attempt assessments stay in the archive but cannot score a new result.
            if record.get('execution_attempt') != trial['attempt'] or record.get('result_hash') != digest(trial.get('result')):
                continue
            key = (record['evaluator'], record['rubric_version'], trial['configuration_id'])
            model_groups.setdefault(key, []).append((row[0], record))
            value = record.get('value')
            scores = value.get('scores', {}) if isinstance(value, dict) else {}
            if isinstance(scores, dict):
                group_dimensions.setdefault(key[:2], set()).update(scores)
            statuses = value.get('score_status', {}) if isinstance(value, dict) else {}
            if isinstance(statuses, dict):
                group_dimensions.setdefault(key[:2], set()).update(statuses)
        for evaluator, rubric in list(group_dimensions):
            for configuration_id in configs:
                model_groups.setdefault((evaluator, rubric, configuration_id), [])
        for (evaluator, rubric, configuration_id), records in model_groups.items():
            for dimension in sorted(group_dimensions.get((evaluator, rubric), [])):
                distribution, score_statuses, scored_trials = Counter(), Counter(), set()
                for trial_id, record in records:
                    value = record.get('value')
                    scores = value.get('scores', {}) if isinstance(value, dict) else {}
                    score = scores.get(dimension) if isinstance(scores, dict) else None
                    statuses = value.get('score_status', {}) if isinstance(value, dict) else {}
                    status = statuses.get(dimension) if isinstance(statuses, dict) else None
                    if status in ('not_applicable', 'unavailable'):
                        score_statuses[status] += 1
                    elif status in (None, 'rated') and (type(score) is int or (type(score) is float and math.isfinite(score))):
                        score_statuses['rated'] += 1
                        distribution[str(score)] += 1
                        scored_trials.add(trial_id)
                    else:
                        score_statuses['unrated'] += 1
                covered = len({trial_id for trial_id, _ in records})
                model_dimensions.append({'evaluator': evaluator, 'rubric_version': rubric,
                    'configuration_id': configuration_id, 'dimension': dimension, 'distribution': dict(distribution),
                    **{status: score_statuses[status] for status in ('rated', 'not_applicable', 'unavailable', 'unrated')},
                    'records': len(records), 'covered_trials': covered,
                    'scored_trials': len(scored_trials), 'total_trials': configs[configuration_id]['trials'],
                    'unreviewed_trials': configs[configuration_id]['trials'] - covered})
        review_counts = {"human": Counter(), "acceptance": Counter()}
        preferences = {}
        dimensions = {}
        for row in rows:
            review = json.loads(row[0])
            review_counts[review["kind"]][review["choice"]] += 1
            outcome = review["choice"]
            mapping = {"A": self.trial(row[1], False)["configuration_id"], "B": self.trial(row[2], False)["configuration_id"]}
            if outcome in mapping:
                outcome = mapping[outcome]
                configs[outcome][review["kind"] + "_wins"] += 1
            if review["kind"] == "human":
                candidate = next((c for c in mapping.values() if c != baseline_id), None)
                preferences.setdefault((row[3], row[4], candidate), set()).add(outcome)
            for dimension, choice in review.get("dimensions", {}).items():
                dimensions.setdefault(dimension, Counter())[review["kind"] + ":" + mapping.get(choice, choice)] += 1
        for group in configs.values():
            durations = group.pop("duration_ms")
            group["mean_duration_ms"] = round(sum(durations) / len(durations)) if durations else None
            group["statuses"], group["rules"] = dict(group["statuses"]), dict(group["rules"])
            group["attempt_statuses"] = dict(group["attempt_statuses"])
            group["rule_dimensions"] = {k: dict(v) for k, v in group["rule_dimensions"].items()}
        return {"configurations": configs, "reviews": {k: dict(v) for k, v in review_counts.items()}, "dimensions": {k: dict(v) for k, v in dimensions.items()},
                "disagreement_pairs": sum(len(v) > 1 for v in preferences.values()), "model_assessments": len(model_rows),
                "model_dimensions": model_dimensions,
                "current_model_assessments": sum(len(records) for records in model_groups.values()),
                "case_count": len({t["case_id"] for t in trials}),
                "family_count": len({case_family(t["case"]) for t in trials}),
                "independent_cases": len({case_family(t["case"]) for t in trials}),
                "paired_units": sum(1 for t in trials if t["configuration_id"] != baseline_id),
                "baseline_id": baseline_id, "candidate_ids": [c for c in configs if c != baseline_id],
                "execution_attempts": sum(sum(g["attempt_statuses"].values()) for g in configs.values()),
                "all_trials_in_denominator": len(trials), "note": "案例变式与重复采样不增加情境家族数；情境家族按 family_id 统计，旧案例按案例 ID。规则、模型评价、人工偏好分别报告，不合成总分。"}

    def _prepare_assessment(self, trial_id, body):
        if not isinstance(body, dict):
            raise ValueError("评分 body 必须为对象")
        current = self.trial(trial_id)
        number = body.get("execution_attempt", current["attempt"])
        if type(number) is not int:
            raise ValueError("execution_attempt 必须为执行尝试序号")
        trial = next((a for a in current["attempts"] if a["attempt"] == number), None)
        if trial is None:
            raise ValueError("所选执行尝试不存在")
        if trial["status"] not in TERMINAL:
            raise ValueError("终态结果才能关联外部模型评价")
        result_hash = digest(trial.get("result"))
        if body.get("result_hash") is not None and body["result_hash"] != result_hash:
            raise StalePresentation("评价绑定的输出哈希不一致；原评价未写入，请重新选定执行尝试")
        if number != current["attempt"] and body.get("result_hash") != result_hash:
            raise ValueError("重评历史尝试必须显式提供该 attempt 的 result_hash")
        if body.get("kind") != "model" or any(not isinstance(body.get(k), str) or not body[k].strip()
                for k in ('evaluator', 'rubric_version', 'reason')):
            raise ValueError("导入模型评价需 kind=model、evaluator、rubric_version 和 reason；不能冒充人工评价")
        value = body.get("value")
        if isinstance(value, dict):
            scores, statuses = value.get("scores", {}), value.get("score_status", {})
            if not isinstance(scores, dict) or any(not isinstance(dimension, str) or not dimension.strip()
                    or (score is not None and not (type(score) is int or (type(score) is float and math.isfinite(score))))
                    for dimension, score in scores.items()):
                raise ValueError("value.scores 必须以维度为键，分值为有限数值或 null；具体量表范围由 rubric_version 定义")
            if not isinstance(statuses, dict):
                raise ValueError("value.score_status 必须为维度状态对象")
            for dimension, status in statuses.items():
                if dimension not in scores or status not in ("rated", "not_applicable", "unavailable"):
                    raise ValueError("score_status 必须对应 scores 中的维度，值为 rated、not_applicable 或 unavailable")
                if (status == "rated") != (scores[dimension] is not None):
                    raise ValueError("rated 必须有分值；not_applicable 和 unavailable 的分值必须为 null")
        record = {"id": uid(), "created_at": now(), "source": "evaluation_job" if body.get("evaluation_job_id") else "external_import", "execution_attempt": number, "result_hash": result_hash,
                  **{k: body[k] for k in ["kind", "evaluator", "rubric_version", "reason", "value", "evidence", "evaluation_job_id", "idempotency_key", "evaluator_snapshot", "input_hash", "rubric_hash", "provenance"] if k in body}}
        return record

    def add_assessment(self, trial_id, body):
        return self.add_assessments([{"trial_id": trial_id, "body": body}])[0]

    def add_assessments(self, items):
        """Validate and commit an entire judge unit atomically, with crash-safe replay."""
        if not isinstance(items, list) or not 1 <= len(items) <= 2000:
            raise ValueError("评分批次必须包含 1–2000 项")
        with self.lock, self.db:
            prepared, keyed = [], {}
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("trial_id"), str):
                    raise ValueError("每项评分需 trial_id 与 body")
                trial_id = item["trial_id"]
                record = self._prepare_assessment(trial_id, item.get("body"))
                job, key = record.get("evaluation_job_id"), record.get("idempotency_key")
                if key is not None and (not isinstance(key, str) or not key.strip() or len(key) > 500 or not isinstance(job, str) or not job.strip()):
                    raise ValueError("idempotency_key 需为非空字符串并同时提供 evaluation_job_id")
                payload_hash = hashlib.sha256(json.dumps({"trial_id": trial_id, **{k: v for k, v in record.items() if k not in {"id", "created_at"}}},
                                                          ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
                existing = None
                if key is not None:
                    identity = (job, key)
                    prior = keyed.get(identity)
                    if prior:
                        if prior[0] != payload_hash:
                            raise ValueError("同一评分幂等键不能绑定不同结果或评分内容")
                        prepared.append((trial_id, prior[1], None, None, True))
                        continue
                    saved = self.db.execute("SELECT trial_id,record_id,payload_hash FROM assessment_keys WHERE job_id=? AND idempotency_key=?", identity).fetchone()
                    if saved:
                        if saved[0] != trial_id or saved[2] != payload_hash:
                            raise ValueError("评分幂等键已用于不同结果或评分内容；旧记录未覆盖")
                        existing = self._row("assessment_records", saved[1])
                    else:
                        record["id"] = digest({"evaluation_job_id": job, "idempotency_key": key, "trial_id": trial_id})
                    keyed[identity] = (payload_hash, existing or record)
                prepared.append((trial_id, existing or record, key, payload_hash, existing is not None))
            # No mutation before every identity and score payload has passed.
            for trial_id, record, key, payload_hash, exists in prepared:
                if exists:
                    continue
                self.db.execute("INSERT INTO assessment_records VALUES(?,?,?)", (record["id"], trial_id, dumps(record)))
                if key is not None:
                    self.db.execute("INSERT INTO assessment_keys VALUES(?,?,?,?,?)", (record["evaluation_job_id"], key, trial_id, record["id"], payload_hash))
            return [record for _, record, _, _, _ in prepared]

    def list_imports(self):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute("SELECT body FROM imports ORDER BY created_at DESC")]

    def _archive_import_evidence(self, bundle, origin):
        """Keep source jobs and decisions intact, outside all current-result tables."""
        bundle = bundle or {}
        issues, sections = [], {}
        for key, expected in (("evaluation", dict), ("decisions", list)):
            if key not in bundle:
                sections[key] = {"availability": "not_provided"}
                issues.append({"section": key, "status": "not_provided", "error": "来源未提供，不能据此认定原记录数为零"})
                continue
            value = bundle[key]
            valid = isinstance(value, expected)
            if key == "evaluation" and valid:
                jobs = value.get("jobs")
                valid = isinstance(jobs, list) and all(isinstance(job, dict) and isinstance(job.get("units"), list) for job in jobs)
                if valid:
                    for job in jobs:
                        units = job["units"]
                        count = job.get("unit_count")
                        if (any(not isinstance(unit, dict) for unit in units)
                                or count is not None and (type(count) is not int or count != len(units))):
                            valid = False
                            break
            if key == "decisions" and valid:
                valid = all(isinstance(decision, dict) for decision in value)
            sections[key] = {"availability": "available" if valid else "invalid_source",
                             "value": value, "value_sha256": digest(value)}
            if not valid:
                issues.append({"section": key, "status": "invalid_source", "error": "来源结构或单位分母不完整；原值保留但不视为有效评测"})
        history = bundle.get("historical_evidence", [])
        history_manifest = bundle.get("experiment", {}).get("historical_evidence_manifest", [])
        if not isinstance(history_manifest, list):
            issues.append({"section": "historical_evidence", "status": "invalid_source", "error": "来源历史归档清单不是数组；原值保留"})
        if not isinstance(history, list):
            issues.append({"section": "historical_evidence", "status": "invalid_source", "error": "来源历史归档不是数组；原值保留"})
        else:
            available = {}
            for entry in history:
                if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                        or entry.get("availability") != "available"
                        or not isinstance(entry.get("archive"), dict)
                        or entry["archive"].get("id") != entry["id"]
                        or digest(entry["archive"]) != entry.get("sha256")):
                    issues.append({"section": "historical_evidence", "status": "unavailable_source", "error": "来源历史归档缺失、损坏或已标记不可用；不将其视为当前证据"})
                else:
                    available[entry.get("id")] = entry["sha256"]
            for declared in history_manifest if isinstance(history_manifest, list) else []:
                if (not isinstance(declared, dict) or not isinstance(declared.get("id"), str)
                        or available.get(declared["id"]) != declared.get("sha256")):
                    issues.append({"section": "historical_evidence", "status": "unavailable_source", "error": "来源声明的历史归档未完整提供；原声明保留"})
        return {"schema_version": "myeval.historical-evidence.v1", "id": uid(), "import_id": origin["import_id"],
                "source_experiment_id": origin["source_experiment_id"], "source_bundle_hash": origin["source_hash"],
                "source_exported_at": bundle.get("exported_at"), "source_filter": bundle.get("filter"),
                "scope": "whole_source_experiment", "binding_status": "original_provenance_only",
                "current_result": False, "registered_for_current_experiment": False, "replayed": False,
                "semantic_validation": "not_recomputed", "sections": sections,
                "historical_evidence": history,
                "source_history_manifest": history_manifest,
                "issues": issues}

    def _export_historical_evidence(self, experiment):
        manifest = experiment.get("historical_evidence_manifest", [])
        if not manifest and experiment.get("origin", {}).get("kind") == "historical_import":
            return [{"availability": "unavailable", "binding_status": "original_provenance_only",
                     "current_result": False, "error": "旧导入未保存完整原评测作业及判定；缺失不能解释为零"}]
        result = []
        with self.lock:
            for entry in manifest:
                row = self.db.execute("SELECT body FROM historical_evidence WHERE id=? AND experiment_id=?", (entry["id"], experiment["id"])).fetchone()
                item = {**entry, "binding_status": "original_provenance_only", "current_result": False}
                try:
                    archive = json.loads(row[0]) if row else None
                    if not isinstance(archive, dict) or digest(archive) != entry["sha256"]:
                        raise ValueError("historical_evidence_integrity")
                    item.update(availability="available", archive=archive)
                except (ValueError, TypeError):
                    item.update(availability="unavailable", error="原评测与判定归档缺失或哈希不符；未导出不可信内容，分母未知")
                result.append(item)
        return result

    def import_traces(self, body):
        """Adopt historical, terminal evidence without making it executable."""
        from .datasets import validate_source
        if not isinstance(body.get("source"), dict) or not body["source"]:
            raise ValueError("历史导入需要 source 对象，记录原系统和证据位置")
        def reject_secrets(value):
            if isinstance(value, dict):
                forbidden = {"api_key", "access_token", "refresh_token", "private_key", "password", "authorization_header",
                             "hidden_reasoning", "chain_of_thought", "reasoning_content", "thinking_content"}
                if any(str(k).lower() in forbidden for k in value):
                    raise ValueError("导入内容包含凭据或隐藏推理字段；请在来源处生成可见证据白名单导出")
                for child in value.values():
                    reject_secrets(child)
            elif isinstance(value, list):
                for child in value:
                    reject_secrets(child)
        reject_secrets(body)
        bundle = body.get("bundle")
        if bundle is not None:
            if not isinstance(bundle, dict) or not isinstance(bundle.get("experiment"), dict):
                raise ValueError("bundle 必须包含 MyEval experiment 和 trials")
            old_experiment = bundle["experiment"]
            raw_trials = bundle.get("trials")
            project = old_experiment.get("project")
        else:
            old_experiment = {}
            raw_trials = body.get("traces")
            project = body.get("project")
        if not isinstance(project, str) or not project or not isinstance(raw_trials, list) or not raw_trials or len(raw_trials) > 50000:
            raise ValueError("历史导入需要 project 与 1–50000 条终态 traces/trials")
        imported_at, import_id, experiment_id = now(), uid(), uid()
        origin = {"kind": "historical_import", "import_id": import_id, "source": body["source"],
                  "source_experiment_id": old_experiment.get("id"), "source_hash": digest(bundle or raw_trials),
                  "imported_at": imported_at, "replayed": False}
        evidence_archive = self._archive_import_evidence(bundle, origin)
        trials, cases, configs, ids, unique = [], {}, {}, {}, set()
        source_attempts, import_warnings = {}, []
        authorization = body.get("authorization") or old_experiment.get("authorization")
        for index, item in enumerate(raw_trials):
            if not isinstance(item, dict) or not isinstance(item.get("case"), dict) or not isinstance(item.get("configuration"), dict):
                raise ValueError("每条历史轨迹需包含 case 与 configuration 快照")
            case, config = dict(item["case"]), dict(item["configuration"])
            if not all(isinstance(x.get("id"), str) and x["id"] for x in (case, config)):
                raise ValueError("案例和配置需要非空 id")
            case.setdefault("name", case["id"])
            config.setdefault("name", config["id"])
            if "source" not in case and body["source"].get("type"):
                case["source"] = body["source"]
            case["source"] = validate_source(case, authorization, project, operation="import")
            if case["id"] in cases and cases[case["id"]] != case:
                raise ValueError("同一历史 case_id 的快照不一致；请在来源处区分案例版本")
            if config["id"] in configs and configs[config["id"]] != config:
                raise ValueError("同一历史 configuration_id 的快照不一致")
            cases[case["id"]], configs[config["id"]] = case, config
            repeat = item.get("repeat_index", 1)
            if type(repeat) is not int or repeat < 1:
                raise ValueError("repeat_index 必须为正整数")
            unit = (case["id"], config["id"], repeat)
            if unit in unique:
                raise ValueError("导入含重复 case/configuration/repeat；不能当作新独立样本")
            unique.add(unit)
            old_id = str(item.get("id", f"trace-{index + 1}"))
            if old_id in ids:
                raise ValueError("来源 trial id 重复")
            identifier = uid()
            ids[old_id] = identifier
            raw_attempts = item.get("attempts") or [item]
            if not isinstance(raw_attempts, list) or not raw_attempts:
                raise ValueError("attempts 必须为非空数组")
            attempts, numbers = [], set()
            for raw in raw_attempts:
                if not isinstance(raw, dict):
                    raise ValueError("attempt 必须为对象")
                number = raw.get("attempt", 1)
                status = raw.get("status") or (raw.get("result") or {}).get("status")
                if type(number) is not int or number < 1 or number in numbers or status not in TERMINAL:
                    raise ValueError("只能导入独立编号的终态尝试；运行中的历史任务不能静默归为成功")
                numbers.add(number)
                result = raw.get("result")
                if not isinstance(result, dict) or result.get("status", status) != status:
                    raise ValueError("历史结果与终态不一致")
                result = {**result, "status": status}
                events = raw.get("events", [])
                if not isinstance(events, list) or any(not isinstance(e, dict) for e in events):
                    raise ValueError("历史 events 必须为事件对象数组")
                attempts.append({"id": identifier, "experiment_id": experiment_id, "project": project,
                                 "case_id": case["id"], "configuration_id": config["id"], "repeat_index": repeat,
                                 "attempt": number, "status": status, "case": case, "configuration": config,
                                 "case_hash": digest(case), "configuration_hash": digest(config),
                                 "timeout_seconds": raw.get("timeout_seconds", item.get("timeout_seconds", 300)),
                                 "queued_at": raw.get("queued_at"), "started_at": raw.get("started_at"),
                                 "ended_at": raw.get("ended_at", imported_at), "duration_ms": raw.get("duration_ms"),
                                 "result": result, "cancellation": raw.get("cancellation"), "platform": {"status": "historical_import", "replayed": False},
                                 "origin": {**origin, "source_trial_id": old_id, "source_attempt": number,
                                            "source_result_hash": digest(raw.get("result"))}, "_events": events})
            attempts.sort(key=lambda a: a["attempt"])
            current_number = item.get("attempt", attempts[-1]["attempt"])
            if current_number != attempts[-1]["attempt"]:
                raise ValueError("当前 attempt 必须是该历史 trial 的最后一次尝试")
            item_events = item.get("events", [])
            if item_events:
                if not isinstance(item_events, list) or any(not isinstance(e, dict) for e in item_events):
                    raise ValueError("events 必须为事件对象数组")
                for attempt in attempts:
                    if not attempt["_events"]:
                        attempt["_events"] = [e for e in item_events if e.get("execution_attempt", current_number) == attempt["attempt"]]
            trials.append((attempts, item.get("assessment_records", [])))
            source_attempts[old_id] = {a["attempt"]: a for a in attempts}

        def remap_identity(identity):
            if not isinstance(identity, dict):
                return None
            source_id = identity.get("trial_id")
            source_attempt = identity.get("execution_attempt")
            target = source_attempts.get(source_id, {}).get(source_attempt)
            if (target is None or identity.get("result_hash") != target["origin"]["source_result_hash"]
                    or identity.get("status", target["status"]) != target["status"]):
                return None
            return {"trial_id": target["id"], "execution_attempt": target["attempt"], "status": target["status"],
                    "result_hash": digest(target["result"])}
        imported_files = []
        revalidation_files = []
        total_artifact_bytes = 0
        from .snapshots import _safe_relative, _excluded
        media_blobs = {a.get("sha256"): a.get("content_base64") for a in (bundle or {}).get("artifacts", [])
                       if isinstance(a, dict) and isinstance(a.get("content_base64"), str)}
        for attachment in (bundle or {}).get("artifacts", []):
            if not isinstance(attachment, dict) or not ("text" in attachment or "content_base64" in attachment or "content_ref_sha256" in attachment):
                continue
            relative = _safe_relative(attachment.get("path"))
            binary = "content_base64" in attachment or "content_ref_sha256" in attachment
            image_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
            if _excluded(relative) or relative.suffix.lower() not in {".json", ".jsonl", ".txt", ".md", ".py", ".swift", ".h", ".hpp", ".c", ".cpp", ".m", ".mm", ".toml", ".yaml", ".yml", *image_suffixes}:
                raise ValueError("历史附件路径或类型不在可见证据白名单")
            if binary:
                encoded = attachment.get("content_base64", media_blobs.get(attachment.get("content_ref_sha256")))
                if (relative.suffix.lower() not in image_suffixes or not isinstance(encoded, str)
                        or attachment.get("content_ref_sha256", attachment.get("sha256")) != attachment.get("sha256")):
                    raise ValueError("二进制附件仅允许明确声明的图片")
                try:
                    raw = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError):
                    raise ValueError("媒体附件 Base64 格式不正确") from None
                if not (raw.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")) or raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"):
                    raise ValueError("二进制附件不是允许的图片字节格式")
            else:
                raw = attachment["text"].encode("utf-8") if isinstance(attachment["text"], str) else None
            if raw is None or hashlib.sha256(raw).hexdigest() != attachment.get("sha256"):
                raise ValueError("历史附件的文本与 SHA-256 不一致")
            total_artifact_bytes += len(raw)
            is_revalidation = attachment.get("kind") in {"revalidation_manifest", "revalidation_receipt"}
            if is_revalidation and (binary or relative.suffix != ".json" or relative.parts[0] not in {"revalidations", "revalidation-history"}):
                raise ValueError("重验收来源附件必须为专用目录内的 JSON")
            limit = 30 * 1024 * 1024 if binary else 64 * 1024 * 1024 if is_revalidation else 10_000_000
            if len(raw) > limit or total_artifact_bytes > 500_000_000:
                raise ValueError("历史附件超过有界导入限制")
            parts = relative.parts
            if len(parts) >= 4 and parts[0] == "evidence" and parts[1] in source_attempts and parts[2].startswith("attempt-"):
                try:
                    number = int(parts[2][8:])
                except ValueError:
                    raise ValueError("历史附件 attempt 路径不正确") from None
                target = source_attempts[parts[1]].get(number)
                if target is None:
                    raise ValueError("历史附件对应的 attempt 未导入")
                local_relative = (Path(*parts[3:]) if binary else Path("origin-artifacts") / Path(*parts[3:])).as_posix()
                if binary:
                    from .exporting import _media_declarations
                    declarations = _media_declarations(target)
                    if not any(m.get("_storage") == "attempt" and m.get("path") == local_relative and m.get("sha256") == attachment["sha256"] for m in declarations):
                        raise ValueError("图片附件未绑定该 attempt 的媒体清单与冻结哈希")
                target.setdefault("imported_artifacts", []).append({"path": local_relative, "sha256": attachment["sha256"], "source_path": attachment["path"]})
                imported_files.append((self.attempt_root(target) / local_relative, raw))
            else:
                storage_path = Path("imports") / import_id / "artifacts" / relative
                imported_files.append((self.directory / storage_path, raw))
                if is_revalidation:
                    reject_secrets(json.loads(raw))
                    revalidation_files.append({"storage_path": storage_path.as_posix(), "source_path": relative.as_posix(),
                                               "kind": attachment["kind"], "receipt_id": attachment.get("receipt_id"),
                                               "sha256": attachment["sha256"], "bytes": len(raw)})
        # A portable alias is metadata, not another copy of the file. Retain it
        # only when its exact same-attempt archive was actually supplied above.
        artifact_manifest = (bundle or {}).get("artifact_manifest", [])
        if not isinstance(artifact_manifest, list):
            raise ValueError("历史附件清单必须为数组")
        from .exporting import TRACE_FILE_NAMES
        for directory in artifact_manifest:
            if not isinstance(directory, dict) or directory.get("kind") != "archived_directory_alias":
                continue
            relative = _safe_relative(directory.get("path"))
            parts = relative.parts
            if (len(parts) < 4 or parts[0] != "evidence" or parts[1] not in source_attempts
                    or not parts[2].startswith("attempt-") or _excluded(relative)):
                raise ValueError("历史轨迹目录声明必须对应已导入的原始 attempt")
            try:
                number = int(parts[2][8:])
            except ValueError:
                raise ValueError("历史轨迹目录声明的 attempt 路径不正确") from None
            target = source_attempts[parts[1]].get(number)
            children = directory.get("declared_children")
            if (target is None or not isinstance(children, list) or not children
                    or any(not isinstance(name, str) or name not in TRACE_FILE_NAMES for name in children)
                    or len(children) != len(set(children))):
                raise ValueError("历史轨迹目录必须保留其明确声明的白名单子文件集合")
            target.setdefault("imported_directory_declarations", []).append({"source_path": relative.as_posix(),
                                                                            "declared_children": children})
        for alias in artifact_manifest:
            if (not isinstance(alias, dict) or alias.get("kind") not in {"archived_alias", "archived_alias_unavailable"}
                    or alias.get("availability") not in {"relocated", "unavailable"}):
                continue
            relative = _safe_relative(alias.get("path"))
            parts = relative.parts
            if (len(parts) < 4 or parts[0] != "evidence" or parts[1] not in source_attempts
                    or not parts[2].startswith("attempt-") or _excluded(relative)):
                raise ValueError("历史附件别名必须明确对应同一原始 attempt 的归档路径")
            try:
                number = int(parts[2][8:])
            except ValueError:
                raise ValueError("历史附件别名的 attempt 路径不正确") from None
            target = source_attempts[parts[1]].get(number)
            if target is None:
                raise ValueError("历史附件别名对应的 attempt 未导入")
            expected = alias.get("sha256") or alias.get("declared_sha256")
            if not isinstance(expected, str) or len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
                raise ValueError("历史附件别名缺少有效的声明 SHA-256")
            existing = [item for item in target.get("imported_artifacts", []) if item["source_path"] == relative.as_posix()]
            if any(item["sha256"] != expected for item in existing):
                raise ValueError("历史附件别名与已提供的原路径内容冲突")

            def preserve_unavailable(source_archive_path):
                declaration = {"source_path": relative.as_posix(), "source_archive_path": source_archive_path,
                               "sha256": expected, "availability": "unavailable"}
                target.setdefault("unavailable_imported_aliases", []).append(declaration)
                import_warnings.append({"kind": "archived_alias_unavailable", **declaration,
                                        "message": "原别名指向的归档字节未提供或哈希不符；未登记迁移映射"})

            if alias["kind"] == "archived_alias_unavailable":
                preserve_unavailable(alias.get("source_archive_path"))
                continue
            archived = _safe_relative(alias.get("archive_path"))
            if archived.parts[:3] != parts[:3] or relative == archived or _excluded(archived):
                raise ValueError("历史附件别名不能引用其他 attempt 或不可用路径")
            matches = [item for item in target.get("imported_artifacts", []) if item["source_path"] == archived.as_posix()
                       and item["sha256"] == expected]
            if len({(item["path"], item["sha256"]) for item in matches}) != 1:
                preserve_unavailable(archived.as_posix())
                continue
            if existing:
                continue
            target.setdefault("imported_artifacts", []).append({"path": matches[0]["path"], "sha256": matches[0]["sha256"],
                                                               "source_path": relative.as_posix(), "source_alias_archive_path": archived.as_posix()})
        source_revalidation_index = (bundle or {}).get("revalidations")
        if source_revalidation_index is not None and not isinstance(source_revalidation_index, dict):
            raise ValueError("历史重验收索引必须为对象")
        baseline = old_experiment.get("baseline_id") or next(iter(configs))
        if baseline not in configs:
            raise ValueError("历史基线配置不在导入轨迹中")
        pack = {"id": "import-" + import_id, "name": str(body.get("title") or old_experiment.get("title") or "历史轨迹导入")[:160],
                "version": "1", "project": project, "cases": list(cases.values()), "configurations": list(configs.values()),
                "rubric": old_experiment.get("pack", {}).get("rubric", body.get("rubric", []))}
        experiment = {"id": experiment_id, "created_at": imported_at, "project": project, "taskpack_id": pack["id"],
                      "title": pack["name"], "purpose": "historical_trace_import", "pack": pack, "cases": pack["cases"],
                      "configurations": list(configs.values()), "configuration_ids": list(configs), "baseline_id": baseline,
                      "candidate_ids": [c for c in configs if c != baseline], "repeats": max(t[0][-1]["repeat_index"] for t in trials),
                      "timeout_seconds": old_experiment.get("timeout_seconds", 300), "stop_requested_at": None,
                      "schema_version": "myeval.v2", "origin": origin, "authorization": authorization,
                      "historical_evidence_manifest": [{"id": evidence_archive["id"], "sha256": digest(evidence_archive)}],
                      "dataset": old_experiment.get("dataset"), "queue": {"paused": True, "read_only_history": True}}
        if source_revalidation_index is not None or revalidation_files:
            available_source_paths = {item["source_path"] for item in revalidation_files}
            available_source_hashes = {item["source_path"]: item["sha256"] for item in revalidation_files}
            missing_sources = []
            for item in (source_revalidation_index or {}).get("receipts", []):
                if not isinstance(item, dict):
                    raise ValueError("历史重验收索引条目必须为对象")
                expected = item.get("artifact_paths", [])
                if not isinstance(expected, list) or any(not isinstance(path, str) for path in expected):
                    raise ValueError("历史重验收附件索引必须是路径数组")
                if set(expected) - available_source_paths:
                    missing_sources.append({"receipt_id": item.get("id"), "status": "unavailable_artifact",
                                            "error": "来源索引声明的完整重验收附件未全部提供；仅保留原分母声明"})
            for history in (source_revalidation_index or {}).get("historical", []):
                if not isinstance(history, dict) or not isinstance(history.get("exported_artifacts", []), list) or not isinstance(history.get("unavailable", []), list):
                    raise ValueError("历史重验收归档索引格式不正确")
                missing_sources.extend(history.get("unavailable", []))
                for item in history.get("exported_artifacts", []):
                    if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("sha256"), str):
                        raise ValueError("历史重验收归档附件缺少路径与哈希")
                    if available_source_hashes.get(item["path"]) != item["sha256"]:
                        missing_sources.append({"receipt_id": item.get("receipt_id"), "status": "unavailable_artifact",
                                                "error": "来源历史归档的附件缺失或与声明哈希不同；保留原分母及不可用状态"})
            experiment["revalidation_history"] = {"import_id": import_id, "scope": "whole_source_experiment",
                                                   "binding_status": "original_provenance_only",
                                                   "source_experiment_id": old_experiment.get("id"),
                                                   "source_index": source_revalidation_index, "files": revalidation_files,
                                                   "unavailable": missing_sources,
                                                   "registered_for_current_experiment": False, "replayed": False}
            import_warnings.append({"kind": "revalidation_original_provenance_only", "artifact_count": len(revalidation_files),
                                    "message": "完整收据仅归档原来源；不改内部 job/trial 身份、不注册为新实验重验收、不执行评分"})
        experiment["snapshot_hash"] = digest(experiment)
        record = {"id": import_id, "created_at": imported_at, "experiment_id": experiment_id, "project": project,
                  "source": body["source"], "source_hash": origin["source_hash"], "trial_count": len(trials),
                  "attempt_count": sum(len(t[0]) for t in trials), "status": "imported", "replayed": False,
                  "warnings": import_warnings}
        import_warnings.append({"kind": "historical_evidence_original_provenance_only", "archive_id": evidence_archive["id"],
                                "issues": evidence_archive["issues"],
                                "message": "完整原评测作业、全部单位和判定仅归档；不重新绑定身份、不注册新作业、不合并当前统计"})
        with self.lock, self.db:
            self.db.execute("INSERT INTO experiments VALUES(?,?,?,?)", (experiment_id, project, imported_at, dumps(experiment)))
            self.db.execute("INSERT INTO historical_evidence VALUES(?,?,?)", (evidence_archive["id"], experiment_id, dumps(evidence_archive)))
            for attempts, assessments in trials:
                current = {k: v for k, v in attempts[-1].items() if k != "_events"}
                self.db.execute("INSERT INTO trials VALUES(?,?,?,?)", (current["id"], experiment_id, current["status"], dumps(current)))
                for attempt in attempts:
                    events = attempt.pop("_events")
                    self.db.execute("INSERT INTO attempts VALUES(?,?,?)", (attempt["id"], attempt["attempt"], dumps(attempt)))
                    root = self.attempt_root(attempt)
                    write_json(root / "request.json", {k: v for k, v in attempt.items() if k != "result"})
                    write_json(root / "result.json", attempt["result"])
                    if events:
                        (root / "events.jsonl").write_text("".join(dumps({**e, "import_origin": import_id}) + "\n" for e in events))
                if not isinstance(assessments, list):
                    raise ValueError("assessment_records 必须为数组")
                for assessment in assessments:
                    if not isinstance(assessment, dict):
                        raise ValueError("历史评分必须为对象")
                    number = assessment.get("execution_attempt", current["attempt"])
                    target = next((a for a in attempts if a["attempt"] == number), None)
                    if target is None or assessment.get("kind") != "model" or assessment.get("result_hash") != target["origin"]["source_result_hash"]:
                        raise ValueError("历史模型评价无法绑定原始输出，导入已拒绝")
                    local = {**assessment, "id": uid(), "source": "historical_import", "result_hash": digest(target["result"]),
                             "origin": {"source_assessment_id": assessment.get("id"), "import_id": import_id}}
                    value = local.get("value")
                    if isinstance(value, dict) and isinstance(value.get("preference"), dict):
                        preference = value["preference"]
                        other = remap_identity({"trial_id": preference.get("other_trial_id"),
                                                "execution_attempt": preference.get("other_attempt"),
                                                "result_hash": preference.get("other_result_hash")})
                        local["origin"]["source_preference"] = preference
                        if other is not None:
                            local["value"] = {**value, "preference": {**preference, "other_trial_id": other["trial_id"],
                                             "other_attempt": other["execution_attempt"], "other_result_hash": other["result_hash"]}}
                        else:
                            # Keep the original rating and unresolved reference, but
                            # never turn an unbound preference into a current vote.
                            local["origin"]["preference_binding"] = "unmapped"
                            local["value"] = {**value, "preference": {**preference, "binding_status": "unmapped",
                                                                      "binding_reason": "other_attempt_or_result_hash_not_imported"}}
                            import_warnings.append({"kind": "unmapped_pairwise_preference", "source_assessment_id": assessment.get("id"),
                                                    "record_id": local["id"], "reason": "other_attempt_or_result_hash_not_imported"})
                    self.db.execute("INSERT INTO assessment_records VALUES(?,?,?)", (local["id"], current["id"], dumps(local)))
            self._new_reviewer(experiment_id, "导入后评审 1")
            for imported_review in (bundle or {}).get("reviews", []):
                review = imported_review.get("assessment")
                if not review:
                    continue
                left, right = ids.get(imported_review.get("A_trial_id")), ids.get(imported_review.get("B_trial_id"))
                identity = review.get("result_identity", {})
                mapped = {side: remap_identity(identity.get(side)) for side in ("A", "B")} if isinstance(identity, dict) else {}
                identity_matches = (mapped.get("A") and mapped.get("B") and mapped["A"]["trial_id"] == left and mapped["B"]["trial_id"] == right)
                if not identity_matches or mapped != self._review_identity(self.trial(left, False), self.trial(right, False)):
                    archive = {"id": uid(), "import_id": import_id, "source_review": imported_review,
                               "mapped_result_identity": mapped if identity_matches else None,
                               "binding_status": "historical_attempt" if identity_matches else "unmapped",
                               "current_result": False, "reason": "历史评价未对应两侧当前输出；仅归档，不计入当前人工偏好"}
                    self.db.execute("INSERT INTO historical_reviews VALUES(?,?,?)", (archive["id"], experiment_id, dumps(archive)))
                    import_warnings.append({"kind": "historical_human_review", "source_review_id": review.get("id"), "record_id": archive["id"], "binding_status": archive["binding_status"]})
                    continue
                if review.get("kind") not in {"human", "acceptance"}:
                    raise ValueError("历史评价类型必须为 human 或 acceptance")
                token, presentation_id = secrets.token_urlsafe(24), uid()
                self.db.execute("INSERT INTO reviewers VALUES(?,?,?,?)", (token, experiment_id, str(imported_review.get("reviewer", "历史评审"))[:80], imported_at))
                self.db.execute("INSERT INTO presentations VALUES(?,?,?,?,?,?)", (presentation_id, token, imported_review["case_id"], imported_review.get("repeat_index", 1), left, right))
                local = {**review, "id": uid(), "origin": {"import_id": import_id, "source_review_id": review.get("id")},
                         "result_identity": mapped}
                self.db.execute("INSERT INTO reviews VALUES(?,?,?)", (local["id"], presentation_id, dumps(local)))
            for historical in (bundle or {}).get("historical_reviews", []):
                archive = {"id": uid(), "import_id": import_id, "source_record": historical,
                           "binding_status": "archived_history", "current_result": False}
                self.db.execute("INSERT INTO historical_reviews VALUES(?,?,?)", (archive["id"], experiment_id, dumps(archive)))
            self.db.execute("INSERT INTO imports VALUES(?,?,?)", (import_id, imported_at, dumps(record)))
            for location, raw in imported_files:
                location.parent.mkdir(parents=True, exist_ok=True)
                location.write_bytes(raw)
        write_json(self.directory / "experiments" / experiment_id / "experiment.json", experiment)
        return {"import": record, "experiment": self.experiment_view(experiment_id)}

    def export(self, experiment_id, case_id=None):
        experiment = self.experiment(experiment_id)
        trials = [self.trial(t["id"]) for t in self.trials(experiment_id) if not case_id or t["case_id"] == case_id]
        with self.lock:
            rows = self.db.execute("SELECT p.*,r.body AS review,v.label FROM presentations p JOIN reviewers v ON v.token=p.token LEFT JOIN reviews r ON p.id=r.presentation_id WHERE v.experiment_id=?", (experiment_id,)).fetchall()
            historical_reviews = [json.loads(r[0]) for r in self.db.execute("SELECT body FROM historical_reviews WHERE experiment_id=?", (experiment_id,))]
        # Administrator export is separate from blind projection and contains explicit mapping.
        reviews = [{"pair_id": r["id"], "case_id": r["case_id"], "repeat_index": r["repeat_index"], "A_trial_id": r["left_id"], "B_trial_id": r["right_id"], "reviewer": r["label"], "assessment": json.loads(r["review"]) if r["review"] else None} for r in rows if not case_id or r["case_id"] == case_id]
        if case_id:
            experiment["cases"] = [c for c in experiment["cases"] if c["id"] == case_id]
        bundle = {"schema_version": "myeval.export.v2", "exported_at": now(), "audience": "administrator_unblinded", "filter": {"case_id": case_id},
                "experiment": experiment, "trials": trials, "reviews": reviews, "historical_reviews": historical_reviews, "comparison": self.comparison(experiment_id),
                "performance": self.performance(experiment_id, trials),
                "comparison_scope": "whole_experiment", "limitations": ["来源声明和授权按案例保存；合成或公开材料结果不构成真实用户质量结论。", "费用未知不是零；未记录供应商隐藏思维链。"]}
        bundle["historical_evidence"] = self._export_historical_evidence(experiment)
        service = getattr(self, "evaluation_service", None)
        if service is not None:
            bundle["evaluation"] = service.export_experiment(experiment_id)
        from .exporting import revalidation_index
        bundle["revalidations"] = revalidation_index(self, experiment, case_id)
        from .decisions import DecisionService
        bundle["decisions"] = DecisionService(self).list_decisions(experiment_id)
        return bundle
