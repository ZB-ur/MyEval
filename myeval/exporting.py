"""Portable evidence attachments from explicitly declared, local trial artifacts."""
from __future__ import annotations

import hashlib
import base64
import copy
import re
from pathlib import Path

from .util import ROOT


TRACE_FILE_NAMES = ("request.json", "prompt.txt", "boundary.txt", "schema.json",
                    "run.json", "result.json", "model-result.json", "events.jsonl")


def _media_declarations(trial):
    case = trial.get("case", {})
    archived = (trial.get("result") or {}).get("media")
    if not archived:
        output = (trial.get("result") or {}).get("review_output")
        archived = output.get("media") if isinstance(output, dict) else None
    if isinstance(archived, list) and archived:
        return [{**item, "source": item.get("source") or case.get("source", {}), "_storage": "attempt"}
                for item in archived if isinstance(item, dict)]
    content = case.get("input") or {}
    content = content if isinstance(content, dict) else {}
    items = case.get("media", content.get("media", []))
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        items = []
    if not items and content.get("image_path"):
        items = [{"path": content["image_path"], "sha256": content.get("image_sha256") or case.get("source", {}).get("media_sha256"),
                  "mime_type": content.get("mime_type"), "source": case.get("source", {})}]
    return [item for item in items if isinstance(item, dict)]


def trial_media(trial):
    """Public metadata, without exposing arbitrary filesystem paths to the UI."""
    return [{"index": index, "sha256": item.get("sha256"), "mime_type": item.get("mime_type"),
             "source": {k: (item.get("source") or trial.get("case", {}).get("source", {})).get(k)
                        for k in ("type", "kind", "license", "url", "attribution")},
             "url": f"/api/trials/{trial['id']}/media/{index}"}
            for index, item in enumerate(_media_declarations(trial))]


