from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from collections import Counter
from pathlib import Path

from .catalog import freeze
from .util import TERMINAL, digest, dumps, now, uid, write_json


class StalePresentation(ValueError):
    pass


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
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
        """)
        self.db.commit()

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
        repeats = body.get("repeats", 1)
        timeout = body.get("timeout_seconds", 300)
        if type(repeats) is not int or not 1 <= repeats <= 5:
            raise ValueError("每配置重复次数为 1–5；重复不增加独立样本数")
        if type(timeout) is not int or not 5 <= timeout <= 900:
            raise ValueError("每 trial 超时必须在 5–900 秒内")
        case_ids = body.get("case_ids")
        if not isinstance(case_ids, list):
            raise ValueError("缺少案例选择")
        pack, cases, configs = freeze(body.get("project"), case_ids, body.get("baseline_id"), body.get("candidate_id"))
        if len(cases) * repeats * 2 > 60:
            raise ValueError("一次实验最多 60 个 trial，请拆分场景")
        experiment = {"id": uid(), "created_at": now(), "project": pack["project"],
                      "title": str(body.get("title") or pack["name"])[:160], "purpose": str(body.get("purpose") or "synthetic_evaluation")[:200],
                      "pack": pack, "cases": cases, "configurations": configs, "repeats": repeats,
                      "timeout_seconds": timeout, "stop_requested_at": None, "schema_version": "myeval.v1"}
        experiment["snapshot_hash"] = digest(experiment)
        with self.lock, self.db:
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
                                 "timeout_seconds": timeout, "result": None, "cancellation": None, "platform": None}
                        self.db.execute("INSERT INTO trials VALUES(?,?,?,?)", (trial["id"], experiment["id"], trial["status"], dumps(trial)))
                        self.db.execute("INSERT INTO attempts VALUES(?,?,?)", (trial["id"], 1, dumps(trial)))
            self._new_reviewer(experiment["id"], "评审 1")
        write_json(self.directory / "experiments" / experiment["id"] / "experiment.json", experiment)
        return self.experiment_view(experiment["id"])

    def _new_reviewer(self, experiment_id, label):
        token = secrets.token_urlsafe(24)
        self.db.execute("INSERT INTO reviewers VALUES(?,?,?,?)", (token, experiment_id, label, now()))
        groups = {}
        for trial in self.trials(experiment_id):
            groups.setdefault((trial["case_id"], trial["repeat_index"]), []).append(trial["id"])
        for (case_id, repeat), ids in groups.items():
            secrets.SystemRandom().shuffle(ids)
            self.db.execute("INSERT INTO presentations VALUES(?,?,?,?,?,?)", (uid(), token, case_id, repeat, ids[0], ids[1]))
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

    def attempt_root(self, trial):
        return self.directory / "experiments" / trial["experiment_id"] / "trials" / trial["id"] / f"attempt-{trial['attempt']}"

    def claim(self):
        with self.lock, self.db:
            row = self.db.execute("SELECT body FROM trials WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
            if not row:
                return None
            trial = json.loads(row[0])
            trial.update(status="running", started_at=now())
            self._save_trial(trial)
            return trial

    def retry(self, identifier):
        with self.lock, self.db:
            trial = self.trial(identifier, False)
            if trial["status"] not in TERMINAL - {"succeeded"}:
                raise ValueError("只允许重试未成功的终态 trial")
            if trial["attempt"] >= 3:
                raise ValueError("本 trial 已达 3 次执行尝试上限；请检查证据后建立新实验")
            reviewed = self.db.execute("SELECT 1 FROM reviews r JOIN presentations p ON p.id=r.presentation_id WHERE p.left_id=? OR p.right_id=?", (identifier, identifier)).fetchone()
            if reviewed:
                raise ValueError("该配对已有评价；为保留评价对应的证据，请建立新实验")
            experiment = self.experiment(trial["experiment_id"])
            experiment["stop_requested_at"] = None
            self.db.execute("UPDATE experiments SET body=? WHERE id=?", (dumps(experiment), experiment["id"]))
            trial.update(attempt=trial["attempt"] + 1, status="queued", queued_at=now(), started_at=None, ended_at=None,
                         duration_ms=None, result=None, cancellation=None, platform=None)
            self._save_trial(trial)
        return trial

    def recover(self):
        recovered = []
        for trial in self.trials():
            if trial["status"] in {"running", "cancelling", "queued"}:
                sent = trial["status"] != "queued"
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
                                  result={"status": "interrupted", "error": {"code": "server_restart", "message": "服务重启后不自动重放；可查看旧尝试并明确重试"},
                                          "usage": None, "cost": None, "review_output": {"说明": "执行被服务重启中断，未确认交付"}},
                                  cancellation={"phase": "restart_recovery", "request_sent": "unknown" if sent else False, "upstream": "unknown" if sent else "not_sent"})
                recovered.append(trial["id"])
        return recovered

    def reviewers(self, experiment_id):
        with self.lock:
            return [{"token": r[0], "label": r[1], "url": "/blind/" + r[0]} for r in self.db.execute("SELECT token,label FROM reviewers WHERE experiment_id=?", (experiment_id,))]

    def experiment_view(self, experiment_id):
        experiment = self.experiment(experiment_id)
        trials = self.trials(experiment_id)
        counts = dict(Counter(t["status"] for t in trials))
        state = "running" if any(t["status"] not in TERMINAL for t in trials) else ("completed_with_issues" if any(t["status"] != "succeeded" for t in trials) else "completed")
        summary = {k: experiment[k] for k in ["id", "title", "project", "created_at", "purpose", "repeats", "timeout_seconds", "stop_requested_at", "snapshot_hash"]}
        summary.update(status=state, counts=counts, total_trials=len(trials), independent_cases=len(experiment["cases"]),
                       completed_trials=sum(counts.get(s, 0) for s in TERMINAL), taskpack={"id": experiment["pack"]["id"], "name": experiment["pack"]["name"], "version": experiment["pack"]["version"]},
                       configurations=experiment["configurations"], reviewers=self.reviewers(experiment_id))
        summary["trials"] = [{k: t.get(k) for k in ["id", "case_id", "configuration_id", "repeat_index", "attempt", "status", "started_at", "ended_at", "duration_ms", "cancellation", "platform"]} | {"case_name": t["case"]["name"], "error": (t.get("result") or {}).get("error")} for t in trials]
        summary["comparison"] = self.comparison(experiment_id)
        summary["rubric"] = experiment["pack"].get("rubric", [])
        return summary

    def list_experiments(self, project=None):
        with self.lock:
            ids = [r[0] for r in self.db.execute("SELECT id FROM experiments WHERE (? IS NULL OR project=?) ORDER BY created_at DESC", (project, project))]
        result = []
        for identifier in ids:
            value = self.experiment_view(identifier)
            value.pop("trials")
            value.pop("configurations")
            result.append(value)
        return result

    def blind(self, token):
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
                pairs.append({"id": presentation["id"], "case": {k: case.get(k) for k in ["name", "task", "input", "initial_state", "source", "expected_behavior"]},
                              "repeat_index": presentation["repeat_index"], "ready": ready, "revision": revision,
                              "A": {"status": left["status"], "output": (left.get("result") or {}).get("review_output")} if ready else None,
                              "B": {"status": right["status"], "output": (right.get("result") or {}).get("review_output")} if ready else None,
                              "review": self._public_review(json.loads(review[0])) if review else None})
            return {"schema_version": "myeval.blind.v1", "reviewer_label": reviewer["label"], "project": experiment["project"],
                    "taskpack_name": experiment["pack"]["name"], "rubric": experiment["pack"].get("rubric", []), "pairs": pairs,
                    "completed": sum(p["review"] is not None for p in pairs), "total": len(pairs),
                    "limitations": ["本地盲评控制配置偏见；不能阻止已看过管理员结果的人根据内容识别。"]}

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
        for evaluator, rubric in list(group_dimensions):
            for configuration_id in configs:
                model_groups.setdefault((evaluator, rubric, configuration_id), [])
        for (evaluator, rubric, configuration_id), records in model_groups.items():
            for dimension in sorted(group_dimensions.get((evaluator, rubric), [])):
                distribution, unrated, scored_trials = Counter(), 0, set()
                for trial_id, record in records:
                    value = record.get('value')
                    scores = value.get('scores', {}) if isinstance(value, dict) else {}
                    score = scores.get(dimension) if isinstance(scores, dict) else None
                    if type(score) in (int, float):
                        distribution[str(score)] += 1
                        scored_trials.add(trial_id)
                    else:
                        unrated += 1
                covered = len({trial_id for trial_id, _ in records})
                model_dimensions.append({'evaluator': evaluator, 'rubric_version': rubric,
                    'configuration_id': configuration_id, 'dimension': dimension, 'distribution': dict(distribution),
                    'unrated': unrated, 'records': len(records), 'covered_trials': covered,
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
                preferences.setdefault((row[3], row[4]), set()).add(outcome)
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
                "independent_cases": len({t["case_id"] for t in trials}), "paired_units": len(trials) // 2,
                "execution_attempts": sum(sum(g["attempt_statuses"].values()) for g in configs.values()),
                "all_trials_in_denominator": len(trials), "note": "重复采样不增加独立案例数；规则、模型评价、人工偏好分别报告，不合成总分。"}

    def add_assessment(self, trial_id, body):
        trial = self.trial(trial_id)
        if trial["status"] not in TERMINAL:
            raise ValueError("终态结果才能关联外部模型评价")
        if body.get("kind") != "model" or any(not isinstance(body.get(k), str) or not body[k].strip()
                for k in ('evaluator', 'rubric_version', 'reason')):
            raise ValueError("导入模型评价需 kind=model、evaluator、rubric_version 和 reason；不能冒充人工评价")
        record = {"id": uid(), "created_at": now(), "source": "external_import", "execution_attempt": trial["attempt"], "result_hash": digest(trial["result"]), **{k: body[k] for k in ["kind", "evaluator", "rubric_version", "reason", "value", "evidence"] if k in body}}
        with self.lock, self.db:
            self.db.execute("INSERT INTO assessment_records VALUES(?,?,?)", (record["id"], trial_id, dumps(record)))
        return record

    def export(self, experiment_id, case_id=None):
        experiment = self.experiment(experiment_id)
        trials = [self.trial(t["id"]) for t in self.trials(experiment_id) if not case_id or t["case_id"] == case_id]
        with self.lock:
            rows = self.db.execute("SELECT p.*,r.body AS review,v.label FROM presentations p JOIN reviewers v ON v.token=p.token LEFT JOIN reviews r ON p.id=r.presentation_id WHERE v.experiment_id=?", (experiment_id,)).fetchall()
        # Administrator export is separate from blind projection and contains explicit mapping.
        reviews = [{"pair_id": r["id"], "case_id": r["case_id"], "repeat_index": r["repeat_index"], "A_trial_id": r["left_id"], "B_trial_id": r["right_id"], "reviewer": r["label"], "assessment": json.loads(r["review"]) if r["review"] else None} for r in rows if not case_id or r["case_id"] == case_id]
        if case_id:
            experiment["cases"] = [c for c in experiment["cases"] if c["id"] == case_id]
        return {"schema_version": "myeval.export.v1", "exported_at": now(), "audience": "administrator_unblinded", "filter": {"case_id": case_id},
                "experiment": experiment, "trials": trials, "reviews": reviews, "comparison": self.comparison(experiment_id),
                "comparison_scope": "whole_experiment", "limitations": ["虚构材料，不构成真实用户质量结论。", "费用未知不是零；未记录供应商隐藏思维链。"]}
