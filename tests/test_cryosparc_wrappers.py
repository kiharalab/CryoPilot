import copy
import types
import unittest

from cryosparc_repl import (
    CRYOSPARC_HELP,
    CryoSPARCWorkflow,
    _WRAPPER_JOB_TYPES,
    plan_particle_extraction,
)


def register_spec(
    job_type,
    title,
    inputs=None,
    outputs=None,
    params=None,
    *,
    description="test job",
    category="utilities",
    stability="stable",
):
    data = {
        "type": job_type,
        "title": title,
        "description": description,
        "category": category,
        "stability": stability,
        "hidden": False,
        "inputs": inputs or {},
        "outputs": outputs or {"result": {"type": "particle", "title": "Result"}},
        "params": params or {},
    }
    return types.SimpleNamespace(type=job_type, model_dump=lambda data=data: data)


def input_spec(kind="particle", *, required=True, many=False, title=None):
    return {
        "type": kind,
        "title": title or kind.title(),
        "description": f"{kind} input",
        "count_min": 1 if required else 0,
        "count_max": "inf" if many else 1,
        "repeat_allowed": many,
        "slots": [{"name": "blob"}],
    }


class FakeJob:
    def __init__(self, uid, job_type, inputs, params):
        self.uid = uid
        self.model = types.SimpleNamespace(uid=uid, status="building")
        self.queue_calls = []
        self.job_type = job_type
        self.inputs = copy.deepcopy(inputs)
        self.params = copy.deepcopy(params)
        self.connections = {}

        if job_type == "reference_motion_correction":
            count = int(params.get("ref_num_volumes", 1))
            for index in range(count):
                self.inputs[f"particles_{index}"] = input_spec(title=f"Particles {index}")
                self.inputs[f"volume_{index}"] = input_spec(
                    "volume", title=f"Volume {index}"
                )
                self.inputs[f"mask_{index}"] = input_spec(
                    "mask", required=False, title=f"Static mask {index}"
                )

        if job_type == "align_3D_new":
            if params.get("align_update_particles"):
                self.inputs["particles_all"] = input_spec(
                    required=False, title="Particles (all)"
                )
            if params.get("align_create_mask_inputs"):
                self.inputs["masks_all_classes"] = input_spec(
                    "mask", required=False, many=True, title="Masks all classes"
                )

    def queue(self, **kwargs):
        self.queue_calls.append(kwargs)

    def connect(self, target_input, source_job_uid, source_output):
        if target_input not in self.inputs:
            raise KeyError(f"Unknown live input: {target_input}")
        source = (source_job_uid, source_output)
        if target_input not in self.connections:
            self.connections[target_input] = source
        elif isinstance(self.connections[target_input], list):
            self.connections[target_input].append(source)
        else:
            self.connections[target_input] = [self.connections[target_input], source]

        if self.job_type == "align_3D_new" and target_input == "volume":
            value = self.connections[target_input]
            count = len(value) if isinstance(value, list) else 1
            index = count - 1
            if self.params.get("align_update_particles"):
                self.inputs[f"particles_{index}"] = input_spec(
                    required=False,
                    title=f"Particles (map to align, connection {index})",
                )
            if self.params.get("align_create_mask_inputs"):
                self.inputs[f"mask_{index}"] = input_spec(
                    "mask",
                    required=False,
                    title=f"Mask (map to align, connection {index})",
                )
        return True


class FakeWorkspace:
    def __init__(self, specs=()):
        self.created = []
        self.specs = {
            spec.type: spec.model_dump()
            for spec in specs
        }

    def create_job(self, job_type, **kwargs):
        inputs = self.specs.get(job_type, {}).get("inputs", {})
        job = FakeJob(
            f"J{len(self.created) + 1}",
            job_type,
            inputs,
            kwargs.get("params") or {},
        )
        for name, sources in (kwargs.get("connections") or {}).items():
            sources = sources if isinstance(sources, list) else [sources]
            for source_uid, source_output in sources:
                job.connect(name, source_uid, source_output)
        self.created.append((job_type, kwargs, job))
        return job


