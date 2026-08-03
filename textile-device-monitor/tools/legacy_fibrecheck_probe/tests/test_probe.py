from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import sys

TOOL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_ROOT))

import probe  # noqa: E402


CONFIG_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <connectionStrings>
    <add name="FibreCheckEntities"
      connectionString="metadata=res://*/FibreCheck.csdl;provider=Oracle.DataAccess.Client;provider connection string=&quot;DATA SOURCE=db.internal/orcl/;PASSWORD=super-secret;USER ID=APPUSER&quot;"
      providerName="System.Data.EntityClient"/>
  </connectionStrings>
</configuration>
"""


class FakeCursor:
    def __init__(self, connection: "FakeConnection"):
        self.connection = connection
        self.description: list[tuple[str]] = []
        self._rows: list[tuple[Any, ...]] = []

    def execute(
        self,
        sql: str,
        parameters: dict[str, str] | None = None,
    ) -> None:
        self.connection.executed.append((sql, parameters))
        if sql == probe.READ_ONLY_TRANSACTION_SQL:
            self.description = []
            self._rows = []
            return
        self.description = [
            ("ID",),
            ("SampleNo",),
            ("FilePath",),
            ("LoginName",),
            ("TaskAssignUser",),
            ("ReportName",),
        ]
        self._rows = [
            (
                "11111111-1111-1111-1111-111111111111",
                "260187115",
                r"\\server\share\record.xls",
                "lisy",
                "BA3DD04BC2BB44A2AC9C58AE5F28664B",
                r"\\server\outpdf$\report.pdf",
            )
        ]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def close(self) -> None:
        return None


class FakeConnection:
    def __init__(self):
        self.autocommit = True
        self.executed: list[tuple[str, dict[str, str] | None]] = []
        self.rollback_count = 0
        self.close_count = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def rollback(self) -> None:
        self.rollback_count += 1

    def close(self) -> None:
        self.close_count += 1


class FailingReadOnlyConnection(FakeConnection):
    def cursor(self) -> FakeCursor:
        connection = self

        class FailingCursor(FakeCursor):
            def execute(
                self,
                sql: str,
                parameters: dict[str, str] | None = None,
            ) -> None:
                connection.executed.append((sql, parameters))
                raise RuntimeError("ORA-01453: SET TRANSACTION must be first statement")

        return FailingCursor(self)


def ok_result(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": "ok",
        "row_count": len(rows),
        "rows": rows,
    }


def empty_final_entry_results() -> dict[str, dict[str, Any]]:
    return {
        key: ok_result([])
        for key in probe.FINAL_ENTRY_REQUIRED_QUERY_KEYS
    }


def path_reference(name: str, *, scope: str = "hidden") -> dict[str, Any]:
    return {
        "count": 1,
        "items": [
            {
                "basename": name,
                "path_hash": probe.digest_text(f"{scope}/{name}"),
            }
        ],
    }


def empty_path_reference() -> dict[str, Any]:
    return {"count": 0, "items": []}


class ProbeTests(unittest.TestCase):
    def make_fibrecheck_dir(self, root: Path) -> Path:
        install_dir = root / "FibreCheck"
        install_dir.mkdir()
        (install_dir / probe.CONFIG_FILENAME).write_text(
            CONFIG_TEMPLATE,
            encoding="utf-8",
        )
        return install_dir

    def test_sample_number_validation_is_strict(self) -> None:
        for valid in ("260187115", "260187115-1", "26X909953"):
            self.assertEqual(probe.validate_sample_no(valid), valid)
        for invalid in (
            "",
            "26018711",
            "260187115 ",
            "260187115_%",
            "260187115/1",
            "260187115-",
            "260187115-1-2",
            "260187115' OR 1=1",
            "26x909953",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(probe.ProbeError):
                    probe.validate_sample_no(invalid)

    def test_all_declared_sql_is_select_only(self) -> None:
        probe.assert_read_only_sql(probe.READ_ONLY_TRANSACTION_SQL)
        for query in probe.QUERIES:
            probe.assert_read_only_sql(query.sql)
            self.assertTrue(probe.compact_sql(query.sql).upper().startswith("SELECT "))
        for unsafe in (
            'DELETE FROM "Task"',
            'SELECT 1 FROM "Task"; DELETE FROM "Task"',
            'BEGIN NULL',
        ):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(probe.ProbeError):
                    probe.assert_read_only_sql(unsafe)

    def test_final_entry_paths_are_covered_by_probe_manifest(self) -> None:
        queries = {query.key: probe.compact_sql(query.sql) for query in probe.QUERIES}

        self.assertIn("currency_item_records", queries)
        self.assertIn("currency_item_record_details", queries)
        self.assertIn("currency_excel_records", queries)
        self.assertIn("original_key_data_list", queries)
        self.assertIn("original_key_data_other", queries)
        self.assertIn("task_entry_routes", queries)
        self.assertIn("check_record_templates", queries)
        self.assertIn(probe.MAPPING_CONFIG_QUERY_KEY, queries)
        self.assertNotIn("currency_excel_templates", queries)
        self.assertIn('FROM "CurrencyItemRecordNew"', queries["currency_item_records"])
        self.assertIn('FROM "CurrExcelOriRecord"', queries["currency_excel_records"])
        self.assertIn(
            'item."OriginalDataInputUIClassName"',
            queries["task_entry_routes"],
        )
        self.assertIn(
            'JOIN "StandardDocument"',
            queries["check_record_templates"],
        )
        self.assertIn(
            'JOIN "Document"',
            queries["check_record_templates"],
        )
        mapping_query = queries[probe.MAPPING_CONFIG_QUERY_KEY]
        self.assertIn('LEFT JOIN "OriginalKeyDataTableMapping"', mapping_query)
        self.assertIn('LEFT JOIN "OriginalKeyDataConfig"', mapping_query)
        self.assertIn("LEFT JOIN USER_TABLES", mapping_query)

    def test_final_entry_view_isolates_projects_and_normalizes_records(self) -> None:
        results = empty_final_entry_results()
        item_a = "sha256:item-a"
        item_b = "sha256:item-b"
        task_id = "sha256:task"
        tci_a = "sha256:tci-a"
        tci_b = "sha256:tci-b"
        register_a = "sha256:register-a"
        register_b1 = "sha256:register-b1"
        register_b2 = "sha256:register-b2"
        common_a = "sha256:common-a"

        results["task_check_items"] = ok_result(
            [
                {
                    "ID": tci_a,
                    "TaskID": task_id,
                    "CheckItemID": item_a,
                    "CheckItemNo": "A.1",
                    "CheckItemName": "通用项目",
                    "CheckMethod": "METHOD-A",
                    "CheckCount": 1,
                    "SeqNum": 1,
                },
                {
                    "ID": tci_b,
                    "TaskID": task_id,
                    "CheckItemID": item_b,
                    "CheckItemNo": "B.2",
                    "CheckItemName": "Excel 项目",
                    "CheckMethod": "METHOD-B",
                    "CheckCount": "2",
                    "SeqNum": 2,
                },
            ]
        )
        results["task_entry_routes"] = ok_result(
            [
                {
                    "TaskCheckItemID": tci_a,
                    "TaskID": task_id,
                    "CheckItemID": item_a,
                    "CatalogCheckItemNo": "A.1",
                    "CatalogCheckItemName": "通用项目",
                    "OriginalDataInputUIClassName": (
                        "Toone.FibreCheck.OriRecord.CurrencyItem."
                        "CurrencyItemRecordUI"
                    ),
                    "PositionID": "sha256:position-a",
                },
                {
                    "TaskCheckItemID": tci_b,
                    "TaskID": task_id,
                    "CheckItemID": item_b,
                    "CatalogCheckItemNo": "B.2",
                    "CatalogCheckItemName": "Excel 项目",
                    "OriginalDataInputUIClassName": None,
                    "PositionID": "sha256:position-b",
                },
            ]
        )
        results["check_record_templates"] = ok_result(
            [
                {
                    "TaskCheckItemID": tci_b,
                    "CheckItemID": item_b,
                    "DocumentID": "sha256:document-b",
                    "DocumentName": path_reference("micro.xls"),
                    "DocumentUploadTime": "2026-01-01T00:00:00",
                    "DocumentUploadIndex": "sha256:upload-index",
                },
                {
                    "TaskCheckItemID": tci_b,
                    "CheckItemID": item_b,
                    "DocumentID": "sha256:document-unused",
                    "DocumentName": path_reference("unused.xls"),
                    "DocumentUploadTime": "2026-01-01T00:00:00",
                    "DocumentUploadIndex": "sha256:upload-index-unused",
                }
            ]
        )
        results[probe.MAPPING_CONFIG_QUERY_KEY] = ok_result(
            [
                {
                    "TaskCheckItemID": tci_b,
                    "CheckItemID": item_b,
                    "DocumentID": "sha256:document-b",
                    "MappingConfigStatus": "complete",
                    "MappingConfigCount": 7,
                    "MappingConfigSha256": "a" * 64,
                    "MappingConfigReason": None,
                    "MappedTableExists": True,
                },
                {
                    "TaskCheckItemID": tci_b,
                    "CheckItemID": item_b,
                    "DocumentID": "sha256:document-unused",
                    "MappingConfigStatus": "incomplete",
                    "MappingConfigCount": None,
                    "MappingConfigSha256": None,
                    "MappingConfigReason": "mapping_missing",
                    "MappedTableExists": None,
                }
            ]
        )
        results["currency_item_records"] = ok_result(
            [
                {
                    "ID": common_a,
                    "CheckRecordRegisterID": register_a,
                    "SampleNo": "260000001",
                    "CheckItemID": item_a,
                    "CheckItemName": "通用项目",
                    "Grade": "合格",
                    "JudgeBasis": "BASIS",
                    "SampleDescription": "样品",
                    "TestMethod": "METHOD-A",
                    "StandardType": "标准",
                    "Unit": "%",
                    "ReportCheckItemName": "通用项目",
                    "AttachInfo": empty_path_reference(),
                    "Remark": "remark",
                    "TotalJudge": "pass",
                    "CheckUser": "sha256:user-a",
                }
            ]
        )
        results["currency_item_record_details"] = ok_result(
            [
                {
                    "ID": "sha256:detail-2",
                    "CurrencyItemRecordNewID": common_a,
                    "SeqNum": 2,
                    "StandardLocation": "S2",
                    "StandardValue": "V2",
                    "RealLocation": "R2",
                    "RealValue": "M2",
                },
                {
                    "ID": "sha256:detail-1",
                    "CurrencyItemRecordNewID": common_a,
                    "SeqNum": 1,
                    "StandardLocation": "S1",
                    "StandardValue": "V1",
                    "RealLocation": "R1",
                    "RealValue": "M1",
                },
            ]
        )
        results["check_record_register"] = ok_result(
            [
                {
                    "ID": register_a,
                    "SampleNo": "260000001",
                    "CheckItemID": item_a,
                    "OriginalRecordID": common_a,
                    "TemplateFilename": empty_path_reference(),
                    "OriginalDataFilename": empty_path_reference(),
                },
                {
                    "ID": register_b1,
                    "SampleNo": "260000001",
                    "CheckItemID": item_b,
                    "TemplateFilename": path_reference("micro.xls"),
                    "OriginalDataFilename": path_reference("first.xls"),
                    "SampleIdentity": "纵面",
                },
                {
                    "ID": register_b2,
                    "SampleNo": "260000001",
                    "CheckItemID": item_b,
                    "TemplateFilename": path_reference("micro.xls"),
                    "OriginalDataFilename": path_reference("second.xls"),
                    "SampleIdentity": "横截面",
                },
            ]
        )
        results["original_key_data"] = ok_result(
            [
                {
                    "CheckItemID": item_a,
                    "OriginalRecordID": common_a,
                    "SeqNum": 1,
                    "CheckResult": "A-result",
                },
                {
                    "CheckItemID": item_b,
                    "OriginalRecordID": register_b1,
                    "SeqNum": 1,
                    "SampleIdentity": "纵面",
                },
                {
                    "CheckItemID": item_b,
                    "OriginalRecordID": register_b2,
                    "SeqNum": 1,
                    "SampleIdentity": "横截面",
                },
                {
                    "CheckItemID": "sha256:unrelated-item",
                    "OriginalRecordID": "sha256:unrelated-register",
                    "SeqNum": 1,
                },
            ]
        )
        results["original_key_data_list"] = ok_result(
            [
                {
                    "CheckItemID": item_b,
                    "OriginalRecordID": register_b1,
                    "SeqNum": 1,
                    "Column1": "list-value",
                }
            ]
        )
        results["original_key_data_other"] = ok_result(
            [
                {
                    "ID": "sha256:other-b",
                    "SampleNo": "260000001",
                    "OriginalRecordID": register_b2,
                    "CheckItemNo": "B.2",
                    "OriginalData": "other-value",
                    "DataType": "text",
                },
                {
                    "ID": "sha256:other-unrelated",
                    "SampleNo": "260000001",
                    "OriginalRecordID": "sha256:unrelated-register",
                    "CheckItemNo": "C.3",
                },
            ]
        )
        results["currency_excel_records"] = ok_result(
            [{"CheckItemID": item_b, "FileName": path_reference("wrong.xls")}]
        )
        results["quantification_tests"] = ok_result(
            [{"CheckItemID": item_b, "FileName": path_reference("wrong-2.xls")}]
        )

        view = probe.build_final_entry_view("260000001", results)

        self.assertEqual(view["status"], "complete")
        self.assertFalse(view["incomplete"])
        projects = {project["check_item_no"]: project for project in view["projects"]}
        common_project = projects["A.1"]
        excel_project = projects["B.2"]
        self.assertEqual(common_project["expected_result_count"], 1)
        self.assertEqual(common_project["generic_record_count"], 1)
        self.assertEqual(common_project["register_count"], 1)
        self.assertEqual(common_project["file_reference_count"], 0)
        self.assertEqual(
            common_project["key_result_linkage_mode"],
            "generic_record",
        )
        self.assertEqual(common_project["key_result_count"], 1)
        self.assertEqual(common_project["list_data_count"], 0)
        self.assertEqual(common_project["other_data_count"], 0)
        detail_table = common_project["generic_records"][0]["detail_table"]
        self.assertEqual(
            detail_table["columns"],
            list(probe.COMMON_DETAIL_TABLE_COLUMNS),
        )
        self.assertEqual(
            [row["seq_num"] for row in detail_table["rows"]],
            [1, 2],
        )
        self.assertEqual(detail_table["rows"][0]["values"], ["S1", "V1", "R1", "M1"])
        self.assertEqual(
            common_project["generic_records"][0]["key_results"][0]["check_result"],
            "A-result",
        )
        self.assertEqual(common_project["excel_records"], [])

        self.assertEqual(excel_project["expected_result_count"], 2)
        self.assertEqual(excel_project["generic_record_count"], 0)
        self.assertEqual(excel_project["register_count"], 2)
        self.assertEqual(excel_project["file_reference_count"], 2)
        self.assertEqual(excel_project["template_reference_count"], 2)
        self.assertEqual(excel_project["unique_template_names"], ["micro.xls"])
        self.assertEqual(
            excel_project["key_result_linkage_mode"],
            "check_record_register",
        )
        self.assertEqual(excel_project["key_result_count"], 2)
        self.assertEqual(excel_project["list_data_count"], 1)
        self.assertEqual(excel_project["other_data_count"], 1)
        self.assertEqual(excel_project["configured_template_count"], 2)
        templates = {
            template["document_name"]["items"][0]["basename"]: template
            for template in excel_project["configured_templates"]
        }
        mapping_config = templates["micro.xls"]["mapping_config"]
        self.assertEqual(mapping_config["status"], "complete")
        self.assertEqual(mapping_config["config_count"], 7)
        self.assertEqual(
            mapping_config["expected_mapping_config_sha256"],
            "a" * 64,
        )
        self.assertTrue(mapping_config["mapped_table_exists"])
        self.assertTrue(
            templates["micro.xls"]["referenced_by_existing_records"]
        )
        self.assertFalse(
            templates["unused.xls"]["referenced_by_existing_records"]
        )
        self.assertEqual(
            templates["unused.xls"]["mapping_config"]["status"],
            "incomplete",
        )
        self.assertEqual(excel_project["status"], "complete")
        self.assertEqual(
            excel_project["entry_route"]["original_data_input_ui_class_name"],
            None,
        )
        self.assertEqual(len(excel_project["excel_records"]), 2)
        rendered = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("wrong.xls", rendered)
        self.assertNotIn("wrong-2.xls", rendered)

    def test_final_entry_view_requires_generic_register_bridge(self) -> None:
        results = empty_final_entry_results()
        task_check_item_id = "sha256:tci"
        check_item_id = "sha256:item"
        generic_id = "sha256:generic"
        results["task_check_items"] = ok_result(
            [
                {
                    "ID": task_check_item_id,
                    "TaskID": "sha256:task",
                    "CheckItemID": check_item_id,
                    "CheckItemNo": "A.1",
                    "CheckCount": 1,
                }
            ]
        )
        results["task_entry_routes"] = ok_result(
            [
                {
                    "TaskCheckItemID": task_check_item_id,
                    "TaskID": "sha256:task",
                    "CheckItemID": check_item_id,
                    "OriginalDataInputUIClassName": (
                        "Toone.FibreCheck.OriRecord.CurrencyItem."
                        "CurrencyItemRecordUI"
                    ),
                }
            ]
        )
        results["currency_item_records"] = ok_result(
            [
                {
                    "ID": generic_id,
                    "CheckItemID": check_item_id,
                    "SampleNo": "260000001",
                }
            ]
        )
        results["check_record_register"] = ok_result(
            [
                {
                    "ID": "sha256:register",
                    "CheckItemID": check_item_id,
                    "OriginalRecordID": "sha256:wrong-generic",
                    "OriginalDataFilename": empty_path_reference(),
                    "TemplateFilename": empty_path_reference(),
                }
            ]
        )
        results["original_key_data"] = ok_result(
            [
                {
                    "CheckItemID": check_item_id,
                    "OriginalRecordID": generic_id,
                    "CheckResult": "must-not-be-exposed-without-bridge",
                }
            ]
        )

        project = probe.build_final_entry_view(
            "260000001",
            results,
        )["projects"][0]

        self.assertEqual(project["status"], "incomplete")
        self.assertEqual(project["key_result_linkage_mode"], "generic_record")
        self.assertIsNone(project["key_result_count"])
        self.assertEqual(project["generic_records"][0]["key_results"], [])
        self.assertEqual(project["excel_records"], [])
        reason_codes = {
            reason["code"] for reason in project["incomplete_reasons"]
        }
        self.assertIn("generic_register_bridge_mismatch", reason_codes)
        self.assertIn("unbridged_generic_records", reason_codes)

    def test_final_entry_view_fails_closed_when_register_query_fails(self) -> None:
        results = empty_final_entry_results()
        task_check_item_id = "sha256:tci"
        check_item_id = "sha256:item"
        results["task_check_items"] = ok_result(
            [
                {
                    "ID": task_check_item_id,
                    "TaskID": "sha256:task",
                    "CheckItemID": check_item_id,
                    "CheckItemNo": "B.2",
                    "CheckItemName": "Excel 项目",
                    "CheckCount": 2,
                }
            ]
        )
        results["task_entry_routes"] = ok_result(
            [
                {
                    "TaskCheckItemID": task_check_item_id,
                    "TaskID": "sha256:task",
                    "CheckItemID": check_item_id,
                    "OriginalDataInputUIClassName": None,
                }
            ]
        )
        results["check_record_register"] = {
            "status": "query_error",
            "row_count": 0,
            "rows": [],
            "error": {"type": "DatabaseError", "code": "ORA-00942"},
        }
        results["original_key_data"] = ok_result(
            [
                {
                    "CheckItemID": check_item_id,
                    "OriginalRecordID": "sha256:unknown-register",
                    "CheckResult": "must-not-be-guessed",
                }
            ]
        )
        results["currency_excel_records"] = ok_result(
            [{"CheckItemID": check_item_id, "FileName": path_reference("guess.xls")}]
        )

        view = probe.build_final_entry_view("260000001", results)

        self.assertEqual(view["status"], "incomplete")
        self.assertTrue(view["incomplete"])
        project = view["projects"][0]
        self.assertEqual(project["status"], "incomplete")
        self.assertEqual(project["expected_result_count"], 2)
        self.assertEqual(project["generic_record_count"], 0)
        self.assertIsNone(project["register_count"])
        self.assertIsNone(project["file_reference_count"])
        self.assertIsNone(project["template_reference_count"])
        self.assertIsNone(project["key_result_count"])
        self.assertIsNone(project["list_data_count"])
        self.assertIsNone(project["other_data_count"])
        self.assertEqual(project["excel_records"], [])
        self.assertIn(
            "check_record_register",
            {issue["query"] for issue in view["incomplete_queries"]},
        )
        rendered = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("must-not-be-guessed", rendered)
        self.assertNotIn("guess.xls", rendered)

    def test_final_entry_view_rejects_ambiguous_task_check_item_scope(self) -> None:
        results = empty_final_entry_results()
        shared_check_item_id = "sha256:shared-item"
        results["task_check_items"] = ok_result(
            [
                {
                    "ID": "sha256:tci-1",
                    "TaskID": "sha256:task",
                    "CheckItemID": shared_check_item_id,
                    "CheckItemNo": "DUP.1",
                    "CheckCount": 1,
                },
                {
                    "ID": "sha256:tci-2",
                    "TaskID": "sha256:task",
                    "CheckItemID": shared_check_item_id,
                    "CheckItemNo": "DUP.2",
                    "CheckCount": 1,
                },
            ]
        )
        results["task_entry_routes"] = ok_result(
            [
                {
                    "TaskCheckItemID": "sha256:tci-1",
                    "TaskID": "sha256:task",
                    "CheckItemID": shared_check_item_id,
                },
                {
                    "TaskCheckItemID": "sha256:tci-2",
                    "TaskID": "sha256:task",
                    "CheckItemID": shared_check_item_id,
                },
            ]
        )

        view = probe.build_final_entry_view("260000001", results)

        self.assertEqual(view["status"], "incomplete")
        for project in view["projects"]:
            self.assertIsNone(project["generic_record_count"])
            self.assertIsNone(project["register_count"])
            self.assertTrue(
                any(
                    reason["code"] == "ambiguous_check_item_id"
                    for reason in project["incomplete_reasons"]
                )
            )

    def test_mapping_config_fingerprint_matches_writer_and_never_leaks(self) -> None:
        raw = {
            "TaskCheckItemID": "private-task-check-item-id",
            "CheckItemID": "private-check-item-id",
            "DocumentID": "private-document-id",
            "MappingCount": 1,
            "DataTableName": "ORIGINALKEYDATA_TEST",
            "MappedTableExists": 1,
            "ConfigPresent": 1,
            "SeqNum": 1,
            "KeyDataField": "A",
            "KeyDataType": "String",
            "ConfigValue": "中文",
            "ConfigValue_En": None,
            "ConfigValue_CnEn": None,
            "ConfigValue_NewCnEn": None,
        }
        raw_values = tuple(raw[column] for column in probe.MAPPING_CONFIG_RAW_COLUMNS)

        class MappingCursor:
            description = [(column,) for column in probe.MAPPING_CONFIG_RAW_COLUMNS]

            def execute(
                self,
                sql: str,
                parameters: dict[str, str] | None = None,
            ) -> None:
                probe.assert_read_only_sql(sql)
                self.parameters = parameters

            def fetchall(self) -> list[tuple[Any, ...]]:
                return [raw_values]

            def close(self) -> None:
                return None

        class MappingConnection:
            def cursor(self) -> MappingCursor:
                return MappingCursor()

        query = next(
            item for item in probe.QUERIES
            if item.key == probe.MAPPING_CONFIG_QUERY_KEY
        )
        result = probe.ReadOnlyProbeRunner(MappingConnection())._execute_query(
            query,
            {"sample_no": "260000001"},
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["row_count"], 1)
        row = result["rows"][0]
        self.assertEqual(row["MappingConfigStatus"], "complete")
        self.assertEqual(row["MappingConfigCount"], 1)
        self.assertTrue(row["MappedTableExists"])
        self.assertEqual(
            row["MappingConfigSha256"],
            "1fe90d3f0da676f908578c000c593c6481a254ac27cff51f680729c50e8ca94f",
        )
        self.assertRegex(row["MappingConfigSha256"], r"^[0-9a-f]{64}$")
        rendered = json.dumps(result, ensure_ascii=False)
        for private_value in (
            "ORIGINALKEYDATA_TEST",
            "中文",
            "private-task-check-item-id",
            "private-check-item-id",
            "private-document-id",
        ):
            self.assertNotIn(private_value, rendered)
        self.assertNotIn("DataTableName", rendered)
        self.assertNotIn("ConfigValue", rendered)

    def test_mapping_config_fingerprint_rejects_unsafe_database_states(self) -> None:
        base = {
            "TaskCheckItemID": "private-task-check-item-id",
            "CheckItemID": "private-check-item-id",
            "DocumentID": "private-document-id",
            "MappingCount": 1,
            "DataTableName": "ORIGINALKEYDATA_TEST",
            "MappedTableExists": 1,
            "ConfigPresent": 1,
            "SeqNum": 1,
            "KeyDataField": "A",
            "KeyDataType": "String",
            "ConfigValue": "private-config",
            "ConfigValue_En": None,
            "ConfigValue_CnEn": None,
            "ConfigValue_NewCnEn": None,
        }

        def values(**overrides: Any) -> tuple[Any, ...]:
            row = {**base, **overrides}
            return tuple(row[column] for column in probe.MAPPING_CONFIG_RAW_COLUMNS)

        cases = (
            ("mapping_missing", [values(MappingCount=0)]),
            ("mapping_not_unique", [values(MappingCount=2)]),
            (
                "mapping_table_name_invalid",
                [values(DataTableName="unsafe_table")],
            ),
            (
                "config_missing",
                [
                    values(
                        ConfigPresent=0,
                        SeqNum=None,
                        KeyDataField=None,
                        KeyDataType=None,
                        ConfigValue=None,
                    )
                ],
            ),
            (
                "duplicate_config_sort_key",
                [
                    values(),
                    values(
                        ConfigValue="second-private-config",
                    ),
                ],
            ),
        )
        for expected_reason, raw_rows in cases:
            with self.subTest(expected_reason=expected_reason):
                result = probe._fingerprint_mapping_config_rows(
                    probe.MAPPING_CONFIG_RAW_COLUMNS,
                    raw_rows,
                )
                self.assertEqual(len(result), 1)
                row = result[0]
                self.assertEqual(row["MappingConfigStatus"], "incomplete")
                self.assertIsNone(row["MappingConfigCount"])
                self.assertIsNone(row["MappingConfigSha256"])
                self.assertEqual(row["MappingConfigReason"], expected_reason)
                rendered = json.dumps(result, ensure_ascii=False)
                self.assertNotIn("ORIGINALKEYDATA_TEST", rendered)
                self.assertNotIn("private-config", rendered)

        missing_table = probe._fingerprint_mapping_config_rows(
            probe.MAPPING_CONFIG_RAW_COLUMNS,
            [values(MappedTableExists=0)],
        )[0]
        self.assertEqual(missing_table["MappingConfigStatus"], "complete")
        self.assertFalse(missing_table["MappedTableExists"])
        self.assertRegex(
            missing_table["MappingConfigSha256"],
            r"^[0-9a-f]{64}$",
        )

        shared_sequence = probe._fingerprint_mapping_config_rows(
            probe.MAPPING_CONFIG_RAW_COLUMNS,
            [
                values(),
                values(
                    KeyDataField="B",
                    ConfigValue="second-private-config",
                ),
            ],
        )[0]
        self.assertEqual(shared_sequence["MappingConfigStatus"], "complete")
        self.assertEqual(shared_sequence["MappingConfigCount"], 2)

    def test_final_entry_view_fails_closed_for_mapping_config_anomalies(self) -> None:
        task_check_item_id = "sha256:tci"
        check_item_id = "sha256:item"
        document_id = "sha256:document"

        def base_results() -> dict[str, dict[str, Any]]:
            results = empty_final_entry_results()
            results["task_check_items"] = ok_result(
                [
                    {
                        "ID": task_check_item_id,
                        "TaskID": "sha256:task",
                        "CheckItemID": check_item_id,
                        "CheckItemNo": "B.2",
                        "CheckItemName": "Excel 项目",
                        "CheckCount": 1,
                    }
                ]
            )
            results["task_entry_routes"] = ok_result(
                [
                    {
                        "TaskCheckItemID": task_check_item_id,
                        "TaskID": "sha256:task",
                        "CheckItemID": check_item_id,
                        "OriginalDataInputUIClassName": None,
                    }
                ]
            )
            results["check_record_templates"] = ok_result(
                [
                    {
                        "TaskCheckItemID": task_check_item_id,
                        "CheckItemID": check_item_id,
                        "DocumentID": document_id,
                        "DocumentName": path_reference("template.xls"),
                    }
                ]
            )
            results["check_record_register"] = ok_result(
                [
                    {
                        "ID": "sha256:register",
                        "SampleNo": "260000001",
                        "CheckItemID": check_item_id,
                        "TemplateFilename": path_reference("template.xls"),
                        "OriginalDataFilename": path_reference("result.xls"),
                    }
                ]
            )
            results["original_key_data"] = ok_result(
                [
                    {
                        "CheckItemID": check_item_id,
                        "OriginalRecordID": "sha256:register",
                        "SeqNum": 1,
                        "CheckResult": "result",
                    }
                ]
            )
            return results

        anomaly_states: tuple[tuple[str, dict[str, Any]], ...] = (
            (
                "mapping_query_incomplete",
                {
                    "status": "query_error",
                    "row_count": 0,
                    "rows": [],
                    "error": {"type": "DatabaseError", "code": "ORA-00942"},
                },
            ),
            (
                "config_missing",
                ok_result(
                    [
                        {
                            "TaskCheckItemID": task_check_item_id,
                            "CheckItemID": check_item_id,
                            "DocumentID": document_id,
                            "MappingConfigStatus": "incomplete",
                            "MappingConfigCount": None,
                            "MappingConfigSha256": None,
                            "MappingConfigReason": "config_missing",
                            "MappedTableExists": False,
                        }
                    ]
                ),
            ),
            (
                "mapping_fingerprint_missing",
                ok_result(
                    [
                        {
                            "TaskCheckItemID": task_check_item_id,
                            "CheckItemID": check_item_id,
                            "DocumentID": "sha256:other-document",
                            "MappingConfigStatus": "complete",
                            "MappingConfigCount": 1,
                            "MappingConfigSha256": "b" * 64,
                            "MappingConfigReason": None,
                            "MappedTableExists": True,
                        }
                    ]
                ),
            ),
        )
        for expected_reason, mapping_state in anomaly_states:
            with self.subTest(expected_reason=expected_reason):
                results = base_results()
                results[probe.MAPPING_CONFIG_QUERY_KEY] = mapping_state
                view = probe.build_final_entry_view("260000001", results)
                project = view["projects"][0]
                mapping_config = project["configured_templates"][0][
                    "mapping_config"
                ]
                self.assertEqual(view["status"], "incomplete")
                self.assertEqual(project["status"], "incomplete")
                self.assertEqual(mapping_config["status"], "incomplete")
                self.assertIsNone(
                    mapping_config["expected_mapping_config_sha256"]
                )
                self.assertEqual(mapping_config["reason"], expected_reason)
                self.assertIn(
                    "template_mapping_config_incomplete",
                    {
                        reason["code"]
                        for reason in project["incomplete_reasons"]
                    },
                )

    def test_mapping_query_failure_does_not_invalidate_generic_project(self) -> None:
        results = empty_final_entry_results()
        results["task_check_items"] = ok_result(
            [
                {
                    "ID": "sha256:tci",
                    "TaskID": "sha256:task",
                    "CheckItemID": "sha256:item",
                    "CheckItemNo": "A.1",
                    "CheckItemName": "通用项目",
                    "CheckCount": 0,
                }
            ]
        )
        results["task_entry_routes"] = ok_result(
            [
                {
                    "TaskCheckItemID": "sha256:tci",
                    "TaskID": "sha256:task",
                    "CheckItemID": "sha256:item",
                    "OriginalDataInputUIClassName": (
                        "Toone.FibreCheck.OriRecord.CurrencyItem."
                        "CurrencyItemRecordUI"
                    ),
                }
            ]
        )
        results[probe.MAPPING_CONFIG_QUERY_KEY] = {
            "status": "query_error",
            "row_count": 0,
            "rows": [],
            "error": {"type": "DatabaseError", "code": "ORA-00942"},
        }

        view = probe.build_final_entry_view("260000001", results)

        self.assertEqual(view["status"], "complete")
        self.assertEqual(view["projects"][0]["status"], "complete")
        self.assertEqual(view["incomplete_queries"], [])
        self.assertEqual(
            view["writer_preflight_incomplete_queries"][0]["query"],
            probe.MAPPING_CONFIG_QUERY_KEY,
        )

    def test_referenced_template_matching_uses_path_hash_not_basename(self) -> None:
        results = empty_final_entry_results()
        results["task_check_items"] = ok_result(
            [
                {
                    "ID": "sha256:tci",
                    "TaskID": "sha256:task",
                    "CheckItemID": "sha256:item",
                    "CheckItemNo": "B.2",
                    "CheckItemName": "Excel 项目",
                    "CheckCount": 1,
                }
            ]
        )
        results["task_entry_routes"] = ok_result(
            [
                {
                    "TaskCheckItemID": "sha256:tci",
                    "TaskID": "sha256:task",
                    "CheckItemID": "sha256:item",
                    "OriginalDataInputUIClassName": None,
                }
            ]
        )
        results["check_record_templates"] = ok_result(
            [
                {
                    "TaskCheckItemID": "sha256:tci",
                    "CheckItemID": "sha256:item",
                    "DocumentID": "sha256:document",
                    "DocumentName": path_reference(
                        "same.xls",
                        scope="configured",
                    ),
                }
            ]
        )
        results[probe.MAPPING_CONFIG_QUERY_KEY] = ok_result(
            [
                {
                    "TaskCheckItemID": "sha256:tci",
                    "CheckItemID": "sha256:item",
                    "DocumentID": "sha256:document",
                    "MappingConfigStatus": "complete",
                    "MappingConfigCount": 1,
                    "MappingConfigSha256": "c" * 64,
                    "MappingConfigReason": None,
                    "MappedTableExists": False,
                }
            ]
        )
        results["check_record_register"] = ok_result(
            [
                {
                    "ID": "sha256:register",
                    "SampleNo": "260000001",
                    "CheckItemID": "sha256:item",
                    "TemplateFilename": path_reference(
                        "same.xls",
                        scope="registered",
                    ),
                    "OriginalDataFilename": path_reference("result.xls"),
                }
            ]
        )
        results["original_key_data"] = ok_result(
            [
                {
                    "CheckItemID": "sha256:item",
                    "OriginalRecordID": "sha256:register",
                    "SeqNum": 1,
                    "CheckResult": "result",
                }
            ]
        )

        view = probe.build_final_entry_view("260000001", results)
        project = view["projects"][0]

        self.assertEqual(project["unique_template_names"], ["same.xls"])
        self.assertFalse(
            project["configured_templates"][0][
                "referenced_by_existing_records"
            ]
        )
        self.assertEqual(project["status"], "incomplete")
        self.assertIn(
            "referenced_template_configuration_missing",
            {reason["code"] for reason in project["incomplete_reasons"]},
        )

    def test_document_template_metadata_is_redacted(self) -> None:
        raw_index = "11111111-1111-1111-1111-111111111111"
        raw_path = r"\\server\templates\record.xls"

        sanitized_index = probe.sanitize_scalar("DocumentUploadIndex", raw_index)
        sanitized_name = probe.sanitize_scalar("DocumentName", raw_path)

        self.assertNotEqual(sanitized_index, raw_index)
        self.assertTrue(str(sanitized_index).startswith("sha256:"))
        self.assertEqual(sanitized_name["items"][0]["basename"], "record.xls")
        self.assertNotIn("server", json.dumps(sanitized_name))

    def test_config_profile_does_not_expose_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            install_dir = self.make_fibrecheck_dir(Path(temp_dir))
            profile = probe.load_primary_profile(install_dir)
            self.assertEqual(profile.user, "APPUSER")
            self.assertEqual(profile.password, "super-secret")
            rendered = json.dumps(profile.public_metadata())
            self.assertNotIn("super-secret", rendered)
            self.assertNotIn("APPUSER", rendered)
            self.assertNotIn("db.internal", rendered)

    def test_read_only_transaction_is_first_sql_and_rolls_back(self) -> None:
        connection = FakeConnection()
        result = probe.ReadOnlyProbeRunner(connection).run("260187115")
        self.assertTrue(result["read_only_transaction_started"])
        self.assertEqual(
            connection.executed[0],
            (probe.READ_ONLY_TRANSACTION_SQL, None),
        )
        self.assertEqual(connection.rollback_count, 1)
        self.assertFalse(connection.autocommit)
        for sql, parameters in connection.executed[1:]:
            probe.assert_read_only_sql(sql)
            self.assertIsNotNone(parameters)

    def test_read_only_transaction_failure_stops_all_queries(self) -> None:
        connection = FailingReadOnlyConnection()
        with self.assertRaises(probe.ProbeError):
            probe.ReadOnlyProbeRunner(connection).run("260187115")
        self.assertEqual(len(connection.executed), 1)
        self.assertEqual(connection.executed[0][0], probe.READ_ONLY_TRANSACTION_SQL)
        self.assertEqual(connection.rollback_count, 1)

    def test_result_rows_are_redacted(self) -> None:
        connection = FakeConnection()
        result = probe.ReadOnlyProbeRunner(connection).run("260187115")
        row = result["results"]["special_wool_exact"]["rows"][0]
        serialized = json.dumps(row, ensure_ascii=False)
        self.assertNotIn("11111111-1111-1111-1111-111111111111", serialized)
        self.assertNotIn(r"\\server\share", serialized)
        self.assertNotIn('"lisy"', serialized)
        self.assertNotIn("BA3DD04BC2BB44A2AC9C58AE5F28664B", serialized)
        self.assertNotIn(r"\\server\outpdf$", serialized)
        self.assertEqual(row["FilePath"]["items"][0]["basename"], "record.xls")
        self.assertEqual(row["ReportName"]["items"][0]["basename"], "report.pdf")
        self.assertEqual(row["LoginName"], "l**y")

    def test_manifest_mode_never_calls_connector(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            install_dir = self.make_fibrecheck_dir(root)
            output = root / "manifest.json"

            def forbidden_connector(
                profile: probe.OracleProfile,
                client_dir: Path | None,
            ) -> Any:
                raise AssertionError("manifest mode must not connect")

            exit_code = probe.run_cli(
                [
                    "--fibrecheck-dir",
                    str(install_dir),
                    "--sample-no",
                    "260187115",
                    "--manifest",
                    "--output",
                    str(output),
                ],
                connector=forbidden_connector,
            )
            self.assertEqual(exit_code, 0)
            document = json.loads(output.read_text(encoding="utf-8"))
            self.assertFalse(document["connection_attempted"])
            self.assertEqual(len(document["query_manifest"]), len(probe.QUERIES))
            rendered = output.read_text(encoding="utf-8")
            self.assertNotIn("super-secret", rendered)
            self.assertNotIn("APPUSER", rendered)
            self.assertNotIn("db.internal", rendered)

    def test_suffixed_number_prefix_check_includes_base_record(self) -> None:
        parameters = probe.query_parameters("260187115-1")

        self.assertEqual(parameters["sample_no"], "260187115-1")
        self.assertEqual(parameters["sample_prefix"], "260187115%")

    def test_cli_probe_closes_connection_and_redacts_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            install_dir = self.make_fibrecheck_dir(root)
            output = root / "probe.json"
            connection = FakeConnection()

            def fake_connector(
                profile: probe.OracleProfile,
                client_dir: Path | None,
            ) -> FakeConnection:
                return connection

            exit_code = probe.run_cli(
                [
                    "--fibrecheck-dir",
                    str(install_dir),
                    "--sample-no",
                    "260187115",
                    "--output",
                    str(output),
                ],
                connector=fake_connector,
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(connection.close_count, 1)
            rendered = output.read_text(encoding="utf-8")
            self.assertNotIn("super-secret", rendered)
            self.assertNotIn("APPUSER", rendered)
            self.assertNotIn("db.internal", rendered)
            self.assertNotIn(r"\\server\share", rendered)

    def test_data_source_override_is_validated_and_applied(self) -> None:
        self.assertEqual(
            probe.validate_data_source_override("192.0.2.10/orcl/"),
            "192.0.2.10/orcl",
        )
        for invalid in (
            "",
            "db.internal",
            "db.internal/orcl;drop",
            "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)))",
            "db.internal/orcl' OR '1'='1",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(probe.ProbeError):
                    probe.validate_data_source_override(invalid)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            install_dir = self.make_fibrecheck_dir(root)
            output = root / "probe.json"
            seen: dict[str, str] = {}

            def fake_connector(
                profile: probe.OracleProfile,
                client_dir: Path | None,
            ) -> FakeConnection:
                seen["data_source"] = profile.data_source
                return FakeConnection()

            exit_code = probe.run_cli(
                [
                    "--fibrecheck-dir",
                    str(install_dir),
                    "--sample-no",
                    "260187115",
                    "--data-source",
                    "192.0.2.10/orcl",
                    "--output",
                    str(output),
                ],
                connector=fake_connector,
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(seen["data_source"], "192.0.2.10/orcl")
            document = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(document["profile"]["data_source_overridden"])
            rendered = output.read_text(encoding="utf-8")
            self.assertNotIn("db.internal", rendered)
            self.assertNotIn("192.0.2.10", rendered)
            self.assertNotIn("super-secret", rendered)

    def test_credential_profile_loads_alternate_entry(self) -> None:
        web_config = """<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <appSettings>
    <add key="PanYuJianWu" value="DATA SOURCE=192.0.2.20/orcl;USER ID=WEBUSER;PASSWORD=web-secret"/>
  </appSettings>
