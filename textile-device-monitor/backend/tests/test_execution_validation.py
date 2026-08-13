from __future__ import annotations

import unittest

from app.execution.registry import node_registry
from app.execution.validation import validate_definition


def valid_definition():
    return {
        "schema_version": "1.0",
        "metadata": {"name": "测试流程"},
        "input_schema": {"type": "object", "properties": {}},
        "global_schema": {"type": "object", "properties": {}},
        "root_slots": [],
        "credential_slots": [],
        "nodes": [
            {
                "id": "start",
                "type": "core.start",
                "type_version": 1,
                "name": "开始",
                "config": {},
            },
            {
                "id": "end",
                "type": "core.end",
                "type_version": 1,
                "name": "结束",
                "config": {},
            },
        ],
        "edges": [{"id": "e1", "source": "start", "target": "end"}],
    }


class WorkflowValidationTests(unittest.TestCase):
    def test_legacy_external_nodes_do_not_require_final_approval(self):
        external_types = (
            "external.legacy_regenerated_fiber_count_upload",
            "external.legacy_special_wool_image_upload",
            "external.legacy_special_wool_review",
            "external.legacy_microscopy_check_record_entry",
            "external.legacy_special_wool_qualitative_upload",
            "external.legacy_special_wool_qualitative_review",
            "external.legacy_generic_check_record_entry",
        )

        for node_type in external_types:
            with self.subTest(node_type=node_type):
                definition = node_registry.get(node_type, 1)
                self.assertIsNotNone(definition)
                approval_schema = definition.output_schema["properties"][
                    "requires_final_approval"
                ]
                self.assertIs(approval_schema["const"], False)

    def test_valid_minimal_dag(self):
        result = validate_definition(valid_definition(), for_publish=True)
        self.assertTrue(result.valid, result.as_dict())

    def test_cycle_is_rejected(self):
        document = valid_definition()
        document["edges"].append(
            {"id": "e2", "source": "end", "target": "start"}
        )
        result = validate_definition(document)
        self.assertFalse(result.valid)
        self.assertIn("cycle_forbidden", {issue.code for issue in result.issues})

    def test_embedded_secret_and_absolute_path_are_rejected(self):
        document = valid_definition()
        document["nodes"][0]["config"] = {
            "password": "do-not-store-me",
            "source_path": "C:\\records\\source.xlsx",
        }
        result = validate_definition(document)
        codes = {issue.code for issue in result.issues}
        self.assertIn("embedded_secret", codes)
        self.assertIn("absolute_path_forbidden", codes)

    def test_unknown_node_version_is_rejected(self):
        document = valid_definition()
        document["nodes"][1]["type_version"] = 999
        result = validate_definition(document)
        self.assertFalse(result.valid)
        self.assertIn("unknown_node_type", {issue.code for issue in result.issues})

    def test_mixed_join_policy_for_same_target_is_rejected(self):
        document = valid_definition()
        document["nodes"] = [
            {
                "id": "start",
                "type": "core.start",
                "name": "开始",
                "config": {},
            },
            {
                "id": "left",
                "type": "result.aggregate",
                "name": "左分支",
                "config": {},
            },
            {
                "id": "right",
                "type": "result.aggregate",
                "name": "右分支",
                "config": {},
            },
            {
                "id": "join",
                "type": "parallel.join",
                "name": "汇合",
                "config": {},
            },
            {
                "id": "end",
                "type": "core.end",
                "name": "结束",
                "config": {},
            },
        ]
        document["edges"] = [
            {"id": "e1", "source": "start", "target": "left"},
            {"id": "e2", "source": "start", "target": "right"},
            {
                "id": "e3",
                "source": "left",
                "target": "join",
                "join_policy": "all",
            },
            {
                "id": "e4",
                "source": "right",
                "target": "join",
                "join_policy": "any",
            },
            {"id": "e5", "source": "join", "target": "end"},
        ]
        result = validate_definition(document)
        self.assertFalse(result.valid)
        self.assertIn("mixed_join_policy", {issue.code for issue in result.issues})

    def test_multiple_publish_nodes_are_rejected(self):
        document = valid_definition()
        document["nodes"].extend(
            [
                {
                    "id": "publish-a",
                    "type": "artifact.publish",
                    "type_version": 1,
                    "name": "发布 A",
                    "config": {},
                },
                {
                    "id": "publish-b",
                    "type": "artifact.publish",
                    "type_version": 1,
                    "name": "发布 B",
                    "config": {},
                },
            ]
        )
        result = validate_definition(document, for_publish=True)
        self.assertFalse(result.valid)
        self.assertIn(
            "multiple_publish_nodes_forbidden",
            {issue.code for issue in result.issues},
        )

    def test_mapping_rejects_unknown_input_and_downstream_node(self):
        document = valid_definition()
        document["input_schema"]["properties"] = {
            "inspection_number": {"type": "string"}
        }
        document["nodes"] = [
            document["nodes"][0],
            {
                "id": "left",
                "type": "result.aggregate",
                "name": "左侧",
                "config": {},
                "input_mapping": {
                    "missing": "$.inputs.missing",
                    "future": "$.nodes.right.output",
                },
            },
            {
                "id": "right",
                "type": "result.aggregate",
                "name": "右侧",
                "config": {},
            },
            document["nodes"][1],
        ]
        document["edges"] = [
            {"source": "start", "target": "left"},
            {"source": "left", "target": "right"},
            {"source": "right", "target": "end"},
        ]

        result = validate_definition(document, for_publish=True)
        codes = {issue.code for issue in result.issues}
        self.assertIn("mapping_source_path_unknown", codes)
        self.assertIn("mapping_source_not_upstream", codes)

    def test_mapping_rejects_known_type_mismatch(self):
        document = valid_definition()
        document["input_schema"]["properties"] = {
            "sample_count": {"type": "integer"}
        }
        document["root_slots"] = [
            {
                "name": "source",
                "root_id": "special_wool_records",
                "access": "read",
            }
        ]
        document["nodes"] = [
            document["nodes"][0],
            {
                "id": "query",
                "type": "file.index_query",
                "name": "查询",
                "config": {"root_id": "special_wool_records"},
                "input_mapping": {
                    "inspection_number": "$.inputs.sample_count"
                },
            },
            document["nodes"][1],
        ]
        document["edges"] = [
            {"source": "start", "target": "query"},
            {"source": "query", "target": "end"},
        ]

        result = validate_definition(document, for_publish=True)
        self.assertIn(
            "mapping_type_mismatch",
            {issue.code for issue in result.issues},
        )

    def test_mapping_accepts_declared_upstream_output(self):
        document = valid_definition()
        document["input_schema"]["properties"] = {
            "inspection_number": {"type": "string"}
        }
        document["root_slots"] = [
            {
                "name": "source",
                "root_id": "special_wool_records",
                "access": "read",
            }
        ]
        document["nodes"] = [
            document["nodes"][0],
            {
                "id": "query",
                "type": "file.index_query",
                "name": "查询",
                "config": {"root_id": "special_wool_records"},
                "input_mapping": {
                    "inspection_number": "$.inputs.inspection_number"
                },
            },
            {
                "id": "select",
                "type": "human.file_selection",
                "name": "选择",
                "config": {},
                "input_mapping": {
                    "candidates": "$.nodes.query.output.candidates"
                },
            },
            document["nodes"][1],
        ]
        document["edges"] = [
            {"source": "start", "target": "query"},
            {"source": "query", "target": "select"},
            {"source": "select", "target": "end"},
        ]

        result = validate_definition(document, for_publish=True)
        self.assertTrue(result.valid, result.as_dict())


if __name__ == "__main__":
    unittest.main()