class FakeCS:
    def __init__(self, specs):
        self.job_register = types.SimpleNamespace(specs=specs)
        self.api = types.SimpleNamespace(projects=types.SimpleNamespace(create=lambda **kwargs: None))


def wrapper_specs():
    i = input_spec
    return [
        register_spec("denoise_train", "Micrograph Denoiser", {"exposures": i("exposure")}),
        register_spec(
            "junk_detector_v1",
            "Micrograph Junk Detector",
            {"exposures": i("exposure"), "particles": i(required=False)},
        ),
        register_spec("exposure_groups", "Exposure Group Utilities", {"exposure": i("exposure")}),
        register_spec(
            "reference_motion_correction",
            "Reference Based Motion Correction",
            {
                "exposures": i("exposure"),
                "hyperparameters": i("hyperparameter", required=False),
            },
            params={
                "ref_num_volumes": {
                    "type": "integer",
                    "title": "Number of reference volumes",
                    "ge": 1,
                }
            },
        ),
        register_spec("subset_particles", "Subset Particles by Statistic", {"particles": i()}),
        register_spec("rebalance_orientations", "Rebalance Orientations", {"particles": i()}),
        register_spec("homo_reconstruct", "Homogeneous Reconstruction Only", {"particles": i(), "mask": i("mask", required=False)}),
        register_spec("hetero_reconstruct", "Heterogeneous Reconstruction Only", {"particles": i(), "mask": i("mask", required=False)}),
        # Deliberately use an unknown future internal key to exercise exact-title fallback.
        register_spec("future_class_3d", "3D Classification", {"particles": i(), "volume": i("volume", required=False, many=True), "solvent_mask": i("mask", required=False), "focus_mask": i("mask", required=False)}),
        register_spec("regroup_3D", "Regroup 3D Classes", {"particles": i(), "volume": i("volume"), "mask": i("mask", required=False)}),
        register_spec("volume_tools", "Volume Tools", {"volume": i("volume", required=False), "mask": i("mask", required=False)}),
        register_spec("volume_alignment_tools", "Volume Alignment Tools", {"volume": i("volume"), "mask": i("mask", required=False), "particles": i(required=False)}),
        register_spec(
            "align_3D_new",
            "Align 3D Maps",
            {
                "volume_ref": i("volume"),
                "mask_ref": i("mask", required=False),
                "volumes": i("volume", required=False),
                "volume": i("volume", required=False, many=True),
            },
            params={
                "align_update_particles": {
                    "type": "boolean",
                    "title": "Update particle alignments",
                },
                "align_create_mask_inputs": {
                    "type": "boolean",
                    "title": "Create mask inputs",
                },
            },
        ),
        register_spec("sym_expand", "Symmetry Expansion", {"particles": i()}),
        register_spec("particle_subtract", "Particle Subtraction", {"particles": i(), "volume": i("volume"), "mask": i("mask")}),
        register_spec("new_local_refine", "Local Refinement", {"particles": i(), "volume": i("volume"), "mask": i("mask")}),
        register_spec("local_resolution", "Local Resolution Estimation", {"volume": i("volume"), "mask": i("mask", required=False)}),
        register_spec("local_filtering", "Local Filtering", {"volume": i("volume")}),
        register_spec("var_3D", "3D Variability", {"particles": i(), "mask": i("mask", required=False)}),
        register_spec("var_3D_display", "3D Variability Display", {"particles": i(), "components": i("volume")}),
        register_spec("flex_prepare", "3D Flex Data Prep", {"particles": i(), "volume": i("volume")}),
        register_spec("flex_mesh", "3D Flex Mesh Prep", {"volume": i("volume"), "mask": i("mask")}),
        register_spec("flex_train", "3D Flex Training", {"particles": i(), "mesh": i("flex_mesh")}),
        register_spec("flex_generate", "3D Flex Generator", {"model": i("flex_model"), "volume": i("volume", required=False)}),
        register_spec("flex_reconstruct", "3D Flex Reconstruction", {"particles": i(), "model": i("flex_model")}),
        register_spec(
            "future_import_particles",
            "Import Particle Stack",
            {"source_exposures": i("exposure", required=False)},
            {"imported_particles": {"type": "particle", "title": "Imported particles"}},
            {
                "meta_file": {
                    "type": "string",
                    "title": "Particle meta path",
                    "required_param": True,
                },
                "data_files": {
                    "type": "string",
                    "title": "Particle data path",
                    "required_param": True,
                },
                "pixel_override": {"type": "number", "title": "Pixel size (A)"},
                "voltage_override": {
                    "type": "number",
                    "title": "Accelerating voltage (kV)",
                },
                "cs_override": {
                    "type": "number",
                    "title": "Spherical aberration (mm)",
                },
                "dose_override": {
                    "type": "number",
                    "title": "Total exposure dose (e/A^2)",
                },
                "sign_override": {
                    "type": "integer",
                    "title": "Data Sign",
                    "enum": [-1, 1],
                },
                "ignore_images": {"type": "boolean", "title": "Ignore raw data"},
                "ignore_poses": {"type": "boolean", "title": "Ignore pose data"},
            },
            category="import",
        ),
    ]


