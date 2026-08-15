"""默认项目匹配规则种子。

代码常量是唯一种子事实源；``ensure_default_project_rules`` 只补缺、
不覆盖管理员修改。本模块的构建函数使用函数级导入以避免与匹配器模块
（其自身依赖 ``project_rules``）产生循环导入。
"""

from __future__ import annotations

from typing import Any


def default_project_rule_seeds() -> list[dict[str, Any]]:
    from app.execution.electron_microscopy import (
        ELECTRON_NODE_TYPE,
        ELECTRON_ROOT_ID,
    )
    from app.execution.microscopy_families import MICROSCOPY_RECORD_FAMILIES
    from app.execution.paper_fiber import (
        PAPER_FIBER_NODE_TYPE,
        PAPER_FIBER_PROJECT_NAME,
        PAPER_FIBER_RESULT_CELL,
        PAPER_FIBER_ROOT_ID,
        PAPER_FIBER_STANDARD_SOURCE_CELL,
        PAPER_FIBER_TEST_METHOD,
        PAPER_FIBER_WORKSHEET,
    )
    from app.execution.project_rules import (
        MICROSCOPY_RULE_KEY_PREFIX,
        PAPER_FIBER_RULE_KEY,
        REGENERATED_RULE_KEYS,
    )
    from app.execution.regenerated_fiber import REGENERATED_FIBER_RULES

    seeds: list[dict[str, Any]] = [
        {
            "rule_key": PAPER_FIBER_RULE_KEY,
            "display_name": "纸、纸板和纸浆纤维鉴别分析 GB/T 4688-2020（定性）",
            "category_key": "other",
            "config": {
                "default_node_type": PAPER_FIBER_NODE_TYPE,
                "source": {
                    "root_id": PAPER_FIBER_ROOT_ID,
                    "folder_match": {
                        "strategy": "level1_contains_number",
                        "entry_kind": "workbook",
                        "max_depth": 2,
                    },
                },
                "task_facts": [
                    {
                        "condition_key": "task_item_name",
                        "fact": "check_item_name",
                        "op": "in",
                        "values": [PAPER_FIBER_PROJECT_NAME],
                    },
                    {
                        "condition_key": "test_method",
                        "fact": "check_method",
                        "op": "eq",
                        "value": PAPER_FIBER_TEST_METHOD,
                    },
                ],
                "probes": [
                    {
                        "name": "qualitative_result",
                        "type": "cell_value",
                        "sheet": PAPER_FIBER_WORKSHEET,
                        "cell": PAPER_FIBER_RESULT_CELL,
                        "parser": "paper_qualitative_v1",
                    },
                    {
                        "name": "standard_source",
                        "type": "cell_value",
                        "sheet": PAPER_FIBER_WORKSHEET,
                        "cell": PAPER_FIBER_STANDARD_SOURCE_CELL,
                        "parser": "text",
                    },
                ],
                "binding": None,
            },
        }
    ]

    for family in MICROSCOPY_RECORD_FAMILIES.values():
        seeds.append(
            {
                "rule_key": f"{MICROSCOPY_RULE_KEY_PREFIX}{family.key}",
                "display_name": (
                    f"{family.check_item_name} {family.test_method}（电镜）"
                ),
                "category_key": "electron_microscopy",
                "config": {
                    "default_node_type": ELECTRON_NODE_TYPE,
                    "record_family": family.key,
                    "source": {
                        "root_id": ELECTRON_ROOT_ID,
                        "folder_match": {
                            "strategy": "electron_image_folders",
                            "entry_kind": "image",
                            "max_depth": 2,
                        },
                    },
                    "task_facts": [
                        {
                            "condition_key": "task_item_name",
                            "fact": "check_item_name",
                            "op": "in",
                            "values": sorted(family.project_name_aliases),
                        },
                        {
                            "condition_key": "test_method",
                            "fact": "check_method",
                            "op": "eq",
                            "value": family.test_method,
                        },
                    ],
                    "probes": [],
                    "binding": {
                        "family_key": family.key,
                        "check_item_no": family.check_item_no,
                        "check_item_name": family.check_item_name,
                    },
                },
            }
        )

    for node_type, rule_key in REGENERATED_RULE_KEYS.items():
        legacy_rule = REGENERATED_FIBER_RULES[node_type]
        seeds.append(
            {
                "rule_key": rule_key,
                "display_name": f"{legacy_rule.name}（再生纤）",
                "category_key": "regenerated_fiber",
                "config": {
                    "default_node_type": node_type,
                    "source": {
                        "root_id": legacy_rule.root_id,
                        "folder_match": {
                            "strategy": "filename_contains_number",
                            "entry_kind": "workbook",
                            "max_depth": 2,
                        },
                    },
                    "task_facts": [],
                    "probes": [
                        {
                            "name": "worksheet_check",
                            "type": "worksheet_exists",
                            "sheet": legacy_rule.worksheet,
                        }
                    ],
                    "binding": None,
                },
            }
        )

    return seeds
