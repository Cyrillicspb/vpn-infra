#!/usr/bin/env python3
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "home" / "scripts" / "update-dpi-presets.py"
SPEC = importlib.util.spec_from_file_location("update_dpi_presets", MODULE_PATH)
update_dpi_presets = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(update_dpi_presets)


class DpiPresetUpdateTests(unittest.TestCase):
    def test_parse_v2fly_domains_skips_unsupported_entries(self) -> None:
        sample = """
        # comment
        youtube.com
        full:youtube.googleapis.com
        domain:googlevideo.com
        domain:ggpht.cn
        keyword:youtube
        regexp:^googlevideo
        include:google
        api.youtube.com
        accounts.youtube.com
        """

        parsed = update_dpi_presets._parse_v2fly_domains(sample)

        self.assertEqual(parsed, ["youtube.com", "youtube.googleapis.com", "googlevideo.com"])

    def test_build_presets_merges_core_and_filtered_v2fly_domains(self) -> None:
        fallback = {
            "youtube": {
                "display": "YouTube",
                "domains": [
                    "youtube.com",
                    "googlevideo.com",
                    "youtubei.googleapis.com",
                    "gvt1.com",
                    "gstatic.com",
                    "yt3.googleusercontent.com",
                ],
            }
        }
        v2fly_text = "\n".join(
            [
                "youtube.com",
                "wide-youtube.l.google.com",
                "youtube.googleapis.com",
                "withyoutube.com",
                "accounts.youtube.com",
                "googleapis.com",
                "full:youtube-nocookie.com",
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            fallback_path = Path(tmpdir) / "presets-default.json"
            fallback_path.write_text(json.dumps(fallback), encoding="utf-8")
            with mock.patch.object(update_dpi_presets, "FALLBACK_PRESETS_CANDIDATES", [fallback_path]):
                with mock.patch.object(update_dpi_presets, "_fetch_text", return_value=v2fly_text):
                    presets = update_dpi_presets.build_presets()

        self.assertEqual(
            presets["youtube"]["domains"],
            [
                "youtube.com",
                "googlevideo.com",
                "youtubei.googleapis.com",
                "gvt1.com",
                "gstatic.com",
                "yt3.googleusercontent.com",
                "wide-youtube.l.google.com",
                "youtube.googleapis.com",
                "withyoutube.com",
                "youtube-nocookie.com",
            ],
        )

    def test_youtube_service_uses_larger_domain_cap(self) -> None:
        core = ["youtube.com", "googlevideo.com"]
        tier2 = [f"youtube{i}.example.com" for i in range(400)]

        merged = update_dpi_presets._merge_service_domains("youtube", core, tier2)

        self.assertEqual(len(merged), 256)
        self.assertEqual(merged[:2], core)


if __name__ == "__main__":
    unittest.main()
