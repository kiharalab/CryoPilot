import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mrcfile
import numpy as np

from cryosparc_repl import CryoSPARCWorkflow, _normalize_class_image


def write_numpy_file(path, array):
    with path.open("wb") as handle:
        np.save(handle, array)


class ClassImageContrastTests(unittest.TestCase):
    def test_small_particle_in_large_box_retains_internal_contrast(self):
        # The particle occupies <2% of the box, as in the PfCRT regression.
        image = np.random.default_rng(17).normal(0, 0.05, (128, 128)).astype(np.float32)
        image[60:68, 60:68] = np.linspace(2, 6, 64).reshape(8, 8)
        before = image.copy()
        lo, hi = np.percentile(image, [2, 98])
        old_display = np.clip((image - lo) / (hi - lo), 0, 1)
        self.assertTrue(np.all(old_display[60:68, 60:68] == 1))

        rendered = _normalize_class_image(image)
        core = rendered[60:68, 60:68]
        self.assertEqual(len(np.unique(core)), 64)
        self.assertEqual(np.count_nonzero(core == 1), 1)
        self.assertGreater(float(core.max() - core.min()), 0.5)
        np.testing.assert_array_equal(image, before)
        self.assertEqual(rendered.dtype, np.float32)

    def test_default_is_linear_and_preserves_negative_density(self):
        image = np.array([[-4, -2, 0], [2, 4, 6]], dtype=np.float32)
        expected = np.array([[0, 0.2, 0.4], [0.6, 0.8, 1]], dtype=np.float32)
        np.testing.assert_allclose(_normalize_class_image(image), expected)
        np.testing.assert_allclose(_normalize_class_image(image * 3 + 8), expected)
        np.testing.assert_allclose(_normalize_class_image(-image), 1 - expected)

    def test_optional_percentiles_are_explicit_and_do_not_modify_source(self):
        image = np.arange(101, dtype=np.float32).reshape(1, -1)
        before = image.copy()
        expected = np.clip((image - 10) / 80, 0, 1)
        np.testing.assert_allclose(_normalize_class_image(image, (10, 90)), expected)
        np.testing.assert_array_equal(image, before)

    def test_nonfinite_pixels_do_not_blank_finite_density(self):
        image = np.array([[np.nan, -2, 0], [np.inf, 2, -np.inf]])
        expected = np.array([[0, 0, 0.5], [0, 1, 0]])
        for contrast in (None, (0, 100)):
            with self.subTest(contrast=contrast):
                np.testing.assert_allclose(_normalize_class_image(image, contrast), expected)

    def test_constant_empty_and_invalid_images_are_finite(self):
        for image in (np.ones((4, 4)), np.full((4, 4), np.nan), np.empty((0, 0))):
            with self.subTest(shape=image.shape):
                rendered = _normalize_class_image(image)
                self.assertEqual(rendered.shape, image.shape)
                self.assertTrue(np.all(rendered == 0))

    def test_collapsed_percentiles_preserve_sparse_signal(self):
        image = np.zeros((128, 128), dtype=np.float32)
        image[64, 64] = 2
        image[64, 65] = 4
        rendered = _normalize_class_image(image, (2, 98))
        self.assertEqual(rendered[64, 64], 0.5)
        self.assertEqual(rendered[64, 65], 1)

    def test_large_float32_range_does_not_overflow(self):
        maximum = np.finfo(np.float32).max
        image = np.array([[-maximum, 0, maximum]], dtype=np.float32)
        with np.errstate(over="raise", invalid="raise"):
            np.testing.assert_allclose(_normalize_class_image(image), [[0, 0.5, 1]])

    def test_invalid_percentile_options_are_rejected(self):
        for contrast in (
            98, "28", (), (2,), (2, 98, 99), (-1, 98), (0, 101),
            (98, 2), (50, 50), (0, np.nan), (0, np.inf), (None, 98),
        ):
            with self.subTest(contrast=contrast):
                with self.assertRaisesRegex(ValueError, "contrast_percentiles"):
                    _normalize_class_image(np.zeros((4, 4)), contrast)


class TwoDClassVisualizationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.job_dir = self.root / "J7"
        self.job_dir.mkdir()

        class_dtype = np.dtype(
            [
                ("uid", "<u8"),
                ("blob/idx", "<u4"),
                ("blob/psize_A", "<f4"),
                ("blob/res_A", "<f4"),
                ("blob/similarity_idx", "<u4"),
            ]
        )
        classes = np.zeros(5, dtype=class_dtype)
        classes["uid"] = np.arange(5)
        classes["blob/idx"] = [4, 3, 2, 1, 0]
        classes["blob/psize_A"] = 1.5
        classes["blob/res_A"] = [8.0, 20.0, 6.0, 30.0, 7.0]
        classes["blob/similarity_idx"] = [2, 4, 0, 3, 1]

        particle_dtype = np.dtype(
            [
                ("alignments2D/class", "<u4"),
                ("alignments2D/class_ess", "<f4"),
            ]
        )
        particles = np.zeros(9, dtype=particle_dtype)
        particles["alignments2D/class"] = [0, 0, 1, 2, 2, 2, 3, 4, 4]
        particles["alignments2D/class_ess"] = [
            1.0,
            1.2,
            1.1,
            2.0,
            2.1,
            2.2,
            1.0,
            1.4,
            1.6,
        ]

        rng = np.random.default_rng(7)
        stack = rng.normal(size=(5, 32, 32)).astype(np.float32)
        for idx in range(5):
            stack[idx, 10:22, 10:22] += idx + 1

        write_numpy_file(self.job_dir / "J7_020_class_averages.cs", classes)
        write_numpy_file(self.job_dir / "J7_020_particles.cs", particles)
        with mrcfile.new(
            self.job_dir / "J7_020_class_averages.mrc", overwrite=True
        ) as mrc:
            mrc.set_data(stack)

        # Local-directory rendering does not need a CryoSPARC connection.
        self.workflow = object.__new__(CryoSPARCWorkflow)

    def test_renders_ordered_batches_and_targeted_single_class_images(self):
        result = self.workflow.render_2d_class_batches(
            self.job_dir,
            batch_size=2,
            cols=2,
            output_dir=self.root / "batches",
        )

        self.assertEqual(result["class_indices"], [2, 4, 0, 3, 1])
        self.assertEqual(result["particle_count"], 9)
        self.assertEqual(
            [batch["class_indices"] for batch in result["batches"]],
            [[2, 4], [0, 3], [1]],
        )
        self.assertTrue(all(Path(path).is_file() for path in result["paths"]))

        targeted = self.workflow.render_2d_class_batches(
            self.job_dir,
            class_indices=[1, 4],
            batch_size=1,
            cols=1,
            output_dir=self.root / "targeted",
        )
        self.assertEqual(targeted["class_indices"], [4, 1])
        self.assertEqual(
            [batch["class_indices"] for batch in targeted["batches"]],
            [[4], [1]],
        )

    def test_previews_selected_and_rejected_groups_with_particle_totals(self):
        preview = self.workflow.preview_2d_class_selection(
            self.job_dir,
            selected_indices=[0, 2, 4],
            batch_size=2,
            cols=2,
            output_dir=self.root / "preview",
        )

        self.assertEqual(preview["selected"]["class_indices"], [0, 2, 4])
        self.assertEqual(preview["selected"]["display_order"], [2, 4, 0])
        self.assertEqual(preview["selected"]["particle_count"], 7)
        self.assertEqual(preview["rejected"]["class_indices"], [1, 3])
        self.assertEqual(preview["rejected"]["display_order"], [3, 1])
        self.assertEqual(preview["rejected"]["particle_count"], 2)
        self.assertAlmostEqual(preview["selected_particle_fraction"], 7 / 9)
        self.assertEqual(len(preview["selected"]["paths"]), 2)
        self.assertEqual(len(preview["rejected"]["paths"]), 1)
        self.assertTrue(
            all(
                Path(path).is_file()
                for group in (preview["selected"], preview["rejected"])
                for path in group["paths"]
            )
        )

    def test_rejects_invalid_render_options_and_class_indices(self):
        with self.assertRaisesRegex(ValueError, "sort_mode"):
            self.workflow.render_2d_class_batches(
                self.job_dir,
                sort_mode="typo",
                output_dir=self.root / "bad-sort",
            )
        with self.assertRaisesRegex(ValueError, "eca_stat"):
            self.workflow.render_2d_class_batches(
                self.job_dir,
                eca_stat="typo",
                output_dir=self.root / "bad-eca",
            )
        with self.assertRaisesRegex(ValueError, "valid range"):
            self.workflow.preview_2d_class_selection(
                self.job_dir,
                selected_indices=[0, 9],
                output_dir=self.root / "bad-index",
            )

    def test_all_renderers_forward_and_report_the_contrast_setting(self):
        renderers = [
            (self.workflow.render_2d_class_sheet, {"output": self.root / "sheet.png"}),
            (self.workflow.render_2d_class_batches, {"output_dir": self.root / "batches"}),
            (self.workflow.preview_2d_class_selection, {
                "output_dir": self.root / "preview", "selected_indices": [0, 2, 4],
            }),
        ]
        for render, kwargs in renderers:
            for contrast in (None, [0.2, 99.8]):
                with self.subTest(renderer=render.__name__, contrast=contrast):
                    with patch(
                        "cryosparc_repl._normalize_class_image", wraps=_normalize_class_image
                    ) as normalize:
                        result = render(
                            self.job_dir, cols=2, contrast_percentiles=contrast, **kwargs
                        )
                    self.assertEqual(normalize.call_count, 5)
                    expected = tuple(contrast) if contrast is not None else None
                    self.assertTrue(all(
                        call.args[1] == expected for call in normalize.call_args_list
                    ))
                    self.assertEqual(result["contrast"], {
                        "mode": "percentile" if contrast is not None else "full_range",
                        "percentiles": contrast, "per_class": True,
                    })

    def test_invalid_contrast_fails_before_resolving_or_downloading_job(self):
        with patch.object(self.workflow, "_resolve_2d_class_source") as resolve:
            for name, kwargs in (
                ("render_2d_class_sheet", {}),
                ("render_2d_class_batches", {}),
                ("preview_2d_class_selection", {"selected_indices": [0]}),
            ):
                with self.subTest(renderer=name):
                    with self.assertRaisesRegex(ValueError, "contrast_percentiles"):
                        getattr(self.workflow, name)(
                            "J7", contrast_percentiles=(99, 1), **kwargs
                        )
            resolve.assert_not_called()

    def test_single_class_contrast_labels_fit_and_do_not_cover_density(self):
        from matplotlib.figure import Figure

        savefig = Figure.savefig
        labels = []

        def check_and_save(figure, *args, **kwargs):
            figure.canvas.draw()
            title = figure._suptitle
            bounds = title.get_window_extent(figure.canvas.get_renderer())
            self.assertGreaterEqual(bounds.x0, 0)
            self.assertLessEqual(bounds.x1, figure.bbox.width)
            self.assertLessEqual(bounds.y1, figure.bbox.height)
            self.assertGreater(bounds.y0, figure.axes[0].get_window_extent().y1)
            labels.append(title.get_text())
            return savefig(figure, *args, **kwargs)

        with patch.object(Figure, "savefig", autospec=True, side_effect=check_and_save):
            for contrast in (None, (0.2, 99.8)):
                self.workflow.render_2d_class_batches(
                    self.job_dir, class_indices=[0], cols=1, batch_size=1,
                    contrast_percentiles=contrast, output_dir=self.root / "single",
                )
        self.assertIn("no clipping", labels[0])
        self.assertIn("0.2-99.8 percentiles", labels[1])


if __name__ == "__main__":
    unittest.main()
