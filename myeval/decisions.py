"""Versioned, explainable gates over saved evidence; never a release action."""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict

from .catalog import case_family
from .util import TERMINAL, digest, dumps, now, uid, write_json


DECISION_ALGORITHM_VERSION = "myeval.gate.v2.1-pair-context"

_DEFAULT_SEVERITIES = ["critical", "high", "medium", "low", "unknown"]
_POLICY_KEYS = {"name", "version", "scope", "candidate_ids", "min_families", "min_completion_rate",
                "min_success_rate", "require_holdout", "rules", "model_criteria", "min_human_review_pairs"}


def _number(value, name, *, minimum=None, maximum=None, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} 必须为有限数值")
    if integer and type(value) is not int:
        raise ValueError(f"{name} 必须为整数")
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise ValueError(f"{name} 超出允许范围")
    return value


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise ValueError(f"{name} 必须为 1–300 字的非空文本")
    return value.strip()


def _strings(value, name):
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError(f"{name} 必须是非空字符串列表")
    if len(value) != len(set(value)):
        raise ValueError(f"{name} 不能重复")
    return value


def normalize_policy(value=None):
    """Public for campaigns to freeze the same policy before a run starts."""
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - _POLICY_KEYS:
        raise ValueError("判定策略含未知字段；请使用文档中的 policy 格式")
    policy = {"name": _text(value.get("name", "质量与执行门槛"), "policy.name"),
              "version": _text(value.get("version", "1"), "policy.version"),
              "scope": value.get("scope", "quality"),
              "candidate_ids": _strings(value.get("candidate_ids", []), "candidate_ids"),
              "min_families": _number(value.get("min_families", 5), "min_families", minimum=1, integer=True),
              "min_completion_rate": _number(value.get("min_completion_rate", 1), "min_completion_rate", minimum=0, maximum=1),
              "min_success_rate": _number(value.get("min_success_rate", 1), "min_success_rate", minimum=0, maximum=1),
              "require_holdout": value.get("require_holdout", False),
              "min_human_review_pairs": _number(value.get("min_human_review_pairs", 0), "min_human_review_pairs", minimum=0, integer=True)}
    if policy["scope"] not in {"quality", "execution"} or type(policy["require_holdout"]) is not bool:
        raise ValueError("scope 为 quality 或 execution；require_holdout 为布尔值")
    rules = value.get("rules", {})
    if not isinstance(rules, dict) or set(rules) - {"required", "max_failures", "severities", "dimensions"}:
        raise ValueError("rules 格式不正确")
    policy["rules"] = {"required": rules.get("required", True),
                       "max_failures": _number(rules.get("max_failures", 0), "rules.max_failures", minimum=0, integer=True),
                       "severities": _strings(rules.get("severities", _DEFAULT_SEVERITIES), "rules.severities"),
                       "dimensions": _strings(rules.get("dimensions", []), "rules.dimensions")}
    if type(policy["rules"]["required"]) is not bool or not policy["rules"]["severities"]:
        raise ValueError("rules.required 为布尔值，severities 不能为空")
    criteria = value.get("model_criteria", [])
    if not isinstance(criteria, list) or len(criteria) > 50:
        raise ValueError("model_criteria 必须为最多 50 项的列表")
    policy["model_criteria"] = []
    for source in criteria:
        if not isinstance(source, dict) or set(source) - {"dimension", "evaluator", "rubric_version", "min_mean", "min_delta", "min_coverage", "min_paired_families", "job_id"}:
            raise ValueError("model_criteria 项含未知字段")
        criterion = {key: _text(source.get(key), f"model_criteria.{key}")
                     for key in ("dimension", "evaluator", "rubric_version")}
        if not any(key in source for key in ("min_mean", "min_delta")):
            raise ValueError("模型门槛必须提供 min_mean 或 min_delta")
        for key in ("min_mean", "min_delta"):
            if key in source:
                criterion[key] = _number(source[key], f"model_criteria.{key}")
        criterion["min_coverage"] = _number(source.get("min_coverage", 1), "model_criteria.min_coverage", minimum=0, maximum=1)
        criterion["min_paired_families"] = _number(source.get("min_paired_families", policy["min_families"]), "model_criteria.min_paired_families", minimum=1, integer=True)
        if "job_id" in source:
            criterion["job_id"] = _text(source["job_id"], "model_criteria.job_id")
        policy["model_criteria"].append(criterion)
    return policy


