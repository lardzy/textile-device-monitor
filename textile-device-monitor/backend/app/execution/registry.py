from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional


NodeExecutor = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class NodeType:
    type: str
    version: int
    name: str
    category: str
    description: str
    execution_kind: str = "automatic"
    required_config: tuple[str, ...] = ()
    config_schema: dict[str, Any] = field(default_factory=dict)
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    test_only: bool = False
    publishable: bool = True

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["required_config"] = list(self.required_config)
        return value


class NodeRegistry:
    def __init__(self) -> None:
        self._types: dict[tuple[str, int], NodeType] = {}
        self._executors: dict[tuple[str, int], NodeExecutor] = {}

    def register(
        self,
        node_type: NodeType,
        executor: Optional[NodeExecutor] = None,
    ) -> None:
        key = (node_type.type, node_type.version)
        if key in self._types:
            raise ValueError(f"duplicate node type: {key}")
        self._types[key] = node_type
        if executor is not None:
            self._executors[key] = executor

    def get(self, node_type: str, version: int = 1) -> Optional[NodeType]:
        return self._types.get((node_type, version))

    def executor(self, node_type: str, version: int = 1) -> Optional[NodeExecutor]:
        return self._executors.get((node_type, version))

    def set_executor(
        self,
        node_type: str,
        version: int,
        executor: NodeExecutor,
    ) -> None:
        if (node_type, version) not in self._types:
            raise KeyError(f"unknown node type: {node_type}@{version}")
        self._executors[(node_type, version)] = executor

    def all(self) -> list[NodeType]:
        return sorted(
            self._types.values(),
            key=lambda item: (item.category, item.name, item.version),
        )


node_registry = NodeRegistry()


def _object_schema(
    properties: dict[str, Any] | None = None,
    *,
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties or {},
        "required": list(required),
        "additionalProperties": True,
    }


ROOT_QUERY_CONFIG_SCHEMA = _object_schema(
    {
        "root_id": {
            "type": "string",
            "pattern": r"^[a-z][a-z0-9_-]{0,63}$",
        },
        "recent_days": {"type": "integer", "minimum": 0, "maximum": 3650},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    },
    required=("root_id",),
)
HUMAN_CONFIG_SCHEMA = _object_schema(
    {
        "title": {"type": "string", "maxLength": 200},
        "description": {"type": "string", "maxLength": 2000},
        "candidate_role": {
            "type": "string",
            "pattern": r"^[a-z][a-z0-9_.-]{0,49}$",
        },
        "form_schema": {"type": "object"},
        "allow_multiple": {"type": "boolean"},
        "allow_primary": {"type": "boolean"},
        "require_primary": {"type": "boolean"},
    }
)
ARTIFACT_REF_SCHEMA = _object_schema(
    {
        "root_id": {"type": "string"},
        "relative_path": {"type": "string"},
    },
    required=("root_id", "relative_path"),
)


