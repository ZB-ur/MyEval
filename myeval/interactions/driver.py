"""Visible-information user simulation; provider transport is deliberately injected."""
from __future__ import annotations

import copy
import hashlib
import json
import queue
import threading
import time
from dataclasses import dataclass, field

from ..context import CancelledError


class InteractionLimit(Exception):
    pass


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _strings(value, name):
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError(f"{name} must be a list of nonempty strings")
    return value


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")
    return value


def _public_provenance(value):
    if not isinstance(value, dict):
        return None
    keys = {"provider", "version", "run_id", "status", "cli_version", "requested", "effective",
            "boundary_sha256", "schema_sha256", "prompt_sha256", "provider_sha256", "tools_observed", "event_stream_complete",
            "output_limit_enforcement", "local_process_stopped", "remote_completion", "remote_billing_stopped"}
    filtered = {}
    for key in keys:
        supplied = value.get(key)
        if key in {"requested", "effective"} and isinstance(supplied, dict):
            filtered[key] = {k: v for k, v in supplied.items()
                             if k in {"model", "reasoning_effort", "max_output_tokens", "provider_model_version"}
                             and (v is None or type(v) in {bool, int} or isinstance(v, str) and len(v) <= 200)}
            if key == "requested" and isinstance(supplied.get("tools"), list):
                filtered[key]["tools"] = [v for v in supplied["tools"] if isinstance(v, str) and len(v) <= 100][:50]
        elif key == "tools_observed" and isinstance(supplied, list):
            filtered[key] = [v for v in supplied if isinstance(v, str) and len(v) <= 100][:50]
        elif supplied is None or type(supplied) is bool or isinstance(supplied, str) and len(supplied) <= 500:
            filtered[key] = supplied
    return filtered


