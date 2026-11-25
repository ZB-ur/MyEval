"""Persistent groups of experiments with explicit, resumable creation evidence."""
from __future__ import annotations

import json
from collections import Counter

from .decisions import current_evidence_hash, normalize_policy
from .util import TERMINAL, digest, dumps, now, safe_error, uid, write_json


class CampaignService:
    def __init__(self, store):
        self.store = store
        with store.lock, store.db:
            store.db.execute("CREATE TABLE IF NOT EXISTS campaign_records(id TEXT PRIMARY KEY, campaign_key TEXT UNIQUE, created_at TEXT, body TEXT)")

    @staticmethod
    def _request(body, *, new):
        allowed = {"experiment_ids", "experiments", "start_paused", "operation_id"}
        if new:
            allowed |= {"name", "purpose", "policy", "campaign_key"}
        if not isinstance(body, dict) or set(body) - allowed:
            raise ValueError("实验批次请求含未知字段")
        ids, specs = body.get("experiment_ids", []), body.get("experiments", [])
        if not isinstance(ids, list) or any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
            raise ValueError("experiment_ids 必须是无重复的实验 ID 列表")
        if not isinstance(specs, list) or any(not isinstance(s, dict) for s in specs):
            raise ValueError("experiments 必须是实验创建参数列表")
        spec_keys = {"title", "purpose", "project", "taskpack_id", "dataset_id", "dataset_version_id", "case_ids", "baseline_id", "candidate_id", "configuration_ids", "split", "start_paused", "budget", "repeats", "timeout_seconds", "authorization"}
        if any(set(spec) - spec_keys for spec in specs):
            raise ValueError("experiments 中包含未声明的创建字段；凭据不得放入实验请求")
        if not ids and not specs or len(ids) + len(specs) > 100:
            raise ValueError("每次批次操作需包含 1–100 个实验")
        if type(body.get("start_paused", True)) is not bool:
            raise ValueError("start_paused 必须为布尔值")
        for key in ("operation_id", "campaign_key"):
            if key in body and (not isinstance(body[key], str) or not 1 <= len(body[key]) <= 200):
                raise ValueError(f"{key} 必须为 1–200 字文本")
        return ids, specs

    def _saved(self, identifier):
        row = self.store.db.execute("SELECT body FROM campaign_records WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise KeyError("实验批次不存在")
        return json.loads(row[0])

    def _save(self, record):
        self.store.db.execute("UPDATE campaign_records SET body=? WHERE id=?", (dumps(record), record["id"]))
        write_json(self.store.directory / "campaigns" / f"{record['id']}.json", record)

    def create_campaign(self, body):
        ids, specs = self._request(body, new=True)
        name = body.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 160:
            raise ValueError("批次名称需为 1–160 字文本")
        if not isinstance(body.get("purpose", ""), str) or len(body.get("purpose", "")) > 2000:
            raise ValueError("purpose 最多 2000 字")
        policy = normalize_policy(body.get("policy")) if body.get("policy") is not None else None
        request_hash = digest(body)
        with self.store.lock, self.store.db:
            if body.get("campaign_key"):
                row = self.store.db.execute("SELECT body FROM campaign_records WHERE campaign_key=?", (body["campaign_key"],)).fetchone()
                if row:
                    previous = json.loads(row[0])
                    if previous["request_hash"] != request_hash:
                        raise ValueError("campaign_key 已被不同创建请求使用")
                    return self._view(previous)
            # Validate all existing references before creating anything.
            for identifier in ids:
                self.store.experiment(identifier)
            record = {"id": uid(), "schema_version": "myeval.campaign.v1", "created_at": now(), "updated_at": now(),
                      "name": name.strip(), "purpose": body.get("purpose", ""), "policy": policy,
                      "policy_hash": digest(policy) if policy else None, "request_hash": request_hash,
                      "campaign_key": body.get("campaign_key"), "entries": [], "operations": [], "status": "preparing"}
            self.store.db.execute("INSERT INTO campaign_records VALUES(?,?,?,?)", (record["id"], record["campaign_key"], record["created_at"], dumps(record)))
        return self.add_experiments(record["id"], {"experiment_ids": ids, "experiments": specs,
                    "start_paused": body.get("start_paused", True), "operation_id": body.get("operation_id", "initial")})

    def add_experiments(self, campaign_id, body):
        ids, specs = self._request(body, new=False)
        fingerprint = digest(body)
        with self.store.lock:
            record = self._saved(campaign_id)
            operation_id = body.get("operation_id") or uid()
            prior = next((op for op in record["operations"] if op["id"] == operation_id), None)
            if prior:
                if prior["request_hash"] != fingerprint:
                    raise ValueError("operation_id 已被不同请求使用")
                return self._view(record)
            for identifier in ids:
                self.store.experiment(identifier)
            operation = {"id": operation_id, "request_hash": fingerprint, "started_at": now(), "completed_at": None,
                         "requested_existing": len(ids), "requested_new": len(specs), "status": "preparing"}
            record["operations"].append(operation)
            with self.store.db:
                self._save(record)
            linked = {entry.get("experiment_id") for entry in record["entries"]}
            for identifier in ids:
                if identifier not in linked:
                    record["entries"].append(self._entry(self.store.experiment(identifier), operation_id, "linked"))
                    linked.add(identifier)
            with self.store.db:
                self._save(record)
            # Each experiment commits independently. Keep earlier successes if a later spec fails.
            for index, original in enumerate(specs):
                spec = dict(original)
                spec.setdefault("start_paused", body.get("start_paused", True))
                before_ids = {row[0] for row in self.store.db.execute("SELECT id FROM experiments")}
                try:
                    created = self.store.create_experiment(spec)
                    experiment = self.store.experiment(created["id"])
                    entry = self._entry(experiment, operation_id, "created")
                    entry["request_hash"] = digest(spec)
                    entry["request_index"] = index
                except Exception as error:
                    # Store may commit before a filesystem mirror/response fails. Never lose that experiment or replay it.
                    added = [row[0] for row in self.store.db.execute("SELECT id FROM experiments") if row[0] not in before_ids]
                    if len(added) == 1:
                        entry = self._entry(self.store.experiment(added[0]), operation_id, "created_with_error")
                        entry.update(request_index=index, request_hash=digest(spec),
                                     error={"type": type(error).__name__, "message": safe_error(error)}, replayed=False)
                    else:
                        entry = {"id": uid(), "operation_id": operation_id, "kind": "creation_failed", "created_at": now(),
                                 "request_index": index, "request_hash": digest(spec), "request": spec,
                                 "error": {"type": type(error).__name__, "message": safe_error(error)},
                                 "experiment_id": None, "replayed": False}
                record["entries"].append(entry)
                record["updated_at"] = now()
                with self.store.db:
                    self._save(record)
            operation["completed_at"] = now()
            operation["status"] = "partial" if any(entry["kind"] in {"creation_failed", "created_with_error"} and entry["operation_id"] == operation_id for entry in record["entries"]) else "completed"
            record["status"] = "partial" if any(entry["kind"] in {"creation_failed", "created_with_error"} for entry in record["entries"]) else "created"
            record["updated_at"] = now()
            with self.store.db:
                self._save(record)
            return self._view(record)

    @staticmethod
    def _entry(experiment, operation_id, kind):
        return {"id": uid(), "operation_id": operation_id, "kind": kind, "created_at": now(),
                "experiment_id": experiment["id"], "experiment_snapshot_hash": experiment.get("snapshot_hash"),
                "project": experiment["project"], "title": experiment.get("title"),
                "dataset": experiment.get("dataset"), "taskpack_id": experiment.get("taskpack_id") or experiment.get("pack", {}).get("id"),
                "taskpack_hash": experiment.get("pack", {}).get("content_hash"), "split": experiment.get("split"),
                "configuration_ids": [c["id"] for c in experiment.get("configurations", [])],
                "baseline_id": experiment.get("baseline_id") or next((c["id"] for c in experiment.get("configurations", [])), None)}

    def get_campaign(self, campaign_id):
        with self.store.lock:
            return self._view(self._saved(campaign_id))

    def list_campaigns(self):
        with self.store.lock:
            rows = self.store.db.execute("SELECT body FROM campaign_records ORDER BY created_at DESC,rowid DESC").fetchall()
            result = []
            for row in rows:
                value = self._view(json.loads(row[0]))
                value.pop("entries", None)
                value.pop("operations", None)
                result.append(value)
            return result

    def _view(self, record):
        # No cross-project score average: progress and gates remain per experiment.
        result = json.loads(dumps(record))
        progress, outcomes = Counter(), Counter()
        has_decisions = self.store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='decision_records'").fetchone()
        for entry in result["entries"]:
            identifier = entry.get("experiment_id")
            if not identifier:
                progress["creation_failed"] += 1
                continue
            counts = dict(self.store.db.execute("SELECT status,count(*) FROM trials WHERE experiment_id=? GROUP BY status", (identifier,)))
            total = sum(counts.values())
            terminal = sum(counts.get(status, 0) for status in TERMINAL)
            experiment = self.store.experiment(identifier)
            paused = bool((experiment.get("queue") or {}).get("paused"))
            state = "running" if counts.get("running") or counts.get("cancelling") else ("paused" if paused else "queued") if terminal < total else "completed" if counts.get("succeeded", 0) == total and total else "completed_with_issues"
            progress[state] += 1
            entry["progress"] = {"status": state, "paused": paused, "counts": counts, "total_trials": total, "terminal_trials": terminal}
            if has_decisions:
                row = self.store.db.execute("SELECT body FROM decision_records WHERE experiment_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (identifier,)).fetchone()
                decision = json.loads(row[0]) if row else None
            else:
                decision = None
            entry["latest_decision"] = {key: decision[key] for key in ("id", "status", "scope", "created_at", "policy_hash", "evidence_snapshot_hash")} if decision else None
            if decision:
                current = current_evidence_hash(self.store, identifier) == decision["evidence_snapshot_hash"]
                entry["latest_decision"]["evidence_current"] = current
                gate_policy = dict(decision["policy"])
                if record.get("policy") and not record["policy"].get("candidate_ids"):
                    gate_policy["candidate_ids"] = []
                policy_matches = record.get("policy") is None or gate_policy == record["policy"]
                entry["latest_decision"]["matches_campaign_policy"] = policy_matches
                outcomes["stale" if not current else "different_policy" if not policy_matches else decision["status"]] += 1
            else:
                outcomes["not_evaluated"] += 1
        result["experiment_count"] = sum(1 for entry in result["entries"] if entry.get("experiment_id"))
        result["projects"] = sorted({entry["project"] for entry in result["entries"] if entry.get("project")})
        result["progress"] = dict(progress)
        result["decision_counts"] = dict(outcomes)
        result["incomplete_operations"] = [op["id"] for op in result["operations"] if not op.get("completed_at")]
        result["requires_creation_review"] = bool(result["incomplete_operations"])
        result["limitations"] = ["批次仅编排与追踪实验，不把不同项目和量表的分数混合。", "新实验默认暂停调度；只有显式恢复或 start_paused=false 才允许执行。", "部分创建失败保留请求与原因；重复幂等请求不会自动重试失败项。"]
        return result