def result_identities(trials):
    return [{"trial_id": t["id"], "case_id": t["case_id"], "family_id": case_family(t["case"]),
             "configuration_id": t["configuration_id"], "execution_attempt": t["attempt"],
             "status": t["status"], "case_hash": t.get("case_hash") or digest(t["case"]),
             "configuration_hash": t.get("configuration_hash") or digest(t["configuration"]),
             "result_hash": digest(t.get("result"))} for t in trials]


def current_evidence_hash(store, experiment_id, trials=None):
    trials = store.trials(experiment_id) if trials is None else trials
    assessments = [json.loads(row[0]) for row in store.db.execute(
        "SELECT a.body FROM assessment_records a JOIN trials t ON t.id=a.trial_id WHERE t.experiment_id=? ORDER BY a.id", (experiment_id,))]
    reviews = [json.loads(row[0]) for row in store.db.execute(
        "SELECT r.body FROM reviews r JOIN presentations p ON p.id=r.presentation_id JOIN reviewers v ON v.token=p.token WHERE v.experiment_id=? ORDER BY r.id", (experiment_id,))]
    return digest({"algorithm_version": DECISION_ALGORITHM_VERSION,
                   "trial_results": result_identities(trials), "assessments": assessments, "reviews": reviews})


def _status(checks):
    states = {check["status"] for check in checks}
    return "fail" if "fail" in states else "inconclusive" if "inconclusive" in states or not checks else "pass"


def _check(code, status, message, *, actual=None, threshold=None, configuration_id=None, evidence=None):
    item = {"code": code, "status": status, "message": message, "actual": actual, "threshold": threshold}
    if configuration_id is not None:
        item["configuration_id"] = configuration_id
    if evidence is not None:
        item["evidence"] = evidence
    return item


def _family_mean(values):
    """One value per trial -> repeats per case -> cases per family, all equal weight."""
    families = defaultdict(lambda: defaultdict(list))
    for trial, value in values:
        families[case_family(trial["case"])][trial["case_id"]].append(value)
    means = {family: sum(sum(v) / len(v) for v in cases.values()) / len(cases)
             for family, cases in families.items() if cases}
    return {"mean": sum(means.values()) / len(means) if means else None,
            "family_count": len(means), "family_means": means,
            "case_count": sum(len(cases) for cases in families.values()), "trial_count": len(values)}