def validate_policy(value):
    if not isinstance(value, dict):
        raise ValueError("policy must be an object")
    allowed = {"id", "version", "mode", "goal", "visible_facts", "forbidden_knowledge", "max_turns",
               "max_seconds", "max_model_calls", "max_model_tokens", "max_output_tokens", "max_text_chars",
               "max_history_chars", "script", "opening", "branches", "default", "stop_on_assistant"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError("unsupported policy fields: " + ", ".join(sorted(unknown)))
    policy = copy.deepcopy(value)
    for key in ("id", "version", "goal"):
        _text(policy.get(key), key)
    if policy.get("mode") not in {"script", "rules", "model"}:
        raise ValueError("mode must be script, rules, or model")
    for key in ("visible_facts", "forbidden_knowledge"):
        _strings(policy.get(key), key)
    limits = {"max_turns": (None, 1, 100), "max_seconds": (120, 1, 900),
              "max_model_calls": (10, 1, 100), "max_model_tokens": (10000, 1, 1000000),
              "max_output_tokens": (256, 1, 8192), "max_text_chars": (8000, 1, 100000),
              "max_history_chars": (64000, 1, 1000000)}
    for key, (default, minimum, maximum) in limits.items():
        number = policy.setdefault(key, default)
        if type(number) is not int or not minimum <= number <= maximum:
            raise ValueError(f"{key} must be an integer from {minimum} to {maximum}")
    _strings(policy.setdefault("stop_on_assistant", []), "stop_on_assistant")
    mode = policy["mode"]
    if mode == "script":
        if any(key in policy for key in ("opening", "branches", "default")):
            raise ValueError("script policies cannot carry rule branches")
        _strings(policy.get("script"), "script")
        if not policy["script"]:
            raise ValueError("script must contain at least one turn")
    elif mode == "rules":
        if "script" in policy:
            raise ValueError("rule policies cannot carry a future transcript")
        _text(policy.get("opening"), "opening")
        branches = policy.get("branches")
        if not isinstance(branches, list) or not branches:
            raise ValueError("branches must be a nonempty list")
        names = set()
        for branch in branches + [policy.get("default")]:
            if not isinstance(branch, dict) or set(branch) - {"id", "contains_any", "text", "stop", "once"}:
                raise ValueError("invalid rule branch")
            name = _text(branch.get("id"), "branch.id")
            if name in names:
                raise ValueError("branch IDs must be unique")
            names.add(name)
            if type(branch.get("stop", False)) is not bool or type(branch.get("once", False)) is not bool:
                raise ValueError("branch stop and once must be boolean")
            if not branch.get("stop"):
                _text(branch.get("text"), "branch.text")
            if branch in branches:
                if not _strings(branch.get("contains_any"), "contains_any"):
                    raise ValueError("branch predicates cannot be empty")
    elif any(key in policy for key in ("script", "opening", "branches", "default")):
        raise ValueError("model policies cannot carry future scripted replies or hidden branches")
    if len(_canonical(policy)) > 150000:
        raise ValueError("policy is too large")
    return policy


@dataclass
class ProviderControl:
    _check: object
    deadline: float
    max_output_tokens: int
    _cancel: threading.Event
    _cancel_handler: object = None
    _handler_lock: object = field(default_factory=threading.RLock)

    def on_cancel(self, handler):
        """Provider may register bounded local-process cleanup, not a remote claim."""
        if not callable(handler):
            raise ValueError("cancellation handler must be callable")
        with self._handler_lock:
            self._cancel_handler = handler
            if self._cancel.is_set():
                handler()

    def request_cancel(self):
        self._cancel.set()
        with self._handler_lock:
            if self._cancel_handler is not None:
                self._cancel_handler()

    @property
    def remaining_seconds(self):
        return max(0.0, self.deadline - time.monotonic())

    def check_cancelled(self):
        self._check()
        if self._cancel.is_set():
            raise CancelledError("Simulator local wait was cancelled; provider completion is unknown.")
        if self.remaining_seconds <= 0:
            raise InteractionLimit("deadline")


class TurnDriver:
    """One isolated conversation. Never pass a task case to a provider callback."""

    def __init__(self, policy, context, episode_id, provider=None):
        self.policy = validate_policy(policy)
        if self.policy["mode"] == "model" and not callable(provider):
            raise ValueError("model mode requires an explicit provider callback")
        if self.policy["mode"] != "model" and provider is not None:
            raise ValueError("only model mode may receive a provider callback")
        self.context, self.episode_id, self.provider = context, _text(episode_id, "episode_id"), provider
        self.transcript, self.used_branches = [], set()
        self.turn, self.model_calls, self.known_tokens = 0, 0, 0
        self.unknown_usage_calls, self.stop_reason = 0, None
        self.provider_receipts = []
        self.pending_usage_stop = False
        self.deadline = time.monotonic() + min(self.policy["max_seconds"], context.timeout_seconds)
        self.policy_hash = hashlib.sha256(_canonical(self.policy).encode()).hexdigest()
        self.identity = {"policy_id": self.policy["id"], "policy_version": self.policy["version"],
                         "policy_sha256": self.policy_hash, "mode": self.policy["mode"],
                         "simulation": {"script": "scripted", "rules": "rule_based", "model": "model_simulated"}[self.policy["mode"]]}
        self._emit("started", {"limits": {k: v for k, v in self.policy.items() if k.startswith("max_")},
                               "goal": self.policy["goal"], "visible_facts": self.policy["visible_facts"],
                               "forbidden_knowledge": self.policy["forbidden_knowledge"]})

    def _emit(self, kind, data):
        self.context.emit("interaction." + kind, {**self.identity, **data},
                          episode=self.episode_id, turn=self.turn, stage="user_simulator")

    def snapshot(self):
        return {**self.identity, "episode_id": self.episode_id, "turns": self.turn,
                "transcript": copy.deepcopy(self.transcript), "stop_reason": self.stop_reason,
                "model_calls": self.model_calls, "known_tokens": self.known_tokens,
                "unknown_usage_calls": self.unknown_usage_calls, "used_branches": sorted(self.used_branches),
                "provider_receipts": copy.deepcopy(self.provider_receipts)}

    def finish(self, reason="external_completion"):
        _text(reason, "stop_reason")
        if len(reason) > 200:
            raise ValueError("stop_reason is too long")
        if self.stop_reason is None:
            self.stop_reason = reason
            self._emit("stopped", {"reason": reason, "model_calls": self.model_calls,
                                    "known_tokens": self.known_tokens, "unknown_usage_calls": self.unknown_usage_calls})
        return {"text": None, "stop_reason": self.stop_reason, "turn": self.turn, **self.identity}

    def _accept_assistant(self, text):
        if not isinstance(text, str):
            raise ValueError("actual assistant delivery must be text")
        if not self.transcript or self.transcript[-1]["role"] != "user":
            raise ValueError("an assistant delivery must follow exactly one user turn")
        if len(text) > self.policy["max_text_chars"]:
            self._emit("delivery_rejected", {"reason": "assistant_text_limit", "chars": len(text),
                                              "sha256": hashlib.sha256(text.encode()).hexdigest()})
            return self.finish("assistant_text_limit")
        self.transcript.append({"role": "assistant", "text": text})
        self._emit("assistant_delivered", {"text": text})
        return None

    def next_user(self, assistant_text=None):
        if self.stop_reason is not None:
            return self.finish(self.stop_reason)
        if assistant_text is not None:
            stopped = self._accept_assistant(assistant_text)
            if stopped:
                return stopped
        elif self.transcript and self.transcript[-1]["role"] == "user":
            raise ValueError("supply the real assistant delivery before requesting another user turn")
        try:
            self.context.check_cancelled()
            if time.monotonic() >= self.deadline:
                return self.finish("deadline")
            if self.turn >= self.policy["max_turns"]:
                return self.finish("max_turns")
            if self.pending_usage_stop:
                return self.finish("provider_usage_unknown")
            if sum(len(t["text"]) for t in self.transcript) > self.policy["max_history_chars"]:
                return self.finish("visible_history_limit")
            latest = assistant_text or ""
            if any(needle.casefold() in latest.casefold() for needle in self.policy["stop_on_assistant"]):
                return self.finish("assistant_stop_rule")
            mode = self.policy["mode"]
            branch = None
            if mode == "script":
                if self.turn >= len(self.policy["script"]):
                    return self.finish("script_exhausted")
                text = self.policy["script"][self.turn]
            elif mode == "rules":
                if not self.turn:
                    text, branch = self.policy["opening"], "opening"
                else:
                    selected = next((rule for rule in self.policy["branches"]
                                     if not (rule.get("once") and rule["id"] in self.used_branches)
                                     and any(s.casefold() in latest.casefold() for s in rule["contains_any"])), self.policy["default"])
                    branch = selected["id"]
                    self.used_branches.add(branch)
                    self._emit("branch_selected", {"branch": branch, "assistant_text": latest})
                    if selected.get("stop"):
                        return self.finish("rule:" + branch)
                    text = selected["text"]
            else:
                response = self._model_turn()
                if response.get("stop_reason"):
                    return self.finish(response["stop_reason"])
                text = response["text"]
            if not isinstance(text, str) or not text.strip():
                return self.finish("empty_user_turn")
            if len(text) > self.policy["max_text_chars"]:
                return self.finish("user_text_limit")
            self.turn += 1
            self.transcript.append({"role": "user", "text": text})
            self._emit("user_generated", {"text": text, "branch": branch})
            return {"text": text, "stop_reason": None, "turn": self.turn, "branch": branch, **self.identity}
        except CancelledError:
            return self.finish("cancelled")
        except InteractionLimit as error:
            return self.finish(str(error))

    def _model_turn(self):
        if self.model_calls >= self.policy["max_model_calls"]:
            return {"stop_reason": "max_model_calls"}
        if self.known_tokens >= self.policy["max_model_tokens"]:
            return {"stop_reason": "max_model_tokens"}
        limit = min(self.policy["max_output_tokens"], self.policy["max_model_tokens"] - self.known_tokens)
        request = {"schema_version": "myeval.simulator-request.v1", **self.identity,
                   "goal": self.policy["goal"], "visible_facts": copy.deepcopy(self.policy["visible_facts"]),
                   "forbidden_knowledge": copy.deepcopy(self.policy["forbidden_knowledge"]),
                   "visible_transcript": copy.deepcopy(self.transcript), "next_turn": self.turn + 1,
                   "max_output_tokens": limit, "max_text_chars": self.policy["max_text_chars"]}
        control = ProviderControl(self.context.check_cancelled, self.deadline, limit, threading.Event())
        self.model_calls += 1
        self._emit("provider_requested", {"request": request, "model_call": self.model_calls})
        result_queue = queue.Queue(maxsize=1)

        def invoke():
            try:
                result_queue.put((True, self.provider(copy.deepcopy(request), control)))
            except Exception as error:
                result_queue.put((False, type(error).__name__))

        threading.Thread(target=invoke, name="myeval-user-simulator", daemon=True).start()
        try:
            while True:
                control.check_cancelled()
                try:
                    ok, value = result_queue.get(timeout=min(.05, control.remaining_seconds))
                    break
                except queue.Empty:
                    continue
        except (CancelledError, InteractionLimit):
            control.request_cancel()
            # Give a cooperative transport a bounded chance to persist its
            # cancellation receipt before the enclosing worker can exit.
            try:
                ok, cancelled_result = result_queue.get(timeout=2)
                if ok and isinstance(cancelled_result, dict):
                    provenance = _public_provenance(cancelled_result.get("provenance"))
                    if provenance:
                        self.provider_receipts.append(provenance)
                    raw = cancelled_result.get("usage")
                    raw = raw if isinstance(raw, dict) else {}
                    usage = {k: raw[k] for k in ("input_tokens", "output_tokens", "total_tokens")
                             if type(raw.get(k)) is int and raw[k] >= 0}
                    if "total_tokens" not in usage and all(k in usage for k in ("input_tokens", "output_tokens")):
                        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
                    if "total_tokens" in usage:
                        self.known_tokens += usage["total_tokens"]
                    else:
                        self.unknown_usage_calls += 1
                    self._emit("provider_terminal_after_local_stop", {"provenance": provenance, "usage": usage or None})
                else:
                    self.unknown_usage_calls += 1
            except queue.Empty:
                self.unknown_usage_calls += 1
            self._emit("provider_wait_stopped", {"provider_completion": "unknown", "billing_stopped": "unknown"})
            raise
        if not ok:
            self._emit("provider_failed", {"error_type": value})
            return {"stop_reason": "provider_error"}
        if not isinstance(value, dict) or type(value.get("stop", False)) is not bool:
            return {"stop_reason": "invalid_provider_response"}
        # A provider may attach its own safe execution receipt. Never forward it
        # into a subsequent simulator request or expose arbitrary provider fields.
        provenance = _public_provenance(value.get("provenance"))
        if provenance:
            self.provider_receipts.append(provenance)
        usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}
        usage = {key: usage[key] for key in ("input_tokens", "output_tokens", "total_tokens")
                 if type(usage.get(key)) is int and usage[key] >= 0}
        total = usage.get("total_tokens")
        if total is None and all(key in usage for key in ("input_tokens", "output_tokens")):
            total = usage["input_tokens"] + usage["output_tokens"]
            usage["total_tokens"] = total
            usage["total_tokens_derived"] = True
        if total is None:
            self.unknown_usage_calls += 1
            self.pending_usage_stop = True
        else:
            self.known_tokens += total
        text = value.get("text")
        public = {"text": text if isinstance(text, str) and len(text) <= self.policy["max_text_chars"] else None,
                  "stop": value.get("stop", False), "usage": usage or None,
                  "model": value.get("model") if isinstance(value.get("model"), str) and len(value["model"]) <= 200 else None,
                  "provenance": provenance}
        self._emit("provider_responded", public)
        provider_error = value.get("provider_error")
        if isinstance(provider_error, dict):
            code = provider_error.get("code")
            safe_codes = {"cli_unavailable", "cli_failed", "invalid_output", "unexpected_tool", "output_limit",
                          "cancelled", "deadline", "provider_exception", "request_rejected"}
            code = code if code in safe_codes else "provider_exception"
            self._emit("provider_failed", {"code": code, "provenance": provenance})
            return {"stop_reason": code if code in {"cancelled", "deadline"} else "provider_error:" + code}
        if value.get("stop"):
            reason = value.get("stop_reason")
            return {"stop_reason": "simulator_completed" if reason == "goal_complete" else "simulator_stopped"}
        if not isinstance(text, str) or not text.strip() or len(text) > self.policy["max_text_chars"]:
            return {"stop_reason": "invalid_provider_text"}
        if usage.get("output_tokens", 0) > limit:
            return {"stop_reason": "provider_output_budget_exceeded"}
        if self.known_tokens > self.policy["max_model_tokens"]:
            return {"stop_reason": "observed_token_budget_exceeded"}
        return {"text": text}
