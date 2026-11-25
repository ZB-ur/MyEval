"""Deterministic, additive validation of complete JSON-valued evidence.

The original normalized judgment is never edited. This module permits a separate
``json_value`` evidence channel only when a strict RFC 6901 pointer locates the
exact same-side value and the whole quoted JSON literal matches its complete
type and value. All other validation remains governed by schema.py.

No model, database, source inspection, or file I/O occurs in revalidate_judgment.
Call validator_identity explicitly when an integration needs source hashes.
"""
from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re

from .schema import validate_judgment as _legacy_validate


VALIDATOR_ID = "strict-json-evidence-v1"
VALIDATOR_VERSION = "1.0.0"
PROJECTION_VERSION = "myeval.judgment.typed-evidence.v1"
_EVIDENCE_KEYS = {"dimension", "source", "pointer", "quote", "explanation", "absence"}
_SIDE_KEYS = {"scores", "score_status", "reason", "evidence", "hard_violations"}
_ARRAY_INDEX = re.compile(r"(?:0|[1-9][0-9]*)\Z", re.ASCII)


class _EvidenceError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def validator_identity():
    """Explicit source identity lookup; never called by module import/validation."""
    here = Path(__file__).resolve()
    implementation = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in (here, here.with_name("schema.py"))}
    identity = {"id": VALIDATOR_ID, "version": VALIDATOR_VERSION,
                "implementation": implementation}
    return {**identity, "hash": hashlib.sha256(_canonical(identity).encode()).hexdigest()}


def _tokens(pointer):
    """Decode the string representation of RFC 6901, never a URI fragment."""
    if type(pointer) is not str or (pointer and not pointer.startswith("/")):
        raise _EvidenceError("invalid_evidence_pointer")
    if pointer == "":
        return []
    tokens = []
    for encoded in pointer[1:].split("/"):
        decoded, index = [], 0
        while index < len(encoded):
            char = encoded[index]
            if char != "~":
                decoded.append(char)
                index += 1
                continue
            if index + 1 >= len(encoded) or encoded[index + 1] not in "01":
                raise _EvidenceError("invalid_evidence_pointer")
            decoded.append("~" if encoded[index + 1] == "0" else "/")
            index += 2
        tokens.append("".join(decoded))
    return tokens


def _locate(value, tokens):
    for token in tokens:
        if type(value) is dict:
            if token not in value:
                raise _EvidenceError("evidence_pointer_missing")
            value = value[token]
        elif type(value) is list:
            # '-' means a nonexistent append position, not an evidence value.
            # No negative, signed, Unicode-digit or leading-zero array indices.
            if _ARRAY_INDEX.fullmatch(token) is None:
                raise _EvidenceError("invalid_evidence_array_index")
            if len(token) > len(str(len(value))):
                raise _EvidenceError("evidence_pointer_missing")
            index = int(token)
            if index >= len(value):
                raise _EvidenceError("evidence_pointer_missing")
            value = value[index]
        else:
            raise _EvidenceError("evidence_pointer_not_container")
    return value


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _EvidenceError("json_value_duplicate_key")
        result[key] = value
    return result


def _constant(_):
    raise _EvidenceError("json_value_nonfinite")


def _literal(quote):
    # json.loads permits the standard surrounding JSON whitespace, but rejects
    # any prefix/suffix content. Decimal preserves every quoted fractional digit.
    try:
        value = json.loads(quote, object_pairs_hook=_object,
                           parse_float=Decimal, parse_constant=_constant)
    except _EvidenceError:
        raise
    except (ValueError, TypeError, OverflowError, RecursionError, InvalidOperation) as error:
        raise _EvidenceError("json_value_invalid_literal") from error
    if type(value) is str:
        raise _EvidenceError("json_value_string_literal_not_supported")
    return value


