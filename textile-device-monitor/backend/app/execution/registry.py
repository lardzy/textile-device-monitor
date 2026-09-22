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