class CryoSPARCResolverTests(unittest.TestCase):
    def workflow(self, specs):
        workflow = CryoSPARCWorkflow(FakeCS(specs))
        workflow.workspace = FakeWorkspace(specs)
        return workflow

    def test_resolver_prefers_alias_then_exact_title_and_rejects_legacy(self):
        workflow = self.workflow(wrapper_specs())
        self.assertEqual(workflow.resolve_job_type(("denoise_train",), title="Micrograph Denoiser"), "denoise_train")
        self.assertEqual(workflow._wrapper_job_type("class_3d"), "future_class_3d")

        legacy = self.workflow(
            [register_spec("subset_particles", "Subset Particles by Statistic", stability="legacy")]
        )
        with self.assertRaisesRegex(KeyError, "legacy/obsolete"):
            legacy._wrapper_job_type("subset_particles")

    def test_resolver_uses_category_to_disambiguate_and_reports_nearby(self):
        workflow = self.workflow(
            [
                register_spec("align_a", "Align Things", category="utilities"),
                register_spec("align_b", "Align Things", category="refinement"),
            ]
        )
        self.assertEqual(
            workflow.resolve_job_type(("missing",), title="Align Things", category="refinement"),
            "align_b",
        )
        with self.assertRaisesRegex(KeyError, "Nearby installed jobs"):
            workflow.resolve_job_type(("align_c",), title="Align Other Things")
        with self.assertRaisesRegex(KeyError, "No active installed"):
            workflow.resolve_job_type(("missing",))

    def test_job_spec_preserves_full_metadata(self):
        spec = register_spec(
            "typed",
            "Typed Job",
            {
                "particles": {
                    **input_spec(),
                    "description": "aligned gold-standard particles",
                }
            },
            {"volume": {"type": "volume", "title": "Volume", "description": "halfmaps", "slots": [{"name": "map"}]}},
            {
                "iterations": {
                    "type": "integer",
                    "title": "Iterations",
                    "description": "iteration count",
                    "required_param": True,
                    "default": 2,
                    "ge": 1,
                    "le": 5,
                    "enum": [1, 2, 3, 4, 5],
                }
            },
            description="full description",
            category="refinement",
            stability="beta",
        )
        detail = self.workflow([spec]).job_spec("typed")
        self.assertEqual(detail["description"], "full description")
        self.assertEqual(detail["category"], "refinement")
        self.assertEqual(detail["stability"], "beta")
        self.assertEqual(detail["input_count"], 1)
        self.assertEqual(detail["output_count"], 1)
        self.assertEqual(detail["inputs"], ["particles"])
        self.assertEqual(detail["outputs"], ["volume"])
        self.assertEqual(detail["input_types"], {"particles": "particle"})
        self.assertEqual(detail["output_types"], {"volume": "volume"})
        self.assertEqual(detail["required_params"], ["iterations"])
        self.assertEqual(detail["param_types"], {"iterations": "integer"})
        self.assertEqual(detail["param_defaults"], {"iterations": 2})
        self.assertEqual(detail["param_enums"], {"iterations": [1, 2, 3, 4, 5]})
        self.assertEqual(detail["param_ranges"], {"iterations": {"ge": 1, "le": 5}})
        self.assertEqual(detail["input_specs"]["particles"]["slots"], [{"name": "blob"}])
        self.assertEqual(detail["output_specs"]["volume"]["description"], "halfmaps")
        self.assertEqual(detail["param_specs"]["iterations"]["ge"], 1)

    def test_validate_job_checks_primitive_enum_bounds_and_connection_counts(self):
        typed = register_spec(
            "typed",
            "Typed Job",
            {"particles": input_spec(many=True)},
            params={
                "mode": {"type": "string", "enum": ["a", "b"], "required_param": True},
                "iterations": {"type": "integer", "ge": 1, "le": 4},
                "scale": {"type": "number", "gt": 0},
                "enabled": {"type": "boolean"},
                "weights": {"type": "array", "items": {"type": "number"}},
            },
        )
        workflow = self.workflow([typed])
        invalid = workflow.validate_job(
            "typed",
            connections={"particles": []},
            params={
                "mode": "c",
                "iterations": True,
                "scale": 0,
                "enabled": 1,
                "weights": [1.0, "bad"],
            },
        )
        self.assertFalse(invalid["ok"])
        self.assertEqual(set(invalid["invalid_params"]), {"mode", "iterations", "scale", "enabled", "weights"})
        self.assertIn("particles", invalid["invalid_inputs"])

        valid = workflow.validate_job(
            "typed",
            connections={"particles": [("J1", "particles")]},
            params={"mode": "a", "iterations": 2, "scale": 0.5, "enabled": True, "weights": [1, 2.5]},
        )
        self.assertTrue(valid["ok"], valid)

    def test_create_project_requires_parent_directory(self):
        workflow = self.workflow([])
        with self.assertRaisesRegex(ValueError, "project_dir is required"):
            workflow.create_project("Unsafe implicit location")