class DecisionService:
    def __init__(self, store):
        self.store = store
        with store.lock, store.db:
            store.db.execute("CREATE TABLE IF NOT EXISTS decision_records(id TEXT PRIMARY KEY, experiment_id TEXT REFERENCES experiments(id), created_at TEXT, body TEXT)")
            store.db.execute("CREATE INDEX IF NOT EXISTS decisions_experiment ON decision_records(experiment_id,created_at)")

    def list_decisions(self, experiment_id):
        self.store.experiment(experiment_id)
        with self.store.lock:
            current_hash = current_evidence_hash(self.store, experiment_id)
            return [self._freshness(json.loads(row[0]), current_hash) for row in self.store.db.execute(
                "SELECT body FROM decision_records WHERE experiment_id=? ORDER BY created_at DESC,rowid DESC", (experiment_id,))]

    def get_decision(self, decision_id):
        with self.store.lock:
            row = self.store.db.execute("SELECT body FROM decision_records WHERE id=?", (decision_id,)).fetchone()
        if row is None:
            raise KeyError("判定记录不存在")
        record = json.loads(row[0])
        with self.store.lock:
            return self._freshness(record, current_evidence_hash(self.store, record["experiment_id"]))

    @staticmethod
    def _freshness(record, current_hash):
        record["evidence_current"] = record["evidence_snapshot_hash"] == current_hash
        record["current_evidence_snapshot_hash"] = current_hash
        return record

    def create_decision(self, experiment_id, body):
        if not isinstance(body, dict) or set(body) - {"policy"}:
            raise ValueError("判定请求格式为 {policy:{...}}")
        policy = normalize_policy(body.get("policy"))
        # Snapshot and persist under the same lock. A retry cannot race evidence identity.
        with self.store.lock, self.store.db:
            experiment = self.store.experiment(experiment_id)
            trials = [self.store.trial(trial["id"]) for trial in self.store.trials(experiment_id)]
            configurations = experiment.get("configurations", [])
            config_ids = [c["id"] for c in configurations]
            baseline = experiment.get("baseline_id") or (config_ids[0] if config_ids else None)
            candidates = policy["candidate_ids"] or [cid for cid in config_ids if cid != baseline]
            if not candidates or any(cid not in config_ids or cid == baseline for cid in candidates):
                raise ValueError("candidate_ids 必须选择本实验中基线以外的候选配置")
            policy["candidate_ids"] = candidates
            checks, candidates_view = [], []
            evidence_ids = result_identities(trials)
            for cid in candidates:
                selected = [t for t in trials if t["configuration_id"] == cid]
                candidate = self._candidate_checks(experiment, selected, cid, policy)
                candidate["model"] = [self._model_check(trials, baseline, cid, criterion) for criterion in policy["model_criteria"]]
                for model in candidate["model"]:
                    candidate["checks"].extend(model["checks"])
                human = self._human_reviews(experiment_id, trials, baseline, cid)
                candidate["human_reviews"] = human
                if policy["min_human_review_pairs"]:
                    actual = human["comparable_pairs"]
                    candidate["checks"].append(_check("human_review_coverage", "pass" if actual >= policy["min_human_review_pairs"] else "inconclusive",
                        "仅计入绑定当前结果的真人评价配对；验收试填与模型评价不计入。", actual=actual,
                        threshold=policy["min_human_review_pairs"], configuration_id=cid, evidence=human["review_ids"]))
                if policy["scope"] == "quality" and not policy["model_criteria"]:
                    candidate["checks"].append(_check("quality_criterion_required", "inconclusive",
                        "质量判定需要明确的独立模型质量门槛；真人评价数量仅证明覆盖，不等于质量获胜。",
                        configuration_id=cid))
                candidate["status"] = _status(candidate["checks"])
                candidates_view.append(candidate)
                checks.extend(candidate["checks"])
            record = {"id": uid(), "schema_version": "myeval.decision.v1", "experiment_id": experiment_id,
                      "created_at": now(), "status": _status(checks), "scope": policy["scope"],
                      "algorithm_version": DECISION_ALGORITHM_VERSION,
                      "policy": policy, "policy_hash": digest(policy), "baseline_id": baseline,
                      "experiment_snapshot_hash": experiment.get("snapshot_hash"),
                      "evidence_snapshot_hash": current_evidence_hash(self.store, experiment_id, trials),
                      "result_snapshot_hash": digest(evidence_ids), "trial_results": evidence_ids,
                      "dataset": experiment.get("dataset"), "import_origin": experiment.get("import_origin") or experiment.get("origin"),
                      "source_types": dict(Counter(self._source_type(t["case"]) for t in trials)),
                      "candidates": candidates_view, "checks": checks,
                      "execution_denominator": len(trials),
                      "limitations": ["判定仅针对冻结策略和当前证据，不切换产品默认、不发布部署。",
                                      "质量分与差值是按情境家族等权的描述统计，不是显著性证明；失败仍在执行成功率分母中。",
                                      "模型评价、真人评价与验收试填分别报告；同一案例重复和重评不增加独立家族数。",
                                      "本记录保存判定时的策略，不自动证明该策略在观察结果前预注册；如需预注册，请先建立带策略的实验批次。"]}
            self.store.db.execute("INSERT INTO decision_records VALUES(?,?,?,?)", (record["id"], experiment_id, record["created_at"], dumps(record)))
        write_json(self.store.directory / "decisions" / f"{record['id']}.json", record)
        return self._freshness(record, record["evidence_snapshot_hash"])

    @staticmethod
    def _source_type(case):
        source = case.get("source") or {}
        return source.get("type") or ("synthetic" if source.get("synthetic") is True else "unknown")

    @staticmethod
    def _candidate_checks(experiment, trials, cid, policy):
        counts = Counter(t["status"] for t in trials)
        total = len(trials)
        completed = sum(counts[s] for s in TERMINAL)
        families = len({case_family(t["case"]) for t in trials})
        terminal = total > 0 and completed == total
        completion_rate = completed / total if total else None
        success_rate = counts["succeeded"] / total if total else None
        checks = [_check("observed_terminal_evidence", "pass" if completed else "inconclusive",
                         "至少需要一个已保存的执行终态；空队列不能通过门槛。", actual=completed, threshold=1, configuration_id=cid),
                  _check("families", "pass" if families >= policy["min_families"] else "inconclusive",
                         "独立情境按 family_id 计数；变式与重复不增加家族数。", actual=families, threshold=policy["min_families"], configuration_id=cid),
                  _check("completion_rate", "pass" if completion_rate is not None and completion_rate >= policy["min_completion_rate"] else "inconclusive",
                         "未终态的执行使结论保持待定。", actual=completion_rate, threshold=policy["min_completion_rate"], configuration_id=cid),
                  _check("success_rate", "pass" if success_rate is not None and success_rate >= policy["min_success_rate"] else ("fail" if terminal else "inconclusive"),
                         "成功率分母包含失败、超时、取消、拒绝、unsupported 与尚未完成的 trial。", actual=success_rate,
                         threshold=policy["min_success_rate"], configuration_id=cid)]
        if policy["require_holdout"]:
            splits = Counter(t["case"].get("split") or (experiment.get("dataset") or {}).get("split") or experiment.get("split") or "unknown" for t in trials)
            checks.append(_check("holdout_only", "pass" if total and splits.get("holdout", 0) == total else "inconclusive",
                "标记为留出仅证明数据标签；独立创建与候选冻结仍需数据来源审计。", actual=dict(splits), threshold="all holdout", configuration_id=cid))
            dataset = experiment.get("dataset") if isinstance(experiment.get("dataset"), dict) else {}
            independence = dataset.get("holdout_independence", "unknown")
            exposed = dataset.get("exposed_holdout_families")
            exposure_list_valid = isinstance(exposed, list) and all(isinstance(f, str) for f in exposed)
            selected_families = {case_family(t["case"]) for t in trials}
            affected = sorted(selected_families & set(exposed)) if exposure_list_valid else []
            if affected or independence == "previously_exposed" and (not exposure_list_valid or not exposed):
                independence_status = "fail"
            elif independence in {"declared_unexposed", "previously_exposed"} and exposure_list_valid:
                independence_status = "pass" if total and splits.get("holdout", 0) == total else "inconclusive"
            else:
                independence_status = "inconclusive"
            checks.append(_check("holdout_independence", independence_status,
                "已披露给开发流程的情境不能继续作为独立留出；未暴露声明仍不是外部审计证明。",
                actual={"declaration": independence, "selected_exposed_families": affected,
                        "exposed_holdout_families": exposed if exposure_list_valid else None},
                threshold="selected holdout families declared unexposed", configuration_id=cid))
        rule_policy, rule_statuses, failures, covered, missing = policy["rules"], Counter(), [], 0, []
        for trial in trials:
            raw_rules = (trial.get("result") or {}).get("assessments")
            rules = [r for r in raw_rules if isinstance(r, dict) and r.get("kind", "rule") == "rule"] if isinstance(raw_rules, list) else []
            if rule_policy["dimensions"]:
                rules = [r for r in rules if r.get("dimension") in rule_policy["dimensions"]]
            valid = [r for r in rules if r.get("status") in {"pass", "fail", "not_applicable"}]
            dimensions = {r.get("dimension") for r in valid}
            complete = bool(valid) and (not rule_policy["dimensions"] or set(rule_policy["dimensions"]) <= dimensions)
            if complete:
                covered += 1
            else:
                missing.append(trial["id"])
            for rule in rules:
                rule_statuses[rule.get("status", "unknown")] += 1
                severity = str(rule.get("severity") or "unknown").lower()
                if rule.get("status") == "fail" and severity in rule_policy["severities"]:
                    failures.append({"trial_id": trial["id"], "execution_attempt": trial["attempt"], "dimension": rule.get("dimension"), "severity": severity})
        if rule_policy["required"] and covered < total or rule_policy["required"] and total == 0:
            checks.append(_check("rule_coverage", "inconclusive", "部分 trial 没有要求的规则终态；未检查不能算通过。",
                actual=covered, threshold=total, configuration_id=cid, evidence=missing))
        elif rule_policy["required"]:
            checks.append(_check("rule_coverage", "pass", "每项 trial 均具有要求的规则终态。", actual=covered, threshold=total, configuration_id=cid))
        checks.append(_check("rule_failures", "fail" if len(failures) > rule_policy["max_failures"] else "pass",
            "按显式严重性筛选规则失败；未标注严重性的规则归入 unknown。", actual=len(failures), threshold=rule_policy["max_failures"], configuration_id=cid, evidence=failures))
        return {"configuration_id": cid, "checks": checks, "execution": {"counts": dict(counts), "total_trials": total, "terminal_trials": completed,
                "family_count": families, "completion_rate": completion_rate, "success_rate": success_rate},
                "rules": {"statuses": dict(rule_statuses), "covered_trials": covered, "total_trials": total, "failures": failures}}

    @staticmethod
    def _selected_scores(trials, criterion, baseline_id, candidate_id):
        """Select a complete scoring context, never independent latest pair sides.

        Pairwise scores depend on the comparator. The latest unit for the exact
        baseline/candidate pair wins, including an incomplete unit (which blocks
        fallback). Individual scores are eligible only when no pair unit exists.
        """
        selected, excluded = {}, []
        trial_map = {trial["id"]: trial for trial in trials}
        individual, pair_units = defaultdict(list), defaultdict(dict)

        def stamp(record):
            return (str(record.get("created_at") or ""), str(record.get("id") or ""))

        def exclude(trial_id, record, reason):
            excluded.append({"assessment_id": record.get("id"), "trial_id": trial_id, "reason": reason})

        def metadata(record):
            value = record.get("value") if isinstance(record.get("value"), dict) else {}
            provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
            job = record.get("job_id") or record.get("evaluation_job_id") or provenance.get("job_id") or value.get("job_id")
            return value, job

        def projection(trial, record=None, context="unrated", unavailable_reason=None):
            if record is None:
                return {"status": "unrated", "score": None, "assessment_id": None,
                        "scoring_context": context, "unavailable_reason": unavailable_reason}
            value, job = metadata(record)
            scores = value.get("scores") if isinstance(value.get("scores"), dict) else {}
            statuses = value.get("score_status") if isinstance(value.get("score_status"), dict) else {}
            score = scores.get(criterion["dimension"])
            status = statuses.get(criterion["dimension"], "rated" if type(score) in {int, float} else "unrated")
            if status == "rated" and not (type(score) in {int, float} and math.isfinite(score)) or status not in {"rated", "not_applicable", "unavailable", "unrated"}:
                status = "unrated"
            if unavailable_reason or trial["status"] != "succeeded":
                status = "unavailable"
                unavailable_reason = unavailable_reason or "execution_not_succeeded"
            preference = value.get("preference") if isinstance(value.get("preference"), dict) else {}
            return {"status": status, "score": score if status == "rated" else None, "assessment_id": record.get("id"),
                    "execution_attempt": trial["attempt"], "result_hash": record["result_hash"],
                    "scoring_context": context, "job_id": job,
                    "unit_id": preference.get("unit_id") or value.get("unit_id"),
                    "other_trial_id": preference.get("other_trial_id"), "other_attempt": preference.get("other_attempt"),
                    "other_result_hash": preference.get("other_result_hash"),
                    "evaluator_hash": value.get("evaluator_hash"), "rubric_hash": value.get("rubric_hash"),
                    "unavailable_reason": unavailable_reason}

        for trial in trials:
            selected[trial["id"]] = projection(trial)
            for record in trial.get("assessment_records", []):
                if not isinstance(record, dict) or record.get("kind") != "model" or record.get("evaluator") != criterion["evaluator"] or record.get("rubric_version") != criterion["rubric_version"]:
                    continue
                if record.get("execution_attempt") != trial["attempt"] or record.get("result_hash") != digest(trial.get("result")):
                    exclude(trial["id"], record, "not_current_attempt_or_result")
                    continue
                value, job = metadata(record)
                if criterion.get("job_id") and job != criterion["job_id"]:
                    continue
                # A malformed pair marker must never turn into an individual rating.
                if "preference" not in value:
                    individual[trial["id"]].append(record)
                    continue
                preference = value.get("preference")
                if not isinstance(preference, dict):
                    exclude(trial["id"], record, "invalid_pair_reference")
                    continue
                other = trial_map.get(preference.get("other_trial_id"))
                if other is None or {trial["configuration_id"], other["configuration_id"]} != {baseline_id, candidate_id}:
                    exclude(trial["id"], record, "other_comparison_context")
                    continue
                unit = preference.get("unit_id") or value.get("unit_id")
                if (not isinstance(unit, str) or not unit or other["id"] == trial["id"]
                        or value.get("unit_id") and value["unit_id"] != unit
                        or trial["case_id"] != other["case_id"] or trial["repeat_index"] != other["repeat_index"]
                        or (trial.get("case_hash") or digest(trial["case"])) != (other.get("case_hash") or digest(other["case"]))
                        or preference.get("other_attempt") != other["attempt"]
                        or preference.get("other_result_hash") != digest(other.get("result"))):
                    exclude(trial["id"], record, "invalid_pair_reference")
                    continue
                key = (*sorted((trial["id"], other["id"])), str(job or ""), unit)
                old = pair_units[key].get(trial["id"])
                if old is None or stamp(record) > stamp(old):
                    if old is not None:
                        exclude(trial["id"], old, "superseded_pair_side")
                    pair_units[key][trial["id"]] = record
                else:
                    exclude(trial["id"], record, "superseded_pair_side")

        latest_pairs = {}
        for unit_key, sides in pair_units.items():
            pair_key = unit_key[:2]
            rank = (max(stamp(record) for record in sides.values()), unit_key[2:])
            previous = latest_pairs.get(pair_key)
            if previous is None or rank > previous["rank"]:
                if previous:
                    for trial_id, record in previous["sides"].items():
                        exclude(trial_id, record, "superseded_pair_unit")
                latest_pairs[pair_key] = {"rank": rank, "sides": sides, "job_id": unit_key[2], "unit_id": unit_key[3]}
            else:
                for trial_id, record in sides.items():
                    exclude(trial_id, record, "superseded_pair_unit")

        baselines = {(trial["case_id"], trial["repeat_index"]): trial for trial in trials if trial["configuration_id"] == baseline_id}
        for candidate in (trial for trial in trials if trial["configuration_id"] == candidate_id):
            baseline = baselines.get((candidate["case_id"], candidate["repeat_index"]))
            participants = [baseline, candidate] if baseline else [candidate]
            pair = latest_pairs.get(tuple(sorted((baseline["id"], candidate["id"])))) if baseline else None
            if pair:
                sides = pair["sides"]
                complete = baseline["id"] in sides and candidate["id"] in sides
                reason = None if complete else "incomplete_pair_score"
                if complete:
                    left, right = (metadata(sides[trial["id"]])[0] for trial in participants)
                    if any(left.get(key) and right.get(key) and left[key] != right[key] for key in ("evaluator_hash", "rubric_hash")):
                        reason = "pair_evaluator_or_rubric_hash_mismatch"
                for trial in participants:
                    record = sides.get(trial["id"])
                    selected[trial["id"]] = projection(trial, record, "pairwise", reason)
                    for old in individual.get(trial["id"], []):
                        exclude(trial["id"], old, "individual_superseded_by_pair_context")
            else:
                for trial in participants:
                    records = sorted(individual.get(trial["id"], []), key=stamp)
                    selected[trial["id"]] = projection(trial, records[-1] if records else None, "individual")
                    for old in records[:-1]:
                        exclude(trial["id"], old, "superseded_individual_assessment")
        return selected, excluded

    def _model_check(self, all_trials, baseline, cid, criterion):
        relevant = [t for t in all_trials if t["configuration_id"] in {baseline, cid}]
        selected, excluded = self._selected_scores(relevant, criterion, baseline, cid)
        candidates = [t for t in relevant if t["configuration_id"] == cid]
        states = Counter(selected[t["id"]]["status"] for t in candidates)
        eligible = len(candidates) - states["not_applicable"]
        coverage = states["rated"] / eligible if eligible else None
        mean = _family_mean([(t, selected[t["id"]]["score"]) for t in candidates if selected[t["id"]]["status"] == "rated"])
        checks = [_check("model_coverage", "pass" if coverage is not None and coverage >= criterion["min_coverage"] else "inconclusive",
            "无判断机会单列；未评、不可用及失败不被补成零。", actual=coverage, threshold=criterion["min_coverage"], configuration_id=cid,
            evidence={"dimension": criterion["dimension"], "statuses": dict(states), "eligible_trials": eligible})]
        if "min_mean" in criterion:
            enough = mean["family_count"] >= criterion["min_paired_families"]
            checks.append(_check("model_mean", "inconclusive" if mean["mean"] is None or not enough else "pass" if mean["mean"] >= criterion["min_mean"] else "fail",
                "分值先在案例内平均，再在情境家族内平均，最后家族等权；这是描述统计。", actual=mean, threshold=criterion["min_mean"], configuration_id=cid,
                evidence={"dimension": criterion["dimension"], "minimum_scored_families": criterion["min_paired_families"]}))
        pairs, unmatched, pair_not_applicable = [], [], 0
        baselines = {(t["case_id"], t["repeat_index"]): t for t in relevant if t["configuration_id"] == baseline}
        for trial in candidates:
            base = baselines.get((trial["case_id"], trial["repeat_index"]))
            reason = None
            if base is None:
                reason = "baseline_missing"
            elif (base.get("case_hash") or digest(base["case"])) != (trial.get("case_hash") or digest(trial["case"])):
                reason = "different_case_snapshot"
            elif base["status"] != "succeeded" or trial["status"] != "succeeded":
                reason = "execution_not_succeeded"
            elif selected[base["id"]]["unavailable_reason"] or selected[trial["id"]]["unavailable_reason"]:
                reason = selected[base["id"]]["unavailable_reason"] or selected[trial["id"]]["unavailable_reason"]
            elif selected[base["id"]]["scoring_context"] != selected[trial["id"]]["scoring_context"]:
                reason = "scoring_context_mismatch"
            elif any(selected[base["id"]].get(key) and selected[trial["id"]].get(key)
                     and selected[base["id"]][key] != selected[trial["id"]][key] for key in ("evaluator_hash", "rubric_hash")):
                reason = "evaluator_or_rubric_hash_mismatch"
            elif selected[base["id"]]["status"] != "rated" or selected[trial["id"]]["status"] != "rated":
                reason = "both_scores_not_available"
            if selected[trial["id"]]["status"] == "not_applicable" or base and selected[base["id"]]["status"] == "not_applicable":
                pair_not_applicable += 1
            if reason:
                unmatched.append({"trial_id": trial["id"], "baseline_trial_id": base["id"] if base else None, "reason": reason})
            else:
                pairs.append((trial, selected[trial["id"]]["score"] - selected[base["id"]]["score"]))
        paired_delta = _family_mean(pairs)
        paired_eligible = len(candidates) - pair_not_applicable
        paired_coverage = len(pairs) / paired_eligible if paired_eligible else None
        if "min_delta" in criterion:
            checks.append(_check("model_paired_coverage", "pass" if paired_coverage is not None and paired_coverage >= criterion["min_coverage"] else "inconclusive",
                "配对缺失和执行失败仍进入配对覆盖率分母，不能只靠少数可用配对通过。", actual=paired_coverage,
                threshold=criterion["min_coverage"], configuration_id=cid,
                evidence={"eligible_pairs": paired_eligible, "not_applicable_pairs": pair_not_applicable, "rated_success_pairs": len(pairs)}))
            enough = paired_delta["family_count"] >= criterion["min_paired_families"]
            checks.append(_check("model_paired_delta", "inconclusive" if paired_delta["mean"] is None or not enough else "pass" if paired_delta["mean"] >= criterion["min_delta"] else "fail",
                "仅比较同案例同重复与当前结果；pairwise 还要求同一 job/unit 及双方互指身份，individual 保持独立评分上下文。",
                actual=paired_delta, threshold=criterion["min_delta"], configuration_id=cid,
                evidence={"dimension": criterion["dimension"], "minimum_paired_families": criterion["min_paired_families"], "unmatched": unmatched}))
        return {"criterion": criterion, "selection": "latest_pair_unit_for_exact_comparator_or_latest_individual_context", "scores": selected,
                "excluded_assessments": excluded, "score_statuses": dict(states), "coverage": coverage,
                "mean": mean, "paired_delta": paired_delta, "paired_coverage": paired_coverage, "unmatched_pairs": unmatched, "checks": checks}

    def _human_reviews(self, experiment_id, trials, baseline, cid):
        lookup = {t["id"]: t for t in trials}
        rows = self.store.db.execute("SELECT r.body,p.left_id,p.right_id,p.case_id,p.repeat_index FROM reviews r JOIN presentations p ON p.id=r.presentation_id JOIN reviewers v ON v.token=p.token WHERE v.experiment_id=?", (experiment_id,)).fetchall()
        states, pairs, ids, stale = Counter(), set(), [], []
        for row in rows:
            left, right = lookup.get(row[1]), lookup.get(row[2])
            if not left or not right or {left["configuration_id"], right["configuration_id"]} != {baseline, cid}:
                continue
            review = json.loads(row[0])
            kind = review.get("kind", "unknown")
            states[kind] += 1
            if kind != "human":
                continue
            identity = review.get("result_identity")
            current = self.store._review_identity(left, right)
            if identity != current:
                stale.append(review.get("id"))
                continue
            ids.append(review["id"])
            pairs.add((row[3], row[4]))
        return {"records_by_kind": dict(states), "current_human_records": len(ids), "comparable_pairs": len(pairs),
                "review_ids": ids, "stale_human_review_ids": stale,
                "note": "真人评价数量只表示覆盖，不隐含偏好或质量通过。"}