def _register_builtins() -> None:
    definitions = [
        NodeType("core.start", 1, "开始", "基础", "流程入口"),
        NodeType("core.end", 1, "结束", "基础", "流程出口"),
        NodeType(
            "variables.set",
            1,
            "设置变量",
            "基础",
            "写入流程全局变量",
            config_schema=_object_schema({"values": {"type": "object"}}),
        ),
        NodeType(
            "input.form",
            1,
            "动态表单",
            "人工",
            "收集必填和选填字段",
            "human",
            config_schema=HUMAN_CONFIG_SCHEMA,
        ),
        NodeType(
            "file.index_query",
            1,
            "文件索引查询",
            "文件",
            "从持久化索引查询候选文件",
            required_config=("root_id",),
            config_schema=ROOT_QUERY_CONFIG_SCHEMA,
            input_schema=_object_schema(
                {"inspection_number": {"type": "string"}},
            ),
            output_schema=_object_schema(
                {
                    "candidates": {"type": "array"},
                    "count": {"type": "integer"},
                }
            ),
        ),
        NodeType(
            "file.regenerated_fiber_count_method",
            1,
            "再生纤-根数法",
            "文件",
            "按编号、工作表和已保存单元格结果识别再生纤根数法记录",
            required_config=("root_id",),
            config_schema=_object_schema(
                {
                    "root_id": {
                        "type": "string",
                        "const": "regenerated_fiber_records",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 6,
                    },
                },
                required=("root_id",),
            ),
            input_schema=_object_schema(
                {"inspection_number": {"type": "string", "minLength": 1}},
                required=("inspection_number",),
            ),
            output_schema=_object_schema(
                {
                    "candidates": {"type": "array"},
                    "count": {"type": "integer"},
                    "diagnostics": {"type": "object"},
                }
            ),
        ),
        NodeType(
            "file.regenerated_fiber_area_method",
            1,
            "再生纤-面积法",
            "文件",
            "按编号、工作表和已保存单元格结果识别再生纤面积法记录",
            required_config=("root_id",),
            config_schema=_object_schema(
                {
                    "root_id": {
                        "type": "string",
                        "const": "regenerated_fiber_records",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 6,
                    },
                },
                required=("root_id",),
            ),
            input_schema=_object_schema(
                {"inspection_number": {"type": "string", "minLength": 1}},
                required=("inspection_number",),
            ),
            output_schema=_object_schema(
                {
                    "candidates": {"type": "array"},
                    "count": {"type": "integer"},
                    "diagnostics": {"type": "object"},
                }
            ),
        ),
        NodeType(
            "result.regenerated_fiber_count_method",
            1,
            "读取再生纤根数法结果",
            "结果",
            "逐个读取根数法工作簿中的部位、纤维含量、备注和插图",
            input_schema=_object_schema(
                {"files": {"type": "array", "minItems": 1}},
                required=("files",),
            ),
            output_schema=_object_schema(
                {
                    "files": {"type": "array"},
                    "count": {"type": "integer"},
                    "success_count": {"type": "integer"},
                    "failed_count": {"type": "integer"},
                }
            ),
        ),
        NodeType(
            "result.regenerated_fiber_area_method",
            1,
            "读取再生纤面积法结果",
            "结果",
            "逐个读取面积法工作簿中的部位、纤维含量、备注和插图",
            input_schema=_object_schema(
                {"files": {"type": "array", "minItems": 1}},
                required=("files",),
            ),
            output_schema=_object_schema(
                {
                    "files": {"type": "array"},
                    "count": {"type": "integer"},
                    "success_count": {"type": "integer"},
                    "failed_count": {"type": "integer"},
                }
            ),
        ),
        NodeType(
            "excel.classify",
            1,
            "Excel 类型识别",
            "Excel",
            "按模板特征识别工作簿",
            config_schema=_object_schema(
                {
                    "data_only": {"type": "boolean"},
                    "types": {"type": "array"},
                }
            ),
            input_schema=_object_schema({"source": ARTIFACT_REF_SCHEMA}),
            output_schema=_object_schema(
                {
                    "source": ARTIFACT_REF_SCHEMA,
                    "format": {"type": "string"},
                    "sheet_names": {"type": "array"},
                    "matched_types": {"type": "array"},
                    "detected_type": {"type": ["string", "null"]},
                    "ambiguous": {"type": "boolean"},
                }
            ),
        ),
        NodeType(
            "excel.extract_summary",
            1,
            "Excel 摘要提取",
            "Excel",
            "读取结构化摘要",
            config_schema=_object_schema(
                {
                    "data_only": {"type": "boolean"},
                    "fail_on_error": {"type": "boolean"},
                    "fields": {"type": "array"},
                }
            ),
            input_schema=_object_schema({"source": ARTIFACT_REF_SCHEMA}),
            output_schema=_object_schema(
                {
                    "source": ARTIFACT_REF_SCHEMA,
                    "values": {"type": "object"},
                    "errors": {"type": "array"},
                }
            ),
        ),
        NodeType(
            "electron.group",
            1,
            "电镜采集分组",
            "文件",
            "聚合同名采集文件",
            required_config=("root_id",),
            config_schema=ROOT_QUERY_CONFIG_SCHEMA,
            input_schema=_object_schema(
                {"inspection_number": {"type": "string"}},
            ),
            output_schema=_object_schema(
                {
                    "groups": {"type": "array"},
                    "count": {"type": "integer"},
                }
            ),
        ),
        NodeType(
            "file.electron_microscopy_gbt36422",
            1,
            "电镜—纤维微观形貌 GB/T 36422-2018",
            "文件",
            "按编号目录与旧系统任务信息查找可选电镜图片",
            required_config=("root_id",),
            config_schema=ROOT_QUERY_CONFIG_SCHEMA,
            input_schema=_object_schema(
                {"inspection_number": {"type": "string"}},
            ),
            output_schema=_object_schema(
                {
                    "folders": {"type": "array"},
                    "images": {"type": "array"},
                    "folder_selection_required": {"type": "boolean"},
                    "selected_folder_ids": {"type": "array"},
                    "image_count": {"type": "integer"},
                    "truncated": {"type": "boolean"},
                    "task": {"type": ["object", "null"]},
                    "task_validation_state": {"type": "string"},
                    "task_cache_state": {"type": "string"},
                    "missing_conditions": {"type": "array"},
                }
            ),
        ),
        NodeType(
            "human.file_selection",
            1,
            "人工选择文件",
            "人工",
            "从候选文件中确认输入",
            "human",
            config_schema=HUMAN_CONFIG_SCHEMA,
            input_schema=_object_schema(
                {
                    "candidates": {"type": "array"},
                    "groups": {"type": "array"},
                    "files": {"type": "array"},
                }
            ),
            output_schema=_object_schema(
                {
                    "selected_files": {"type": "array"},
                    "primary_file_id": {"type": ["string", "null"]},
                    "primary_file": {"type": ["object", "null"]},
                },
            ),
        ),
        NodeType(
            "human.image_selection",
            1,
            "人工选择图片",
            "人工",
            "从编号文件夹中选择 1 至 10 张图片",
            "human",
            config_schema=HUMAN_CONFIG_SCHEMA,
            input_schema=_object_schema(
                {
                    "folders": {"type": "array"},
                    "images": {"type": "array"},
                    "folder_selection_required": {"type": "boolean"},
                    "truncated": {"type": "boolean"},
                    "task_validation_state": {"type": "string"},
                    "task_cache_state": {"type": "string"},
                    "missing_conditions": {"type": "array"},
                }
            ),
            output_schema=_object_schema(
                {
                    "selected_folder_ids": {"type": "array"},
                    "selected_image_ids": {"type": "array"},
                    "selected_images": {"type": "array"},
                    "primary_image_id": {"type": ["string", "null"]},
                    "primary_image": {"type": ["object", "null"]},
                }
            ),
        ),
        NodeType(
            "human.input",
            1,
            "人工输入",
            "人工",
            "收集运行时输入",
            "human",
            config_schema=HUMAN_CONFIG_SCHEMA,
        ),
        NodeType(
            "human.confirm",
            1,
            "人工确认",
            "人工",
            "确认或驳回变更",
            "human",
            config_schema=HUMAN_CONFIG_SCHEMA,
        ),
        NodeType("branch.condition", 1, "条件分支", "控制", "按受限条件选择路径"),
        NodeType("parallel.split", 1, "并行拆分", "控制", "激活全部输出分支"),
        NodeType("parallel.join", 1, "并行汇合", "控制", "等待输入分支"),
        NodeType("result.aggregate", 1, "结果汇总", "基础", "汇总上游输出"),
        NodeType(
            "workbook.copy",
            1,
            "创建工作副本",
            "Excel",
            "把源工作簿复制到运行暂存区",
            required_config=("staging_root_id",),
            config_schema=_object_schema(
                {
                    "staging_root_id": {
                        "type": "string",
                        "pattern": r"^[a-z][a-z0-9_-]{0,63}$",
                    }
                },
                required=("staging_root_id",),
            ),
            input_schema=_object_schema(
                {
                    "source": ARTIFACT_REF_SCHEMA,
                    "mutation_id": {"type": "string"},
                }
            ),
            output_schema=_object_schema(
                {
                    "mutation_id": {"type": "string"},
                    "working_copy": ARTIFACT_REF_SCHEMA,
                }
            ),
        ),
        NodeType(
            "workbook.write_cells",
            1,
            "映射字段写入",
            "Excel",
            "向工作副本写入字段",
            input_schema=_object_schema(
                {
                    "mutation_id": {"type": "string"},
                    "working_copy": ARTIFACT_REF_SCHEMA,
                    "writes": {"type": "array"},
                }
            ),
            output_schema=_object_schema(
                {
                    "mutation_id": {"type": "string"},
                    "working_copy": ARTIFACT_REF_SCHEMA,
                    "verification": {"type": "object"},
                }
            ),
        ),
        NodeType(
            "workbook.verify",
            1,
            "保存后核对",
            "Excel",
            "重读并核对写入结果",
            input_schema=_object_schema(
                {
                    "mutation_id": {"type": "string"},
                    "working_copy": ARTIFACT_REF_SCHEMA,
                    "writes": {"type": "array"},
                    "target": ARTIFACT_REF_SCHEMA,
                }
            ),
            output_schema=_object_schema(
                {
                    "mutation_id": {"type": "string"},
                    "approval_context": {"type": "object"},
                }
            ),
        ),
        NodeType(
            "artifact.publish",
            1,
            "发布制品",
            "文件",
            "将已核对制品发布到目标根",
            required_config=("publish_root_id", "confirmation_node_id"),
            config_schema=_object_schema(
                {
                    "publish_root_id": {
                        "type": "string",
                        "pattern": r"^[a-z][a-z0-9_-]{0,63}$",
                    },
                    "confirmation_node_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 100,
                    },
                    "target_relative_path": {
                        "type": "string",
                        "maxLength": 1000,
                    },
                },
                required=("publish_root_id", "confirmation_node_id"),
            ),
            input_schema=_object_schema(
                {
                    "mutation_id": {"type": "string"},
                    "working_copy": ARTIFACT_REF_SCHEMA,
                    "target": ARTIFACT_REF_SCHEMA,
                }
            ),
            output_schema=_object_schema(
                {
                    "mutation_id": {"type": "string"},
                    "published": ARTIFACT_REF_SCHEMA,
                }
            ),
        ),
        NodeType(
            "external.legacy_inspection",
            1,
            "旧检务系统",
            "连接器",
            "预留的旧检务系统连接器",
            publishable=False,
        ),
        NodeType(
            "external.legacy_regenerated_fiber_count_upload",
            1,
            "旧系统上传-再生纤-根数法",
            "连接器",
            "生成旧检务系统上传预检单并等待最终人工批准",
            execution_kind="external_side_effect",
            required_config=("credential_slot", "selection_node_id"),
            config_schema=_object_schema(
                {
                    "credential_slot": {
                        "type": "string",
                        "pattern": r"^[A-Za-z][A-Za-z0-9_.-]{0,99}$",
                    },
                    "selection_node_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 100,
                    },
                },
                required=("credential_slot", "selection_node_id"),
            ),
            input_schema=_object_schema(
                {
                    "selected_files": {
                        "type": "array",
                        "minItems": 1,
                    },
                    "primary_file_id": {
                        "type": "string",
                        "minLength": 1,
                    },
                    "primary_file": {"type": ["object", "null"]},
                },
                required=("selected_files", "primary_file_id"),
            ),
            output_schema=_object_schema(
                {
                    "operation_id": {"type": "string"},
                    "operation_key": {
                        "type": "string",
                        "pattern": r"^[0-9a-f]{64}$",
                    },
                    "payload_checksum": {
                        "type": "string",
                        "pattern": r"^[0-9a-f]{64}$",
                    },
                    "status": {
                        "enum": ["prepared", "approved"],
                    },
                    "requires_final_approval": {
                        "type": "boolean",
                        "const": True,
                    },
                    "remote_write_performed": {
                        "type": "boolean",
                        "const": False,
                    },
                },
                required=(
                    "operation_id",
                    "operation_key",
                    "payload_checksum",
                    "status",
                    "requires_final_approval",
                    "remote_write_performed",
                ),
            ),
        ),
        NodeType(
            "external.new_inspection",
            1,
            "新检务系统",
            "连接器",
            "预留的新检务系统连接器",
            publishable=False,
        ),
    ]
    for definition in definitions:
        node_registry.register(definition)


_register_builtins()
