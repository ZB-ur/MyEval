"""Pure, provenance-aware summaries of exported experiments.

Product attempts, judge calls and reviewers are different denominators.  Scores
are only compared on matching current outputs.  Family means, not judge votes,
repetitions or case variants, are the bootstrap observations.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
import math
import random

from ..util import TERMINAL, digest


SCHEMA_VERSION = "myeval.analysis.v2"
BOOTSTRAP_REPLICATES = 2000
BOOTSTRAP_SEED_VERSION = "myeval.family-bootstrap.v1"


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _mean(values):
    return sum(values) / len(values) if values else None


def _sorted_counts(counts):
    return dict(sorted(counts.items(), key=lambda item: str(item[0])))


def _stamp(record, index=0):
    """Timestamps determine re-score order; stable IDs break exact ties."""
    created = record.get("created_at")
    try:
        timestamp = datetime.fromisoformat(str(created).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError):
        timestamp = float("-inf")
    return timestamp, str(record.get("id", "")), index


def _identity(trial):
    return trial.get("attempt"), digest(trial.get("result"))


def _succeeded(trial):
    return bool(trial and trial.get("status") == "succeeded" and trial.get("result") is not None
                and _mapping(trial.get("result")).get("status") in (None, "succeeded"))


def _record_identity_matches(record, trial):
    number, result_hash = _identity(trial)
    return (record.get("execution_attempt") == number
            and record.get("result_hash") == result_hash)


def _percentile(sorted_values, fraction):
    position = (len(sorted_values) - 1) * fraction
    left = int(position)
    right = min(left + 1, len(sorted_values) - 1)
    return sorted_values[left] + (sorted_values[right] - sorted_values[left]) * (position - left)


def _family_effect(observations, seed_parts):
    """Average repeats within cases, case variants within families, then families.

    Each observation is (family_id, case_id, repeat_index, delta, baseline,
    candidate).  Nullable side values are useful for preference effects.
    """
    by_case = defaultdict(list)
    for family_id, case_id, repeat, delta, baseline, candidate in observations:
        by_case[(family_id, case_id)].append((repeat, delta, baseline, candidate))
    by_family = defaultdict(list)
    for (family_id, case_id), values in sorted(by_case.items()):
        by_family[family_id].append({
            "case_id": case_id,
            "paired_observations": len(values),
            "repeat_indexes": sorted({value[0] for value in values}),
            "mean_delta": _mean([value[1] for value in values]),
            "baseline_mean": _mean([value[2] for value in values if value[2] is not None]),
            "candidate_mean": _mean([value[3] for value in values if value[3] is not None]),
        })
    rows = []
    for family_id, cases in sorted(by_family.items()):
        rows.append({
            "family_id": family_id,
            "paired_cases": len(cases),
            "paired_observations": sum(case["paired_observations"] for case in cases),
            "mean_delta": _mean([case["mean_delta"] for case in cases]),
            "baseline_mean": _mean([case["baseline_mean"] for case in cases if case["baseline_mean"] is not None]),
            "candidate_mean": _mean([case["candidate_mean"] for case in cases if case["candidate_mean"] is not None]),
            "cases": cases,
        })
    values = [row["mean_delta"] for row in rows]
    seed_input = json.dumps([BOOTSTRAP_SEED_VERSION, *seed_parts], ensure_ascii=False, separators=(",", ":"))
    seed = int.from_bytes(hashlib.sha256(seed_input.encode()).digest()[:8], "big")
    interval = None
    if len(values) >= 2:
        rng = random.Random(seed)
        sampled = sorted(sum(values[rng.randrange(len(values))] for _ in values) / len(values)
                         for _ in range(BOOTSTRAP_REPLICATES))
        interval = {"low": _percentile(sampled, .025), "high": _percentile(sampled, .975), "level": .95}
    return {
        "family_equal_weight_mean_delta": _mean(values),
        "baseline_family_equal_weight_mean": _mean([row["baseline_mean"] for row in rows if row["baseline_mean"] is not None]),
        "candidate_family_equal_weight_mean": _mean([row["candidate_mean"] for row in rows if row["candidate_mean"] is not None]),
        "ci95": interval,
        "ci_unavailable_reason": "fewer_than_two_scored_families" if len(values) < 2 else None,
        "seed": seed,
        "bootstrap_replicates": BOOTSTRAP_REPLICATES if interval else 0,
        "paired_observations": len(observations),
        "scored_cases": len(by_case),
        "scored_families": len(rows),
        "families_candidate_higher": sum(value > 0 for value in values),
        "families_baseline_higher": sum(value < 0 for value in values),
        "families_tied": sum(value == 0 for value in values),
        "family_rows": rows,
    }


def _attempts(trial):
    # Historical attempts are execution history, never additional samples.
    raw = _list(trial.get("attempts"))
    if not raw:
        return [trial], "current_only"
    by_number = {row.get("attempt"): row for row in raw if isinstance(row, dict)}
    by_number[trial.get("attempt")] = trial
    return list(by_number.values()), "full_export"


def _execution(trials, expected_trials=None):
    statuses, attempt_statuses = Counter(), Counter()
    incomplete_attempt_histories = 0
    for trial in trials:
        statuses[str(trial.get("status") or "unknown")] += 1
        attempts, history = _attempts(trial)
        incomplete_attempt_histories += history != "full_export"
        attempt_statuses.update(str(attempt.get("status") or "unknown") for attempt in attempts)
    terminal = sum(count for status, count in statuses.items() if status in TERMINAL)
    return {
        "trials": len(trials),
        "expected_trials": expected_trials,
        "missing_trials": max(0, expected_trials - len(trials)) if expected_trials is not None else None,
        "statuses": _sorted_counts(statuses),
        "succeeded": statuses["succeeded"],
        "terminal_trials": terminal,
        "non_success_terminal_trials": terminal - statuses["succeeded"],
        "nonterminal_trials": len(trials) - terminal,
        "success_rate_among_terminal": statuses["succeeded"] / terminal if terminal else None,
        "execution_attempts": sum(attempt_statuses.values()),
        "attempt_statuses": _sorted_counts(attempt_statuses),
        "attempt_history_not_supplied_trials": incomplete_attempt_histories,
    }


def _assessment_index(trials):
    current = []
    counts = Counter({key: 0 for key in (
        "raw_records", "malformed_records", "duplicate_record_ids", "non_model_records",
        "missing_evaluator_or_rubric", "unbound_legacy_records", "historical_attempt_records",
        "stale_result_hash_records", "unknown_or_mismatched_attempt_records", "current_bound_records",
        "invalid_pair_reference_records")})
    seen = set()
    individual = {}
    pair_units = defaultdict(dict)
    dimensions = defaultdict(set)
    groups = set()
    trial_map = {trial["id"]: trial for trial in trials}
    for trial in trials:
        for index, record in enumerate(_list(trial.get("assessment_records"))):
            counts["raw_records"] += 1
            if not isinstance(record, dict):
                counts["malformed_records"] += 1
                continue
            identity = (trial["id"], record.get("id")) if record.get("id") else (trial["id"], index)
            if identity in seen:
                counts["duplicate_record_ids"] += 1
                continue
            seen.add(identity)
            if record.get("kind") != "model":
                counts["non_model_records"] += 1
                continue
            if not record.get("evaluator") or not record.get("rubric_version"):
                counts["missing_evaluator_or_rubric"] += 1
                continue
            if "execution_attempt" not in record or not record.get("result_hash"):
                counts["unbound_legacy_records"] += 1
                continue
            if not _record_identity_matches(record, trial):
                if record.get("execution_attempt") != trial.get("attempt"):
                    historical = next((attempt for attempt in _list(trial.get("attempts"))
                        if isinstance(attempt, dict) and _record_identity_matches(record, attempt)), None)
                    counts["historical_attempt_records" if historical else "unknown_or_mismatched_attempt_records"] += 1
                else:
                    counts["stale_result_hash_records"] += 1
                continue
            evaluator, rubric = str(record["evaluator"]), str(record["rubric_version"])
            group = (evaluator, rubric)
            groups.add(group)
            value = _mapping(record.get("value"))
            dimensions[group].update(str(key) for key in _mapping(value.get("scores")))
            dimensions[group].update(str(key) for key in _mapping(value.get("score_status")))
            current.append((trial, record))
            counts["current_bound_records"] += 1
            preference = _mapping(value.get("preference"))
            if preference:
                other_id = preference.get("other_trial_id")
                other = trial_map.get(other_id)
                unit = preference.get("unit_id") or value.get("unit_id")
                if (not other or other_id == trial["id"] or not unit
                        or other.get("case_id") != trial.get("case_id")
                        or other.get("repeat_index") != trial.get("repeat_index")
                        or preference.get("other_attempt") != other.get("attempt")
                        or preference.get("other_result_hash") != digest(other.get("result"))):
                    counts["invalid_pair_reference_records"] += 1
                    continue
                pair_key = (*sorted((trial["id"], other_id)), evaluator, rubric, str(unit))
                previous = pair_units[pair_key].get(trial["id"])
                if previous is None or _stamp(record, index) > _stamp(previous):
                    pair_units[pair_key][trial["id"]] = record
            else:
                key = (trial["id"], evaluator, rubric)
                previous = individual.get(key)
                if previous is None or _stamp(record, index) > _stamp(previous):
                    individual[key] = record
    # Pair-wise baseline assessments retain their comparator; never borrow a
    # baseline score produced while judging a different candidate or unit.
    paired = {}
    for unit_key, sides in pair_units.items():
        pair_key = unit_key[:-1]
        stamp = max(_stamp(record) for record in sides.values())
        previous = paired.get(pair_key)
        if previous is None or (stamp, unit_key[-1]) > (previous["stamp"], previous["unit_id"]):
            paired[pair_key] = {"unit_id": unit_key[-1], "records": sides, "stamp": stamp}
    counts["latest_individual_records"] = len(individual)
    counts["latest_pair_units"] = len(paired)
    counts["latest_complete_pair_units"] = sum(len(unit["records"]) == 2 for unit in paired.values())
    counts["latest_incomplete_pair_units"] = sum(len(unit["records"]) != 2 for unit in paired.values())
    selected = len(individual) + sum(len(unit["records"]) for unit in paired.values())
    counts["selected_context_records"] = selected
    counts["superseded_context_records"] = (counts["current_bound_records"]
        - counts["invalid_pair_reference_records"] - selected)
    return {"counts": _sorted_counts(counts), "individual": individual, "paired": paired,
            "dimensions": dimensions, "groups": sorted(groups), "current": current}


def _selected_records(index, baseline, candidate, evaluator, rubric):
    if not baseline or not candidate:
        return None, None, "missing_trial"
    key = (*sorted((baseline["id"], candidate["id"])), evaluator, rubric)
    paired = index["paired"].get(key)
    if paired:
        records = paired["records"]
        if len(records) != 2:
            return records.get(baseline["id"]), records.get(candidate["id"]), "incomplete_pair_score"
        return records[baseline["id"]], records[candidate["id"]], "pairwise"
    return (index["individual"].get((baseline["id"], evaluator, rubric)),
            index["individual"].get((candidate["id"], evaluator, rubric)), "individual")


def _score_state(trial, record, dimension):
    if trial is None:
        return "missing_trial", None
    if not _succeeded(trial):
        return "unavailable", None
    if record is None:
        return "missing_assessment", None
    value = _mapping(record.get("value"))
    scores, statuses = _mapping(value.get("scores")), _mapping(value.get("score_status"))
    if dimension not in scores and dimension not in statuses:
        return "missing_dimension", None
    score, state = scores.get(dimension), statuses.get(dimension)
    if state is None and _number(score):
        return "rated_legacy_inferred", score
    if state == "rated" and _number(score):
        return "rated", score
    if state in {"unavailable", "not_applicable"} and score is None:
        return state, None
    return "invalid_score", None


def _dimension_summary(pairs, index, evaluator, rubric, dimension, seed_parts):
    side_states = {"baseline": Counter(), "candidate": Counter()}
    distributions = {"baseline": Counter(), "candidate": Counter()}
    sources, excluded, counts = Counter(), Counter(), Counter()
    observations = []
    for case, repeat, baseline, candidate in pairs:
        left, right, source = _selected_records(index, baseline, candidate, evaluator, rubric)
        sources[source] += 1
        states = [_score_state(baseline, left, dimension), _score_state(candidate, right, dimension)]
        for side, (state, score) in zip(("baseline", "candidate"), states):
            side_states[side][state] += 1
            if score is not None:
                distributions[side][str(score)] += 1
        if source == "incomplete_pair_score":
            excluded[source] += 1
            continue
        if not all(score is not None for _, score in states):
            reasons = {state for state, score in states if score is None}
            # One exclusive reason per pair; the full side counts retain overlaps.
            reason = next(item for item in ("missing_trial", "unavailable", "invalid_score",
                "missing_assessment", "missing_dimension", "not_applicable") if item in reasons)
            excluded[reason] += 1
            continue
        baseline_score, candidate_score = states[0][1], states[1][1]
        delta = candidate_score - baseline_score
        observations.append((case["family_id"], case["id"], repeat, delta, baseline_score, candidate_score))
        counts["candidate_higher_pairs" if delta > 0 else "baseline_higher_pairs" if delta < 0 else "tied_pairs"] += 1
    return {
        "evaluator": evaluator, "rubric_version": rubric, "dimension": dimension,
        "expected_pairs": len(pairs), "paired_rated": len(observations),
        "side_states": {side: _sorted_counts(value) for side, value in side_states.items()},
        "score_distributions": {side: _sorted_counts(value) for side, value in distributions.items()},
        "score_contexts": _sorted_counts(sources), "excluded_pairs": _sorted_counts(excluded),
        "candidate_higher_pairs": counts["candidate_higher_pairs"],
        "baseline_higher_pairs": counts["baseline_higher_pairs"], "tied_pairs": counts["tied_pairs"],
        "effect": _family_effect(observations, [*seed_parts, evaluator, rubric, dimension]),
        "interpretation": "Descriptive numeric difference on the declared scale. Ordinal score means are not interval-scale measurements or a total quality score.",
    }


def _canonical_choice(preference, self_id, candidate_id):
    choice = preference.get("choice")
    if choice in {"tie", "unable"}:
        return choice
    if choice == "self":
        return "candidate" if self_id == candidate_id else "baseline"
    if choice == "other":
        return "baseline" if self_id == candidate_id else "candidate"
    return "invalid"


def _model_choice(index, baseline, candidate, evaluator, rubric, allow_legacy_pair=False):
    if not baseline or not candidate:
        return "missing_trial", "none"
    if not _succeeded(baseline) or not _succeeded(candidate):
        return "unavailable", "none"
    left, right, source = _selected_records(index, baseline, candidate, evaluator, rubric)
    if source == "incomplete_pair_score":
        return source, source
    if not left or not right:
        return "missing_assessment", source
    if source == "pairwise":
        choices = [_canonical_choice(_mapping(_mapping(record.get("value")).get("preference")), trial["id"], candidate["id"])
                   for record, trial in ((left, baseline), (right, candidate))]
        return (choices[0] if choices[0] == choices[1] else "inconsistent_pair_records"), source
    # Legacy imported assessments used presented_side without a stable unit ID.
    # Only infer a preference when BOTH current records explicitly agree and
    # opposite presented sides establish the mapping.  Scores remain usable
    # independently when a legacy preference cannot be reconstructed.
    lvalue, rvalue = _mapping(left.get("value")), _mapping(right.get("value"))
    choice = lvalue.get("pair_preference")
    if (allow_legacy_pair and choice in {"A", "B", "tie", "unable"} and choice == rvalue.get("pair_preference")
            and {lvalue.get("presented_side"), rvalue.get("presented_side")} == {"A", "B"}):
        if choice in {"tie", "unable"}:
            return choice, "legacy_inferred_pair"
        return ("candidate" if choice == rvalue["presented_side"] else "baseline"), "legacy_inferred_pair"
    return "not_pairwise_scored", source


def _preference_effect(votes, seed_parts):
    """Votes are collapsed within pair before repeat/case/family averaging."""
    by_pair = defaultdict(list)
    counts = Counter()
    for case, repeat, choice, reviewer in votes:
        counts[choice] += 1
        by_pair[(case["family_id"], case["id"], repeat)].append((choice, reviewer))
    observations, disagreements = [], []
    for (family, case_id, repeat), rows in sorted(by_pair.items()):
        eligible = [dict(candidate=1, baseline=-1, tie=0)[choice] for choice, _ in rows
                    if choice in {"candidate", "baseline", "tie"}]
        if eligible:
            observations.append((family, case_id, repeat, _mean(eligible), None, None))
        choices = {choice for choice, _ in rows}
        if len(choices) > 1:
            disagreements.append({"case_id": case_id, "repeat_index": repeat,
                "choices": sorted(choices), "votes": [{"reviewer": reviewer, "choice": choice} for choice, reviewer in rows]})
    return {
        "votes": len(votes), "choice_counts": _sorted_counts(counts),
        "reviewed_pairs": len(by_pair), "eligible_pairs": len(observations),
        "unable_votes": counts["unable"], "tied_votes": counts["tie"],
        "disagreement_pairs": len(disagreements), "disagreements": disagreements,
        "effect": _family_effect(observations, seed_parts),
        "effect_scale": "candidate=+1, baseline=-1, tie=0; unable and missing excluded with denominators retained",
    }


def _review_index(bundle, trial_map):
    counts = {kind: Counter({key: 0 for key in ("raw_records", "duplicate_record_ids", "unknown_trial_records",
        "unbound_legacy_records", "historical_or_stale_records", "invalid_choice_records",
        "mismatched_case_or_repeat", "current_bound_records")}) for kind in ("human", "acceptance", "other")}
    current = []
    seen = set()
    for row in _list(bundle.get("reviews")):
        if not isinstance(row, dict):
            counts["other"]["malformed_presentations"] += 1
            continue
        record = row.get("assessment")
        if record is None:
            counts["other"]["unreviewed_presentations"] += 1
            continue
        record = _mapping(record)
        kind = record.get("kind") if record.get("kind") in {"human", "acceptance"} else "other"
        counts[kind]["raw_records"] += 1
        identifier = record.get("id") or row.get("pair_id")
        if identifier is not None and identifier in seen:
            counts[kind]["duplicate_record_ids"] += 1
            continue
        if identifier is not None:
            seen.add(identifier)
        sides = {side: trial_map.get(row.get(f"{side}_trial_id")) for side in ("A", "B")}
        if not all(sides.values()):
            counts[kind]["unknown_trial_records"] += 1
            continue
        identity = _mapping(record.get("result_identity"))
        if not identity:
            counts[kind]["unbound_legacy_records"] += 1
            continue
        matching = True
        for side, trial in sides.items():
            side_identity = _mapping(identity.get(side))
            matching &= (side_identity.get("trial_id") == trial["id"]
                         and side_identity.get("status") == trial.get("status")
                         and _record_identity_matches(side_identity, trial))
        if not matching:
            counts[kind]["historical_or_stale_records"] += 1
            continue
        if record.get("choice") not in {"A", "B", "tie", "unable"}:
            counts[kind]["invalid_choice_records"] += 1
            continue
        if (sides["A"].get("case_id") != sides["B"].get("case_id")
                or sides["A"].get("repeat_index") != sides["B"].get("repeat_index")):
            counts[kind]["mismatched_case_or_repeat"] += 1
            continue
        if kind == "other":
            continue
        counts[kind]["current_bound_records"] += 1
        current.append((row, record, sides))
    return current, {kind: _sorted_counts(value) for kind, value in counts.items()}


def _human_preferences(reviews, pairs, candidate_id, baseline_id, kind, seed_parts):
    case_lookup = {(case["id"], repeat): case for case, repeat, _, _ in pairs}
    votes, dimensions = [], defaultdict(list)
    declared_choices, rubric_votes = Counter(), defaultdict(list)
    product_unavailable = 0
    for row, record, sides in reviews:
        if record["kind"] != kind:
            continue
        if {trial.get("configuration_id") for trial in sides.values()} != {candidate_id, baseline_id}:
            continue
        case_id, repeat = sides["A"].get("case_id"), sides["A"].get("repeat_index")
        case = case_lookup.get((case_id, repeat))
        if not case:
            continue
        reviewer = str(row.get("reviewer") or "unnamed_reviewer")
        unavailable = any(not _succeeded(trial) for trial in sides.values())
        product_unavailable += unavailable

        def mapped(choice):
            if choice in {"A", "B"}:
                return "candidate" if sides[choice].get("configuration_id") == candidate_id else "baseline"
            return choice if choice in {"tie", "unable"} else "invalid"

        declared_choices[mapped(record["choice"])] += 1
        vote = (case, repeat, "unavailable" if unavailable else mapped(record["choice"]), reviewer)
        votes.append(vote)
        rubric_votes[str(record.get("rubric_version") or "unspecified")].append(vote)
        for dimension, choice in _mapping(record.get("dimensions")).items():
            dimension_key = (str(record.get("rubric_version") or "unspecified"), str(dimension))
            dimensions[dimension_key].append((case, repeat, "unavailable" if unavailable else mapped(choice), reviewer))
    result = _preference_effect(votes, [*seed_parts, kind, "overall"])
    if len(rubric_votes) > 1:
        result["effect"] = _family_effect([], [*seed_parts, kind, "mixed_rubrics"])
        result["effect"]["ci_unavailable_reason"] = "multiple_rubrics_kept_separate"
    result.update({"kind": kind, "expected_pairs": len(pairs), "unreviewed_pairs": len(pairs) - result["reviewed_pairs"],
                   "product_unavailable_reviews": product_unavailable,
                   "declared_choice_counts": _sorted_counts(declared_choices),
                   "reviewer_labels": sorted({vote[3] for vote in votes}),
                   "by_rubric": [{"rubric_version": rubric, **_preference_effect(values, [*seed_parts, kind, rubric, "overall"])}
                                 for rubric, values in sorted(rubric_votes.items())],
                   "dimensions": [{"rubric_version": rubric, "dimension": dimension,
                                   **_preference_effect(values, [*seed_parts, kind, rubric, dimension])}
                                  for (rubric, dimension), values in sorted(dimensions.items())]})
    return result


def _judge_jobs(bundle):
    """Describe evaluator workload separately from all product execution usage."""
    jobs = _list(_mapping(bundle.get("evaluation")).get("jobs"))
    job_states, unit_states = Counter(), Counter()
    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_states[str(job.get("status") or "unknown")] += 1
        for unit in _list(job.get("units")):
            if isinstance(unit, dict):
                unit_states[str(unit.get("status") or "unknown")] += 1
    return {"exported_jobs": len(jobs), "job_statuses": _sorted_counts(job_states),
            "unit_statuses": _sorted_counts(unit_states),
            "usage_source": "evaluation.jobs; never added to product performance",
            "usage_aggregation": "See each persisted job; absence of jobs does not mean zero judge usage."}


def summarize(experiment_bundle):
    """Return a deterministic JSON-compatible analysis; do not mutate the export.

    Declared cases and repeats create the expected denominator.  Missing trials
    and duplicate (case, repeat, configuration) rows cannot quietly disappear or
    be paired arbitrarily.  Unknown split labels are preserved verbatim.
    """
    if not isinstance(experiment_bundle, dict):
        raise ValueError("experiment_bundle must be an exported experiment object")
    bundle = experiment_bundle
    experiment = _mapping(bundle.get("experiment"))
    warnings = []
    trials, duplicate_ids = [], 0
    seen_ids = set()
    malformed_trials = 0
    for trial in _list(bundle.get("trials")):
        if not isinstance(trial, dict) or not isinstance(trial.get("id"), str):
            malformed_trials += 1
            continue
        if trial["id"] in seen_ids:
            duplicate_ids += 1
            continue
        seen_ids.add(trial["id"])
        trials.append(trial)
    trial_map = {trial["id"]: trial for trial in trials}
    configurations = [configuration.get("id") if isinstance(configuration, dict) else configuration
                      for configuration in _list(experiment.get("configurations"))]
    configurations = list(dict.fromkeys(str(identifier) for identifier in configurations if identifier))
    for trial in trials:
        identifier = trial.get("configuration_id")
        if identifier and identifier not in configurations:
            configurations.append(identifier)
    baseline = experiment.get("baseline_id")
    baseline_source = "explicit"
    if not baseline:
        baseline = configurations[0] if configurations else None
        baseline_source = "legacy_first_frozen_configuration" if baseline else "unavailable"
    if baseline not in configurations:
        warnings.append("Declared baseline is absent from the configuration list; comparisons retain missing baseline trials.")
        if baseline:
            configurations.insert(0, baseline)
    cases = {}
    for case in _list(experiment.get("cases")):
        if isinstance(case, dict) and case.get("id"):
            cases[str(case["id"])] = {"id": str(case["id"]), "family_id": str(case.get("family_id") or case["id"]),
                                     "split": str(case.get("split") if case.get("split") is not None else "unspecified")}
    metadata_conflicts = 0
    for trial in trials:
        case_id = str(trial.get("case_id") or _mapping(trial.get("case")).get("id") or "unknown_case")
        case = _mapping(trial.get("case"))
        observed = {"id": case_id, "family_id": str(case.get("family_id") or case_id),
                    "split": str(case.get("split") if case.get("split") is not None else "unspecified")}
        if case_id in cases and cases[case_id] != observed:
            metadata_conflicts += 1
        cases.setdefault(case_id, observed)
    repeats = experiment.get("repeats")
    declared_repeats = repeats if type(repeats) is int and 1 <= repeats <= 1000 else None
    if repeats is not None and declared_repeats is None:
        warnings.append("Invalid or excessive declared repeat count; expected pairs use only observed repeat indexes.")
    slots = defaultdict(list)
    observed_keys = set()
    invalid_repeats = 0
    for trial in trials:
        repeat = trial.get("repeat_index")
        if type(repeat) is not int or repeat < 1:
            invalid_repeats += 1
            continue
        case_id = str(trial.get("case_id") or _mapping(trial.get("case")).get("id") or "unknown_case")
        slots[(case_id, repeat, trial.get("configuration_id"))].append(trial)
        observed_keys.add((case_id, repeat))
    expected_keys = set(observed_keys)
    if declared_repeats is not None:
        expected_keys.update((case_id, repeat) for case_id in cases for repeat in range(1, declared_repeats + 1))
    duplicate_slots = sum(len(rows) > 1 for rows in slots.values())
    family_splits = defaultdict(set)
    for case in cases.values():
        family_splits[case["family_id"]].add(case["split"])
    shared_families = [{"family_id": family, "splits": sorted(splits)} for family, splits in sorted(family_splits.items()) if len(splits) > 1]
    if shared_families:
        warnings.append("Some family IDs occur in multiple splits; those splits do not establish independent holdout evidence.")
    if duplicate_slots:
        warnings.append("Duplicate case/repeat/configuration slots are excluded from paired analysis; inspect the retained trial denominator.")
    if metadata_conflicts:
        warnings.append("Trial case metadata differs from frozen experiment cases; pairing uses frozen experiment case metadata.")
    index = _assessment_index(trials)
    reviews, review_counts = _review_index(bundle, trial_map)
    splits = sorted({case["split"] for case in cases.values()})
    execution = []
    comparisons = []
    for split in splits:
        keys = sorted(key for key in expected_keys if cases[key[0]]["split"] == split)
        for configuration in configurations:
            selected = [trial for trial in trials if trial.get("configuration_id") == configuration
                        and cases[str(trial.get("case_id") or _mapping(trial.get("case")).get("id") or "unknown_case")]["split"] == split]
            execution.append({"configuration_id": configuration, "split": split, **_execution(selected, len(keys))})
        for candidate in configurations:
            if candidate == baseline:
                continue
            pairs, pair_counts = [], Counter()
            for case_id, repeat in keys:
                lefts, rights = slots.get((case_id, repeat, baseline), []), slots.get((case_id, repeat, candidate), [])
                left = lefts[0] if len(lefts) == 1 else None
                right = rights[0] if len(rights) == 1 else None
                pairs.append((cases[case_id], repeat, left, right))
                if len(lefts) > 1 or len(rights) > 1:
                    pair_counts["ambiguous_duplicate_slot"] += 1
                elif not left or not right:
                    pair_counts["missing_trial"] += 1
                elif _succeeded(left) and _succeeded(right):
                    pair_counts["both_succeeded"] += 1
                elif left.get("status") not in TERMINAL or right.get("status") not in TERMINAL:
                    pair_counts["pending_or_nonterminal"] += 1
                else:
                    pair_counts["non_success_terminal"] += 1
            seed_parts = [str(experiment.get("id") or "unknown"), str(baseline), str(candidate), split]
            model_dimensions, model_preferences = [], []
            cross_judge = defaultdict(list)
            for evaluator, rubric in index["groups"]:
                for dimension in sorted(index["dimensions"][(evaluator, rubric)]):
                    model_dimensions.append(_dimension_summary(pairs, index, evaluator, rubric, dimension, seed_parts))
                choices, excluded, contexts = [], Counter(), Counter()
                for case, repeat, left, right in pairs:
                    choice, context = _model_choice(index, left, right, evaluator, rubric, len(configurations) == 2)
                    contexts[context] += 1
                    if choice in {"candidate", "baseline", "tie", "unable"}:
                        choices.append((case, repeat, choice, evaluator))
                        cross_judge[(rubric, case["id"], repeat)].append((choice, evaluator))
                    else:
                        excluded[choice] += 1
                preference = _preference_effect(choices, [*seed_parts, evaluator, rubric, "preference"])
                preference.update({"evaluator": evaluator, "rubric_version": rubric,
                                   "expected_pairs": len(pairs), "excluded_pairs": _sorted_counts(excluded),
                                   "score_contexts": _sorted_counts(contexts)})
                model_preferences.append(preference)
            disagreement = []
            for (rubric, case_id, repeat), votes in sorted(cross_judge.items()):
                if len({choice for choice, _ in votes}) > 1:
                    disagreement.append({"rubric_version": rubric, "case_id": case_id, "repeat_index": repeat,
                                         "votes": [{"choice": choice, "evaluator": evaluator} for choice, evaluator in votes]})
            comparisons.append({
                "baseline_id": baseline, "candidate_id": candidate, "split": split,
                "denominators": {"expected_pairs": len(keys), "cases": len({key[0] for key in keys}),
                    "families": len({cases[key[0]]["family_id"] for key in keys}),
                    "repeat_indexes": sorted({key[1] for key in keys}), "declared_repeats_per_case": declared_repeats},
                "execution_pairs": _sorted_counts(pair_counts), "model_dimensions": model_dimensions,
                "model_preferences": model_preferences,
                "model_preference_disagreements": disagreement,
                "human_preferences": _human_preferences(reviews, pairs, candidate, baseline, "human", seed_parts),
                "acceptance_preferences": _human_preferences(reviews, pairs, candidate, baseline, "acceptance", seed_parts),
            })
    total = _execution(trials, len(expected_keys) * len(configurations))
    return {
        "schema_version": SCHEMA_VERSION, "experiment_id": experiment.get("id"),
        "baseline_id": baseline, "baseline_source": baseline_source, "configuration_ids": configurations,
        "scope": {"attempts": "current_output_only_for_quality", "split_labels": splits,
                  "source_filter": bundle.get("filter"), "source_comparison_scope": bundle.get("comparison_scope")},
        "denominators": {**total, "cases": len(cases), "families": len(family_splits),
            "configurations": len(configurations), "case_repeat_slots": len(expected_keys),
            "repeat_indexes": sorted({key[1] for key in expected_keys}), "declared_repeats_per_case": declared_repeats,
            "duplicate_trial_ids": duplicate_ids, "malformed_trials": malformed_trials,
            "duplicate_configuration_slots": duplicate_slots, "invalid_repeat_trials": invalid_repeats,
            "case_metadata_conflicts": metadata_conflicts, "families_shared_between_splits": shared_families},
        "execution": execution, "comparisons": comparisons,
        "model_assessment_records": index["counts"], "review_records": review_counts,
        "judge_workload": _judge_jobs(bundle),
        "method": {"version": BOOTSTRAP_SEED_VERSION, "effect_direction": "candidate minus baseline",
            "aggregation": "matching repeat pairs -> mean within case -> equal case means within family -> equal family means",
            "bootstrap": "percentile bootstrap resampling entire family means with replacement",
            "bootstrap_replicates": BOOTSTRAP_REPLICATES, "confidence_level": .95,
            "seed": "first 64 bits of SHA256(version, experiment, baseline, candidate, split, evaluator/kind, rubric, dimension)",
            "rescore_selection": "latest individual assessment per evaluator/rubric/output; pairwise assessments retain the comparator and use two records from one latest unit",
            "missingness": "complete observed pairs only for effects; all expected pairs retained in denominators",
            "significance_test": "not_performed", "multiple_comparison_adjustment": "not_performed"},
        "warnings": warnings,
        "limitations": [
            "Intervals describe variation across the observed families under exchangeability; small, selected or synthetic task sets do not establish real-user generalization.",
            "Ordinal score averages and differences are descriptive; scale spacing is not validated and no total quality score is constructed.",
            "Missing, unavailable and not-applicable scores are never zero. Complete-pair effects can be biased when outcomes are missing selectively.",
            "Repeated generations, case variants, re-scores and reviewer votes do not increase the independent family count.",
            "Evaluator versions and rubrics stay separate. No significance claim or multiplicity correction is made across candidates and dimensions.",
            "The latest unit must contain both sides for pairwise scoring; missing pair members are not borrowed from another judge context.",
            "Human and acceptance records with no result identity remain visible as unbound history and do not establish preference for current outputs.",
            "Reviewer labels do not establish distinct human identities. AI judgments and internal anchors are not human or teacher gold labels.",
            "Product usage and evaluator usage remain separate; missing usage is unknown rather than zero.",
        ],
    }
