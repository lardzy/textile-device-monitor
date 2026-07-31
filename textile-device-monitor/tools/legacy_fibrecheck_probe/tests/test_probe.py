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