class ParticleExtractionSafetyTests(unittest.TestCase):
    @staticmethod
    def extraction_spec():
        return register_spec(
            "extract_micrographs_multi",
            "Extract From Micrographs",
            {
                "micrographs": input_spec("exposure"),
                "particles": input_spec("particle"),
            },
            {"particles": {"type": "particle", "title": "Particles"}},
            {
                "box_size_pix": {"type": "integer", "ge": 2},
                "bin_size_pix": {"type": "integer", "ge": 2},
                "output_f16": {"type": "boolean"},
                "compute_num_gpus": {"type": "integer", "ge": 1},
            },
            category="extraction",
        )

    def workflow(self):
        spec = self.extraction_spec()
        workflow = CryoSPARCWorkflow(FakeCS([spec]))
        workflow.workspace = FakeWorkspace([spec])
        return workflow

    def test_preflight_reports_physical_field_sampling_and_flexible_warning(self):
        plan = plan_particle_extraction(
            box_size_pix=336,
            source_pixel_size_A=0.9,
            particle_extent_A=130,
            fourier_crop_size_pix=192,
            target_resolution_A=3.5,
            flexible=True,
        )

        self.assertEqual(plan["physical_box_A"], 302.4)
        self.assertAlmostEqual(
            plan["box_to_particle_ratio"], 302.4 / 130, places=6
        )
        self.assertEqual(plan["output_pixel_size_A"], 1.575)
        self.assertEqual(plan["nyquist_resolution_A"], 3.15)
        self.assertEqual(plan["recommended_box_to_particle_ratio"], 2.5)
        self.assertTrue(plan["post_extraction_review_required"])
        self.assertIn("preferred 2.5x", plan["warnings"][0])

    def test_10827_sized_box_is_rejected_at_raw_and_binned_sampling(self):
        for pixel_size in (0.45, 0.9):
            with self.subTest(pixel_size=pixel_size):
                with self.assertRaisesRegex(ValueError, "Unsafe extraction box"):
                    plan_particle_extraction(
                        box_size_pix=224,
                        source_pixel_size_A=pixel_size,
                        particle_extent_A=130,
                        flexible=True,
                    )

    def test_unsafe_override_requires_and_records_a_rationale(self):
        with self.assertRaisesRegex(ValueError, "small_box_rationale"):
            plan_particle_extraction(
                224,
                0.9,
                130,
                allow_small_box=True,
            )

        plan = plan_particle_extraction(
            224,
            0.9,
            130,
            allow_small_box=True,
            small_box_rationale="Reproduce a published diagnostic only",
        )
        self.assertTrue(plan["small_box_override"])
        self.assertEqual(
            plan["small_box_rationale"], "Reproduce a published diagnostic only"
        )
        self.assertIn("Explicit override", plan["warnings"][0])

    def test_preflight_rejects_invalid_sizes_and_warns_on_nyquist(self):
        with self.assertRaisesRegex(ValueError, "positive even integer"):
            plan_particle_extraction(335, 0.9, 130)
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            plan_particle_extraction(336, 0.9, 130, fourier_crop_size_pix=384)

        plan = plan_particle_extraction(
            336,
            0.9,
            130,
            fourier_crop_size_pix=160,
            target_resolution_A=3.0,
        )
        self.assertTrue(
            any("cannot support" in warning for warning in plan["warnings"])
        )

    def test_extract_requires_preflight_inputs_and_records_plan_without_queueing(self):
        workflow = self.workflow()
        with self.assertRaisesRegex(ValueError, "source_pixel_size_A"):
            workflow.extract_particles("M1", "P1", 336)

        with self.assertWarnsRegex(UserWarning, "preferred 2.5x"):
            job = workflow.extract_particles(
                "M1",
                "P1",
                336,
                bin_size_pix=192,
                source_pixel_size_A=0.9,
                particle_extent_A=130,
                target_resolution_A=3.5,
                flexible=True,
                title="Audited Extraction",
            )

        self.assertEqual(
            job.params,
            {
                "box_size_pix": 336,
                "bin_size_pix": 192,
                "output_f16": True,
                "compute_num_gpus": 1,
            },
        )
        self.assertFalse(job.queue_calls)
        description = workflow.workspace.created[0][1]["desc"]
        self.assertIn("cryoagent_extraction_plan_v1", description)
        self.assertIn('"physical_box_A":302.4', description)
        self.assertIn("Post-extraction review required", description)


