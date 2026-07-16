from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.crud import area as area_crud
from app.models import SystemConfig


class AreaConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        SystemConfig.__table__.create(self.engine)
        self.session = sessionmaker(bind=self.engine)()

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def test_folder_blacklist_defaults_and_persists_empty_list(self):
        initial = area_crud.get_area_config(self.session)
        self.assertEqual(initial["folder_blacklist"], [".recycle", "旧"])

        updated = area_crud.update_area_config(
            self.session,
            root_path="/data",
            old_root_path="/old",
            result_output_root="/output",
            model_mapping={"model": "model.pth"},
            inference_defaults={},
            folder_blacklist=[],
        )
        self.assertEqual(updated["folder_blacklist"], [])
        self.assertEqual(area_crud.get_area_config(self.session)["folder_blacklist"], [])

    def test_folder_blacklist_is_normalized_and_old_clients_preserve_it(self):
        updated = area_crud.update_area_config(
            self.session,
            root_path="/data",
            old_root_path="/old",
            result_output_root="/output",
            model_mapping={"model": "model.pth"},
            inference_defaults={},
            folder_blacklist=[" .recycle ", "旧", "OLD", "old", "../invalid"],
        )
        self.assertEqual(updated["folder_blacklist"], [".recycle", "旧", "OLD"])

        preserved = area_crud.update_area_config(
            self.session,
            root_path="/data-2",
            old_root_path="/old",
            result_output_root="/output",
            model_mapping={"model": "model.pth"},
            inference_defaults={},
            folder_blacklist=None,
        )
        self.assertEqual(preserved["folder_blacklist"], [".recycle", "旧", "OLD"])


if __name__ == "__main__":
    unittest.main()
