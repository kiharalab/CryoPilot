from __future__ import annotations

import unittest

from prompts import SYSTEM_PROMPT


class SystemPromptTests(unittest.TestCase):
    """The instructions give goals, expert guidance and a few ground rules, not a fixed protocol."""

    def test_states_the_goal_and_leaves_decisions_to_the_agent(self):
        for text in ("most accurate atomic model", "You decide how to get there", "not a checklist",
                     "explain your decisions"):
            self.assertIn(text, SYSTEM_PROMPT)

    def test_keeps_the_ground_rules(self):
        for text in ("Submit jobs only when the user asks for modeling", "never copy their coordinates",
                     "Never mirror an atomic model", "flip_map", "complete biological assembly",
                     "independent modeling attempt"):
            self.assertIn(text, SYSTEM_PROMPT)

    def test_gives_guidance_on_each_modeling_decision(self):
        for text in ("Map and contour", "Map hand", "Candidates", "Assembly sanity checks", "What to remove",
                     "Refinement", "Judging a model", "unscored does not mean supported", "cleavage sites",
                     "Expression tags", "go back to the previous version"):
            self.assertIn(text, SYSTEM_PROMPT)

    def test_names_the_available_software(self):
        for text in ("refine_phenix", "refine_rosetta", "residue_support", "edit_model", "phenix.dock_in_map",
                     "phenix.fit_loops", "phenix.mtriage", "chimerax --nogui", "ROSETTA3_DB", "no OpenGL"):
            self.assertIn(text, SYSTEM_PROMPT)

    def test_has_no_scripted_protocol(self):
        for text in ("Keep a stage only if", "then refine_rosetta, then refine_phenix again",
                     "just to the right of this first knee", "below -0.5", "strictly higher",
                     "trim_unsupported_regions", "at most once"):
            self.assertNotIn(text, SYSTEM_PROMPT)

    def test_requests_auditable_deliverables(self):
        for text in ("final_model.cif", "decision_log.md", "final_summary.json", "starting_candidate", "limitations"):
            self.assertIn(text, SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
