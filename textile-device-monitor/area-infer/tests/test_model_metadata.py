from __future__ import annotations

import unittest

from app.model_metadata import (
    resolve_model_classes,
    resolve_requested_class_mapping,
)


class ModelMetadataTests(unittest.TestCase):
    def test_viscose_lyocell_uses_actual_weight_index_order(self) -> None:
        classes, trusted = resolve_model_classes(
            model_name="粘纤-莱赛尔",
            model_file="b_v1_1.3.pth",
        )

        self.assertTrue(trusted)
        self.assertEqual(classes, ("莱赛尔", "粘纤"))

    def test_cotton_lyocell_uses_actual_weight_index_order(self) -> None:
        classes, trusted = resolve_model_classes(
            model_name="棉-莱赛尔",
            model_file="b_c1_1.3.pth",
        )

        self.assertTrue(trusted)
        self.assertEqual(classes, ("莱赛尔", "棉"))

    def test_unknown_custom_weight_falls_back_to_display_order(self) -> None:
        classes, trusted = resolve_model_classes(
            model_name="棉-莱-莫",
            model_file="custom.pth",
        )

        self.assertFalse(trusted)
        self.assertEqual(classes, ("棉", "莱赛尔", "莫代尔"))

        requested_classes, mapping = resolve_requested_class_mapping(
            model_name="棉-莱-莫",
            model_file="custom.pth",
            class_mapping_version=1,
        )
        self.assertEqual(requested_classes, classes)
        self.assertEqual(mapping, "caller_display_order_v1")

    def test_known_model_name_cannot_use_unregistered_weight_file(self) -> None:
        with self.assertRaisesRegex(ValueError, "model_name_file_mismatch"):
            resolve_model_classes(
                model_name="粘纤-莱赛尔",
                model_file="renamed.pth",
            )

    def test_old_backend_keeps_legacy_order_during_rolling_upgrade(self) -> None:
        classes, mapping = resolve_requested_class_mapping(
            model_name="粘纤-莱赛尔",
            model_file="b_v1_1.3.pth",
            class_mapping_version=None,
        )

        self.assertEqual(classes, ("粘纤", "莱赛尔"))
        self.assertEqual(mapping, "legacy_display_order")

    def test_new_backend_opts_into_trusted_weight_order(self) -> None:
        classes, mapping = resolve_requested_class_mapping(
            model_name="粘纤-莱赛尔",
            model_file="b_v1_1.3.pth",
            class_mapping_version=1,
        )

        self.assertEqual(classes, ("莱赛尔", "粘纤"))
        self.assertEqual(mapping, "trusted_metadata_v1")

    def test_known_model_name_and_weight_mismatch_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "model_name_file_mismatch"):
            resolve_model_classes(
                model_name="粘纤-莱赛尔",
                model_file="b_c1_1.3.pth",
            )

    def test_unknown_name_cannot_silently_relabel_known_weight(self) -> None:
        with self.assertRaisesRegex(ValueError, "model_name_file_mismatch"):
            resolve_model_classes(
                model_name="棉-莫代尔",
                model_file="b_v1_1.3.pth",
            )

    def test_equivalent_expanded_alias_name_is_accepted(self) -> None:
        classes, trusted = resolve_model_classes(
            model_name="棉-粘纤-莱赛尔-莫代尔",
            model_file="b_cvlm_1.3.pth",
        )

        self.assertTrue(trusted)
        self.assertEqual(classes, ("棉", "粘纤", "莱赛尔", "莫代尔"))

if __name__ == "__main__":
    unittest.main()
