"""Read-only performance projections from execution metadata and visible events.

No prompt, response text, or reasoning text is copied into this projection. One
measurement represents one execution attempt; retries never replace old costs.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime

from .util import TERMINAL


TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens",
                "cache_hit_input_tokens", "cache_miss_input_tokens")
ACTIVE = {"queued", "running", "cancelling"}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _elapsed(start, end):
    try:
        value = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000
        return round(value, 3) if _number(value) else None
    except (TypeError, ValueError):
        return None


def _duration(start, end):
    value = (end.get("data") or {}).get("duration_ms")
    return value if _number(value) else _elapsed(start.get("timestamp"), end.get("timestamp"))


def _usage(value):
    """Only numeric usage fields; total may be derived, reasoning is a subset."""
    value = value if isinstance(value, dict) else {}
    aliases = {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
        "total_tokens": ("total_tokens",),
        "reasoning_tokens": ("reasoning_tokens", "reasoning_output_tokens"),
        "cache_hit_input_tokens": ("prompt_cache_hit_tokens", "cached_input_tokens"),
        "cache_miss_input_tokens": ("prompt_cache_miss_tokens",),
    }
    result = {field: next((value[k] for k in keys if type(value.get(k)) is int and value[k] >= 0), None)
              for field, keys in aliases.items()}
    for detail_key in ("completion_tokens_details", "output_tokens_details"):
        detail = value.get(detail_key)
        if isinstance(detail, dict) and result["reasoning_tokens"] is None:
            count = detail.get("reasoning_tokens")
            if type(count) is int and count >= 0:
                result["reasoning_tokens"] = count
    detail = value.get("input_tokens_details") or value.get("prompt_tokens_details")
    if isinstance(detail, dict) and result["cache_hit_input_tokens"] is None:
        count = detail.get("cached_tokens")
        if type(count) is int and count >= 0:
            result["cache_hit_input_tokens"] = count
    derived = result["total_tokens"] is None and all(result[k] is not None for k in ("input_tokens", "output_tokens"))
    if derived:
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result, derived


def _kind(event):
    return str(event.get("kind", "")).rsplit(".", 1)[-1]


def _request_id(event):
    data = event.get("data") or {}
    return data.get("product_request_id") or data.get("request_id") or event.get("request_id")


def _model_key(event):
    return (_request_id(event), event.get("episode"), event.get("turn"),
            event.get("stage"), event.get("attempt"))


def _operation(event, operations):
    data = event.get("data") or {}
    return (data.get("operation") or operations.get(_request_id(event))
            or str(data.get("endpoint") or "").split("?")[0].rstrip("/").rsplit("/", 1)[-1]
            or event.get("stage") or "unknown")


def measure_attempt(attempt, events, *, events_available=True):
    """Accept the shared event envelope, including namespaced model events.

    HTTP delivery is request start to parsed HTTP response, not first token or
    final device rendering. Unclosed spans stay missing, even on failed trials.
    """
    seen, unique = set(), []
    for event in events:
        if not isinstance(event, dict):
            continue
        identifier = event.get("id")
        if identifier and identifier in seen:
            continue
        if identifier:
            seen.add(identifier)
        unique.append(event)
    operations = {_request_id(e): e["data"]["operation"] for e in unique
                  if _request_id(e) and isinstance(e.get("data"), dict) and e["data"].get("operation")}
    calls, http_spans = [], []
    pending_models, pending_http = defaultdict(list), defaultdict(list)
    retry_count = retry_unknown = retry_intents = stage_errors = orphan_responses = 0
    logical_calls = Counter()
    for event in unique:
        kind, data = _kind(event), event.get("data") or {}
        if kind == "model_request":
            key = _model_key(event)
            logical = key[:-1]
            internal_attempt = event.get("attempt")
            if type(internal_attempt) is int and internal_attempt >= 1:
                retry_count += internal_attempt > 1
            elif data.get("product_request_id") and event.get("stage"):
                retry_count += logical_calls[logical] > 0
            else:
                retry_unknown += 1
            logical_calls[logical] += 1
            pending_models[key].append(len(calls))
            calls.append({"operation": _operation(event, operations), "stage": event.get("stage") or "unknown",
                          "outcome": "unfinished", "duration_ms": None, "usage": None, "start": event})
        elif kind in {"model_response", "model_error", "stage_error"}:
            if kind == "stage_error":
                stage_errors += 1
                retry_intents += data.get("will_retry") is True
            pending = pending_models.get(_model_key(event))
            if pending:
                call = calls[pending.pop(0)]
                call.update(outcome="succeeded" if kind == "model_response" else "failed",
                            duration_ms=_duration(call["start"], event), usage=data.get("usage"))
            elif kind == "model_response":
                # Preserve known usage even if the request event was lost.
                orphan_responses += 1
                calls.append({"operation": _operation(event, operations), "stage": event.get("stage") or "unknown",
                              "outcome": "unmatched_response", "duration_ms": None, "usage": data.get("usage")})
        elif event.get("kind") in {"http_request", "http.request.started"}:
            pending_http[_request_id(event)].append(len(http_spans))
            http_spans.append({"operation": _operation(event, operations), "outcome": "unfinished",
                               "duration_ms": None, "start": event})
        elif event.get("kind") in {"http_response", "http.request.finished", "http_error", "http.request.error"}:
            pending = pending_http.get(_request_id(event))
            if pending:
                span = http_spans[pending.pop(0)]
                status = data.get("status", data.get("http_status"))
                outcome = "succeeded" if type(status) is int and 200 <= status < 300 else "failed"
                span.update(outcome=outcome, duration_ms=_duration(span["start"], event))
    for record in calls + http_spans:
        record.pop("start", None)
    for call in calls:
        call["usage"], call["total_tokens_derived"] = _usage(call["usage"])
    result = attempt.get("result") or {}
    fallback_usage, derived = _usage(result.get("usage"))
    # Per-call events have priority. Do not add an already aggregated result a
    # second time or fill missing calls with an ambiguous aggregate total.
    fallback = None if calls else {"values": fallback_usage, "total_tokens_derived": derived}
    status = attempt.get("status", "unknown")
    started = bool(attempt.get("started_at")) or _number(attempt.get("duration_ms"))
    duration = attempt.get("duration_ms")
    duration = duration if _number(duration) else _elapsed(attempt.get("started_at"), attempt.get("ended_at"))
    reported_requests = (result.get("usage") or {}).get("calls_requested") if isinstance(result.get("usage"), dict) else None
    return {"trial_id": attempt["id"], "attempt": attempt.get("attempt", 1),
            "configuration_id": attempt["configuration_id"], "status": status, "started": started,
            "episode_duration_ms": duration if status in TERMINAL else None,
            "events_available": events_available, "calls": calls, "http_spans": http_spans,
            "observed_requests": len(calls) - orphan_responses, "orphan_responses": orphan_responses,
            "reported_requests": reported_requests if type(reported_requests) is int and reported_requests >= 0 else None,
            "internal_retries": retry_count, "retry_unclassified_calls": retry_unknown,
            "retry_intents": retry_intents, "stage_errors": stage_errors, "fallback_usage": fallback}


def _distribution(values):
    known = sorted(v for v in values if _number(v))
    return {"count": len(values), "n": len(known), "missing": len(values) - len(known),
            "p50_ms": known[math.ceil(.50 * len(known)) - 1] if known else None,
            "p95_ms": known[math.ceil(.95 * len(known)) - 1] if known else None}


def _timings(records):
    return {outcome: _distribution([r["duration_ms"] for r in records if outcome == "all" or r["outcome"] == outcome])
            for outcome in ("all", "succeeded", "failed", "unfinished", "unmatched_response")}


def aggregate_performance(trials, measurements):
    """Aggregate attempts without reading assessments or business output."""
    configurations = {}
    for trial in trials:
        identifier = trial["configuration_id"]
        if identifier not in configurations:
            configurations[identifier] = {"name": trial["configuration"].get("name", identifier),
                                           "trial_statuses": Counter(), "measurements": []}
        configurations[identifier]["trial_statuses"][trial["status"]] += 1
    for measurement in measurements:
        if measurement["configuration_id"] in configurations:
            configurations[measurement["configuration_id"]]["measurements"].append(measurement)
    for group in configurations.values():
        items = group.pop("measurements")
        statuses = dict(group.pop("trial_statuses"))
        group["trials"] = {"total": sum(statuses.values()), "statuses": statuses,
                           "succeeded": statuses.get("succeeded", 0),
                           "failed_terminal": sum(n for s, n in statuses.items() if s in TERMINAL - {"succeeded"}),
                           "active": sum(n for s, n in statuses.items() if s in ACTIVE)}
        group["attempts"] = {"total": len(items), "statuses": dict(Counter(a["status"] for a in items)),
                             "execution_retries": sum(a["attempt"] > 1 for a in items),
                             "not_started": sum(not a["started"] for a in items),
                             "active": sum(a["status"] in ACTIVE for a in items),
                             "missing_event_files": sum(a["started"] and not a["events_available"] for a in items)}
        group["episode"] = _timings([{"duration_ms": a["episode_duration_ms"],
                                       "outcome": "succeeded" if a["status"] == "succeeded" else "failed"}
                                      for a in items if a["started"] and a["status"] in TERMINAL])
        operations, stages = defaultdict(list), defaultdict(list)
        for attempt in items:
            for span in attempt["http_spans"]:
                operations[span["operation"]].append(span)
            for call in attempt["calls"]:
                stages[(call["operation"], call["stage"])].append(call)
        group["operations"] = {operation: _timings(spans) for operation, spans in sorted(operations.items())}
        group["model_stages"] = [{"operation": key[0], "stage": key[1], "timing": _timings(calls)}
                                 for key, calls in sorted(stages.items())]
        unobserved = [a for a in items if a["started"] and not a["observed_requests"]]
        group["model"] = {
            "observed_requests": sum(a["observed_requests"] for a in items),
            "reported_requests_without_events": sum(a["reported_requests"] or 0 for a in unobserved),
            "request_count_unknown_attempts": sum(a["reported_requests"] is None for a in unobserved),
            "internal_retries": sum(a["internal_retries"] for a in items),
            "retry_unclassified_calls": sum(a["retry_unclassified_calls"] for a in items),
            "retry_unknown_attempts": len(unobserved),
            "retry_intents": sum(a["retry_intents"] for a in items),
            "stage_errors": sum(a["stage_errors"] for a in items),
            "orphan_responses": sum(a["orphan_responses"] for a in items),
        }
        usage_records = []
        for attempt in items:
            failed = attempt["status"] in TERMINAL - {"succeeded"}
            for call in attempt["calls"]:
                usage_records.append((call["usage"], "call", failed, call["total_tokens_derived"]))
            fallback = attempt["fallback_usage"]
            if fallback and any(v is not None for v in fallback["values"].values()):
                usage_records.append((fallback["values"], "attempt", failed, fallback["total_tokens_derived"]))
        fields = {}
        for field in TOKEN_FIELDS:
            values = [record[0][field] for record in usage_records if record[0][field] is not None]
            failed_values = [record[0][field] for record in usage_records if record[2] and record[0][field] is not None]
            fields[field] = {"known_total": sum(values) if values else None, "known_records": len(values),
                             "failed_attempt_known_total": sum(failed_values) if failed_values else None,
                             "missing_call_records": sum(r[1] == "call" and r[0][field] is None for r in usage_records),
                             "missing_attempt_records": sum(r[1] == "attempt" and r[0][field] is None for r in usage_records),
                             "derived_records": sum(r[3] for r in usage_records) if field == "total_tokens" else 0}
        group["usage"] = {"fields": fields,
                          "call_records": sum(r[1] == "call" for r in usage_records),
                          "calls_missing_usage": sum(r[1] == "call" and all(v is None for v in r[0].values()) for r in usage_records),
                          "fallback_attempt_records": sum(r[1] == "attempt" for r in usage_records),
                          "attempts_without_usage_or_calls": sum(a["started"] and not a["calls"] and
                              not any(v is not None for v in (a["fallback_usage"] or {}).get("values", {}).values()) for a in items)}
    return {"schema_version": "myeval.performance.v1", "scope": "selected_trials_all_execution_attempts",
            "quantiles": {"method": "nearest_rank", "definition": "升序排列有效样本，p 分位取第 ceil(p × n) 项；不插值。n 为已知耗时数，缺失不记零。"},
            "ttft": {"status": "unavailable", "reason": "未记录首 token 时间；非流式请求完成耗时不是首字时间。不计算解码速度。"},
            "notes": ["性能包含全部执行尝试；当前 trial 终态另计。完整执行从 runner 开始到终态，含启动、持久化与清理，不含排队。",
                      "每轮交付从 HTTP 请求发送到完整响应读取/解析；包含各模型阶段及内部重试，不含设备渲染、语音或用户输入时间。",
                      "成功与失败分位分别计算；未闭合请求只计缺失，不用整段 trial 时间替代。聊天/整理以 operation 或 endpoint 元数据识别。",
                      "模型阶段按请求到响应或错误计时；返回响应后发生的结构错误另计，不改写模型响应耗时。",
                      "模型调用有事件时逐调用统计；没有调用事件才采用整次 result.usage，二者不重复相加。失败中的已知用量保留，缺失不是零。",
                      "total_tokens 缺失且输入/输出均已知时相加，推算条数另列。reasoning 是输出用量的子集，不再叠加，也不是可见文本长度。",
                      "样本按执行/请求汇总，变式和重复不等于独立用户；缓存、并发、供应商时段会影响耗时。"],
            "configurations": configurations}