</configuration>
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            install_dir = self.make_fibrecheck_dir(root)
            (install_dir / "WebService.dll.config").write_text(
                web_config,
                encoding="utf-8",
            )
            output = root / "probe.json"
            seen: dict[str, str] = {}

            def fake_connector(
                profile: probe.OracleProfile,
                client_dir: Path | None,
            ) -> FakeConnection:
                seen["user"] = profile.user
                seen["data_source"] = profile.data_source
                return FakeConnection()

            exit_code = probe.run_cli(
                [
                    "--fibrecheck-dir",
                    str(install_dir),
                    "--sample-no",
                    "260187115",
                    "--credential-profile",
                    "WebService.dll.config:PanYuJianWu",
                    "--output",
                    str(output),
                ],
                connector=fake_connector,
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(seen["user"], "WEBUSER")
            self.assertEqual(seen["data_source"], "192.0.2.20/orcl")
            rendered = output.read_text(encoding="utf-8")
            self.assertNotIn("web-secret", rendered)
            self.assertNotIn("WEBUSER", rendered)

            for bad_spec in (
                "WebService.dll.config",
                ":PanYuJianWu",
                "../secret.config:PanYuJianWu",
                "WebService.dll.config:Missing",
            ):
                with self.subTest(bad_spec=bad_spec):
                    bad_output = root / "bad.json"
                    exit_code = probe.run_cli(
                        [
                            "--fibrecheck-dir",
                            str(install_dir),
                            "--sample-no",
                            "260187115",
                            "--credential-profile",
                            bad_spec,
                            "--output",
                            str(bad_output),
                        ],
                        connector=fake_connector,
                    )
                    self.assertEqual(exit_code, 2)
                    rendered = bad_output.read_text(encoding="utf-8")
                    self.assertNotIn("web-secret", rendered)


if __name__ == "__main__":
    unittest.main()
