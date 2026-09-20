from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import probe


NUMBER = "26W006701"
ITEM, REGISTER, ORIGINAL = ("sha256:" + c * 16 for c in "abc")


def results():
    rows = {key: [] for key in probe.CHECK_RECORD_QUERY_KEYS}
    rows["check_record_register"] = [{"ID": REGISTER, "SampleNo": NUMBER, "CheckItemID": ITEM,
                                       "OriginalRecordID": ORIGINAL}]
    rows["currency_item_records"] = [{"ID": ORIGINAL, "CheckRecordRegisterID": REGISTER,
                                       "SampleNo": NUMBER, "CheckItemID": ITEM}]
    rows["currency_item_record_details"] = [
        {"ID": "sha256:" + "d" * 16, "CurrencyItemRecordNewID": ORIGINAL, "SeqNum": 1, "RealValue": "木浆"},
        {"ID": "sha256:" + "e" * 16, "CurrencyItemRecordNewID": ORIGINAL, "SeqNum": 2, "RealValue": "竹浆"},
    ]
    rows["original_key_data"] = [{"OriginalRecordID": ORIGINAL, "SampleNo": NUMBER,
                                   "CheckItemID": ITEM, "CheckResult": "木浆、竹浆", "SeqNum": 1}]
    return {key: {"status": "ok", "rows": values, "row_count": len(values)} for key, values in rows.items()}


class RecordReadsTests(unittest.TestCase):
    def test_fingerprint_stable_across_query_order_but_changes_for_every_field_group(self):
        original = results()
        first = probe.build_check_record_snapshot(NUMBER, original)["records"][0]
        self.assertEqual(first["record_ref"], "check-record:" + REGISTER)
        self.assertEqual(first["key_results"][0]["OriginalRecordID"], ORIGINAL)
        self.assertEqual(first["association_issues"], [])
        reordered = deepcopy(original)
        reordered["currency_item_record_details"]["rows"].reverse()
        self.assertEqual(probe.build_check_record_snapshot(NUMBER, reordered)["records"][0], first)
        for key in ["check_record_register", "currency_item_records", "currency_item_record_details", "original_key_data"]:
            with self.subTest(key=key):
                changed = deepcopy(original)
                changed[key]["rows"][0]["Remark"] = "更正后"
                current = probe.build_check_record_snapshot(NUMBER, changed)["records"][0]
                self.assertEqual(current["record_ref"], first["record_ref"])
                self.assertNotEqual(current["content_fingerprint"], first["content_fingerprint"])

    def test_excel_projection_uses_register_identity_and_same_text_never_merges_records(self):
        data = results()
        excel_id = "sha256:" + "f" * 16
        row = {"ID": excel_id, "SampleNo": NUMBER, "CheckItemID": ITEM,
               "TemplateFilename": probe.sanitize_path_value(r"C:\templates\微观形貌.xls")}
        data["check_record_register"]["rows"].append(row)
        data["original_key_data"]["rows"].append({"OriginalRecordID": excel_id, "SampleNo": NUMBER,
                                                   "CheckItemID": ITEM, "CheckResult": "木浆、竹浆", "SeqNum": 1})
        for state in data.values():
            state["row_count"] = len(state["rows"])
        records = probe.build_check_record_snapshot(NUMBER, data)["records"]
        self.assertEqual(len(records), 2)
        excel = next(r for r in records if r["record_kind"] == "excel")
        self.assertEqual(excel["key_results"][0]["OriginalRecordID"], excel_id)
        self.assertIsNone(excel["generic_record"])

    def test_query_failure_truncation_and_orphan_are_not_reported_as_empty(self):
        for defect in ("failed", "missing", "count", "truncated", "orphan"):
            with self.subTest(defect=defect):
                data = results()
                state = data["currency_item_records"]
                if defect == "failed":
                    state["status"] = "error"
                elif defect == "missing":
                    del data["currency_item_records"]
                elif defect == "count":
                    state["row_count"] = 99
                elif defect == "truncated":
                    state["truncated"] = True
                else:
                    state["rows"][0]["CheckRecordRegisterID"] = "sha256:" + "0" * 16
                with self.assertRaises(probe.ProbeError):
                    probe.build_check_record_snapshot(NUMBER, data)

    def test_mismatched_links_are_visible_and_normal_task_scope_is_unchanged(self):
        data = results()
        data["check_record_register"]["rows"][0]["OriginalRecordID"] = REGISTER
        value = probe.build_check_record_snapshot(NUMBER, data)["records"][0]
        self.assertIn("generic_register_link_mismatch", value["association_issues"])
        self.assertFalse(set(probe.TASK_SNAPSHOT_QUERY_KEYS) & set(probe.CHECK_RECORD_QUERY_KEYS))
        self.assertEqual({q.key for q in probe.CHECK_RECORD_QUERIES},
                         set(probe.TASK_SNAPSHOT_QUERY_KEYS + probe.CHECK_RECORD_QUERY_KEYS))
        for query in probe.CHECK_RECORD_QUERIES:
            probe.assert_read_only_sql(query.sql)