def _same_value(actual, quoted):
    """No coercion, partial objects, bool/int equivalence, or numeric rounding.

    Integers and fractional/exponent numbers are kept distinct conservatively.
    A finite Python float is compared with the exact decimal number in its JSON
    round-trip representation, not by rounding a quoted Decimal into a float.
    """
    if actual is None:
        return quoted is None
    if type(actual) is bool:
        return type(quoted) is bool and actual is quoted
    if type(actual) is int:
        return type(quoted) is int and actual == quoted
    if type(actual) is float:
        if not math.isfinite(actual) or type(quoted) is not Decimal or not quoted.is_finite():
            return False
        canonical = Decimal(json.dumps(actual, allow_nan=False))
        return canonical == quoted and (canonical != 0 or canonical.is_signed() == quoted.is_signed())
    if type(actual) is str:
        return type(quoted) is str and actual == quoted
    if type(actual) is list:
        return (type(quoted) is list and len(actual) == len(quoted)
                and all(_same_value(a, b) for a, b in zip(actual, quoted)))
    if type(actual) is dict:
        return (type(quoted) is dict and all(type(k) is str for k in actual)
                and set(actual) == set(quoted)
                and all(_same_value(actual[k], quoted[k]) for k in actual))
    return False


def _json_type(value):
    if value is None:
        return "null"
    return {bool: "boolean", int: "integer", float: "number", list: "array", dict: "object"}.get(type(value))


def _strings(value):
    # Exactly the legacy text-evidence scope: leaf strings below the located
    # value. A typed channel never changes or manufactures a textual quotation.
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)


def _sort_key(value):
    """Stable even for malformed, nonfinite fields in a rejected input issue."""
    if value is None:
        return ("null",)
    if type(value) is bool:
        return ("boolean", str(value))
    if type(value) is int:
        return ("integer", str(value))
    if type(value) is float:
        return ("number", value.hex())
    if type(value) is str:
        return ("string", value)
    if type(value) is list:
        return ("array", tuple(_sort_key(item) for item in value))
    if type(value) is dict:
        return ("object", tuple(sorted((_sort_key(k), _sort_key(v)) for k, v in value.items())))
    return ("unsupported", type(value).__name__)


def _ordered_issues(issues):
    ordered, previous = [], None
    for issue in sorted(issues, key=_sort_key):
        key = _sort_key(issue)
        if key != previous:
            ordered.append(copy.deepcopy(issue))
        previous = key
    return ordered


def _valid_item(item, dimensions):
    return (type(item) is dict and set(item) == _EVIDENCE_KEYS
            and type(item.get("dimension")) is str and item["dimension"] in dimensions
            and type(item.get("source")) is str and item["source"] in {"case", "output", "media"}
            and type(item.get("quote")) is str and type(item.get("explanation")) is str
            and bool(item["explanation"].strip()) and type(item.get("absence")) is bool)


