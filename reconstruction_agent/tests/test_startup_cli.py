import copy
import json
import unittest

import startup_cli


class FakeInput:
    def __init__(self, values):
        self.values = iter(values)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return next(self.values)


def collect(values):
    return startup_cli.collect_session_manifest(input_fn=FakeInput(values))


class StartupCliTests(unittest.TestCase):
    def test_collects_movie_manifest_and_preserves_server_paths(self):
        dataset_path = "/data/session with spaces/Grid 1/*.mrc"
        gain_path = "/refs/gain files/gain.mrc"
        defect_path = "/refs/calibration/*.txt"
        manifest = collect(
            [
                "My reconstruction",
                "/cryosparc projects",
                "MOVIES",
                dataset_path,
                "0.83",
                "300",
                "2.7",
                "48.5",
                gain_path,
                defect_path,
                "n",
                "yes",
                "no",
                "membrane complex",
                "180",
                "450",
                "C3",
                "Find rare particles and characterize heterogeneity",
            ]
        )

        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["session"], {"id": None})
        self.assertEqual(manifest["project"]["project_uid"], None)
        self.assertEqual(manifest["project"]["workspace_uid"], None)
        self.assertEqual(manifest["project"]["workspace_title"], "Automated Reconstruction")
        self.assertEqual(manifest["dataset"]["kind"], "movies")
        self.assertEqual(manifest["dataset"]["path"], dataset_path)
        self.assertIsNone(manifest["dataset"]["particle_metadata_path"])
        self.assertEqual(manifest["dataset"]["gain_reference_path"], gain_path)
        self.assertEqual(manifest["dataset"]["defect_file_path"], defect_path)
        self.assertEqual(
            manifest["dataset"]["eer"],
            {"enabled": False, "fractions": None, "upsampling": None},
        )
        self.assertTrue(manifest["acquisition"]["negative_stain"])
        self.assertFalse(manifest["acquisition"]["phase_plate"])
        self.assertEqual(manifest["sample"]["molecular_mass_kDa"], 450.0)
        self.assertEqual(manifest["sample"]["expected_symmetry"], "C3")

    def test_collects_eer_branch_with_positive_integer_settings(self):
        manifest = collect(
            [
                "EER project",
                "/projects",
                "movies",
                "/raw/*.eer",
                "0.5",
                "200",
                "2.0",
                "40",
                "",
                "",
                "yes",
                "40",
                "2",
                "",
                "",
                "",
                "120",
                "",
                "",
                "Resolve continuous motions",
            ]
        )

        self.assertEqual(
            manifest["dataset"]["eer"],
            {"enabled": True, "fractions": 40, "upsampling": 2},
        )
        self.assertIsNone(manifest["dataset"]["gain_reference_path"])
        self.assertIsNone(manifest["sample"]["molecular_mass_kDa"])
        self.assertEqual(manifest["sample"]["expected_symmetry"], "C1")

    def test_micrographs_skip_movie_questions(self):
        fake = FakeInput(
            [
                "Micrographs",
                "/projects",
                "micrographs",
                "/processed/set 1/*.mrc",
                "1.1",
                "300",
                "0",
                "55",
                "n",
                "y",
                "ribosome",
                "250",
                "",
                "",
                "Refine a consensus structure",
            ]
        )
        manifest = startup_cli.collect_session_manifest(input_fn=fake)

        self.assertEqual(manifest["dataset"]["kind"], "micrographs")
        self.assertIsNone(manifest["dataset"]["particle_metadata_path"])
        self.assertIsNone(manifest["dataset"]["gain_reference_path"])
        self.assertIsNone(manifest["dataset"]["defect_file_path"])
        self.assertEqual(
            manifest["dataset"]["eer"],
            {"enabled": False, "fractions": None, "upsampling": None},
        )
        self.assertNotIn("Gain reference", "\n".join(fake.prompts))
        self.assertEqual(manifest["acquisition"]["spherical_aberration_mm"], 0.0)
        self.assertTrue(manifest["acquisition"]["phase_plate"])

    def test_particle_stack_collects_metadata_and_data_paths_and_updates_prompts(self):
        metadata_path = "/archive/11665/data/J503 particles.star"
        particle_path = "/archive/11665/data/extract/*.mrc"
        fake = FakeInput(
            [
                "Particle reconstruction",
                "/projects",
                "PARTICLES",
                metadata_path,
                particle_path,
                "1.16",
                "200",
                "2.1",
                "60",
                "n",
                "n",
                "HER2 with trastuzumab and pertuzumab Fabs",
                "150",
                "165",
                "C1",
                "Resolve continuous heterogeneity with 3DVA",
            ]
        )
        manifest = startup_cli.collect_session_manifest(input_fn=fake)

        self.assertEqual(manifest["dataset"]["kind"], "particles")
        self.assertEqual(manifest["dataset"]["particle_metadata_path"], metadata_path)
        self.assertEqual(manifest["dataset"]["path"], particle_path)
        self.assertIsNone(manifest["dataset"]["gain_reference_path"])
        self.assertIsNone(manifest["dataset"]["defect_file_path"])
        self.assertNotIn("Gain reference", "\n".join(fake.prompts))
        self.assertNotIn("EER dataset", "\n".join(fake.prompts))

        bound = startup_cli.bind_manifest_scope(
            manifest,
            session_id="particle-session",
            project_uid="P55",
            workspace_uid="W2",
        )
        developer = startup_cli.build_developer_instruction(bound)
        user = startup_cli.build_user_prompt(bound)
        for prompt in (developer, user):
            self.assertIn("Import Particle Stack", prompt)
            self.assertIn("csw.import_particle_stack", prompt)
            self.assertNotIn("Import Micrographs", prompt)
        self.assertIn("already-extracted particle stack", developer)
        self.assertIn(metadata_path, user)
        self.assertIn(particle_path, user)

    def test_retries_invalid_required_numeric_choice_boolean_and_eer_integer(self):
        fake = FakeInput(
            [
                "",
                "Validated project",
                "/projects",
                "volumes",
                "movies",
                "",
                "/data/*.eer",
                "zero",
                "0.7",
                "0",
                "300",
                "-1",
                "2.7",
                "nan",
                "50",
                "",
                "",
                "maybe",
                "yes",
                "0",
                "30",
                "1.5",
                "2",
                "perhaps",
                "no",
                "",
                "",
                "-10",
                "150",
                "none",
                "-1",
                "",
                "C1",
                "",
                "Find a rare state",
            ]
        )
        manifest = startup_cli.collect_session_manifest(input_fn=fake)

        self.assertEqual(manifest["project"]["title"], "Validated project")
        self.assertEqual(manifest["dataset"]["path"], "/data/*.eer")
        self.assertEqual(manifest["acquisition"]["pixel_size_A"], 0.7)
        self.assertEqual(manifest["acquisition"]["accelerating_voltage_kV"], 300.0)
        self.assertEqual(manifest["acquisition"]["spherical_aberration_mm"], 2.7)
        self.assertEqual(manifest["acquisition"]["total_dose_e_per_A2"], 50.0)
        self.assertEqual(manifest["dataset"]["eer"]["fractions"], 30)
        self.assertEqual(manifest["dataset"]["eer"]["upsampling"], 2)
        self.assertEqual(manifest["sample"]["particle_diameter_A"], 150.0)
        self.assertIsNone(manifest["sample"]["molecular_mass_kDa"])
        self.assertEqual(manifest["user_goal"], "Find a rare state")

    def test_credentials_only_prompt_for_missing_values_and_use_getpass(self):
        fake_input = FakeInput(["scientist@example.org"])
        secret_prompts = []

        def fake_getpass(prompt):
            secret_prompts.append(prompt)
            return "secret"

        credentials = startup_cli.collect_cryosparc_credentials(
            url="https://cryosparc.internal",
            input_fn=fake_input,
            getpass_fn=fake_getpass,
        )

        self.assertEqual(
            credentials,
            {
                "url": "https://cryosparc.internal",
                "email": "scientist@example.org",
                "password": "secret",
            },
        )
        self.assertEqual(len(fake_input.prompts), 1)
        self.assertEqual(secret_prompts, ["CryoSPARC password: "])

    def test_credentials_reject_user_info_in_persisted_cryosparc_url(self):
        with self.assertRaisesRegex(ValueError, "embedded user-info credentials"):
            startup_cli.collect_cryosparc_credentials(
                url="https://username:password@cryosparc.example.test",
                email="scientist@example.test",
                password="runtime-only-password",
            )

        answers = iter(
            [
                "https://username:password@cryosparc.example.test",
                "https://cryosparc.example.test",
                "scientist@example.test",
            ]
        )
        credentials = startup_cli.collect_cryosparc_credentials(
            input_fn=lambda _prompt: next(answers),
            getpass_fn=lambda _prompt: "runtime-only-password",
        )
        self.assertEqual(credentials["url"], "https://cryosparc.example.test")

    def test_binding_is_immutable_and_prompts_pin_exact_scope(self):
        original = collect(
            [
                "Scoped",
                "/projects",
                "micrographs",
                "/data/*.mrc",
                "1",
                "300",
                "2.7",
                "50",
                "n",
                "n",
                "target",
                "100",
                "100",
                "",
                "Find rare particles",
            ]
        )
        before = copy.deepcopy(original)
        bound = startup_cli.bind_manifest_scope(
            original,
            session_id="session-123",
            project_uid="P42",
            workspace_uid="W7",
        )

        self.assertEqual(original, before)
        self.assertEqual(bound["session"]["id"], "session-123")
        self.assertEqual(bound["project"]["project_uid"], "P42")
        self.assertEqual(bound["project"]["workspace_uid"], "W7")

        developer = startup_cli.build_developer_instruction(bound)
        user = startup_cli.build_user_prompt(bound)
        canonical = startup_cli.canonical_manifest_json(bound)
        self.assertIn("Session ID: session-123", developer)
        self.assertIn("CryoSPARC project UID: P42", developer)
        self.assertIn("CryoSPARC workspace UID: W7", developer)
        self.assertIn("Operate only in that project and workspace", developer)
        self.assertIn("Import Micrographs", developer)
        self.assertIn("project P42, workspace W7, session session-123", user)
        self.assertIn("Import Micrographs", user)
        self.assertIn(canonical, user)
        self.assertEqual(json.loads(canonical), bound)

    def test_prompt_builders_reject_unbound_manifest(self):
        manifest = {
            "schema_version": 1,
            "session": {"id": None},
            "project": {
                "project_uid": None,
                "workspace_uid": None,
                "workspace_title": startup_cli.WORKSPACE_TITLE,
            },
            "dataset": {"kind": "movies"},
        }
        with self.assertRaisesRegex(ValueError, "session ID"):
            startup_cli.build_user_prompt(manifest)

    def test_prompt_builders_reject_unsupported_dataset_kind(self):
        manifest = {
            "schema_version": 1,
            "session": {"id": "session-123"},
            "project": {
                "project_uid": "P1",
                "workspace_uid": "W1",
                "workspace_title": startup_cli.WORKSPACE_TITLE,
            },
            "dataset": {"kind": "volumes"},
        }
        with self.assertRaisesRegex(ValueError, "Unsupported dataset kind: 'volumes'"):
            startup_cli.build_developer_instruction(manifest)


if __name__ == "__main__":
    unittest.main()
