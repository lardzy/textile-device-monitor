from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from app.api.area import _with_editor_image_urls, download_area_excel


class AreaEditorApiTests(unittest.TestCase):
    def test_overlay_url_is_versioned_after_save_or_reset(self) -> None:
        detail = {
            "image": {
                "overlay_filename": "sample_overlay.png",
                "edit_version": 3,
            }
        }

        payload = _with_editor_image_urls("job-1", 7, detail)

        self.assertEqual(
            payload["image"]["overlay_url"],
            "/api/area/jobs/job-1/artifacts/image/sample_overlay.png"
            "?v=3&mapping=semantic-v1",
        )
        self.assertEqual(
            payload["image"]["source_url"],
            "/api/area/jobs/job-1/editor/images/7/source",
        )
        self.assertNotIn("overlay_url", detail["image"])

    def test_excel_download_uses_the_generated_template_filename(self) -> None:
        path = Path("/tmp/26X910095-1-面积法-定量试验原始记录-新系统.xls")
        with (
            patch("app.api.area._ensure_enabled"),
            patch("app.api.area.area_job_manager.get_excel_path", return_value=path),
            patch("app.api.area.FileResponse", return_value="response") as response,
        ):
            self.assertEqual(download_area_excel("job-1"), "response")

        self.assertEqual(response.call_args.kwargs["path"], path)
        self.assertEqual(response.call_args.kwargs["filename"], path.name)


if __name__ == "__main__":
    unittest.main()