def revalidate_judgment(judgment, rubric, payload):
    """Return an additive projection without changing the supplied judgment.

    Only a successfully verified typed evidence item's own quote_not_found and
    its now-supported dimension's rated_without_valid_evidence can be removed.
    All legacy score, reason, preference, media, hard-violation and shape checks
    survive. Invalid RFC 6901 paths are additionally rejected even where the
    legacy pointer implementation happened to accept them.
    """
    try:
        legacy = _legacy_validate(judgment, rubric, payload)
    except (ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError) as error:
        # Malformed input is rejected, never interpreted as an empty issue list.
        legacy = [{"code": "legacy_validator_error", "type": type(error).__name__}]
    issues, typed, transforms = [], [], []
    typed_locations, supported, recomputed = set(), {}, set()
    sides = judgment.get("sides", {}) if type(judgment) is dict else {}
    source_sides = payload.get("sides", {}) if type(payload) is dict else {}
    dimensions = rubric.get("dimensions", {}) if type(rubric) is dict else {}
    if type(dimensions) is not dict or not dimensions or any(type(key) is not str for key in dimensions):
        issues.append({"code": "invalid_revalidation_rubric_dimensions"})
        dimensions = set()
    else:
        dimensions = set(dimensions)
    if type(sides) is not dict:
        sides = {}
    if type(source_sides) is not dict:
        source_sides = {}
    for label in sorted(sides, key=_sort_key):
        side = sides[label]
        if type(side) is not dict or type(side.get("evidence")) is not list:
            continue
        eligible = (type(label) is str and label in source_sides and type(source_sides[label]) is dict
                    and set(side) == _SIDE_KEYS and type(side.get("scores")) is dict
                    and set(side["scores"]) == dimensions and type(side.get("score_status")) is dict
                    and set(side["score_status"]) == dimensions and len(side["evidence"]) <= 150)
        if eligible:
            supported[label] = set()
            recomputed.add(label)
        for index, item in enumerate(side["evidence"]):
            reference = {"side": label, "evidence_index": index}
            pointer = item.get("pointer") if type(item) is dict else None
            try:
                tokens = _tokens(pointer)
            except _EvidenceError as error:
                issues.append({**reference, "code": error.code})
                continue
            if not eligible or not _valid_item(item, dimensions):
                continue
            dimension = item["dimension"]
            if item["source"] == "media":
                media = payload.get("media", [])
                if (type(media) is list and not item["quote"] and any(type(m) is dict
                        and type(m.get("id")) is str and "/" + m["id"] == pointer
                        and m.get("attached") for m in media)):
                    supported[label].add(dimension)
                continue
            source = payload.get("case") if item["source"] == "case" else source_sides[label].get("output")
            # Missing roots are different from a present JSON null root.
            if ((item["source"] == "case" and "case" not in payload)
                    or (item["source"] == "output" and "output" not in source_sides[label])):
                issues.append({**reference, "code": "evidence_pointer_missing"})
                continue
            try:
                located = _locate(source, tokens)
                if item["quote"] and any(item["quote"] in text for text in _strings(located)):
                    supported[label].add(dimension)
                    continue  # The existing strict textual channel already suffices.
                quoted = _literal(item["quote"])
                if _json_type(located) is None or not _same_value(located, quoted):
                    raise _EvidenceError("json_value_type_or_value_mismatch")
                canonical = _canonical(located)
            except _EvidenceError as error:
                issues.append({**reference, "code": error.code})
                continue
            except (ValueError, TypeError, OverflowError, RecursionError, InvalidOperation):
                issues.append({**reference, "code": "json_value_invalid_or_unrepresentable"})
                continue
            typed_locations.add((label, index))
            supported[label].add(dimension)
            typed.append({**reference, "dimension": dimension, "source": item["source"],
                          "pointer": pointer, "channel": "json_value", "json_type": _json_type(located),
                          "json_value": copy.deepcopy(located), "canonical_json": canonical,
                          "value_sha256": hashlib.sha256(canonical.encode()).hexdigest()})
            transforms.append({**reference, "action": "add_typed_evidence", "channel": "json_value",
                               "reason": "complete_literal_matches_exact_pointer_type_and_value",
                               "original_quote_changed": False, "original_pointer_changed": False,
                               "judgment_changed": False})
    for issue in legacy:
        location = (issue.get("side"), issue.get("evidence_index"))
        if issue.get("code") == "quote_not_found" and location in typed_locations:
            continue
        if (issue.get("code") == "rated_without_valid_evidence" and issue.get("side") in recomputed
                and issue.get("dimension") in supported.get(issue.get("side"), set())):
            continue
        issues.append(issue)
    for label in sorted(recomputed):
        for dimension in sorted(dimensions):
            if sides[label]["score_status"][dimension] == "rated" and dimension not in supported[label]:
                issues.append({"code": "rated_without_valid_evidence", "side": label, "dimension": dimension})
    issues = _ordered_issues(issues)
    return {"schema_version": PROJECTION_VERSION,
            "validator": {"id": VALIDATOR_ID, "version": VALIDATOR_VERSION},
            "original_judgment": copy.deepcopy(judgment),
            "typed_evidence": typed, "transform_log": transforms,
            "validation_issues": issues, "legacy_validation_issues": _ordered_issues(legacy),
            "accepted": not issues}
