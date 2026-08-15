"""编号 → 项目类型匹配规则（管理员可编辑）的规则表访问与求值。

规则表只承载"发现期事实"：目录匹配策略、任务单事实条件（项目名别名、
测试方法）与结果探针（单元格/工作表读取）。模板绑定、写门禁与登记参数
继续钉在代码里（见 ``microscopy_families.py`` 等），规则表通过
``binding.family_key`` 引用它们——配置提供数据，代码强制校验。

规则由 ``project_rule_seeds`` 幂等播种（代码常量是唯一事实源）；管理员
在流程管理中实时编辑，``revision`` 自增即让依赖缓存（如纸类 W32 预读
profile）自动失效，无需清空任何表。

为后续演进预留的形状：
- 节点 config 以 ``match_rule: <rule_key>`` 引用规则（类型化参数）；
- ``evaluate_project_rule`` 是通用匹配节点 ``core.project_match`` 与
  规则干跑测试端点共用的求值入口；
- 探针是命名输出，供下游节点 input_mapping 引用。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.execution.errors import ExecutionApiError
from app.execution.models import ExecutionProjectRule


# 目录匹配策略：实现分散在各匹配器模块中（本步不重写查询逻辑）。
FOLDER_STRATEGIES = frozenset(
    {
        "level1_contains_number",
        "filename_contains_number",
        "electron_image_folders",
    }
)
ENTRY_KINDS = frozenset({"workbook", "image"})
PROBE_TYPES = frozenset({"cell_value", "worksheet_exists"})
PROBE_PARSERS = frozenset({"text", "paper_qualitative_v1"})
FACT_OPS = frozenset({"eq", "in"})
# 任务单事实条件键：full_match 判定与前端展示都认识这两个键。
TASK_CONDITION_KEYS = frozenset({"task_item_name", "test_method"})
SNAPSHOT_PROJECT_FACTS = frozenset({"check_item_name", "check_method"})

PAPER_FIBER_RULE_KEY = "paper_gbt4688_qualitative"
MICROSCOPY_RULE_KEY_PREFIX = "microscopy_gbt36422_"
REGENERATED_RULE_KEYS = {
    "file.regenerated_fiber_count_method": "regenerated_fiber_count_method",
    "file.regenerated_fiber_area_method": "regenerated_fiber_area_method",
}


def normalized_fact(value: object) -> str:
    """NFKC + 空白折叠归一化，与既有匹配语义一致。"""

    return " ".join(
        unicodedata.normalize("NFKC", str(value or "")).strip().split()
    )


@dataclass(frozen=True, slots=True)
class TaskFact:
    condition_key: str
    fact: str
    op: str
    values: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RuleProbe:
    name: str
    type: str
    sheet: str
    cell: Optional[str] = None
    parser: str = "text"


@dataclass(frozen=True, slots=True)
class ResolvedRule:
    key: str
    display_name: str
    category_key: Optional[str]
    enabled: bool
    revision: int
    default_node_type: str
    record_family: Optional[str]
    source_root_id: str
    folder_strategy: str
    entry_kind: str
    max_depth: Optional[int]
    task_facts: tuple[TaskFact, ...] = ()
    probes: tuple[RuleProbe, ...] = ()
    binding: dict[str, Any] = field(default_factory=dict)

    def probe(self, name: str) -> Optional[RuleProbe]:
        for item in self.probes:
            if item.name == name:
                return item
        return None

    def fact_values(self, condition_key: str) -> tuple[str, ...]:
        for item in self.task_facts:
            if item.condition_key == condition_key:
                return item.values
        return ()


def parse_task_fact(raw: object) -> TaskFact:
    data = raw if isinstance(raw, dict) else {}
    values = data.get("values")
    if values is None:
        value = data.get("value")
        values = [] if value is None else [value]
    return TaskFact(
        condition_key=str(data.get("condition_key") or "").strip(),
        fact=str(data.get("fact") or "").strip(),
        op=str(data.get("op") or "eq").strip(),
        values=tuple(str(item) for item in values if item is not None),
    )


def _parse_probe(raw: object) -> RuleProbe:
    data = raw if isinstance(raw, dict) else {}
    cell = data.get("cell")
    return RuleProbe(
        name=str(data.get("name") or "").strip(),
        type=str(data.get("type") or "cell_value").strip(),
        sheet=str(data.get("sheet") or "").strip(),
        cell=str(cell).strip().upper() if cell not in (None, "") else None,
        parser=str(data.get("parser") or "text").strip(),
    )


def row_to_rule(row: ExecutionProjectRule) -> ResolvedRule:
    config = dict(row.config or {})
    source = dict(config.get("source") or {})
    folder = dict(source.get("folder_match") or {})
    binding = config.get("binding")
    max_depth = folder.get("max_depth")
    record_family = config.get("record_family")
    return ResolvedRule(
        key=str(row.rule_key),
        display_name=str(row.display_name),
        category_key=row.category_key,
        enabled=bool(row.enabled),
        revision=int(row.revision or 1),
        default_node_type=str(config.get("default_node_type") or ""),
        record_family=(
            str(record_family).strip() if record_family not in (None, "") else None
        ),
        source_root_id=str(source.get("root_id") or "").strip(),
        folder_strategy=str(folder.get("strategy") or "").strip(),
        entry_kind=str(folder.get("entry_kind") or "workbook").strip(),
        max_depth=int(max_depth) if max_depth not in (None, "") else None,
        task_facts=tuple(
            parse_task_fact(item) for item in config.get("task_facts") or []
        ),
        probes=tuple(
            _parse_probe(item) for item in config.get("probes") or []
        ),
        binding=dict(binding) if isinstance(binding, dict) else {},
    )


def validate_rule_config(config: object) -> list[str]:
    """Return human-readable config errors; empty list means valid."""

    issues: list[str] = []
    if not isinstance(config, dict):
        return ["规则配置必须是对象"]
    source = config.get("source")
    if not isinstance(source, dict):
        issues.append("source 必须是对象")
        source = {}
    root_id = str(source.get("root_id") or "").strip()
    if not root_id:
        issues.append("source.root_id 不能为空")
    folder = source.get("folder_match")
    if not isinstance(folder, dict):
        issues.append("source.folder_match 必须是对象")
        folder = {}
    strategy = str(folder.get("strategy") or "").strip()
    if strategy not in FOLDER_STRATEGIES:
        issues.append(
            "source.folder_match.strategy 必须是 "
            + "/".join(sorted(FOLDER_STRATEGIES))
        )
    entry_kind = str(folder.get("entry_kind") or "workbook").strip()
    if entry_kind not in ENTRY_KINDS:
        issues.append("source.folder_match.entry_kind 必须是 workbook/image")
    if (
        strategy == "electron_image_folders"
        and entry_kind != "image"
    ):
        issues.append("electron_image_folders 策略的 entry_kind 必须是 image")
    if (
        strategy != "electron_image_folders"
        and entry_kind == "image"
    ):
        issues.append("workbook 类策略的 entry_kind 不能是 image")
    max_depth = folder.get("max_depth")
    if max_depth is not None:
        if not isinstance(max_depth, int) or max_depth < 1 or max_depth > 6:
            issues.append("source.folder_match.max_depth 必须是 1-6 的整数")

    task_facts = config.get("task_facts") or []
    if not isinstance(task_facts, list):
        issues.append("task_facts 必须是数组")
        task_facts = []
    seen_conditions: set[str] = set()
    for index, raw in enumerate(task_facts):
        fact = parse_task_fact(raw)
        label = f"task_facts[{index}]"
        if fact.condition_key not in TASK_CONDITION_KEYS:
            issues.append(
                f"{label}.condition_key 必须是 "
                + "/".join(sorted(TASK_CONDITION_KEYS))
            )
        elif fact.condition_key in seen_conditions:
            issues.append(f"{label}.condition_key 重复：{fact.condition_key}")
        seen_conditions.add(fact.condition_key)
        if fact.fact not in SNAPSHOT_PROJECT_FACTS:
            issues.append(
                f"{label}.fact 必须是 "
                + "/".join(sorted(SNAPSHOT_PROJECT_FACTS))
            )
        if fact.op not in FACT_OPS:
            issues.append(f"{label}.op 必须是 eq/in")
        if not fact.values or any(not item.strip() for item in fact.values):
            issues.append(f"{label}.values 不能为空")

    probes = config.get("probes") or []
    if not isinstance(probes, list):
        issues.append("probes 必须是数组")
        probes = []
    seen_probes: set[str] = set()
    for index, raw in enumerate(probes):
        probe = _parse_probe(raw)
        label = f"probes[{index}]"
        if not probe.name:
            issues.append(f"{label}.name 不能为空")
        elif probe.name in seen_probes:
            issues.append(f"{label}.name 重复：{probe.name}")
        seen_probes.add(probe.name)
        if probe.type not in PROBE_TYPES:
            issues.append(f"{label}.type 必须是 cell_value/worksheet_exists")
        if not probe.sheet:
            issues.append(f"{label}.sheet 不能为空")
        if probe.type == "cell_value" and not probe.cell:
            issues.append(f"{label}.cell 不能为空（cell_value 探针）")
        if probe.parser not in PROBE_PARSERS:
            issues.append(
                f"{label}.parser 必须是 " + "/".join(sorted(PROBE_PARSERS))
            )

    binding = config.get("binding")
    if binding is not None:
        if not isinstance(binding, dict):
            issues.append("binding 必须是对象")
        else:
            from app.execution.microscopy_families import (
                microscopy_family_for_key,
            )

            family_key = str(binding.get("family_key") or "").strip()
            if microscopy_family_for_key(family_key) is None:
                issues.append("binding.family_key 不是已知的电镜记录家族")
            for key in ("check_item_no", "check_item_name"):
                if not str(binding.get(key) or "").strip():
                    issues.append(f"binding.{key} 不能为空")
    return issues


def ensure_default_project_rules(db: Session) -> bool:
    """Idempotently insert missing seed rules; never overwrite edits."""

    from app.execution.project_rule_seeds import default_project_rule_seeds

    seeds = default_project_rule_seeds()
    existing = {
        row.rule_key
        for row in db.query(ExecutionProjectRule.rule_key).filter(
            ExecutionProjectRule.rule_key.in_([item["rule_key"] for item in seeds])
        )
    }
    changed = False
    for seed in seeds:
        if seed["rule_key"] in existing:
            continue
        db.add(
            ExecutionProjectRule(
                rule_key=seed["rule_key"],
                display_name=seed["display_name"],
                category_key=seed.get("category_key"),
                enabled=True,
                revision=1,
                config=seed["config"],
            )
        )
        changed = True
    if changed:
        db.flush()
    return changed


def resolve_rule(
    db: Session,
    rule_key: object,
    *,
    ensure: bool = True,
) -> ResolvedRule:
    key = str(rule_key or "").strip()
    if not key:
        raise ExecutionApiError(
            422,
            "project_rule_key_missing",
            "节点缺少匹配规则引用（match_rule）",
        )
    if ensure:
        ensure_default_project_rules(db)
    row = (
        db.query(ExecutionProjectRule)
        .filter(ExecutionProjectRule.rule_key == key)
        .one_or_none()
    )
    if row is None:
        raise ExecutionApiError(
            422,
            "project_rule_unknown",
            f"未知的项目匹配规则：{key}",
            details={"rule_key": key},
        )
    return row_to_rule(row)


def require_enabled_rule(rule: ResolvedRule) -> ResolvedRule:
    if not rule.enabled:
        raise ExecutionApiError(
            422,
            "project_rule_disabled",
            f"项目匹配规则已停用：{rule.display_name}",
            details={"rule_key": rule.key},
        )
    return rule


def list_rules(
    db: Session,
    *,
    enabled_only: bool = False,
    ensure: bool = True,
) -> list[ResolvedRule]:
    if ensure:
        ensure_default_project_rules(db)
    query = db.query(ExecutionProjectRule)
    if enabled_only:
        query = query.filter(ExecutionProjectRule.enabled.is_(True))
    rows = query.order_by(
        ExecutionProjectRule.category_key.asc().nulls_last(),
        ExecutionProjectRule.rule_key.asc(),
    ).all()
    return [row_to_rule(row) for row in rows]


def microscopy_rule_key(family_key: object) -> str:
    family = str(family_key or "").strip() or "microscopy"
    return f"{MICROSCOPY_RULE_KEY_PREFIX}{family}"


def default_rule_key_for_node_type(
    node_type: object,
    record_family: object = None,
) -> Optional[str]:
    """Map a legacy specialized node type to its seeded rule key."""

    node_type_text = str(node_type or "").strip()
    if node_type_text == "file.paper_fiber_gbt4688_qualitative":
        return PAPER_FIBER_RULE_KEY
    if node_type_text == "file.electron_microscopy_gbt36422":
        return microscopy_rule_key(record_family)
    if node_type_text in REGENERATED_RULE_KEYS:
        return REGENERATED_RULE_KEYS[node_type_text]
    return None


def _task_fact_matches(project: dict[str, Any], fact: TaskFact) -> bool:
    candidate = normalized_fact(project.get(fact.fact))
    if not candidate:
        return False
    if fact.op == "eq":
        return any(
            candidate == normalized_fact(value) for value in fact.values
        )
    return any(candidate == normalized_fact(value) for value in fact.values)


def evaluate_task_facts(
    snapshot: Optional[dict[str, Any]],
    task_facts: tuple[TaskFact, ...] | list[TaskFact],
) -> tuple[list[str], Optional[dict[str, Any]]]:
    """Best condition set across snapshot projects (legacy semantics).

    Returns ``(condition_keys, matched_project)``; iteration stops at the
    first project matching every configured fact, mirroring the previous
    hard-coded matchers.
    """

    best_conditions: list[str] = []
    best_project: Optional[dict[str, Any]] = None
    facts = list(task_facts)
    if not facts:
        return best_conditions, best_project
    for project in (snapshot or {}).get("projects") or []:
        if not isinstance(project, dict):
            continue
        conditions = [
            fact.condition_key
            for fact in facts
            if _task_fact_matches(project, fact)
        ]
        if len(conditions) > len(best_conditions):
            best_conditions = conditions
            best_project = dict(project)
        if len(best_conditions) == len(facts):
            break
    return best_conditions, best_project


def rule_for_facts(
    db: Session,
    *,
    check_item_no: object,
    check_item_name: object,
) -> Optional[ResolvedRule]:
    """Fail-closed reverse lookup used by the external write gates.

    Matches enabled rules by their code-linked ``binding`` facts (canonical
    item number + item name), so an administrator's match-time alias edits
    govern the write path without weakening template validation.
    """

    no = normalized_fact(check_item_no)
    name = normalized_fact(check_item_name)
    if not no or not name:
        return None
    for rule in list_rules(db, enabled_only=True):
        binding = rule.binding
        if not binding:
            continue
        if (
            normalized_fact(binding.get("check_item_no")) == no
            and normalized_fact(binding.get("check_item_name")) == name
        ):
            return rule
    return None


def evaluate_project_rule(
    db: Session,
    *,
    rule: ResolvedRule,
    inspection_number: str,
    gateway=None,
    result_limit: int = 6,
) -> dict[str, Any]:
    """Generic matcher entry shared by ``core.project_match`` and dry-run.

    Dispatches to the strategy's established matcher implementation with the
    rule pinned, so query behaviour stays byte-compatible while every fact
    comes from the rule table.
    """

    require_enabled_rule(rule)
    node_type = rule.default_node_type
    if node_type == "file.paper_fiber_gbt4688_qualitative":
        from app.execution.paper_fiber import paper_fiber_match

        return paper_fiber_match(
            db,
            inspection_number=inspection_number,
            gateway=gateway,
            result_limit=result_limit,
            rule=rule,
        )
    if node_type == "file.electron_microscopy_gbt36422":
        from app.execution.electron_microscopy import electron_microscopy_match
        from app.execution.microscopy_families import microscopy_family_for_key

        return electron_microscopy_match(
            db,
            inspection_number=inspection_number,
            family=microscopy_family_for_key(rule.record_family),
            rule=rule,
        )
    if node_type in REGENERATED_RULE_KEYS:
        from app.execution.regenerated_fiber import (
            match_regenerated_fiber_workbooks,
        )

        return match_regenerated_fiber_workbooks(
            db,
            node_type=node_type,
            inspection_number=inspection_number,
            gateway=gateway,
            result_limit=result_limit,
            project_rule=rule,
        )
    raise ExecutionApiError(
        422,
        "project_rule_strategy_unknown",
        "匹配规则没有可用的求值策略",
        details={"rule_key": rule.key, "default_node_type": node_type},
    )


def _project_match_executor(context) -> dict[str, Any]:
    """通用"编号项目匹配"节点：规则键来自节点 config（match_rule）。"""

    config = context.node.get("config") or {}
    rule = resolve_rule(context.db, config.get("match_rule"))
    inspection_number = str(
        context.input_data.get("inspection_number")
        or context.run.inspection_number
    ).strip()
    result = evaluate_project_rule(
        context.db,
        rule=rule,
        inspection_number=inspection_number,
        result_limit=min(int(config.get("limit", 6)), 6),
    )
    result["rule_key"] = rule.key
    result["rule_revision"] = rule.revision
    return result


def register_project_rule_executors() -> None:
    from app.execution.registry import node_registry

    node_registry.set_executor(
        "core.project_match", 1, _project_match_executor
    )
