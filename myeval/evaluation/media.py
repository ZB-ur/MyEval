"""Copy only explicitly declared, authorized local images into immutable judge evidence."""
from __future__ import annotations

import copy
import hashlib
import os
import re
from pathlib import Path

from ..datasets import validate_source


def _format(raw):
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", ".gif"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise ValueError("媒体不是支持的 PNG/JPEG/GIF/WebP 图片")


def prepare_media(case, configuration, directory, project, authorization=None, *, attempt_media=None, attempt_root=None):
    source = validate_source(case, authorization, project)
    value = case.get("input", {})
    declared = case.get("media", value.get("media", []) if isinstance(value, dict) else [])
    if not declared and isinstance(value, dict) and value.get("image_path"):
        declared = [{"path": value["image_path"], "sha256": value.get("image_sha256") or source.get("media_sha256")}]
    original_declarations = declared
    from_attempt = attempt_media is not None
    if from_attempt:
        if not isinstance(attempt_media, list) or not attempt_media or attempt_root is None:
            raise ValueError("历史媒体快照需明确的清单和具体 attempt 根目录")
        declared = attempt_media
    if not declared:
        return []
    if not isinstance(declared, list) or len(declared) > 4:
        raise ValueError("每个评分单位最多明确声明 4 张图片")
    directory = Path(directory).resolve()
    media_root = directory / "evaluation" / "media"
    media_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    project_root = ((configuration.get("provenance") or {}).get("source") or {}).get("project_root")
    result = []
    for index, item in enumerate(declared):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("每张图片需要本机 path")
        expected = item.get("sha256")
        if not isinstance(expected, str) or not re.fullmatch("[a-fA-F0-9]{64}", expected):
            raise ValueError("图片需要冻结的 SHA-256")
        if from_attempt:
            original_hashes = {m.get("sha256", "").lower() for m in original_declarations if isinstance(m, dict) and isinstance(m.get("sha256"), str)} if isinstance(original_declarations, list) else set()
            if original_hashes and expected.lower() not in original_hashes:
                raise ValueError("历史图片快照与冻结案例媒体哈希不一致")
        item_source = copy.deepcopy(item.get("source", source))
        item_case = {"id": case.get("id"), "source": item_source,
                     "input": {"image_path": item["path"], "image_sha256": expected}}
        item_grant = item.get("authorization") or (item_source.get("authorization") if isinstance(item_source, dict) else None) or authorization
        checked_source = validate_source(item_case, item_grant, project, operation="evaluate")
        if checked_source["type"] != source["type"]:
            raise ValueError("图片来源类型与案例声明冲突；请将混合来源拆为分别授权的案例")
        if checked_source.get("media_sha256") and checked_source["media_sha256"].lower() != expected.lower():
            raise ValueError("图片自身来源声明的哈希与媒体条目不一致")
        path = Path(item["path"]).expanduser()
        if from_attempt:
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("历史媒体快照必须使用具体 attempt 内的相对路径")
            snapshot_root = Path(attempt_root)
            path = snapshot_root / path
            if not path.resolve().is_relative_to(snapshot_root.resolve()):
                raise ValueError("历史媒体快照越过具体 attempt 根目录")
        elif not path.is_absolute():
            if not project_root:
                raise ValueError("相对图片路径需要配置中冻结的 provenance.source.project_root")
            path = Path(project_root) / path
        if path.is_symlink() or any(p.is_symlink() for p in path.parents):
            raise ValueError("评分图片路径不能经过符号链接")
        path = path.resolve()
        if not path.is_file() or not 0 < path.stat().st_size <= 20_000_000:
            raise ValueError("图片必须存在且大小在 1 byte–20 MB 内")
        raw = path.read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected.lower():
            raise ValueError("评分图片与冻结哈希不一致")
        mime, suffix = _format(raw)
        target = media_root / (actual + suffix)
        try:
            with target.open("xb") as handle:
                handle.write(raw)
            os.chmod(target, 0o600)
        except FileExistsError:
            if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != actual:
                raise ValueError("现存评分媒体缓存哈希不一致") from None
        result.append({"id": "image-" + str(index + 1), "sha256": actual, "mime_type": mime,
                       "bytes": len(raw), "artifact_path": str(target.relative_to(directory)),
                       "source_type": checked_source["type"], "source_url": checked_source.get("url"),
                       "license": checked_source.get("license"), "attribution": checked_source.get("attribution"),
                       "source_id": checked_source.get("id"), "source_inherited_from_case": "source" not in item,
                       "authorization_hash": checked_source.get("authorization_hash"),
                       "read_from": "immutable_attempt_snapshot" if from_attempt else "declared_case_source",
                       "snapshot_path": item["path"] if from_attempt else None,
                       "attached": True})
    return result