class CryoSPARCWrapperParityTests(unittest.TestCase):
    def setUp(self):
        self.specs = wrapper_specs()
        self.workflow = CryoSPARCWorkflow(FakeCS(self.specs))
        self.workspace = FakeWorkspace(self.specs)
        self.workflow.workspace = self.workspace

    def test_all_25_wrappers_create_only_and_preserve_sources(self):
        class Controller:
            model = types.SimpleNamespace(uid="P-CONTROLLER")

        calls = [
            lambda: self.workflow.micrograph_denoiser("E1"),
            lambda: self.workflow.micrograph_junk_detector("E1", particles=Controller()),
            lambda: self.workflow.exposure_groups(exposures="E1"),
            lambda: self.workflow.reference_motion_correction(
                "E1",
                ["P1", Controller()],
                ["V1", ("V2", "custom_volume")],
                masks=["M1", "M2"],
            ),
            lambda: self.workflow.subset_particles(particles=("P1", "custom_particles")),
            lambda: self.workflow.rebalance_orientations("P1"),
            lambda: self.workflow.homogeneous_reconstruct("P1"),
            lambda: self.workflow.heterogeneous_reconstruct("P1"),
            lambda: self.workflow.class_3d("P1"),
            lambda: self.workflow.regroup_3d("P1", "V1"),
            lambda: self.workflow.volume_tools(volume="V1"),
            lambda: self.workflow.volume_alignment_tools("V1"),
            lambda: self.workflow.align_3d_maps("REF", ["V1", "V2"]),
            lambda: self.workflow.symmetry_expand("P1"),
            lambda: self.workflow.particle_subtract("P1", "V1", "M1"),
            lambda: self.workflow.local_refine("P1", "V1", "M1"),
            lambda: self.workflow.local_resolution("V1"),
            lambda: self.workflow.local_filter("V1"),
            lambda: self.workflow.variability_3d("P1"),
            lambda: self.workflow.variability_3d_display("P1", "V1"),
            lambda: self.workflow.flex_data_prep("P1", "V1"),
            lambda: self.workflow.flex_mesh_prep("V1", "M1"),
            lambda: self.workflow.flex_train("P1", "MESH1"),
            lambda: self.workflow.flex_generate("MODEL1"),
            lambda: self.workflow.flex_reconstruct("P1", "MODEL1"),
        ]
        jobs = [call() for call in calls]

        self.assertEqual(len(jobs), 25)
        self.assertEqual(len(self.workspace.created), 25)
        self.assertTrue(all(not job.queue_calls for job in jobs))
        expected_types_and_inputs = [
            ("denoise_train", {"exposures"}),
            ("junk_detector_v1", {"exposures", "particles"}),
            ("exposure_groups", {"exposure"}),
            (
                "reference_motion_correction",
                {
                    "exposures",
                    "particles_0",
                    "particles_1",
                    "volume_0",
                    "volume_1",
                    "mask_0",
                    "mask_1",
                },
            ),
            ("subset_particles", {"particles"}),
            ("rebalance_orientations", {"particles"}),
            ("homo_reconstruct", {"particles"}),
            ("hetero_reconstruct", {"particles"}),
            ("future_class_3d", {"particles"}),
            ("regroup_3D", {"particles", "volume"}),
            ("volume_tools", {"volume"}),
            ("volume_alignment_tools", {"volume"}),
            ("align_3D_new", {"volume_ref", "volume"}),
            ("sym_expand", {"particles"}),
            ("particle_subtract", {"particles", "volume", "mask"}),
            ("new_local_refine", {"particles", "volume", "mask"}),
            ("local_resolution", {"volume"}),
            ("local_filtering", {"volume"}),
            ("var_3D", {"particles"}),
            ("var_3D_display", {"particles", "components"}),
            ("flex_prepare", {"particles", "volume"}),
            ("flex_mesh", {"volume", "mask"}),
            ("flex_train", {"particles", "mesh"}),
            ("flex_generate", {"model"}),
            ("flex_reconstruct", {"particles", "model"}),
        ]
        self.assertEqual(
            [
                (job_type, set(job.connections))
                for (job_type, _kwargs, job) in self.workspace.created
            ],
            expected_types_and_inputs,
        )

        junk_connections = self.workspace.created[1][1]["connections"]
        self.assertEqual(junk_connections["particles"], ("P-CONTROLLER", "particles"))
        rbmc_connections = jobs[3].connections
        self.assertEqual(
            [rbmc_connections["particles_0"], rbmc_connections["particles_1"]],
            [("P1", "particles"), ("P-CONTROLLER", "particles")],
        )
        self.assertEqual(
            [rbmc_connections["volume_0"], rbmc_connections["volume_1"]],
            [("V1", "volume"), ("V2", "custom_volume")],
        )
        self.assertEqual(
            self.workspace.created[3][1]["params"]["ref_num_volumes"], 2
        )
        self.assertEqual(
            self.workspace.created[4][1]["connections"]["particles"],
            ("P1", "custom_particles"),
        )
        self.assertEqual(self.workspace.created[8][0], "future_class_3d")

    def test_import_particle_stack_uses_live_params_and_never_queues(self):
        job = self.workflow.import_particle_stack(
            "/data/particles.star",
            "/data/extract/*.mrc",
            source_exposures=("J1", "custom_exposures"),
            psize_A=1.16,
            accel_kv=200,
            cs_mm=2.1,
            total_dose_e_per_A2=60,
            data_sign=1,
            ignore_raw_data=False,
            ignore_pose_data=False,
        )

        self.assertEqual(job.job_type, "future_import_particles")
        self.assertEqual(
            job.params,
            {
                "meta_file": "/data/particles.star",
                "data_files": "/data/extract/*.mrc",
                "pixel_override": 1.16,
                "voltage_override": 200,
                "cs_override": 2.1,
                "dose_override": 60,
                "sign_override": 1,
                "ignore_images": False,
                "ignore_poses": False,
            },
        )
        self.assertEqual(
            job.connections,
            {"source_exposures": ("J1", "custom_exposures")},
        )
        self.assertFalse(job.queue_calls)

        with self.assertRaisesRegex(ValueError, "particle_meta_path is required"):
            self.workflow.import_particle_stack("", "/data/extract/*.mrc")
        with self.assertRaisesRegex(ValueError, "particle_data_path is required"):
            self.workflow.import_particle_stack("/data/particles.star", "")

    def test_wrapper_argument_guards(self):
        with self.assertRaises(ValueError):
            self.workflow.exposure_groups()
        with self.assertRaises(ValueError):
            self.workflow.subset_particles()
        with self.assertRaises(ValueError):
            self.workflow.subset_particles("P1", initial_particles="P0", final_particles="P1")
        with self.assertRaises(ValueError):
            self.workflow.volume_tools(volume="V1", mask="M1")
        with self.assertRaises(ValueError):
            self.workflow.reference_motion_correction("E1", ["P1", "P2"], ["V1"])
        with self.assertRaises(ValueError):
            self.workflow.align_3d_maps("REF", volumes_group="VG", individual_volumes=["V1"])
        with self.assertRaises(ValueError):
            self.workflow.align_3d_maps(
                "REF", individual_volumes=["V1", "V2"], particles=["P1"]
            )

    def test_repeated_sources_use_inputs_materialized_after_job_creation(self):
        job = self.workflow.reference_motion_correction(
            "E1", ["P1", "P2"], ["V1", "V2"]
        )
        self.assertEqual(
            job.connections,
            {
                "exposures": ("E1", "exposures"),
                "particles_0": ("P1", "particles"),
                "particles_1": ("P2", "particles"),
                "volume_0": ("V1", "volume"),
                "volume_1": ("V2", "volume"),
            },
        )
        self.assertEqual(job.params["ref_num_volumes"], 2)

        scalar_job = self.workflow.reference_motion_correction("E1", "P1", "V1")
        self.assertEqual(
            scalar_job.connections,
            {
                "exposures": ("E1", "exposures"),
                "particles_0": ("P1", "particles"),
                "volume_0": ("V1", "volume"),
            },
        )

    def test_align_3d_maps_routes_group_and_individual_modes_exactly(self):
        group_job = self.workflow.align_3d_maps(
            "REF",
            volumes_group="VG",
            particles="PG",
            masks=["M1", "M2"],
        )
        self.assertEqual(
            group_job.connections,
            {
                "volume_ref": ("REF", "volume"),
                "volumes": ("VG", "volume"),
                "particles_all": ("PG", "particles"),
                "masks_all_classes": [("M1", "mask"), ("M2", "mask")],
            },
        )
        self.assertTrue(group_job.params["align_update_particles"])
        self.assertTrue(group_job.params["align_create_mask_inputs"])

        individual_job = self.workflow.align_3d_maps(
            "REF",
            individual_volumes=["V1", "V2"],
            particles=["P1", "P2"],
            masks=["M1", "M2"],
        )
        self.assertEqual(
            individual_job.connections,
            {
                "volume_ref": ("REF", "volume"),
                "volume": [("V1", "volume"), ("V2", "volume")],
                "particles_0": ("P1", "particles"),
                "particles_1": ("P2", "particles"),
                "mask_0": ("M1", "mask"),
                "mask_1": ("M2", "mask"),
            },
        )

    def test_help_has_every_public_wrapper_and_create_only_contract(self):
        for method_name in _WRAPPER_JOB_TYPES:
            self.assertIn(f"csw.{method_name}(", CRYOSPARC_HELP)
        self.assertGreaterEqual(CRYOSPARC_HELP.count("create-only"), 3)
        for warning in ("halfmaps", "cannot", "does not realign", "exploratory"):
            self.assertIn(warning, CRYOSPARC_HELP)
        for extraction_rule in (
            "csw.plan_particle_extraction(",
            "below 2x particle extent",
            "actual upstream motion-corrected micrograph",
            "MUST render and",
            "outer 10-15%",
        ):
            self.assertIn(extraction_rule, CRYOSPARC_HELP)



