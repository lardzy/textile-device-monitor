"""Compile editable portable graphs through the existing release validator."""

from copy import deepcopy

from app.execution.errors import ExecutionApiError
from app.execution.release_v2 import (
    _content_semantic_issues, _rebuild_candidate_dependencies, _resolve_dependencies,
    _validate_document_shape,
)
from app.execution.v2.canonical import canonical_sha256
from app.execution.v2.registry import get_installed_registry, resolve_connector_reference


def seal(document):
    document.pop("integrity", None)
    document["integrity"] = {"algorithm": "sha256", "canonicalization": "RFC8785",
                             "scope": "document_without_integrity", "digest": canonical_sha256(document), "signatures": []}
    return document


def compile_document(document):
    """Resolve current native contracts, derive capabilities, and return reviewable JSON.

    This neither publishes nor changes deployment identities. Imported signed releases
    stay immutable; an edit is a new unsigned candidate with a new digest.
    """
    candidate = deepcopy(document)
    issues = _validate_document_shape(seal(candidate))
    if issues:
        return {"document": candidate, "content_valid": False, "issues": issues}
    from app.execution.v2.domain_profiles import expand_domain_profiles

    expand_domain_profiles(candidate)
    registry = get_installed_registry()
    connectors = {}
    try:
        for node in candidate["definition"]["nodes"]:
            if node["type"] in {"core.start", "core.end"} and node["type_version"] == 1:
                node["type_version"] = 2
            installed = registry.resolve_node_spec(node["type"], node["type_version"])
            if installed.source != "resource":
                raise ValueError(f"{node['type']}@{node['type_version']} 是历史兼容节点，请先迁移")
            node["__native_p2"] = True
            ref_key = {"connector.query": "query_ref", "external.operation": "operation_ref"}.get(node["type"])
            if not ref_key:
                continue
            reference = node.get("config", {}).get(ref_key, "")
            connector_id, name, version = resolve_connector_reference(reference, [c.connector_id for c in registry.connectors.all()])
            connector = registry.resolve_connector(connector_id, "*")
            item = connectors.setdefault(connector_id, {"connector_id": connector_id, "version_range": connector.version,
                "distribution_digest": connector.distribution_digest, "operations": [], "queries": []})
            kind = "query" if ref_key == "query_ref" else "operation"
            contract = getattr(registry.connectors, f"resolve_{kind}")(connector_id, connector.version, name, int(version))
            dependency = {kind: name, "contract_version": int(version), "contract_digest": contract.contract_digest}
            group = "queries" if kind == "query" else "operations"
            if dependency not in item[group]:
                item[group].append(dependency)
        candidate["dependencies"]["packs"] = []
        _rebuild_candidate_dependencies(candidate)
        candidate["dependencies"]["engine"] = {"version_range": ">=2.5.0 <3.0.0"}
        candidate["dependencies"]["connectors"] = sorted(connectors.values(), key=lambda value: value["connector_id"])
        for connector_id in sorted(connectors):
            connector = registry.resolve_connector(connector_id, "*")
            packs = candidate["dependencies"]["packs"]
            if not any(p["pack_id"] == connector.pack_id for p in packs):
                packs.append({"pack_id": connector.pack_id, "version_range": connector.pack_version,
                              "distribution_digest": connector.distribution_digest, "required_on": ["api", "worker", "bridge"]})
        candidate["dependencies"]["packs"].sort(key=lambda value: value["pack_id"])
    except (LookupError, ValueError, TypeError) as exc:
        raise ExecutionApiError(422, "designer_contract_unavailable", str(exc)) from exc
    _lock, computed = _resolve_dependencies(candidate, [])
    candidate["capabilities"] = computed
    seal(candidate)
    issues = _validate_document_shape(candidate)
    if not issues:
        _resolve_dependencies(candidate, issues)
        issues.extend(_content_semantic_issues(candidate))
    return {"document": candidate, "content_valid": not any(i["level"] == "error" for i in issues), "issues": issues}


def starter_document():
    from app.execution.v2.examples import build_readonly_file_query_smoke_release

    document = build_readonly_file_query_smoke_release()
    document["release"].update(slug="new-workflow-v2", name="新工作流", description="", release_note="")
    document["resources"]["root_slots"] = []
    document["definition"]["nodes"] = [n for n in document["definition"]["nodes"] if n["type"] in {"core.start", "core.end"}]
    document["definition"]["nodes"][-1]["input_mapping"] = {}
    document["definition"]["output_schema"] = {"type": "object", "properties": {}, "additionalProperties": False}
    document["definition"]["edges"] = [{"id": "start-end", "source": "start", "target": "end", "join_policy": "all"}]
    return compile_document(document)["document"]