def read_trial_media(store, trial, index):
    from .catalog import adapter
    from .datasets import validate_source
    from .snapshots import _check_path
    items = _media_declarations(trial)
    if not 0 <= index < len(items):
        raise KeyError("媒体不存在")
    item = items[index]
    case = trial["case"]
    experiment = store.experiment(trial["experiment_id"])
    source = item.get("source") or case.get("source", {})
    validate_source({**case, "source": source}, experiment.get("authorization"), trial["project"])
    declared = item.get("path")
    expected = item.get("sha256")
    if not isinstance(declared, str) or not isinstance(expected, str) or len(expected) != 64:
        raise ValueError("媒体需明确声明本机路径与 SHA-256")
    path = Path(declared).expanduser()
    if item.get("_storage") == "attempt":
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("执行媒体快照必须是 attempt 内的相对路径")
        path = store.attempt_root(trial) / path
    registered_root = None
    roots = [store.directory / "media", store.directory / "experiments", ROOT / "taskpacks", ROOT / ".local" / "holdouts", ROOT / ".local" / "media"]
    try:
        implementation = adapter(trial["project"])
        root = getattr(implementation, "_root", None)
        if callable(root):
            registered_root = Path(root()).resolve()
            roots.append(registered_root)
    except (ValueError, ImportError):
        pass
    if not path.is_absolute():
        # Product taskpacks use paths relative to their registered product root;
        # evaluation-owned paths may instead be relative to MyEval.
        candidates = ([registered_root / path] if registered_root else []) + [ROOT / path]
        path = next((p for p in candidates if p.is_file()), candidates[0])
    allowed = next((root for root in roots if path.absolute().is_relative_to(root.absolute())), None)
    if allowed is None:
        raise ValueError("媒体路径不在注册项目或专用评测材料目录内")
    path = _check_path(path, allowed)
    if not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"} or path.stat().st_size > 30_000_000:
        raise ValueError("仅支持 30 MB 以内的 PNG、JPEG、WebP、GIF 图片")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected.lower():
        raise ValueError("媒体内容与冻结 SHA-256 不一致；拒绝展示漂移的图片")
    mime = ("image/png" if raw.startswith(b"\x89PNG\r\n\x1a\n") else
            "image/jpeg" if raw.startswith(b"\xff\xd8\xff") else
            "image/gif" if raw.startswith((b"GIF87a", b"GIF89a")) else
            "image/webp" if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP" else None)
    if mime is None:
        raise ValueError("媒体字节不是允许的图片格式")
    return raw, mime


def attachments(store, trials):
    """Keep declared evidence in the manifest even when its bytes are unavailable."""
    from .receipts import _read
    from .snapshots import _check_path, _excluded, _safe_relative

    result = []
    image_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    text_suffixes = {".json", ".jsonl", ".txt", ".md", ".py", ".swift", ".h", ".hpp",
                     ".c", ".cpp", ".m", ".mm", ".toml", ".yaml", ".yml"}
    trace_names = TRACE_FILE_NAMES
    for trial in trials:
        for attempt in trial.get("attempts", [trial]):
            root = Path(store.attempt_root(attempt)).absolute()
            boundary = Path(getattr(store, "directory", root)).absolute()
            pending, emitted, collected = {}, set(), {}
            prefix = f"evidence/{trial['id']}/attempt-{attempt['attempt']}/"
            origin = attempt.get("origin") or {}
            source_prefix = (f"evidence/{origin['source_trial_id']}/attempt-{origin['source_attempt']}/"
                             if origin.get("kind") == "historical_import"
                             and isinstance(origin.get("source_trial_id"), str)
                             and type(origin.get("source_attempt")) is int else None)
            imported_artifacts = [item for item in attempt.get("imported_artifacts", []) if isinstance(item, dict)]

            def declare(relative, *, explicit=False, label=None, expected=None, media=None, directory=False, original=False, requires_archive=False):
                # Null product_trace means the product never declared a trace.
                if not isinstance(relative, str):
                    return
                entry = pending.setdefault(relative, {"relative": relative, "explicit": False,
                                                      "labels": [], "hashes": [], "directory": False, "media": None,
                                                      "original": False, "requires_archive": False})
                entry["explicit"] |= explicit
                entry["directory"] |= directory
                entry["original"] |= original
                entry["requires_archive"] |= requires_archive
                if label is not None and label not in entry["labels"]:
                    entry["labels"].append(label)
                if expected is not None and expected not in entry["hashes"]:
                    entry["hashes"].append(expected)
                if media is not None:
                    entry["media"] = media

            for name in ("request.json", "result.json", "events.jsonl"):
                declare(name)
            for imported in imported_artifacts:
                declare(imported.get("path"), explicit=True, label="imported_artifact", expected=imported.get("sha256"))
            declarations = (attempt.get("result") or {}).get("artifacts", {})
            for label, relative in declarations.items() if isinstance(declarations, dict) else []:
                declare(relative, explicit=True, label=str(label), directory=True, original=True)
            for media in _media_declarations(attempt):
                if media.get("_storage") == "attempt":
                    declare(media.get("path"), explicit=True, label="attempt_media", expected=media.get("sha256"), media=media, original=True)

            def collect(spec):
                relative = spec["relative"]
                if relative in emitted:
                    return
                emitted.add(relative)
                entry = {"trial_id": trial["id"], "attempt": attempt["attempt"], "declared_path": relative}
                if spec["labels"]:
                    entry["declarations"] = spec["labels"]
                if len(spec["hashes"]) == 1:
                    entry["declared_sha256"] = spec["hashes"][0]
                elif spec["hashes"]:
                    entry["declared_sha256_values"] = spec["hashes"]

                def omitted(code, message, **detail):
                    entry.update(availability="unavailable", omitted=message,
                                 error={"code": code, "message": message, **detail})
                    collected[relative] = entry
                    result.append(entry)

                try:
                    local = _safe_relative(relative)
                    entry["path"] = prefix + local.as_posix()
                    if _excluded(local):
                        omitted("attachment_path_rejected", "声明的附件不在可见证据白名单内")
                        return
                    mappings = [item for item in imported_artifacts
                                if spec["original"] and source_prefix is not None
                                and item.get("source_path") == source_prefix + local.as_posix()]
                    if mappings:
                        identities = {(item.get("path"), item.get("sha256")) for item in mappings
                                      if isinstance(item.get("path"), str) and isinstance(item.get("sha256"), str)}
                        if (len(identities) != 1
                                or any(not isinstance(item.get("path"), str) or not isinstance(item.get("sha256"), str) for item in mappings)):
                            omitted("archive_mapping_ambiguous", "原声明对应多个或无效的归档映射；未猜测附件位置")
                            return
                        archived_relative, expected = next(iter(identities))
                        if not re.fullmatch(r"[0-9a-f]{64}", expected):
                            omitted("archive_mapping_invalid", "原声明的归档映射缺少有效 SHA-256")
                            return
                        if archived_relative != relative:
                            collect(pending[archived_relative])
                            archived = collected.get(archived_relative, {})
                            entry.update(archive_path=archived.get("path"), archive_declared_path=archived_relative,
                                         source_path=source_prefix + local.as_posix(), declared_sha256=expected, kind="archived_alias")
                            if (archived.get("availability") != "available" or archived.get("sha256") != expected
                                    or spec["hashes"] and spec["hashes"] != [expected]):
                                omitted("archived_attachment_unavailable", "明确映射的归档附件缺失、不可读或哈希不符；未视为已迁移",
                                        archive_error=archived.get("error"))
                                return
                            entry.update(availability="relocated", kind="archived_alias", sha256=expected,
                                         bytes=archived["bytes"], binding_status="exact_imported_artifact_mapping")
                            collected[relative] = entry
                            result.append(entry)
                            return
                        if expected not in spec["hashes"]:
                            spec["hashes"].append(expected)
                        entry["declared_sha256"] = expected
                    else:
                        # A trace directory is represented by exact, declared
                        # source children; this is never a basename search.
                        source_directory = source_prefix + local.as_posix().rstrip("/") + "/" if source_prefix else None
                        child_sources = imported_artifacts + [item for item in attempt.get("unavailable_imported_aliases", []) if isinstance(item, dict)]
                        mapped_children = {item["source_path"][len(source_directory):] for item in child_sources
                                           if spec["original"] and spec["directory"] and source_directory
                                           and isinstance(item.get("source_path"), str)
                                           and item["source_path"].startswith(source_directory)
                                           and item["source_path"][len(source_directory):] in trace_names}
                        declared_children = set()
                        for parent in attempt.get("imported_directory_declarations", []):
                            if (isinstance(parent, dict) and spec["original"] and spec["directory"] and source_prefix
                                    and parent.get("source_path") == source_prefix + local.as_posix()):
                                names = parent.get("declared_children")
                                if not isinstance(names, list) or any(name not in trace_names for name in names):
                                    raise ValueError("invalid_archived_directory_declaration")
                                declared_children.update(names)
                        children = sorted(mapped_children | declared_children)
                        if children:
                            child_entries = []
                            for name in children:
                                child = (local / name).as_posix()
                                declare(child, explicit=True, label="trace:" + relative, original=True, requires_archive=True)
                                collect(pending[child])
                                child_entries.append(collected.get(child, {}))
                            entry.update(kind="archived_directory_alias", declared_children=children,
                                         scope="declared_children_only",
                                         archive_paths=[item.get("archive_path", item.get("path")) for item in child_entries])
                            if any(item.get("availability") not in {"available", "relocated"} for item in child_entries):
                                omitted("archived_directory_incomplete", "轨迹目录的已声明归档子文件缺失或校验失败")
                            else:
                                entry.update(availability="relocated", binding_status="exact_imported_child_mappings")
                                collected[relative] = entry
                                result.append(entry)
                            return
                        unavailable = [item for item in attempt.get("unavailable_imported_aliases", [])
                                       if isinstance(item, dict) and spec["original"] and source_prefix is not None
                                       and item.get("source_path") == source_prefix + local.as_posix()]
                        if unavailable:
                            entry.update(kind="archived_alias_unavailable", source_declarations=unavailable,
                                         declared_sha256=unavailable[0].get("sha256"),
                                         source_archive_path=unavailable[0].get("source_archive_path"))
                            omitted("archived_alias_source_unavailable", "来源归档字节未提供或校验失败；保留原别名与声明 SHA-256，未视为已迁移")
                            return
                        if spec["requires_archive"]:
                            entry.update(kind="archived_directory_child_unavailable", source_path=source_prefix + local.as_posix())
                            omitted("declared_archive_child_missing", "父目录已声明该子文件，但来源未提供其可验证归档映射或字节")
                            return
                    path = _check_path(root / local, boundary, allow_missing=True)
                    if not path.exists():
                        if spec["explicit"]:
                            omitted("declared_attachment_missing", "已声明的附件文件或目录缺失；没有导出字节")
                        return
                    if path.is_dir() and spec["directory"] and spec["media"] is None:
                        # The directory declares a trace container, not that every
                        # optional trace filename must have been produced.
                        children = [path / name for name in trace_names
                                    if (path / name).exists() or (path / name).is_symlink()]
                        if not children:
                            omitted("declared_trace_empty", "已声明的轨迹目录没有可导出的白名单证据文件")
                        for child in children:
                            child_relative = child.relative_to(root).as_posix()
                            declare(child_relative, explicit=True, label="trace:" + relative, original=True)
                            collect(pending[child_relative])
                        return
                    is_image = path.suffix.lower() in image_suffixes
                    if not is_image and path.suffix.lower() not in text_suffixes:
                        omitted("attachment_type_unsupported", "声明的附件类型不在可见证据白名单内")
                        return
                    limit = 30 * 1024 * 1024 if is_image else 8_000_000
                    if path.stat().st_size > limit:
                        omitted("attachment_too_large", "附件超过有界导出大小；本机声明仍保留", limit_bytes=limit)
                        return
                    raw = _read(path, boundary, limit)
                    observed = hashlib.sha256(raw).hexdigest()
                    if spec["hashes"] and (len(spec["hashes"]) != 1 or spec["hashes"][0] != observed):
                        entry["observed_sha256"] = observed
                        omitted("attachment_hash_mismatch", "附件内容与声明 SHA-256 不一致；没有导出漂移字节")
                        return
                    if is_image and (spec["media"] is None or not spec["hashes"]):
                        entry["observed_sha256"] = observed
                        omitted("media_identity_unavailable", "媒体未绑定 attempt 内冻结 SHA-256；没有导出字节")
                        return
                    if is_image:
                        entry.update(content_base64=base64.b64encode(raw).decode(), kind="attempt_media", source=spec["media"].get("source"))
                    else:
                        entry["text"] = raw.decode("utf-8")
                    entry.update(sha256=observed, bytes=len(raw), availability="available")
                    collected[relative] = entry
                    result.append(entry)
                except (OSError, ValueError) as error:
                    # Rejected paths remain metadata only; never become ZIP members.
                    entry.setdefault("path", prefix + "unavailable-" + hashlib.sha256(relative.encode("utf-8")).hexdigest()[:20])
                    code = "attachment_invalid_utf8" if isinstance(error, UnicodeError) else "attachment_unreadable_or_rejected"
                    omitted(code, "已声明或已发现的附件无法安全读取；没有导出字节", error_type=type(error).__name__)

            for relative in sorted(pending):
                collect(pending[relative])
    return result


def source_attachments(store, experiment):
    result = []
    for entry in experiment.get("source_snapshot", {}).get("files", []):
        relative = entry.get("path")
        if entry.get("status") != "captured" or not isinstance(relative, str):
            continue
        path = store.directory / relative
        from .snapshots import _check_path
        try:
            path = _check_path(path, store.directory / "source-snapshots")
            if path.stat().st_size > 10_000_000:
                raise ValueError("source_size_limit")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != entry.get("sha256"):
                raise ValueError("source_snapshot_hash_mismatch")
            result.append({"path": relative, "sha256": entry["sha256"], "bytes": len(raw), "kind": "frozen_source", "text": raw.decode("utf-8")})
        except (OSError, ValueError) as error:
            result.append({"path": relative, "kind": "frozen_source", "omitted": str(error) if isinstance(error, ValueError) else type(error).__name__})
    return result


def revalidation_index(store, experiment, case_id=None):
    """Whole-job receipts never inherit a case-only export filter."""
    try:
        listing = store.revalidations.list(experiment_id=experiment["id"])
    except (OSError, ValueError, KeyError):
        listing = {"receipts": [], "invalid": [{"status": "unavailable_registry", "error": "重验收目录无法安全读取；分母未知，未视为零"}]}
    receipts = []
    for item in listing["receipts"]:
        prefix = "revalidations/" + item["id"]
        receipts.append({**item, "scope": "whole_experiment", "availability": "indexed",
                         "artifact_paths": [prefix + "/manifest.json", prefix + "/receipt.json"]})
    history = copy.deepcopy(experiment.get("revalidation_history"))
    return {"schema_version": "myeval.revalidation-export.v1", "scope": "whole_experiment",
            "experiment_id": experiment["id"], "requested_case_filter": case_id,
            "receipts": receipts, "invalid": listing["invalid"], "historical": [history] if history else [],
            "attachments_included": False, "assessment_count_kind": "planned_not_import_confirmation",
            "limitations": ["完整原作业分母保留；单案例筛选不裁剪重验收收据。", "登记及导出不是评分导入确认。",
                            "历史来源收据保留原始 job/trial 身份，不绑定新实验、不执行评分。",
                            "无法识别实验归属的损坏收据仍显式列出；未知分母不能记为零。"]}


def revalidation_attachments(store, bundle):
    """Attach only fixed verified registry files or hash-bound import archives."""
    from .receipts import _read, MAX_RECEIPT_BYTES
    from .snapshots import _safe_relative
    index = bundle.get("revalidations")
    if not isinstance(index, dict):
        return []
    result = []
    for item in index.get("receipts", []):
        try:
            files = store.revalidations.artifact_files(item["id"])
            for file in files:
                result.append({"path": "revalidations/" + item["id"] + "/" + file["name"],
                               "sha256": file["sha256"], "bytes": file["bytes"],
                               "kind": "revalidation_manifest" if file["name"] == "manifest.json" else "revalidation_receipt",
                               "receipt_id": item["id"], "scope": "whole_experiment",
                               "binding_status": "registered_original_experiment", "text": file["raw"].decode("utf-8")})
            item.update(availability="available", counts_verified_for_export=True)
        except (OSError, ValueError, KeyError, TypeError):
            item.update(availability="unavailable", counts_verified_for_export=False,
                        error="重验收材料完整性校验失败；保留索引但未导出不可信字节")
            index["invalid"].append({"id": item.get("id"), "status": "unavailable_artifact", "error": item["error"]})
    for history in index.get("historical", []):
        unavailable = copy.deepcopy(history.get("unavailable", []))
        exported_files = []
        import_id = history.get("import_id", "")
        for item in history.get("files", []):
            try:
                if not isinstance(import_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", import_id):
                    raise ValueError("invalid_history_identity")
                relative = _safe_relative(item["storage_path"])
                root = store.directory / "imports" / import_id / "artifacts"
                raw = _read(store.directory / relative, root, MAX_RECEIPT_BYTES)
                if hashlib.sha256(raw).hexdigest() != item["sha256"] or len(raw) != item["bytes"]:
                    raise ValueError("archived_revalidation_hash_mismatch")
                exported = {"path": "revalidation-history/" + import_id + "/" + item["sha256"] + ".json",
                            "sha256": item["sha256"], "bytes": len(raw), "kind": item["kind"],
                            "receipt_id": item.get("receipt_id"), "scope": "whole_source_experiment",
                            "binding_status": "original_provenance_only", "source_path": item["source_path"], "text": raw.decode("utf-8")}
                result.append(exported)
                exported_files.append({k: v for k, v in exported.items() if k != "text"})
            except (OSError, ValueError, KeyError, TypeError):
                unavailable.append({"receipt_id": item.get("receipt_id"), "status": "unavailable_artifact",
                                    "error": "原来源重验收附件缺失或 SHA-256 不符；分母不能据此省略"})
        history["exported_artifacts"], history["unavailable"] = exported_files, unavailable
    index["attachments_included"] = True
    return result