class RefinementWrapperDefaultTests(unittest.TestCase):
    def refine_spec(self, *, v5):
        params = {
            "refine_symmetry": {"type": "string"},
            "refine_dynamic_mask_start_res": {
                "type": "number",
                "title": "Dynamic mask start resolution (A)",
            },
            "refine_scale_min": {"type": "boolean"},
            "random_seed": {"type": "integer"},
        }
        if v5:
            params["refine_dynamic_mask_use"] = {
                "type": "boolean",
                "title": "Use dynamic refinement mask",
            }
        return register_spec(
            "nonuniform_refine_new",
            "Non-uniform Refinement",
            {"particles": input_spec(), "volume": input_spec("volume")},
            params=params,
            category="refinement",
        )

    def abinit_spec(self):
        return register_spec(
            "homo_abinit",
            "Ab-Initio Reconstruction",
            {"particles": input_spec()},
            params={
                "abinit_K": {"type": "integer"},
                "abinit_num_particles": {"type": "integer"},
                "abinit_symmetry": {"type": "string"},
                "random_seed": {"type": "integer"},
            },
            category="reconstruction",
        )

    def workflow(self, *specs):
        workflow = CryoSPARCWorkflow(FakeCS(list(specs)))
        workflow.workspace = FakeWorkspace(list(specs))
        return workflow

    def created_params(self, workflow):
        return workflow.workspace.created[-1][1]["params"]

    def test_nonuniform_disables_dynamic_mask_with_v5_switch(self):
        workflow = self.workflow(self.refine_spec(v5=True))
        workflow.nonuniform_refine("J1", "J2")
        params = self.created_params(workflow)
        self.assertIs(params["refine_dynamic_mask_use"], False)
        self.assertNotIn("refine_dynamic_mask_start_res", params)

    def test_nonuniform_falls_back_to_fine_start_resolution_before_v5(self):
        workflow = self.workflow(self.refine_spec(v5=False))
        workflow.nonuniform_refine("J1", "J2")
        self.assertEqual(
            self.created_params(workflow)["refine_dynamic_mask_start_res"], 0.1
        )

    def test_nonuniform_keeps_dynamic_mask_when_requested(self):
        workflow = self.workflow(self.refine_spec(v5=True))
        workflow.nonuniform_refine("J1", "J2", dynamic_mask=True)
        params = self.created_params(workflow)
        self.assertNotIn("refine_dynamic_mask_use", params)
        self.assertNotIn("refine_dynamic_mask_start_res", params)

    def test_abinit_uses_whole_stack_unless_a_subset_is_requested(self):
        workflow = self.workflow(self.abinit_spec())
        workflow.abinit("J1")
        self.assertNotIn("abinit_num_particles", self.created_params(workflow))

        workflow.abinit("J1", num_particles=20000)
        self.assertEqual(
            self.created_params(workflow)["abinit_num_particles"], 20000
        )

    def test_wrapper_lookup_prefers_live_v5_job_keys(self):
        self.assertEqual(
            _WRAPPER_JOB_TYPES["variability_3d_display"][0][0], "var_3D_disp"
        )
        self.assertEqual(
            _WRAPPER_JOB_TYPES["rebalance_orientations"][0][0], "rebalance_3D"
        )


if __name__ == "__main__":
    unittest.main()
