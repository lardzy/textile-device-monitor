"""Build-only Pack checks: no database, runtime registry, or application fixtures.

Run with ``python -m unittest discover -s build_tests -v`` from backend/.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.execution.v2.canonical import canonical_sha256, resource_set_digest
from prepare_execution_packs import prepare_execution_packs


def pack_digest(root: Path, document: dict) -> str:
    manifest = dict(document)
    manifest.pop("distribution_digest", None)
    resources = [(name, (root / name).read_bytes()) for name in manifest["resource_paths"]]
    return canonical_sha256(
        {"manifest": manifest, "resources_digest": resource_set_digest(resources)}
    )


class PrepareExecutionPacksTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="execution-pack-build-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "execution"
        self.manifests = self.root / "v2/resources/manifests"
        self.manifests.mkdir(parents=True)

    def write(self, relative_path: str, content: bytes) -> Path:
        target = self.root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def seal(self, name: str, paths: list[str]) -> dict:
        document = {
            "manifest_schema_version": "1.0",
            "pack_id": f"fixture.{name}",
            "pack_version": "1.0.0",
            "engine_version_range": ">=2.5.0 <3.0.0",
            "provides": {"nodes": [], "connectors": [], "capabilities": []},
            "assets": [],
            "resource_paths": paths,
        }
        document["distribution_digest"] = pack_digest(self.root, document)
        self.write(
            f"v2/resources/manifests/{name}.json",
            (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        )
        return document

    def inventory(self) -> dict[str, bytes]:
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }

    def test_valid_pack_and_undeclared_files_are_unchanged(self):
        self.write("node.py", b"def main():\n    return 1\n")
        self.seal("valid", ["node.py"])
        self.write("not_in_pack.py", b"leave_this_alone = True\r\n")
        before = self.inventory()

        self.assertEqual(prepare_execution_packs(self.root), [])
        self.assertEqual(self.inventory(), before)

    def test_build_helper_does_not_import_database_module(self):
        self.assertNotIn("app.database", sys.modules)

    def test_shared_resources_are_repaired_once_across_packs(self):
        original = {
            "shared.py": b"VALUE = 1\n",
            "first.json": b'{"label": "sample"}\n',
            "second.py": b"VALUE = 2\n",
        }
        for name, content in original.items():
            self.write(name, content)
        first = self.seal("a_first", ["shared.py", "first.json"])
        second = self.seal("b_second", ["shared.py", "second.py"])
        for name, content in original.items():
            self.write(name, content.replace(b"\n", b"\r\n"))
        manifests_before = {path: path.read_bytes() for path in self.manifests.iterdir()}

        changed = prepare_execution_packs(self.root)

        self.assertCountEqual(changed, list(original))
        self.assertEqual(len(changed), len(set(changed)))
        for name, content in original.items():
            self.assertEqual((self.root / name).read_bytes(), content)
        for document in (first, second):
            self.assertEqual(pack_digest(self.root, document), document["distribution_digest"])
        self.assertEqual({path: path.read_bytes() for path in self.manifests.iterdir()}, manifests_before)

    def test_binary_crlf_bytes_are_preserved_while_text_is_repaired(self):
        binary = b"\xd0\xcf\x11\xe0\x00workbook\r\n\x00\xff\r\n"
        self.write("template.xls", binary)
        self.write("renderer.py", b"VALUE = 'template.xls'\n")
        document = self.seal("workbook", ["renderer.py", "template.xls"])
        self.write("renderer.py", b"VALUE = 'template.xls'\r\n")

        self.assertEqual(prepare_execution_packs(self.root), ["renderer.py"])
        self.assertEqual((self.root / "template.xls").read_bytes(), binary)
        self.assertEqual(pack_digest(self.root, document), document["distribution_digest"])

    def test_semantic_drift_rejects_all_packs_before_any_file_is_written(self):
        self.write("first.py", b"VALUE = 1\n")
        self.write("second.py", b"VALUE = 2\n")
        self.seal("a_repairable", ["first.py"])
        self.seal("z_modified", ["second.py"])
        self.write("first.py", b"VALUE = 1\r\n")
        self.write("second.py", b"VALUE = 3\r\n")
        before = self.inventory()

        with self.assertRaises(ValueError):
            prepare_execution_packs(self.root)

        self.assertEqual(self.inventory(), before)

    def test_valid_sealed_crlf_text_is_not_rewritten(self):
        self.write("intentional.py", b"VALUE = 1\r\n")
        self.seal("already_valid", ["intentional.py"])
        before = self.inventory()

        self.assertEqual(prepare_execution_packs(self.root), [])
        self.assertEqual(self.inventory(), before)

    def test_conflicting_shared_resource_seals_reject_before_writing(self):
        self.write("shared.py", b"VALUE = 1\n")
        self.seal("a_requires_lf", ["shared.py"])
        self.write("shared.py", b"VALUE = 1\r\n")
        self.seal("b_requires_crlf", ["shared.py"])
        before = self.inventory()

        with self.assertRaises(ValueError):
            prepare_execution_packs(self.root)

        self.assertEqual(self.inventory(), before)

    def test_repeated_preparation_is_idempotent(self):
        self.write("node.py", b"VALUE = 1\n")
        self.seal("repeat", ["node.py"])
        self.write("node.py", b"VALUE = 1\r\n")

        self.assertEqual(prepare_execution_packs(self.root), ["node.py"])
        after_first = self.inventory()
        self.assertEqual(prepare_execution_packs(self.root), [])
        self.assertEqual(self.inventory(), after_first)

    def test_windows_checkout_restores_all_real_pack_bytes_and_frozen_digests(self):
        source = BACKEND_ROOT / "app/execution"
        documents = []
        originals = {}
        for path in sorted((source / "v2/resources/manifests").glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            documents.append(document)
            self.assertEqual(pack_digest(source, document), document["distribution_digest"])
            # Git can also check out manifests with CRLF. Their canonical JSON
            # identity is unchanged, so preparation must not reseal them.
            self.write(
                path.relative_to(source).as_posix(),
                path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"),
            )
            for name in document["resource_paths"]:
                originals[name] = (source / name).read_bytes()
        self.assertEqual(len(documents), 5)
        changed_expected = set()
        for name, original in originals.items():
            windows_bytes = original
            if name.endswith((".py", ".json")):
                windows_bytes = original.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            self.write(name, windows_bytes)
            if windows_bytes != original:
                changed_expected.add(name)
        snapshot_name = "v2/resources/snapshots/execution-v2-contracts.json"
        snapshot_bytes = (source / snapshot_name).read_bytes()
        self.write(snapshot_name, snapshot_bytes)
        manifest_bytes = {path: path.read_bytes() for path in self.manifests.iterdir()}

        changed = prepare_execution_packs(self.root)

        self.assertCountEqual(changed, changed_expected)
        for name, original in originals.items():
            with self.subTest(resource=name):
                self.assertEqual((self.root / name).read_bytes(), original)
        calculated = {
            document["pack_id"]: pack_digest(self.root, document)
            for document in documents
        }
        for document in documents:
            self.assertEqual(calculated[document["pack_id"]], document["distribution_digest"])
        # Published capabilities keep their frozen pack/implementation inputs:
        # every implementation resource is byte-identical and the snapshot is
        # untouched, including implementation_digest and contract_digest.
        self.assertEqual((self.root / snapshot_name).read_bytes(), snapshot_bytes)
        snapshot = json.loads(snapshot_bytes)
        for entry in snapshot["packs"] + snapshot["nodes"] + snapshot["connectors"]:
            self.assertEqual(entry["distribution_digest"], calculated[entry["pack_id"]])
        self.assertEqual({path: path.read_bytes() for path in self.manifests.iterdir()}, manifest_bytes)
        self.assertEqual(prepare_execution_packs(self.root), [])


if __name__ == "__main__":
    unittest.main()
