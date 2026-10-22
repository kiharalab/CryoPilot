import os
import unittest
from pathlib import Path
from unittest import mock

import harness


class ProcessingGuideSelectionTests(unittest.TestCase):
    def test_default_is_the_original_guide(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CRYOEM_GUIDE", None)
            self.assertEqual(
                harness.resolve_cryoem_guide(), harness.CRYOEM_GUIDES["original"]
            )

    def test_environment_and_argument_select_a_guide(self):
        with mock.patch.dict(os.environ, {"CRYOEM_GUIDE": "v5"}):
            self.assertEqual(
                harness.resolve_cryoem_guide(), harness.CRYOEM_GUIDES["v5"]
            )
            self.assertEqual(
                harness.resolve_cryoem_guide("original"),
                harness.CRYOEM_GUIDES["original"],
            )
        self.assertEqual(
            harness.resolve_cryoem_guide("~/notes/guide.md"),
            Path("~/notes/guide.md").expanduser(),
        )
        with self.assertRaisesRegex(ValueError, "Unknown processing guide"):
            harness.resolve_cryoem_guide("v6")

    def test_every_bundled_guide_loads_with_sections(self):
        for name, path in harness.CRYOEM_GUIDES.items():
            with self.subTest(guide=name):
                guide = harness.load_cryoem_data_processing(path)
                self.assertIsNone(guide["load_error"])
                self.assertGreater(len(guide["sections"]), 10)


if __name__ == "__main__":
    unittest.main()
