"""Versioned evaluation datasets and explicit material provenance.

The registry stores declarations and their evidence references. It does not fetch
URLs, open supplied media paths, or infer consent from a public/ private label.
Executable configurations always come from locally registered task packages.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .util import dumps, now, uid


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_SPLITS = {"development": "development", "dev": "development", "smoke": "development",
           "regression": "development", "holdout": "holdout", "new_holdout": "holdout"}
_SOURCE_TYPES = {"synthetic", "licensed_public", "authorized_private"}


def _hash(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _text(value, label, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label}必须为 1–{maximum} 字的非空字符串")
    return value.strip()


def _identifier(value, label):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{label}需为 1–128 位字母、数字及 . _ : -，以字母或数字开头")
    return value


def _date(value, label):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed
    except (AttributeError, TypeError, ValueError):
        raise ValueError(f"{label}需为含时区的 ISO 8601 时间") from None


def _url(value, label):
    value = _text(value, label, 3000)
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{label}需为不含凭据的 http(s) 来源地址")
    return value


def _media_values(value):
    """Inspect only submitted metadata, never the referenced filesystem."""
    found, hashes = False, []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"image_path", "photo_path", "media_path", "asset_path", "image_file"} and item:
                found = True
            if key in {"media_sha256", "sha256", "image_sha256", "asset_sha256"} and item is not None:
                if not isinstance(item, str) or not _SHA256.fullmatch(item):
                    raise ValueError("媒体 SHA-256 必须为 64 位十六进制值")
                hashes.append(item.lower())
            child_found, child_hashes = _media_values(item)
            found = found or child_found
            hashes.extend(child_hashes)
    elif isinstance(value, list):
        for item in value:
            child_found, child_hashes = _media_values(item)
            found = found or child_found
            hashes.extend(child_hashes)
    return found, hashes


def validate_source(case, authorization=None, project=None, operation="evaluate"):
    """Validate one case's declared source and return a normalized copy.

    Private authorization is concrete and scoped to the project and case/source.
    The caller must pass the same declaration again at execution freeze, so an
    expired grant cannot be reused merely because dataset import once succeeded.
    """
    if not isinstance(case, dict) or not isinstance(case.get("source"), dict):
        raise ValueError("每个案例必须声明 source；不能从内容推定材料来源")
    source = copy.deepcopy(case["source"])
    kind = source.get("type", source.get("kind"))
    if kind is None and source.get("synthetic") is True:
        kind = "synthetic"
    if kind not in _SOURCE_TYPES:
        raise ValueError("source.type 必须为 synthetic、licensed_public 或 authorized_private")
    if kind != "synthetic" and source.get("synthetic") is True:
        raise ValueError("真实或公开材料不能同时声明 synthetic=true")
    if kind == "synthetic" and source.get("synthetic") is False:
        raise ValueError("synthetic 材料的来源声明相互矛盾")
    source["type"], source["synthetic"] = kind, kind == "synthetic"
    has_media, media_hashes = _media_values(case)
    if has_media and not media_hashes:
        raise ValueError("媒体案例需保留 source.media_sha256 或媒体清单中的 SHA-256；导入不会自动读取文件")
    if kind == "licensed_public":
        source["url"] = _url(source.get("url") or source.get("source_url") or source.get("original_url"), "公开材料来源 URL")
        license_value = source.get("license")
        if isinstance(license_value, dict):
            _text(license_value.get("name") or license_value.get("id"), "公开材料许可", 300)
            if license_value.get("url"):
                _url(license_value["url"], "许可 URL")
        else:
            _text(license_value, "公开材料许可", 1000)
        attribution = source.get("attribution")
        if isinstance(attribution, dict):
            if not any(isinstance(attribution.get(k), str) and attribution[k].strip() for k in ("author", "name", "credit")):
                raise ValueError("公开材料 attribution 需包含作者、名称或署名说明")
        else:
            _text(attribution, "公开材料署名 attribution", 3000)
    elif kind == "authorized_private":
        grant = authorization if authorization is not None else source.get("authorization")
        if not isinstance(grant, dict):
            raise ValueError("私有材料需提供有证据和范围的 authorization，不能仅勾选已授权")
        _text(grant.get("granted_by"), "授权主体 granted_by", 300)
        _date(grant.get("granted_at"), "授权时间 granted_at")
        evidence = grant.get("evidence") or grant.get("evidence_ref")
        if isinstance(evidence, dict):
            _text(evidence.get("reference") or evidence.get("uri"), "授权证据引用", 5000)
        else:
            _text(evidence, "授权证据引用 evidence", 5000)
        scope = grant.get("scope")
        if not isinstance(scope, dict):
            raise ValueError("私有材料授权需明确 scope")
        projects = scope.get("projects", [scope.get("project")])
        if not isinstance(projects, list) or project is None or project not in projects or "*" in projects:
            raise ValueError("authorization.scope 必须明确覆盖本项目")
        case_ids, source_ids = scope.get("case_ids", []), scope.get("source_ids", [])
        for values in (case_ids, source_ids):
            if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() or v == "*" for v in values):
                raise ValueError("授权范围需列出具体 case_ids 或 source_ids，不能使用通配符")
        if case.get("id") not in case_ids and source.get("id") not in source_ids:
            raise ValueError("authorization.scope 未覆盖该案例或来源 ID")
        operations = scope.get("operations")
        required = {"evaluate", "evaluation", "import"} if operation == "import" else {"evaluate", "evaluation"}
        if not isinstance(operations, list) or not required.intersection(operations):
            action = "导入" if operation == "import" else "执行评测"
            raise ValueError(f"authorization.scope.operations 未明确允许{action}；import 授权不能替代模型评测授权")
        if grant.get("revoked") is True:
            raise ValueError("材料授权已撤销")
        if grant.get("expires_at") and _date(grant["expires_at"], "授权到期时间 expires_at") <= datetime.now(timezone.utc):
            raise ValueError("材料授权已到期")
        source["authorization_hash"] = _hash(grant)
    return source


def _bare_config(value):
    return {k: v for k, v in value.items() if k != "provenance"}


def _registered_configuration_set(body, project):
    # Lazy import keeps catalog free to reuse validate_source during freeze.
    from .catalog import packs
    available = [p for p in packs() if p["project"] == project]
    taskpack_id = body.get("taskpack_id")
    if taskpack_id is not None:
        available = [p for p in available if p["id"] == taskpack_id]
    if not available:
        raise ValueError("数据集项目或 taskpack_id 没有已注册的任务包")
    requested = body.get("configuration_ids", body.get("configurations"))
    if requested is not None:
        if not isinstance(requested, list) or not requested or len(requested) > 64:
            raise ValueError("数据集配置需为 1–64 个已注册配置的 ID 列表")
        ids = [v.get("id") if isinstance(v, dict) else v for v in requested]
        if any(not isinstance(v, str) or not v for v in ids) or len(set(ids)) != len(ids):
            raise ValueError("数据集配置 ID 不得缺失或重复")
        available = [p for p in available if set(ids) <= {c["id"] for c in p["configurations"]}]
    else:
        ids = None
    if len(available) != 1:
        raise ValueError("请指定唯一 taskpack_id，且配置须全部来自该已注册任务包")
    pack = available[0]
    registered = {c["id"]: c for c in pack["configurations"]}
    if requested is not None:
        for submitted in requested:
            if isinstance(submitted, dict) and set(submitted) != {"id"}:
                if _hash(_bare_config(submitted)) != _hash(_bare_config(registered[submitted["id"]])):
                    raise ValueError("数据集不能覆盖已注册配置参数；请先注册新的产品配置")
    submitted_configs = body.get("configurations")
    if submitted_configs is not None:
        if not isinstance(submitted_configs, list):
            raise ValueError("configurations 必须为已注册配置列表")
        supplied_ids = [v.get("id") if isinstance(v, dict) else v for v in submitted_configs]
        if supplied_ids != ids:
            raise ValueError("configuration_ids 与 configurations 的 ID 和顺序必须一致")
        for submitted in submitted_configs:
            if isinstance(submitted, dict) and set(submitted) != {"id"}:
                if _hash(_bare_config(submitted)) != _hash(_bare_config(registered[submitted["id"]])):
                    raise ValueError("冻结的数据集配置已与本机注册配置不同；请注册原配置或导入新的数据集版本")
    configs = [copy.deepcopy(registered[k]) for k in (ids or registered)]
    return pack, configs


def _disclosed_family(used_by, family_id):
    if not isinstance(used_by, list):
        return False
    for record in used_by:
        if not isinstance(record, dict):
            continue
        families = record.get("family_ids", [record.get("family_id")])
        if isinstance(families, list) and family_id in families and any(
            isinstance(record.get(k), str) and record[k].strip() for k in ("experiment_id", "reference", "reason")
        ):
            return True
    return False


class DatasetRegistry:
    def __init__(self, store):
        self.store = store
        with store.lock:
            store.db.executescript("""
                CREATE TABLE IF NOT EXISTS datasets(
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, created_at TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS dataset_versions(
                    id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES datasets(id),
                    version TEXT NOT NULL, created_at TEXT NOT NULL, content_hash TEXT NOT NULL, body TEXT NOT NULL, summary TEXT NOT NULL,
                    UNIQUE(dataset_id, version));
                CREATE TABLE IF NOT EXISTS dataset_family_splits(
                    dataset_id TEXT NOT NULL REFERENCES datasets(id), family_id TEXT NOT NULL, split TEXT NOT NULL,
                    first_version_id TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(dataset_id,family_id));
                CREATE INDEX IF NOT EXISTS dataset_versions_lineage ON dataset_versions(dataset_id,created_at);
            """)
            columns = {row[1] for row in store.db.execute("PRAGMA table_info(dataset_versions)")}
            if "summary" not in columns:
                store.db.execute("ALTER TABLE dataset_versions ADD COLUMN summary TEXT NOT NULL DEFAULT '{}'")
                for row in store.db.execute("SELECT id,body FROM dataset_versions").fetchall():
                    store.db.execute("UPDATE dataset_versions SET summary=? WHERE id=?",
                                     (dumps(self._summary(json.loads(row[1]))), row[0]))
            store.db.commit()

    @staticmethod
    def _summary(version):
        return {k: version[k] for k in (
            "id", "version_id", "dataset_id", "version", "name", "project", "created_at", "content_hash",
            "case_count", "family_count", "split_counts", "source_types", "taskpack_id", "configuration_ids"
        )}

    def list_datasets(self):
        with self.store.lock:
            ids = [r[0] for r in self.store.db.execute("SELECT id FROM datasets ORDER BY created_at DESC,id")]
            return [self.get_dataset(identifier) for identifier in ids]

    def get_dataset(self, identifier):
        with self.store.lock:
            row = self.store.db.execute("SELECT body FROM datasets WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise KeyError("数据集不存在")
            dataset = json.loads(row[0])
            versions = [json.loads(r[0]) for r in self.store.db.execute(
                "SELECT summary FROM dataset_versions WHERE dataset_id=? ORDER BY rowid DESC", (identifier,))]
        latest = versions[0] if versions else None
        return {**dataset, "name": latest["name"] if latest else dataset["name"],
                "versions": versions, "version_count": len(versions), "latest_version": latest,
                "latest_version_id": latest["id"] if latest else None,
                **{k: latest[k] for k in ("case_count", "family_count", "split_counts", "source_types") if latest}}

    def get_version(self, identifier, version_id=None):
        with self.store.lock:
            if version_id is None or version_id == "latest":
                row = self.store.db.execute(
                    "SELECT body FROM dataset_versions WHERE dataset_id=? ORDER BY rowid DESC LIMIT 1", (identifier,)).fetchone()
            else:
                row = self.store.db.execute(
                    "SELECT body FROM dataset_versions WHERE dataset_id=? AND id=?", (identifier, version_id)).fetchone()
                if row is None:
                    row = self.store.db.execute(
                        "SELECT body FROM dataset_versions WHERE dataset_id=? AND version=?", (identifier, version_id)).fetchone()
            if row is None:
                raise KeyError("数据集版本不存在")
            return json.loads(row[0])

    def import_version(self, body):
        if not isinstance(body, dict):
            raise ValueError("数据集导入需为 JSON 对象")
        identifier = _identifier(body.get("dataset_id", body.get("id")), "数据集 ID")
        project = _identifier(body.get("project"), "项目 ID")
        name = _text(body.get("name", identifier), "数据集名称", 200)
        version_label = _text(body.get("version"), "数据集版本", 100)
        if version_label == "latest":
            raise ValueError("latest 为查询别名，不能作为版本标签")
        pack, configs = _registered_configuration_set(body, project)
        submitted = body.get("cases")
        if not isinstance(submitted, list) or not 1 <= len(submitted) <= 10000:
            raise ValueError("数据集需包含 1–10000 个案例")
        cases, ids, family_splits = [], set(), {}
        authorization = copy.deepcopy(body.get("authorization"))
        for raw in submitted:
            if not isinstance(raw, dict):
                raise ValueError("每个案例需为 JSON 对象")
            case = copy.deepcopy(raw)
            case_id = _identifier(case.get("id"), "案例 ID")
            if case_id in ids:
                raise ValueError("同一数据集版本内案例 ID 不得重复")
            ids.add(case_id)
            case["name"] = _text(case.get("name", case_id), "案例名称", 300)
            family = _identifier(case.get("family_id", case_id), "情境家族 ID")
            case["family_id"] = family
            split_label = case.get("split", "development")
            if split_label not in _SPLITS:
                raise ValueError("案例 split 需为 development 或 holdout（兼容 smoke/regression/new_holdout）")
            split = _SPLITS[split_label]
            if split_label != split:
                case["split_label"] = split_label
            case["split"] = split
            if family in family_splits and family_splits[family] != split:
                raise ValueError("同一情境家族不能同时进入开发和留出划分")
            family_splits[family] = split
            if "source" not in case and body.get("source") is not None:
                case["source"] = copy.deepcopy(body["source"])
            case["source"] = validate_source(case, authorization, project, operation="import")
            cases.append(case)
        rubric = copy.deepcopy(body.get("rubric", pack.get("rubric", [])))
        if not isinstance(rubric, (list, dict)):
            raise ValueError("rubric 需为 JSON 列表或对象")
        used_by = copy.deepcopy(body.get("used_by", []))
        if not isinstance(used_by, list):
            raise ValueError("used_by 需为已使用/已暴露记录列表")
        content = {"schema_version": "myeval.dataset.v2", "dataset_id": identifier, "project": project,
                   "name": name, "version": version_label, "taskpack_id": pack["id"],
                   "taskpack_content_hash": pack.get("content_hash"), "cases": cases, "configurations": configs,
                   "configuration_ids": [c["id"] for c in configs], "rubric": rubric,
                   "authorization": authorization, "source": copy.deepcopy(body.get("source")),
                   "notes": copy.deepcopy(body.get("notes", "")), "used_by": used_by,
                   "metadata": copy.deepcopy(body.get("metadata", {}))}
        content_hash = _hash(content)
        created_at, version_id = now(), uid()
        version = {**content, "id": version_id, "version_id": version_id, "created_at": created_at,
                   "content_hash": content_hash, "case_count": len(cases), "family_count": len(family_splits),
                   "split_counts": dict(Counter(c["split"] for c in cases)),
                   "source_types": dict(Counter(c["source"]["type"] for c in cases))}
        with self.store.lock, self.store.db:
            existing = self.store.db.execute(
                "SELECT content_hash,body FROM dataset_versions WHERE dataset_id=? AND version=?",
                (identifier, version_label)).fetchone()
            if existing:
                if existing[0] != content_hash:
                    raise ValueError("此版本已冻结，内容不能覆盖；请使用新的版本标签")
                return {**json.loads(existing[1]), "import_status": "unchanged"}
            dataset_row = self.store.db.execute("SELECT project FROM datasets WHERE id=?", (identifier,)).fetchone()
            if dataset_row and dataset_row[0] != project:
                raise ValueError("同一数据集不能更换项目；请使用新的数据集 ID")
            if not dataset_row:
                dataset = {"id": identifier, "name": name, "project": project, "created_at": created_at}
                self.store.db.execute("INSERT INTO datasets VALUES(?,?,?,?)", (identifier, project, created_at, dumps(dataset)))
            for family, split in family_splits.items():
                old = self.store.db.execute(
                    "SELECT split,body FROM dataset_family_splits WHERE dataset_id=? AND family_id=?", (identifier, family)).fetchone()
                disclosed = _disclosed_family(used_by, family)
                if old and old[0] != split:
                    if old[0] == "development":
                        raise ValueError(f"家族 {family} 已用于开发，不能重新声明为独立留出")
                    if not disclosed:
                        raise ValueError(f"家族 {family} 从留出回流开发需 used_by 明确记录已使用或暴露的证据")
                    history = json.loads(old[1])
                    history["exposed"] = True
                    history.setdefault("exposure_version_ids", []).append(version_id)
                    history.setdefault("transitions", []).append({"version_id": version_id, "from": old[0], "to": split, "at": created_at})
                    self.store.db.execute(
                        "UPDATE dataset_family_splits SET split=?,body=? WHERE dataset_id=? AND family_id=?",
                        (split, dumps(history), identifier, family))
                elif old and disclosed:
                    history = json.loads(old[1])
                    history["exposed"] = True
                    history.setdefault("exposure_version_ids", []).append(version_id)
                    self.store.db.execute("UPDATE dataset_family_splits SET body=? WHERE dataset_id=? AND family_id=?",
                                          (dumps(history), identifier, family))
                elif not old:
                    history = {"created_at": created_at, "transitions": [], "exposed": disclosed,
                               "exposure_version_ids": [version_id] if disclosed else []}
                    self.store.db.execute("INSERT INTO dataset_family_splits VALUES(?,?,?,?,?)",
                                          (identifier, family, split, version_id, dumps(history)))
            self.store.db.execute("INSERT INTO dataset_versions(id,dataset_id,version,created_at,content_hash,body,summary) VALUES(?,?,?,?,?,?,?)",
                                  (version_id, identifier, version_label, created_at, content_hash, dumps(version), dumps(self._summary(version))))
        return {**version, "import_status": "created"}

    def resolve_pack(self, identifier, version_id=None):
        version = self.get_version(identifier, version_id)
        # Require the imported definitions to remain registered; no silent upgrade.
        _registered_configuration_set(version, version["project"])
        for case in version["cases"]:
            validate_source(case, version.get("authorization"), version["project"])
        with self.store.lock:
            lineage = {r[0]: {"current_split": r[1], **json.loads(r[2])} for r in self.store.db.execute(
                "SELECT family_id,split,body FROM dataset_family_splits WHERE dataset_id=?", (identifier,))}
        exposed = sorted({c["family_id"] for c in version["cases"]
                          if c["split"] == "holdout" and (
                              lineage.get(c["family_id"], {}).get("current_split") == "development"
                              or lineage.get(c["family_id"], {}).get("exposed"))})
        dataset = {**self._summary(version), "exposed_holdout_families": exposed,
                   "holdout_independence": "previously_exposed" if exposed else "declared_unexposed"}
        return {"id": f"dataset:{identifier}:{version['version_id']}", "name": version["name"],
                "project": version["project"], "version": version["version"], "content_hash": version["content_hash"],
                "cases": version["cases"], "configurations": version["configurations"], "rubric": version["rubric"],
                "authorization": version.get("authorization"),
                "dataset": dataset, "source_taskpack_id": version["taskpack_id"],
                "used_by": version.get("used_by", []), "source": version.get("source")}
