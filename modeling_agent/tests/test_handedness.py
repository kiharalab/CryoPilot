import csv
import json
import tempfile
import unittest
from pathlib import Path

from handedness import compare_scores, compare_stage_scores


class HandednessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def table(self, name, values, settings=None, metric="aa"):
        path = self.root / f"{name}.csv"
        fields = ["chain", "residue_number", "insertion_code", "residue_name", "candidate_index", "atom", metric]
        with path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(fields)
            for i, value in enumerate(values):
                writer.writerow(["A", 10 + i, "", "ALA", 1, "CA", value])
        path.with_suffix(".metadata.json").write_text(json.dumps({
            "metric_type": "daq" if metric == "aa" else "qscore", "map_path": f"/{name}.mrc",
            "settings": settings or {"contour": .1, "seed": 0},
            "reference": {"source_sha256": "fixture", "candidate_index": 1}}))
        return path

    def test_hand_comparison_uses_only_matched_residues_with_explicit_coverage(self):
        # Independently averaged values would favor original due to its unmatched 100.
        original = self.table("original", [1, 100, ""])
        flipped = self.table("flipped", [2, "", 0])
        result = compare_scores(original, flipped, self.root / "comparison.json")
        self.assertTrue(result["flipped_higher"])
        self.assertNotIn("rerun_cryozeta", result)   # the tool reports evidence; the agent decides
        self.assertEqual(result["delta_flipped_minus_original"], 1)
        self.assertEqual(result["coverage"]["matched"], 1)
        self.assertEqual(result["coverage"]["excluded_from_both_means"], 2)
        self.assertTrue(Path(result["paired_scores"]).exists())

    def test_tie_or_worse_is_not_flipped_higher(self):
        for values in ([1, 2], [0, 1]):
            result = compare_scores(self.table("original", [1, 2]), self.table("flipped", values), self.root / "comparison.json")
            self.assertFalse(result["flipped_higher"])

    def test_qscore_comparison_is_per_atom(self):
        result = compare_scores(self.table("original", [.1], metric="qscore"),
                                self.table("flipped", [.2], metric="qscore"), self.root / "comparison.json", "qscore")
        self.assertTrue(result["flipped_higher"])
        self.assertEqual(result["metric"], "qscore")

    def test_mismatched_settings_identity_and_missing_coverage_are_rejected(self):
        original = self.table("original", [1, ""])
        cases = [(self.table("different_settings", [2, ""], {"contour": .2}), "settings differ"),
                 (self.table("different_residues", [2]), "identities differ"),
                 (self.table("no_overlap", ["", 2]), "No jointly scored")]
        for flipped, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                compare_scores(original, flipped, self.root / "comparison.json")

    def test_daq_comparison_uses_19_residue_mean_over_matched_residues(self):
        # 30 residues; flipped is better by 0.2 everywhere except one unmatched spike in the original.
        orig = [0.0] * 30
        flip = [0.2] * 30
        orig[10] = 50.0
        flip[10] = ""
        result = compare_scores(self.table("original", orig), self.table("flipped", flip), self.root / "comparison.json")
        self.assertEqual(result["metric"], "aa_19_residue_mean")
        self.assertAlmostEqual(result["delta_flipped_minus_original"], 0.2)
        self.assertEqual(result["coverage"]["matched"], 29)
        with open(result["paired_scores"]) as handle:
            header = next(csv.reader(handle))
        self.assertIn("original_19_residue_mean", header)

    def test_window_is_configurable(self):
        result = compare_scores(self.table("original", [0.0, 1.0, 0.0]), self.table("flipped", [1.0, 0.0, 1.0]),
                                self.root / "comparison.json", window=1)
        self.assertEqual(result["metric"], "aa_1_residue_mean")
        self.assertAlmostEqual(result["delta_flipped_minus_original"], 1 / 3)

    def test_stage_comparison_matches_residues_after_trimming(self):
        before = self.table("before", [0.1] * 20)
        after = self.table("after", [0.3] * 15)          # last five residues trimmed
        meta = json.loads(before.with_suffix(".metadata.json").read_text())
        after.with_suffix(".metadata.json").write_text(json.dumps(meta))
        result = compare_stage_scores(before, after, self.root / "stage.json")
        self.assertEqual(result["coverage"], {"before_residues": 20, "after_residues": 15, "matched": 15})
        self.assertAlmostEqual(result["delta_after_minus_before"], 0.2)

    def test_stage_comparison_rejects_different_maps(self):
        before, after = self.table("before", [0.1] * 5), self.table("after", [0.2] * 5)
        with self.assertRaisesRegex(ValueError, "different maps"):
            compare_stage_scores(before, after, self.root / "stage.json")

    def test_different_candidates_with_identical_residue_labels_are_rejected(self):
        original, flipped = self.table("original", [1]), self.table("flipped", [2])
        metadata_path = flipped.with_suffix(".metadata.json")
        metadata = json.loads(metadata_path.read_text())
        metadata["reference"]["candidate_index"] = 2
        metadata_path.write_text(json.dumps(metadata))
        with self.assertRaisesRegex(ValueError, "different source candidates"):
            compare_scores(original, flipped, self.root / "comparison.json")


if __name__ == "__main__":
    unittest.main()
