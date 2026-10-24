import copy
import difflib
import getpass
import json
import math
import os
import re
import time
import warnings
from pathlib import Path
from typing import Any

from cryosparc.tools import CryoSPARC


DEFAULT_URL = "http://localhost:61000"


# Job type keys are internal CryoSPARC identifiers and have changed between
# releases.  The installed job register remains authoritative; these are only
# ordered lookup hints used by the high-level wrappers below.
_WRAPPER_JOB_TYPES = {
    "import_particle_stack": (
        ("import_particles", "import_particle_stack"),
        ("Import Particle Stack", "Import Particles"),
    ),
    "micrograph_denoiser": (("denoise_train", "micrograph_denoiser"), "Micrograph Denoiser"),
    "micrograph_junk_detector": (("junk_detector_v1", "micrograph_junk_detector"), "Micrograph Junk Detector"),
    "exposure_groups": (("exposure_groups", "exposure_group_utility", "exposure_group_utilities"), "Exposure Group Utilities"),
    "reference_motion_correction": (("reference_motion_correction", "reference_motion_correction_multi"), "Reference Based Motion Correction"),
    "subset_particles": (("subset_particles", "subset_particles_by_statistic"), "Subset Particles by Statistic"),
    "rebalance_orientations": (("rebalance_3D", "rebalance_orientations"), "Rebalance Orientations"),
    "homogeneous_reconstruct": (("homo_reconstruct", "homogeneous_reconstruct"), "Homogeneous Reconstruction Only"),
    "heterogeneous_reconstruct": (("hetero_reconstruct", "heterogeneous_reconstruct"), "Heterogeneous Reconstruction Only"),
    "class_3d": (("class_3D", "class_3d"), "3D Classification"),
    "regroup_3d": (("regroup_3D", "regroup_3d"), "Regroup 3D Classes"),
    "volume_tools": (("volume_tools",), "Volume Tools"),
    "volume_alignment_tools": (("volume_alignment_tools",), "Volume Alignment Tools"),
    "align_3d_maps": (("align_3D_new", "align_3D_maps", "align_3d_maps"), "Align 3D Maps"),
    "symmetry_expand": (("sym_expand", "symmetry_expansion", "symmetry_expand"), "Symmetry Expansion"),
    "particle_subtract": (("particle_subtract", "particle_subtraction", "subtract_particles"), "Particle Subtraction"),
    "local_refine": (("new_local_refine", "local_refine_new", "local_refine"), "Local Refinement"),
    "local_resolution": (("local_resolution", "local_resolution_estimation"), "Local Resolution Estimation"),
    "local_filter": (("local_filter", "local_filtering"), "Local Filtering"),
    "variability_3d": (("var_3D", "variability_3D", "variability_3d"), "3D Variability"),
    "variability_3d_display": (("var_3D_disp", "var_3D_display", "variability_3D_display", "variability_3d_display"), "3D Variability Display"),
    "flex_data_prep": (("flex_prepare", "flex_data_prep"), "3D Flex Data Prep"),
    "flex_mesh_prep": (("flex_mesh", "flex_mesh_prep", "flex_prepare_mesh"), "3D Flex Mesh Prep"),
    "flex_train": (("flex_train",), "3D Flex Training"),
    "flex_generate": (("flex_generate",), "3D Flex Generator"),
    "flex_reconstruct": (("flex_reconstruct", "flex_refine"), "3D Flex Reconstruction"),
}


def _model_dict(value):
    """Return a plain mapping without assuming one CryoSPARC/Pydantic version."""
    if isinstance(value, dict):
        return copy.deepcopy(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "dict"):
        return value.dict()
    return copy.deepcopy(getattr(value, "__dict__", {}))


def _named_specs(value):
    """Normalize job-register input/output/parameter containers to a dict."""
    if value is None:
        return {}
    if hasattr(value, "root"):
        value = value.root
    if isinstance(value, (list, tuple)):
        return {str(name): {} for name in value}
    if not isinstance(value, dict):
        value = _model_dict(value)
    if set(value) == {"root"} and isinstance(value["root"], dict):
        value = value["root"]
    if not isinstance(value, dict):
        return {}
    return {
        str(name): (_model_dict(metadata) if metadata is not None else {})
        for name, metadata in value.items()
    }


def _normalized_name(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _job_is_unavailable(spec):
    stability = str(spec.get("stability") or spec.get("status") or "").casefold()
    category = str(spec.get("category") or "").casefold()
    title = str(spec.get("title") or "").casefold()
    return bool(
        spec.get("obsolete")
        or spec.get("legacy")
        or spec.get("is_obsolete")
        or spec.get("is_legacy")
        or stability in {"legacy", "obsolete"}
        or category in {"legacy", "obsolete"}
        or "(legacy)" in title
        or "(obsolete)" in title
    )


def _primitive_type_matches(value, expected):
    expected = str(expected or "").casefold()
    if expected in {"", "any", "object"}:
        return True
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, (list, tuple))
    return True  # Unknown registry type: defer to CryoSPARC rather than reject.


def _param_validation_errors(value, metadata):
    """Validate portable JSON-schema-like metadata exposed by the live registry."""
    metadata = metadata if isinstance(metadata, dict) else {}
    expected = []
    if metadata.get("type") is not None:
        expected.append(metadata["type"])
    for variant in metadata.get("anyOf") or metadata.get("any_of") or []:
        if isinstance(variant, dict) and variant.get("type") is not None:
            expected.append(variant["type"])
    expected = list(dict.fromkeys(expected))

    errors = []
    if expected and not any(_primitive_type_matches(value, kind) for kind in expected):
        errors.append(f"expected one of {expected}, got {type(value).__name__}")
        return errors

    enum = metadata.get("enum")
    if enum is None:
        enum = metadata.get("choices") or metadata.get("values")
    if enum is not None and value not in enum:
        errors.append(f"must be one of {list(enum)!r}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        bounds = (
            ("gt", metadata.get("gt"), lambda x, limit: x > limit, ">"),
            ("ge", metadata.get("ge", metadata.get("minimum")), lambda x, limit: x >= limit, ">="),
            ("lt", metadata.get("lt"), lambda x, limit: x < limit, "<"),
            ("le", metadata.get("le", metadata.get("maximum")), lambda x, limit: x <= limit, "<="),
            ("exclusiveMinimum", metadata.get("exclusiveMinimum"), lambda x, limit: x > limit, ">"),
            ("exclusiveMaximum", metadata.get("exclusiveMaximum"), lambda x, limit: x < limit, "<"),
        )
        for _name, limit, predicate, label in bounds:
            if limit is not None:
                try:
                    if not predicate(value, limit):
                        errors.append(f"must be {label} {limit}")
                except TypeError:
                    pass
        multiple = metadata.get("multiple_of", metadata.get("multipleOf"))
        if multiple not in (None, 0):
            try:
                quotient = float(value) / float(multiple)
                if not math.isclose(quotient, round(quotient), rel_tol=1e-9, abs_tol=1e-9):
                    errors.append(f"must be a multiple of {multiple}")
            except (TypeError, ValueError, ZeroDivisionError):
                pass

    if isinstance(value, (list, tuple)):
        item_spec = metadata.get("items")
        if isinstance(item_spec, dict) and item_spec.get("type"):
            invalid = [index for index, item in enumerate(value) if not _primitive_type_matches(item, item_spec["type"])]
            if invalid:
                errors.append(f"items at indices {invalid} do not have type {item_spec['type']!r}")
    return errors


def _clean(values):
    return {key: value for key, value in values.items() if value is not None}


def _positive_float(name, value):
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive number, got {value!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive number, got {value!r}")
    return value


def _positive_even_int(name, value):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive even integer, got {value!r}")
    try:
        integer = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{name} must be a positive even integer, got {value!r}"
        ) from exc
    if integer != value or integer <= 0 or integer % 2:
        raise ValueError(f"{name} must be a positive even integer, got {value!r}")
    return integer


def plan_particle_extraction(
    box_size_pix,
    source_pixel_size_A,
    particle_extent_A,
    fourier_crop_size_pix=None,
    target_resolution_A=None,
    flexible=False,
    allow_small_box=False,
    small_box_rationale=None,
):
    """Validate and summarize the physical sampling of a particle extraction.

    The full extraction box determines the physical field of view. Fourier
    cropping preserves that field of view while increasing the output pixel
    size and lowering its Nyquist limit. The maximum plausible particle extent
    must therefore be compared with the full physical field, not with either
    pixel count in isolation.
    """

    box_size_pix = _positive_even_int("box_size_pix", box_size_pix)
    source_pixel_size_A = _positive_float(
        "source_pixel_size_A", source_pixel_size_A
    )
    particle_extent_A = _positive_float("particle_extent_A", particle_extent_A)
    if not isinstance(flexible, bool):
        raise ValueError(f"flexible must be boolean, got {flexible!r}")
    if not isinstance(allow_small_box, bool):
        raise ValueError(
            f"allow_small_box must be boolean, got {allow_small_box!r}"
        )

    if fourier_crop_size_pix is not None:
        fourier_crop_size_pix = _positive_even_int(
            "fourier_crop_size_pix", fourier_crop_size_pix
        )
        if fourier_crop_size_pix > box_size_pix:
            raise ValueError(
                "fourier_crop_size_pix cannot exceed the full extraction "
                f"box ({fourier_crop_size_pix} > {box_size_pix})"
            )

    if target_resolution_A is not None:
        target_resolution_A = _positive_float(
            "target_resolution_A", target_resolution_A
        )

    physical_box_A = box_size_pix * source_pixel_size_A
    box_to_particle_ratio = physical_box_A / particle_extent_A
    output_box_size_pix = fourier_crop_size_pix or box_size_pix
    output_pixel_size_A = physical_box_A / output_box_size_pix
    nyquist_resolution_A = 2.0 * output_pixel_size_A
    minimum_ratio = 2.0
    recommended_ratio = 2.5 if flexible else minimum_ratio
    plan_warnings = []

    if box_to_particle_ratio < minimum_ratio:
        message = (
            "Unsafe extraction box: the physical field of view is "
            f"{physical_box_A:.2f} A ({box_to_particle_ratio:.2f}x a "
            f"{particle_extent_A:.2f} A particle), below the required "
            f"{minimum_ratio:.1f}x minimum."
        )
        if not allow_small_box:
            raise ValueError(
                message
                + " Increase box_size_pix or pass allow_small_box=True with a "
                "non-empty small_box_rationale for an explicit override."
            )
        if not str(small_box_rationale or "").strip():
            raise ValueError(
                message
                + " A non-empty small_box_rationale is required for an override."
            )
        plan_warnings.append(
            message + f" Explicit override: {str(small_box_rationale).strip()}"
        )
    elif flexible and box_to_particle_ratio < recommended_ratio:
        plan_warnings.append(
            "Flexible-particle extraction is below the preferred 2.5x physical "
            f"field of view ({box_to_particle_ratio:.2f}x); verify extended "
            "conformations are fully contained."
        )

    if (
        target_resolution_A is not None
        and nyquist_resolution_A > target_resolution_A
    ):
        plan_warnings.append(
            "Fourier-cropped sampling has a Nyquist limit of "
            f"{nyquist_resolution_A:.2f} A, which cannot support the requested "
            f"{target_resolution_A:.2f} A target. Use a larger crop for the "
            "high-resolution particle stack."
        )

    return {
        "schema_version": 1,
        "box_size_pix": box_size_pix,
        "source_pixel_size_A": source_pixel_size_A,
        "particle_extent_A": particle_extent_A,
        "physical_box_A": round(physical_box_A, 6),
        "box_to_particle_ratio": round(box_to_particle_ratio, 6),
        "minimum_box_to_particle_ratio": minimum_ratio,
        "recommended_box_to_particle_ratio": recommended_ratio,
        "fourier_crop_size_pix": fourier_crop_size_pix,
        "output_box_size_pix": output_box_size_pix,
        "output_pixel_size_A": round(output_pixel_size_A, 6),
        "nyquist_resolution_A": round(nyquist_resolution_A, 6),
        "target_resolution_A": target_resolution_A,
        "flexible": flexible,
        "small_box_override": bool(box_to_particle_ratio < minimum_ratio),
        "small_box_rationale": (
            str(small_box_rationale).strip()
            if box_to_particle_ratio < minimum_ratio
            else None
        ),
        "warnings": plan_warnings,
        "post_extraction_review_required": True,
    }


def _uid(job_or_uid):
    if isinstance(job_or_uid, str):
        return job_or_uid
    if hasattr(job_or_uid, "model"):
        return job_or_uid.model.uid
    return job_or_uid.uid


def _find_required_file(job_dir: Path, pattern: str, exclude: tuple[str, ...] = ()) -> Path:
    matches = sorted(
        path
        for path in job_dir.glob(pattern)
        if all(token not in path.name for token in exclude)
    )
    if not matches:
        raise FileNotFoundError(f"No files matched {pattern!r} in {job_dir}")
    if len(matches) > 1:
        raise FileNotFoundError(
            f"Multiple files matched {pattern!r} in {job_dir}: {[p.name for p in matches]}"
        )
    return matches[0]


def _validate_class_contrast(contrast_percentiles):
    if contrast_percentiles is None:
        return None
    try:
        if isinstance(contrast_percentiles, (str, bytes)):
            raise ValueError
        lo, hi = contrast_percentiles
        lo, hi = float(lo), float(hi)
    except (TypeError, ValueError):
        raise ValueError(
            "contrast_percentiles must be None or two numbers with 0 <= low < high <= 100"
        ) from None
    if not (math.isfinite(lo) and math.isfinite(hi) and 0 <= lo < hi <= 100):
        raise ValueError(
            "contrast_percentiles must be None or two numbers with 0 <= low < high <= 100"
        )
    return lo, hi


def _class_contrast_metadata(contrast_percentiles):
    percentiles = _validate_class_contrast(contrast_percentiles)
    return {
        "mode": "full_range" if percentiles is None else "percentile",
        "percentiles": list(percentiles) if percentiles is not None else None,
        "per_class": True,
    }


def _normalize_class_image(image, contrast_percentiles=None):
    """Scale for display only; preserve the full finite intensity range by default.

    Whole-box percentile clipping can saturate a small particle's entire core.
    Only clip when explicitly requested, and ignore non-finite pixels when
    calculating limits. Constant/invalid images and invalid pixels display black.
    """

    import numpy as np

    percentiles = _validate_class_contrast(contrast_percentiles)
    image = np.asarray(image, dtype=np.float64)
    finite = np.isfinite(image)
    normalized = np.zeros(image.shape, dtype=np.float32)
    if not np.any(finite):
        return normalized
    values = image[finite]
    lo, hi = values.min(), values.max()
    if percentiles is not None:
        clipped_lo, clipped_hi = np.percentile(values, percentiles)
        # Sparse images can have identical percentile limits; don't erase them.
        if clipped_hi > clipped_lo:
            lo, hi = clipped_lo, clipped_hi
    if hi > lo:
        normalized[finite] = np.clip((values - lo) / (hi - lo), 0.0, 1.0)
    return normalized


def _class_counts(particles, n_classes: int):
    import numpy as np

    return np.bincount(particles["alignments2D/class"], minlength=n_classes).astype(np.int64)


def _class_eca(particles, n_classes: int, stat: str):
    import numpy as np

    if stat not in {"median", "mean"}:
        raise ValueError(f"eca_stat must be 'median' or 'mean', got {stat!r}")
    values = np.full(n_classes, np.nan, dtype=np.float32)
    classes = particles["alignments2D/class"]
    ess = particles["alignments2D/class_ess"]
    reducer = np.median if stat == "median" else np.mean
    for class_idx in range(n_classes):
        mask = classes == class_idx
        if np.any(mask):
            values[class_idx] = float(reducer(ess[mask]))
    return values


def _sort_order(classes, counts, mode: str):
    import numpy as np

    if mode == "similarity":
        if "blob/similarity_idx" not in classes.dtype.names:
            raise ValueError("Requested similarity sort, but blob/similarity_idx is missing")
        return np.argsort(classes["blob/similarity_idx"], kind="stable")
    if mode == "size":
        return np.argsort(-counts, kind="stable")
    if mode == "res":
        return np.argsort(classes["blob/res_A"], kind="stable")
    if mode == "index":
        return np.arange(len(classes))
    raise ValueError(
        f"sort_mode must be 'similarity', 'size', 'res', or 'index', got {mode!r}"
    )


def _parse_class_indices(class_indices) -> list[int]:
    if isinstance(class_indices, str):
        parsed = []
        for token in class_indices.replace(" ", "").split(","):
            if not token:
                continue
            if "-" in token:
                start, end = token.split("-", 1)
                parsed.extend(range(int(start), int(end) + 1))
            else:
                parsed.append(int(token))
        class_indices = parsed
    indices = sorted({int(idx) for idx in class_indices})
    if not indices:
        raise ValueError("At least one class index is required.")
    if any(idx < 0 for idx in indices):
        raise ValueError(f"Class indices must be non-negative: {indices}")
    return indices


def _load_2d_class_data(job_dir: Path):
    """Load one exported 2D-classification result and validate its stack."""

    import mrcfile
    import numpy as np

    class_meta_path = _find_required_file(job_dir, "*_class_averages.cs")
    class_stack_path = _find_required_file(job_dir, "*_class_averages.mrc")
    particle_meta_path = _find_required_file(
        job_dir, "*_particles.cs", exclude=("passthrough", "rejected")
    )

    classes = np.load(class_meta_path, allow_pickle=True)
    particles = np.load(particle_meta_path, allow_pickle=True)

    with mrcfile.open(class_stack_path, permissive=True) as mrc:
        stack = np.asarray(mrc.data, dtype=np.float32)

    if stack.ndim == 2:
        stack = stack[np.newaxis, ...]

    n_classes = len(classes)
    if n_classes == 0:
        raise ValueError(f"No classes were found in {class_meta_path}")
    if stack.ndim != 3:
        raise ValueError(
            f"Expected a 2D class stack with 2 or 3 dimensions, got shape {stack.shape}"
        )
    if stack.shape[0] != n_classes:
        raise ValueError(
            f"Stack contains {stack.shape[0]} images but metadata contains {n_classes} classes"
        )

    return classes, particles, stack


def _ordered_class_subset(order, class_indices, n_classes: int):
    """Filter a display order to a validated set of class indices."""

    import numpy as np

    if class_indices is None:
        return np.asarray(order, dtype=np.int64)

    indices = _parse_class_indices(class_indices)
    out_of_range = [idx for idx in indices if idx >= n_classes]
    if out_of_range:
        raise ValueError(
            f"Class indices are outside the valid range 0-{n_classes - 1}: {out_of_range}"
        )
    wanted = set(indices)
    return np.asarray([idx for idx in order if int(idx) in wanted], dtype=np.int64)


def _render_2d_class_indices(
    classes,
    stack,
    counts,
    eca,
    order,
    output: Path,
    cols: int = 10,
    scale_bar_a: float = 120.0,
    sheet_title: str | None = None,
    title_color: str = "white",
    contrast_percentiles=None,
) -> Path:
    """Render a prepared, ordered subset without recomputing class statistics."""

    import matplotlib
    import numpy as np

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if cols <= 0:
        raise ValueError("cols must be greater than zero")
    if len(order) == 0:
        raise ValueError("At least one class is required to render a sheet")
    contrast_percentiles = _validate_class_contrast(contrast_percentiles)
    contrast_label = "Linear full range (per class; no clipping)"
    if contrast_percentiles is not None:
        lo, hi = contrast_percentiles
        contrast_label = f"Linear {lo:g}-{hi:g} percentiles (per class; clipped)"

    tile_h = stack.shape[1]
    effective_cols = min(cols, len(order))
    rows = math.ceil(len(order) / effective_cols)
    title_height = 0.55 if sheet_title else 0.35
    if effective_cols == 1:
        contrast_label = contrast_label.replace(" (", "\n(")
        title_height += 0.2
    figure_height = rows * 2.05 + title_height

    fig, axes = plt.subplots(
        rows,
        effective_cols,
        figsize=(effective_cols * 2.05, figure_height),
        dpi=200,
    )
    axes = np.atleast_2d(axes)

    bg = "#3a3a3a"
    lime = "#39ff14"
    fig.patch.set_facecolor(bg)
    # Keep contrast provenance visible even if the PNG is viewed without JSON.
    fig.suptitle(
        f"{sheet_title}\n{contrast_label}" if sheet_title else contrast_label,
        color=title_color,
        fontsize=8 if effective_cols == 1 else 10,
        y=0.99,
    )

    pixel_size = float(classes["blob/psize_A"][0])
    scale_bar_px = int(round(scale_bar_a / pixel_size)) if scale_bar_a > 0 else 0

    for panel_idx, ax in enumerate(axes.flat):
        ax.set_facecolor(bg)
        ax.set_xticks([])
        ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_visible(False)

        if panel_idx >= len(order):
            ax.axis("off")
            continue

        class_idx = int(order[panel_idx])
        image = _normalize_class_image(
            stack[int(classes["blob/idx"][class_idx])], contrast_percentiles
        )
        ax.imshow(image, cmap="gray", interpolation="nearest", vmin=0, vmax=1)

        ax.text(
            0.04,
            0.98,
            f"#{class_idx}",
            color="white",
            fontsize=9,
            fontweight="bold",
            ha="left",
            va="top",
            transform=ax.transAxes,
            bbox=dict(facecolor="black", alpha=0.6, edgecolor="none", pad=1.5),
        )
        ax.text(
            0.5,
            0.98,
            f"{counts[class_idx]} ptcls",
            color=lime,
            fontsize=8,
            ha="center",
            va="top",
            transform=ax.transAxes,
            fontweight="bold",
        )

        eca_text = "nan" if np.isnan(eca[class_idx]) else f"{eca[class_idx]:.1f}"
        ax.text(
            0.5,
            0.03,
            f"{classes['blob/res_A'][class_idx]:.1f} A    {eca_text} ECA",
            color=lime,
            fontsize=8,
            ha="center",
            va="bottom",
            transform=ax.transAxes,
        )

        if scale_bar_px > 0 and panel_idx % effective_cols == 0:
            x = 18
            y0 = tile_h - 18
            y1 = max(y0 - scale_bar_px, 8)
            ax.plot([x, x], [y0, y1], color="white", linewidth=2)
            ax.text(
                x - 7,
                (y0 + y1) / 2,
                f"{int(scale_bar_a)} A",
                color="white",
                fontsize=8,
                rotation=90,
                ha="center",
                va="center",
            )

    top = 1.0 - title_height / figure_height
    plt.subplots_adjust(
        left=0.01,
        right=0.99,
        top=top,
        bottom=0.01,
        wspace=0.02,
        hspace=0.02,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, facecolor=bg)
    plt.close(fig)
    return output.resolve()


def _render_2d_batches(
    classes,
    stack,
    counts,
    eca,
    order,
    output_dir: Path,
    output_stem: str,
    batch_size: int,
    cols: int,
    scale_bar_a: float,
    sheet_label: str,
    title_color: str = "white",
    contrast_percentiles=None,
) -> list[dict[str, Any]]:
    """Render prepared classes in batches and describe every generated image."""

    if batch_size <= 0:
        raise ValueError("batch_size must be greater than zero")
    if cols <= 0:
        raise ValueError("cols must be greater than zero")
    if len(order) == 0:
        return []

    total_batches = math.ceil(len(order) / batch_size)
    batches = []
    for batch_number, start in enumerate(range(0, len(order), batch_size), start=1):
        batch_order = order[start : start + batch_size]
        output = output_dir / (
            f"{output_stem}_batch_{batch_number:03d}_of_{total_batches:03d}.png"
        )
        path = _render_2d_class_indices(
            classes=classes,
            stack=stack,
            counts=counts,
            eca=eca,
            order=batch_order,
            output=output,
            cols=cols,
            scale_bar_a=scale_bar_a,
            sheet_title=f"{sheet_label} - batch {batch_number}/{total_batches}",
            title_color=title_color,
            contrast_percentiles=contrast_percentiles,
        )
        class_indices = [int(idx) for idx in batch_order]
        batches.append(
            {
                "batch": batch_number,
                "path": str(path),
                "class_indices": class_indices,
                "particle_count": int(sum(counts[idx] for idx in class_indices)),
            }
        )
    return batches


def _prepare_2d_class_render(job_dir: Path, sort_mode: str, eca_stat: str):
    classes, particles, stack = _load_2d_class_data(job_dir)
    counts = _class_counts(particles, len(classes))
    eca = _class_eca(particles, len(classes), eca_stat)
    order = _sort_order(classes, counts, sort_mode)
    return classes, stack, counts, eca, order


def _render_2d_class_sheet_from_dir(
    job_dir: Path,
    output: Path,
    sort_mode: str = "similarity",
    eca_stat: str = "median",
    cols: int = 10,
    scale_bar_a: float = 120.0,
    contrast_percentiles=None,
) -> dict[str, Any]:
    classes, stack, counts, eca, order = _prepare_2d_class_render(
        job_dir, sort_mode, eca_stat
    )
    path = _render_2d_class_indices(
        classes=classes,
        stack=stack,
        counts=counts,
        eca=eca,
        order=order,
        output=output,
        cols=cols,
        scale_bar_a=scale_bar_a,
        contrast_percentiles=contrast_percentiles,
    )
    n_classes = len(classes)
    return {
        "path": str(path),
        "n_classes": n_classes,
        "sort": sort_mode,
        "contrast": _class_contrast_metadata(contrast_percentiles),
        "class_order": [int(idx) for idx in order],
        "counts": {int(idx): int(counts[idx]) for idx in range(n_classes)},
        "res_A": {int(idx): float(classes["blob/res_A"][idx]) for idx in range(n_classes)},
    }


class CryoSPARCWorkflow:
    """Small REPL-friendly wrapper around cryosparc-tools."""

    def __init__(self, cs, project_uid=None, workspace_uid=None):
        self.cs = cs
        self.project = None
        self.workspace = None
        self.specs = {}
        for register_spec in cs.job_register.specs:
            spec = _model_dict(register_spec)
            job_type = spec.get("type") or getattr(register_spec, "type", None)
            if job_type:
                self.specs[str(job_type)] = spec
        if project_uid:
            self.use_project(project_uid)
        if workspace_uid:
            self.use_workspace(workspace_uid)

    def __repr__(self):
        project = self.project.model.uid if self.project else None
        workspace = self.workspace.model.uid if self.workspace else None
        return f"CryoSPARCWorkflow(project={project!r}, workspace={workspace!r})"

    def use_project(self, project_uid):
        self.project = self.cs.find_project(project_uid)
        return self

    def create_project(self, title, project_dir=None, desc=None):
        if not project_dir:
            raise ValueError(
                "project_dir is required and must be an existing server-visible "
                "CryoSPARC project parent directory."
            )
        kwargs = {"title": title}
        if desc is not None:
            kwargs["description"] = desc
        if project_dir is not None:
            kwargs["parent_dir"] = project_dir

        try:
            project = self.cs.api.projects.create(**kwargs)
        except TypeError:
            if project_dir is not None:
                kwargs["project_dir"] = kwargs.pop("parent_dir")
            project = self.cs.api.projects.create(**kwargs)

        self.project = self.cs.find_project(project.uid)
        return self.project

    def use_workspace(self, workspace_uid, project_uid=None):
        if project_uid:
            self.use_project(project_uid)
        if not self.project:
            raise ValueError("Select a project first with use_project('P1').")
        self.workspace = self.project.find_workspace(workspace_uid)
        return self

    def create_workspace(self, title, project_uid=None, desc=None):
        if project_uid:
            self.use_project(project_uid)
        if not self.project:
            raise ValueError("Select a project first with use_project('P1').")
        self.workspace = self.project.create_workspace(title, desc=desc)
        return self.workspace

    def list_projects(self):
        return [
            {"uid": p.model.uid, "title": getattr(p, "title", None)}
            for p in self.cs.find_projects()
        ]

    def list_workspaces(self, project_uid=None):
        if project_uid:
            project = self.cs.find_project(project_uid)
            workspaces = project.find_workspaces()
        elif self.project:
            workspaces = self.project.find_workspaces()
        else:
            workspaces = self.cs.find_workspaces()
        return [
            {"uid": w.model.uid, "project_uid": w.model.project_uid, "title": getattr(w, "title", None)}
            for w in workspaces
        ]

    def list_jobs(self, project_uid=None, workspace_uid=None, limit=20):
        if project_uid or workspace_uid:
            jobs = self.cs.find_jobs(project_uid=project_uid, workspace_uid=workspace_uid)
        elif self.workspace:
            jobs = self.workspace.find_jobs(order=-1)
        elif self.project:
            jobs = self.project.find_jobs(order=-1)
        else:
            jobs = self.cs.find_jobs(order=-1)
        return [job_summary(job) for _, job in zip(range(limit), jobs)]

    def audit_jobs(self, project_uid=None, workspace_uid=None, limit=50, include_ok=False):
        if project_uid or workspace_uid:
            jobs = self.cs.find_jobs(project_uid=project_uid, workspace_uid=workspace_uid, order=-1)
        elif self.workspace:
            jobs = self.workspace.find_jobs(order=-1)
        elif self.project:
            jobs = self.project.find_jobs(order=-1)
        else:
            jobs = self.cs.find_jobs(order=-1)

        rows = []
        for _, job in zip(range(limit), jobs):
            summary = job_summary(job)
            summary["has_error"] = bool(getattr(job.model, "has_error", False))
            summary["has_warning"] = bool(getattr(job.model, "has_warning", False))
            summary["queue_status"] = getattr(job.model, "queue_status", None)
            summary["queue_message"] = getattr(job.model, "queue_message", None)
            summary["errors_run"] = getattr(job.model, "errors_run", None)
            summary["build_errors"] = getattr(job.model, "build_errors", None)
            if include_ok or summary["status"] in {"failed", "killed"} or summary["has_error"] or summary["has_warning"]:
                rows.append(summary)
        return rows

    def find_job(self, job_uid, project_uid=None):
        project_uid = project_uid or (self.project.model.uid if self.project else None)
        if not project_uid:
            raise ValueError("Select a project first or pass project_uid.")
        return self.cs.find_job(project_uid, _uid(job_uid))

    def export_png_assets(self, job_or_uid, out_dir="cryosparc_png_assets", project_uid=None):
        job = job_or_uid if hasattr(job_or_uid, "list_assets") else self.find_job(job_or_uid, project_uid=project_uid)
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)

        paths = []
        for asset in job.list_assets():
            if isinstance(asset, dict):
                filename = asset.get("filename")
                asset_id = asset.get("id") or asset.get("_id")
            else:
                filename = getattr(asset, "filename", None)
                asset_id = getattr(asset, "id", None) or getattr(asset, "_id", None)
            if not filename or not asset_id:
                continue
            if filename.lower().endswith(".png"):
                path = out / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                job.download_asset(asset_id, path)
                paths.append(str(path))

        return {"job_uid": job.model.uid, "count": len(paths), "paths": paths}

    def _download_2d_class_files(self, job, cache_dir="cryosparc_class_sheets"):
        job_dir = Path(cache_dir) / f"{job.project_uid}_{job.uid}"
        job_dir.mkdir(parents=True, exist_ok=True)

        files = job.list_files(recursive=True)
        wanted = []
        for filename in files:
            name = Path(filename).name
            if name.endswith("_class_averages.cs"):
                wanted.append(filename)
            elif name.endswith("_class_averages.mrc"):
                wanted.append(filename)
            elif (
                name.endswith("_particles.cs")
                and "passthrough" not in name
                and "rejected" not in name
            ):
                wanted.append(filename)

        if not wanted:
            raise FileNotFoundError(f"No 2D class files found in job {job.uid}.")

        for filename in wanted:
            target = job_dir / Path(filename).name
            if not target.exists():
                job.download_file(filename, target)
        return job_dir

    def _resolve_2d_class_source(
        self,
        job_or_uid_or_dir,
        project_uid=None,
        cache_dir="cryosparc_class_sheets",
    ):
        path = (
            Path(job_or_uid_or_dir).expanduser()
            if isinstance(job_or_uid_or_dir, (str, os.PathLike))
            else None
        )
        if path is not None and path.exists() and path.is_dir():
            return path, path.name

        job = (
            job_or_uid_or_dir
            if hasattr(job_or_uid_or_dir, "list_files")
            else self.find_job(job_or_uid_or_dir, project_uid=project_uid)
        )
        return self._download_2d_class_files(job, cache_dir=cache_dir), job.uid

    def render_2d_class_sheet(
        self,
        job_or_uid_or_dir,
        output=None,
        sort_mode="similarity",
        eca_stat="median",
        cols=10,
        scale_bar_a=120.0,
        project_uid=None,
        cache_dir="cryosparc_class_sheets",
        contrast_percentiles=None,
    ):
        """Render classes with unclipped linear contrast unless explicitly overridden."""

        contrast_percentiles = _validate_class_contrast(contrast_percentiles)
        job_dir, job_uid = self._resolve_2d_class_source(
            job_or_uid_or_dir,
            project_uid=project_uid,
            cache_dir=cache_dir,
        )

        output = (
            Path(output).expanduser()
            if output
            else Path(cache_dir) / f"{job_uid}_class_sheet_{sort_mode}.png"
        )
        return _render_2d_class_sheet_from_dir(
            job_dir=job_dir,
            output=output,
            sort_mode=sort_mode,
            eca_stat=eca_stat,
            cols=cols,
            scale_bar_a=scale_bar_a,
            contrast_percentiles=contrast_percentiles,
        )

    def render_2d_class_batches(
        self,
        job_or_uid_or_dir,
        batch_size=8,
        class_indices=None,
        output_dir=None,
        output_stem=None,
        sort_mode="similarity",
        eca_stat="median",
        cols=4,
        scale_bar_a=120.0,
        project_uid=None,
        cache_dir="cryosparc_class_sheets",
        contrast_percentiles=None,
    ):
        """Render all or selected 2D classes into model-reviewable batches."""

        contrast_percentiles = _validate_class_contrast(contrast_percentiles)
        job_dir, job_uid = self._resolve_2d_class_source(
            job_or_uid_or_dir,
            project_uid=project_uid,
            cache_dir=cache_dir,
        )
        classes, stack, counts, eca, order = _prepare_2d_class_render(
            job_dir, sort_mode, eca_stat
        )
        display_order = _ordered_class_subset(order, class_indices, len(classes))
        output_dir = (
            Path(output_dir).expanduser()
            if output_dir
            else Path(cache_dir) / f"{job_uid}_class_review"
        )
        output_stem = output_stem or f"{job_uid}_{sort_mode}"
        rendered = _render_2d_batches(
            classes=classes,
            stack=stack,
            counts=counts,
            eca=eca,
            order=display_order,
            output_dir=output_dir,
            output_stem=output_stem,
            batch_size=batch_size,
            cols=cols,
            scale_bar_a=scale_bar_a,
            sheet_label=job_uid,
            contrast_percentiles=contrast_percentiles,
        )
        indices = [int(idx) for idx in display_order]
        return {
            "job_uid": job_uid,
            "source_dir": str(job_dir.resolve()),
            "sort": sort_mode,
            "contrast": _class_contrast_metadata(contrast_percentiles),
            "batch_size": int(batch_size),
            "class_indices": indices,
            "n_classes": len(indices),
            "particle_count": int(sum(counts[idx] for idx in indices)),
            "batches": rendered,
            "paths": [batch["path"] for batch in rendered],
        }

    def preview_2d_class_selection(
        self,
        job_or_uid_or_dir,
        selected_indices,
        batch_size=8,
        output_dir=None,
        sort_mode="similarity",
        eca_stat="median",
        cols=4,
        scale_bar_a=120.0,
        project_uid=None,
        cache_dir="cryosparc_class_sheets",
        contrast_percentiles=None,
    ):
        """Render proposed selected and rejected classes before creating a job."""

        contrast_percentiles = _validate_class_contrast(contrast_percentiles)
        job_dir, job_uid = self._resolve_2d_class_source(
            job_or_uid_or_dir,
            project_uid=project_uid,
            cache_dir=cache_dir,
        )
        classes, stack, counts, eca, order = _prepare_2d_class_render(
            job_dir, sort_mode, eca_stat
        )
        selected_numeric = _parse_class_indices(selected_indices)
        selected_order = _ordered_class_subset(order, selected_numeric, len(classes))
        selected_set = set(selected_numeric)
        rejected_order = order[
            [int(idx) not in selected_set for idx in order]
        ]
        output_dir = (
            Path(output_dir).expanduser()
            if output_dir
            else Path(cache_dir) / f"{job_uid}_class_review"
        )

        selected_batches = _render_2d_batches(
            classes=classes,
            stack=stack,
            counts=counts,
            eca=eca,
            order=selected_order,
            output_dir=output_dir,
            output_stem=f"{job_uid}_selected",
            batch_size=batch_size,
            cols=cols,
            scale_bar_a=scale_bar_a,
            sheet_label=f"SELECTED {job_uid}",
            title_color="#39ff14",
            contrast_percentiles=contrast_percentiles,
        )
        rejected_batches = _render_2d_batches(
            classes=classes,
            stack=stack,
            counts=counts,
            eca=eca,
            order=rejected_order,
            output_dir=output_dir,
            output_stem=f"{job_uid}_rejected",
            batch_size=batch_size,
            cols=cols,
            scale_bar_a=scale_bar_a,
            sheet_label=f"REJECTED {job_uid}",
            title_color="#ff6b6b",
            contrast_percentiles=contrast_percentiles,
        )

        def summary(indices, rendered):
            display_indices = [int(idx) for idx in indices]
            numeric_indices = sorted(display_indices)
            return {
                "class_indices": numeric_indices,
                "display_order": display_indices,
                "n_classes": len(display_indices),
                "particle_count": int(sum(counts[idx] for idx in display_indices)),
                "batches": rendered,
                "paths": [batch["path"] for batch in rendered],
            }

        selected = summary(selected_order, selected_batches)
        rejected = summary(rejected_order, rejected_batches)
        total_particles = selected["particle_count"] + rejected["particle_count"]
        return {
            "job_uid": job_uid,
            "source_dir": str(job_dir.resolve()),
            "sort": sort_mode,
            "contrast": _class_contrast_metadata(contrast_percentiles),
            "batch_size": int(batch_size),
            "total_classes": len(classes),
            "total_particles": total_particles,
            "selected": selected,
            "rejected": rejected,
            "selected_particle_fraction": (
                selected["particle_count"] / total_particles if total_particles else 0.0
            ),
        }

    def job_types(self, search=None):
        types = sorted(self.specs)
        if search:
            search = search.lower()
            types = [
                name
                for name in types
                if search in name.lower()
                or search in str(self.specs[name].get("title") or "").lower()
            ]
        return types

    def resolve_job_type(self, candidates, title=None, category=None):
        """Resolve an active installed job, preferring ordered internal keys.

        Title/category matching is deliberately exact (after punctuation and
        case normalization), because a fuzzy match must never silently select a
        scientifically different CryoSPARC job. Fuzzy matches are error hints
        only.
        """
        candidates = (candidates,) if isinstance(candidates, str) else tuple(candidates)
        rejected = []
        for candidate in candidates:
            spec = self.specs.get(candidate)
            if spec is None:
                continue
            if _job_is_unavailable(spec):
                rejected.append(candidate)
                continue
            return candidate

        titles = (title,) if isinstance(title, str) else tuple(title or ())
        wanted_titles = {_normalized_name(value) for value in titles if value}
        wanted_categories = {
            _normalized_name(value)
            for value in ((category,) if isinstance(category, str) else tuple(category or ()))
            if value
        }
        title_matches = []
        rejected_title_matches = []
        if wanted_titles:
            for job_type, spec in self.specs.items():
                if _normalized_name(spec.get("title")) not in wanted_titles:
                    continue
                if _job_is_unavailable(spec):
                    rejected_title_matches.append(job_type)
                    continue
                title_matches.append(job_type)

        if wanted_categories:
            title_matches = [
                job_type
                for job_type in title_matches
                if _normalized_name(self.specs[job_type].get("category")) in wanted_categories
            ]

        if len(title_matches) == 1:
            return title_matches[0]
        if len(title_matches) > 1:
            matches = [
                {
                    "type": job_type,
                    "title": self.specs[job_type].get("title"),
                    "category": self.specs[job_type].get("category"),
                }
                for job_type in title_matches
            ]
            raise ValueError(
                f"Ambiguous active CryoSPARC job title/category for {titles!r}: {matches}"
            )

        query_terms = [str(value) for value in (*candidates, *titles) if value]
        searchable = list(self.specs) + [
            str(spec.get("title")) for spec in self.specs.values() if spec.get("title")
        ]
        nearby = []
        for query in query_terms:
            nearby.extend(difflib.get_close_matches(query, searchable, n=4, cutoff=0.45))
        nearby = list(dict.fromkeys(nearby))[:8]
        unavailable = list(dict.fromkeys(rejected + rejected_title_matches))
        detail = f" Only legacy/obsolete matches were found: {unavailable}." if unavailable else ""
        hint = f" Nearby installed jobs: {nearby}." if nearby else ""
        raise KeyError(
            f"No active installed CryoSPARC job matches keys {list(candidates)!r} "
            f"or exact title(s) {list(titles)!r}.{detail}{hint}"
        )

    def job_spec(self, job_type):
        resolved = self.resolve_job_type(job_type, title=job_type)
        spec = self.specs[resolved]
        inputs = _named_specs(spec.get("inputs"))
        outputs = _named_specs(spec.get("outputs"))
        params = _named_specs(spec.get("params"))
        param_ranges = {}
        for name, metadata in params.items():
            bounds = {
                key: metadata[key]
                for key in (
                    "gt",
                    "ge",
                    "lt",
                    "le",
                    "minimum",
                    "maximum",
                    "exclusiveMinimum",
                    "exclusiveMaximum",
                    "multiple_of",
                    "multipleOf",
                )
                if metadata.get(key) is not None
            }
            if bounds:
                param_ranges[name] = bounds
        return {
            "type": resolved,
            "title": spec.get("title", resolved),
            "description": spec.get("description", ""),
            "category": spec.get("category"),
            "stability": spec.get("stability"),
            "hidden": spec.get("hidden", False),
            "input_count": len(inputs),
            "output_count": len(outputs),
            "inputs": list(inputs),
            "outputs": list(outputs),
            "input_types": {name: value.get("type") for name, value in inputs.items()},
            "output_types": {name: value.get("type") for name, value in outputs.items()},
            "required_params": [
                key
                for key, value in params.items()
                if value.get("required_param") or value.get("required")
            ],
            "params": list(params),
            "param_types": {name: value.get("type") for name, value in params.items()},
            "param_defaults": {
                name: value.get("default")
                for name, value in params.items()
                if "default" in value
            },
            "param_enums": {
                name: value.get("enum", value.get("choices", value.get("values")))
                for name, value in params.items()
                if any(key in value for key in ("enum", "choices", "values"))
            },
            "param_ranges": param_ranges,
            "input_specs": inputs,
            "output_specs": outputs,
            "param_specs": params,
        }

    def out(self, job_or_uid, output):
        return (_uid(job_or_uid), output)

    def validate_job(self, job_type, connections=None, params=None, require_all_inputs=True):
        connections = connections or {}
        params = params or {}
        resolved = self.resolve_job_type(job_type, title=job_type)
        spec = self.specs[resolved]
        param_specs = _named_specs(spec.get("params"))
        input_specs = _named_specs(spec.get("inputs"))
        param_keys = set(param_specs)
        input_keys = set(input_specs)
        required_params = {
            key
            for key, value in param_specs.items()
            if value.get("required_param") or value.get("required")
        }
        required_inputs = {
            key
            for key, value in input_specs.items()
            if _positive_min(value.get("count_min"))
        }

        invalid_params = {
            key: errors
            for key, value in params.items()
            if key in param_specs
            and (errors := _param_validation_errors(value, param_specs[key]))
        }
        invalid_inputs = {}
        for key, value in connections.items():
            if key not in input_specs:
                continue
            metadata = input_specs[key]
            count = len(value) if isinstance(value, list) else (0 if value is None else 1)
            count_min = metadata.get("count_min")
            count_max = metadata.get("count_max")
            if _positive_min(count_min) and count < int(count_min):
                invalid_inputs.setdefault(key, []).append(
                    f"requires at least {count_min} connection(s), got {count}"
                )
            if count_max not in (None, "inf"):
                try:
                    if count > int(count_max):
                        invalid_inputs.setdefault(key, []).append(
                            f"allows at most {count_max} connection(s), got {count}"
                        )
                except (TypeError, ValueError):
                    pass

        return {
            "ok": not (
                (set(params) - param_keys)
                or (set(connections) - input_keys)
                or (required_params - set(params))
                or (require_all_inputs and (required_inputs - set(connections)))
                or invalid_params
                or invalid_inputs
            ),
            "job_type": resolved,
            "unknown_params": sorted(set(params) - param_keys),
            "unknown_inputs": sorted(set(connections) - input_keys),
            "missing_required_params": sorted(required_params - set(params)),
            "missing_required_inputs": sorted(required_inputs - set(connections)) if require_all_inputs else [],
            "invalid_params": invalid_params,
            "invalid_inputs": invalid_inputs,
            "known_inputs": sorted(input_keys),
            "known_params": sorted(param_keys),
        }

    def create_job(
        self,
        job_type,
        connections=None,
        params=None,
        title="",
        desc="",
        validate=True,
        require_all_inputs=True,
    ):
        if not self.workspace:
            raise ValueError("Select or create a workspace first.")
        resolved = self.resolve_job_type(job_type, title=job_type)
        params = params or {}
        connections = connections or {}
        if validate:
            result = self.validate_job(resolved, connections, params, require_all_inputs=require_all_inputs)
            if not result["ok"]:
                problems = {
                    key: value
                    for key, value in result.items()
                    if key.startswith(("unknown_", "missing_", "invalid_")) and value
                }
                raise ValueError(f"Invalid {resolved} job spec: {problems}")
        return self.workspace.create_job(
            resolved,
            connections=connections,
            params=params,
            title=title or self.specs[resolved].get("title", resolved),
            desc=desc,
        )

    def queue(self, job, lane=None, hostname=None, gpus=None, cluster_vars=None, wait=False, timeout=None):
        job.queue(
            lane=lane,
            hostname=hostname,
            gpus=gpus or [],
            cluster_vars=cluster_vars or {},
        )
        if wait:
            return job.wait_for_done(timeout=timeout)
        return job_summary(job)

    def job_log_tail(self, job_or_uid, lines=80, project_uid=None):
        job = job_or_uid if hasattr(job_or_uid, "refresh") else self.find_job(job_or_uid, project_uid=project_uid)
        job.refresh()
        text = ""
        source = None
        local_path = Path(str(job.dir)) / "job.log"

        if local_path.exists():
            text = local_path.read_text(errors="replace")
            source = str(local_path)
        else:
            try:
                with job.download("job.log") as res:
                    data = res.read()
                text = data.decode(errors="replace")
                source = "download:job.log"
            except Exception as exc:
                return {
                    "uid": job.model.uid,
                    "status": job.model.status,
                    "has_error": bool(getattr(job.model, "has_error", False)),
                    "has_warning": bool(getattr(job.model, "has_warning", False)),
                    "source": None,
                    "lines": [],
                    "text": "",
                    "error": repr(exc),
                }

        tail_lines = text.splitlines()[-int(lines):]
        return {
            "uid": job.model.uid,
            "status": job.model.status,
            "has_error": bool(getattr(job.model, "has_error", False)),
            "has_warning": bool(getattr(job.model, "has_warning", False)),
            "source": source,
            "lines": tail_lines,
            "text": "\n".join(tail_lines),
        }

    def queue_and_wait(
        self,
        job,
        lane=None,
        hostname=None,
        gpus=None,
        cluster_vars=None,
        timeout=None,
        error_on_failed=True,
        log_lines=80,
    ):
        job.refresh()
        if job.model.status == "building":
            job.queue(
                lane=lane,
                hostname=hostname,
                gpus=gpus or [],
                cluster_vars=cluster_vars or {},
            )

        status = job.wait_for_done(timeout=timeout)
        job.refresh()
        result = job_summary(job)
        result["has_error"] = bool(getattr(job.model, "has_error", False))
        result["has_warning"] = bool(getattr(job.model, "has_warning", False))

        if result["status"] in {"failed", "killed"} or result["has_error"]:
            result["log_tail"] = self.job_log_tail(job, lines=log_lines)
            if error_on_failed:
                text = result["log_tail"].get("text") or ""
                raise RuntimeError(
                    f"Job {job.model.uid} ended with status {result['status']} "
                    f"(has_error={result['has_error']}).\n{text}"
                )
        elif result["has_warning"]:
            result["log_tail"] = self.job_log_tail(job, lines=log_lines)

        return result

    def wait(self, job, timeout=None, error_on_incomplete=False):
        status = job.wait_for_done(timeout=timeout, error_on_incomplete=error_on_incomplete)
        return {"uid": job.model.uid, "status": status}

    def wait_for_status(
        self,
        job_or_uid,
        statuses,
        stop_statuses=("completed", "failed", "killed"),
        poll_seconds=5,
        project_uid=None,
    ):
        job = job_or_uid if hasattr(job_or_uid, "refresh") else self.find_job(job_or_uid, project_uid=project_uid)
        wanted = {statuses} if isinstance(statuses, str) else set(statuses)
        stops = {stop_statuses} if isinstance(stop_statuses, str) else set(stop_statuses or [])

        while True:
            job.refresh()
            status = job.model.status
            if status in wanted:
                return {
                    "uid": job.model.uid,
                    "status": status,
                    "ok": True,
                    "reason": "matched",
                }
            if status in stops:
                return {
                    "uid": job.model.uid,
                    "status": status,
                    "ok": False,
                    "reason": "terminal_status_before_match",
                    "wanted": sorted(wanted),
                }
            time.sleep(poll_seconds)

    def status(self, job):
        job.refresh()
        return job_summary(job)

    def clear(self, job):
        job.clear()
        return {"uid": job.model.uid, "cleared": True}

    def kill(self, job):
        job.kill()
        return {"uid": job.model.uid, "killed": True}

    def delete(self, job_or_uid):
        if not self.project:
            raise ValueError("Select a project first with use_project('P1').")
        job_uid = _uid(job_or_uid)
        self.cs.api.jobs.delete(self.project.model.uid, job_uid)
        return {"uid": job_uid, "deleted": True}

    def connect(self, target_job, target_input, source_job, source_output):
        return target_job.connect(target_input, _uid(source_job), source_output)

    def _wrapper_job_type(self, wrapper_name):
        candidates, title = _WRAPPER_JOB_TYPES[wrapper_name]
        return self.resolve_job_type(candidates, title=title)

    def _input_name(self, job_type, aliases):
        aliases = (aliases,) if isinstance(aliases, str) else tuple(aliases)
        inputs = _named_specs(self.specs[job_type].get("inputs"))
        return self._input_name_from_specs(job_type, inputs, aliases)

    @staticmethod
    def _input_name_from_specs(job_label, inputs, aliases):
        aliases = (aliases,) if isinstance(aliases, str) else tuple(aliases)
        for alias in aliases:
            if alias in inputs:
                return alias

        wanted = {_normalized_name(alias) for alias in aliases}
        matches = []
        for name, metadata in inputs.items():
            if _normalized_name(name) in wanted or _normalized_name(metadata.get("title")) in wanted:
                matches.append(name)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(
                f"Ambiguous inputs {matches} in {job_label} for aliases {list(aliases)}. "
                "Inspect csw.job_spec(...) and use csw.create_job(...) explicitly."
            )
        raise KeyError(
            f"Installed {job_label} has no input matching {list(aliases)!r}; "
            f"available inputs are {sorted(inputs)!r}. Inspect the live job inputs."
        )

    def _param_name(self, job_type, aliases):
        aliases = (aliases,) if isinstance(aliases, str) else tuple(aliases)
        params = _named_specs(self.specs[job_type].get("params"))
        for alias in aliases:
            if alias in params:
                return alias
        wanted = {_normalized_name(alias) for alias in aliases}
        matches = [
            name
            for name, metadata in params.items()
            if _normalized_name(name) in wanted
            or _normalized_name(metadata.get("title")) in wanted
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(
                f"Ambiguous parameters {matches} in {job_type} for aliases {list(aliases)}."
            )
        return None

    def _set_structural_param(self, job_type, params, aliases, value, purpose):
        """Set a live structural parameter implied by explicitly supplied inputs."""
        name = self._param_name(job_type, aliases)
        if name is None:
            return None
        if name in params and params[name] != value:
            raise ValueError(
                f"Parameter {name!r} must be {value!r} when {purpose}; got {params[name]!r}."
            )
        params[name] = value
        return name

    def _set_wrapper_param(self, job_type, params, aliases, value, purpose, *, required=False):
        """Map one wrapper argument to live parameter metadata without guessing."""
        if value is None:
            if required:
                raise ValueError(f"{purpose} is required.")
            return None
        name = self._param_name(job_type, aliases)
        if name is None:
            raise KeyError(
                f"Installed {job_type} has no parameter for {purpose}; "
                f"tried aliases {list(aliases)!r}. Inspect csw.job_spec({job_type!r})."
            )
        if name in params and params[name] != value:
            raise ValueError(
                f"Parameter {name!r} conflicts with the explicit {purpose}: "
                f"{params[name]!r} != {value!r}."
            )
        params[name] = value
        return name

    @staticmethod
    def _job_input_specs(job):
        inputs = getattr(job, "inputs", None)
        if inputs is None:
            model = getattr(job, "model", None)
            spec = getattr(model, "spec", None)
            inputs = getattr(spec, "inputs", None)
            if inputs is None and isinstance(spec, dict):
                inputs = spec.get("inputs")
        return _named_specs(inputs)

    @staticmethod
    def _source_values(value):
        return value if isinstance(value, list) else [value]

    def _connect_job_sources(self, job, job_type, aliases, value, output):
        """Connect sources against the created job's live, parameterized inputs.

        CryoSPARC materializes some numbered inputs only after job creation (and,
        for Align 3D Maps, after maps are connected). Resolving against
        ``job.inputs`` avoids assuming those dynamic slots exist in the registry
        base spec.
        """
        if value is None:
            return
        aliases = (aliases,) if isinstance(aliases, str) else tuple(aliases)
        values = self._source_values(value)
        if not values:
            raise ValueError(f"At least one source is required for {aliases!r}.")

        inputs = self._job_input_specs(job)
        base_name = None
        try:
            base_name = self._input_name_from_specs(job_type, inputs, aliases)
        except KeyError:
            pass

        if base_name is not None:
            metadata = inputs.get(base_name, {})
            repeat_allowed = bool(metadata.get("repeat_allowed"))
            count_max = metadata.get("count_max")
            allows_many = repeat_allowed or count_max == "inf"
            if count_max not in (None, "inf"):
                try:
                    allows_many = allows_many or int(count_max) > 1
                except (TypeError, ValueError):
                    pass
            if len(values) == 1 or allows_many:
                for item in values:
                    source_uid, source_output = self._source(item, output)
                    job.connect(base_name, source_uid, source_output)
                return

        # Dynamic reference/map inputs are commonly numbered from either zero
        # or one. Titles are included because internal keys vary by release.
        normalized = {
            _normalized_name(name): name for name in inputs
        }
        normalized.update(
            {
                _normalized_name(metadata.get("title")): name
                for name, metadata in inputs.items()
                if metadata.get("title")
            }
        )
        zero_markers = {
            _normalized_name(pattern)
            for alias in aliases
            for pattern in (
                f"{alias}_0",
                f"{alias} 0",
                f"{alias} (map to align, connection 0)",
                f"{alias} (connection 0)",
            )
        }
        offset = 0 if zero_markers.intersection(normalized) else 1

        for index, item in enumerate(values):
            connection_number = index + offset
            numbered_aliases = []
            for alias in aliases:
                numbered_aliases.extend(
                    (
                        f"{alias}_{connection_number}",
                        f"{alias} {connection_number}",
                        f"{alias} (map to align, connection {connection_number})",
                        f"{alias} (connection {connection_number})",
                    )
                )
            inputs = self._job_input_specs(job)
            try:
                input_name = self._input_name_from_specs(
                    job_type, inputs, numbered_aliases
                )
            except KeyError as exc:
                raise KeyError(
                    f"Created {job_type} job {getattr(job, 'uid', '<unknown>')} has no "
                    f"dynamic input for {aliases!r} connection {connection_number}. "
                    "Inspect job.inputs and the live parameters that control input creation."
                ) from exc
            source_uid, source_output = self._source(item, output)
            job.connect(input_name, source_uid, source_output)

    def _create_parameterized_wrapper_job(self, wrapper_name, title, params):
        """Create a building job first so its dynamic live inputs can be used."""
        job_type = self._wrapper_job_type(wrapper_name)
        job = self.create_job(
            job_type,
            connections={},
            params=params,
            title=title,
            require_all_inputs=False,
        )
        return job_type, job

    def _bind_wrapper_source(self, job_type, connections, aliases, value, output):
        """Bind a controller/UID/tuple or a repeated list using the live input spec."""
        if value is None:
            return
        aliases = (aliases,) if isinstance(aliases, str) else tuple(aliases)
        values = value if isinstance(value, list) else None
        input_error = None
        try:
            input_name = self._input_name(job_type, aliases)
        except (KeyError, ValueError) as exc:
            input_error = exc
            input_name = None

        if input_name is not None:
            metadata = _named_specs(self.specs[job_type].get("inputs")).get(input_name, {})
            repeat_allowed = bool(metadata.get("repeat_allowed"))
            count_max = metadata.get("count_max")
            allows_many = repeat_allowed or count_max == "inf"
            if count_max not in (None, "inf"):
                try:
                    allows_many = allows_many or int(count_max) > 1
                except (TypeError, ValueError):
                    pass
            if values is None:
                connections[input_name] = self._source(value, output)
                return
            if len(values) <= 1 or allows_many:
                sources = [self._source(item, output) for item in values]
                connections[input_name] = sources if allows_many or len(sources) != 1 else sources[0]
                return

        # Some versions expose repeated groups as explicitly numbered inputs.
        inputs = _named_specs(self.specs[job_type].get("inputs"))
        zero_aliases = {_normalized_name(f"{alias}_0") for alias in aliases}
        zero_based = any(
            _normalized_name(name) in zero_aliases
            or _normalized_name(metadata.get("title")) in zero_aliases
            for name, metadata in inputs.items()
        )
        offset = 0 if zero_based else 1
        source_values = [value] if values is None else values
        for index, item in enumerate(source_values):
            numbered = []
            for alias in aliases:
                numbered.extend(
                    (
                        f"{alias}_{index + offset}",
                        f"{alias} {index + offset}",
                    )
                )
            try:
                numbered_name = self._input_name(job_type, numbered)
            except (KeyError, ValueError):
                if input_error is not None:
                    raise input_error
                raise
            if numbered_name in connections:
                raise ValueError(
                    f"Could not uniquely map repeated {aliases!r} inputs in {job_type}; "
                    "inspect the live job spec and use create_job explicitly."
                )
            connections[numbered_name] = self._source(item, output)

    def _create_wrapper_job(
        self,
        wrapper_name,
        sources,
        title,
        params,
        require_all_inputs=True,
    ):
        job_type = self._wrapper_job_type(wrapper_name)
        connections = {}
        for aliases, value, output in sources:
            self._bind_wrapper_source(job_type, connections, aliases, value, output)
        return self.create_job(
            job_type,
            connections=connections,
            params=params,
            title=title,
            require_all_inputs=require_all_inputs,
        )

    def import_movies(
        self,
        blob_paths,
        psize_A,
        accel_kv,
        cs_mm,
        total_dose_e_per_A2,
        gainref_path=None,
        title="Import Movies",
        **params,
    ):
        params.update(
            _clean(
                {
                    "blob_paths": blob_paths,
                    "gainref_path": gainref_path,
                    "psize_A": psize_A,
                    "accel_kv": accel_kv,
                    "cs_mm": cs_mm,
                    "total_dose_e_per_A2": total_dose_e_per_A2,
                }
            )
        )
        return self.create_job("import_movies", params=params, title=title)

    def import_micrographs(self, blob_paths, psize_A, accel_kv, cs_mm, total_dose_e_per_A2, title="Import Micrographs", **params):
        params.update(
            {
                "blob_paths": blob_paths,
                "psize_A": psize_A,
                "accel_kv": accel_kv,
                "cs_mm": cs_mm,
                "total_dose_e_per_A2": total_dose_e_per_A2,
            }
        )
        return self.create_job("import_micrographs", params=params, title=title)

    def import_particle_stack(
        self,
        particle_meta_path,
        particle_data_path,
        source_exposures=None,
        exposures_output="imported_micrographs",
        psize_A=None,
        accel_kv=None,
        cs_mm=None,
        total_dose_e_per_A2=None,
        data_sign=None,
        ignore_raw_data=None,
        ignore_pose_data=None,
        title="Import Particle Stack",
        **params,
    ):
        """Create, but do not queue, an Import Particle Stack job from live metadata."""
        if not str(particle_meta_path or "").strip():
            raise ValueError("particle_meta_path is required.")
        if not str(particle_data_path or "").strip():
            raise ValueError("particle_data_path is required.")

        job_type = self._wrapper_job_type("import_particle_stack")
        mappings = (
            (
                ("particle_meta_path", "particle_metadata_path", "Particle meta path"),
                particle_meta_path,
                "particle metadata path",
                True,
            ),
            (
                ("particle_blob_path", "particle_data_path", "blob_paths", "Particle data path"),
                particle_data_path,
                "particle data path",
                True,
            ),
            (
                ("psize_A", "pixel_size_A", "Pixel size (A)"),
                psize_A,
                "pixel-size override",
                False,
            ),
            (
                ("accel_kv", "accelerating_voltage_kV", "Accelerating voltage (kV)"),
                accel_kv,
                "accelerating-voltage override",
                False,
            ),
            (
                ("cs_mm", "spherical_aberration_mm", "Spherical aberration (mm)"),
                cs_mm,
                "spherical-aberration override",
                False,
            ),
            (
                (
                    "total_dose_e_per_A2",
                    "total_exposure_dose",
                    "Total exposure dose (e/A^2)",
                ),
                total_dose_e_per_A2,
                "total-dose override",
                False,
            ),
            (
                ("blob_sign", "data_sign", "Data Sign"),
                data_sign,
                "particle data sign",
                False,
            ),
            (
                ("ignore_raw_data", "Ignore raw data"),
                ignore_raw_data,
                "ignore-raw-data setting",
                False,
            ),
            (
                ("ignore_pose", "ignore_pose_data", "Ignore pose data"),
                ignore_pose_data,
                "ignore-pose-data setting",
                False,
            ),
        )
        for aliases, value, purpose, required in mappings:
            self._set_wrapper_param(
                job_type,
                params,
                aliases,
                value,
                purpose,
                required=required,
            )

        return self._create_wrapper_job(
            "import_particle_stack",
            [
                (
                    (
                        "source_exposures",
                        "source_micrographs",
                        "exposures",
                        "micrographs",
                        "Source Exposures",
                    ),
                    source_exposures,
                    exposures_output,
                )
            ],
            title,
            params=params,
        )

    def import_volume(self, volume_blob_path, volume_out_name="imported_volume", volume_psize=None, title="Import Volume", **params):
        params.update(
            _clean(
                {
                    "volume_blob_path": volume_blob_path,
                    "volume_out_name": volume_out_name,
                    "volume_psize": volume_psize,
                }
            )
        )
        return self.create_job("import_volumes", params=params, title=title)

    def create_templates(self, volume, volume_output="imported_volume", n_templates=50, title="Create Templates", **params):
        params.update({"n_templates": n_templates})
        return self.create_job(
            "create_templates",
            connections={"volume": self._source(volume, volume_output)},
            params=params,
            title=title,
        )

    def patch_motion(self, movies, movies_output="imported_movies", output_f16=True, random_seed=0, gpus=1, title="Patch Motion Correction", **params):
        params.update({"output_f16": output_f16, "random_seed": random_seed, "compute_num_gpus": gpus})
        return self.create_job(
            "patch_motion_correction_multi",
            connections={"movies": self._source(movies, movies_output)},
            params=params,
            title=title,
        )

    def patch_ctf(self, exposures, exposures_output="micrographs", gpus=1, title="Patch CTF Estimation", **params):
        params.update({"compute_num_gpus": gpus})
        return self.create_job(
            "patch_ctf_estimation_multi",
            connections={"exposures": self._source(exposures, exposures_output)},
            params=params,
            title=title,
        )

    def curate_exposures(self, exposures, exposures_output="exposures", title="Curate Exposures", **params):
        return self.create_job(
            "curate_exposures_v2",
            connections={"exposures": self._source(exposures, exposures_output)},
            params=params,
            title=title,
        )

    def blob_pick(self, micrographs, diameter_A, micrographs_output="exposures_accepted", diameter_max_A=None, title="Blob Picker", **params):
        params.update(_clean({"diameter": diameter_A, "diameter_max": diameter_max_A}))
        return self.create_job(
            "blob_picker_gpu",
            connections={"micrographs": self._source(micrographs, micrographs_output)},
            params=params,
            title=title,
        )

    def inspect_picks(self, particles, micrographs=None, particles_output="particles", micrographs_output="micrographs", title="Inspect Picks", **params):
        connections = {"particles": self._source(particles, particles_output)}
        if micrographs is not None:
            connections["micrographs"] = self._source(micrographs, micrographs_output)
        return self.create_job("inspect_picks_v2", connections=connections, params=params, title=title)

    def remove_duplicates(self, particles, micrographs, particles_output="particles", micrographs_output="micrographs", min_distance_A=50, title="Remove Duplicate Particles", **params):
        params.update({"min_dist_A": min_distance_A})
        return self.create_job(
            "remove_duplicate_particles",
            connections={
                "particles": self._source(particles, particles_output),
                "micrographs": self._source(micrographs, micrographs_output),
            },
            params=params,
            title=title,
        )

    def plan_particle_extraction(
        self,
        box_size_pix,
        source_pixel_size_A,
        particle_extent_A,
        fourier_crop_size_pix=None,
        target_resolution_A=None,
        flexible=False,
        allow_small_box=False,
        small_box_rationale=None,
    ):
        """Return the mandatory physical-field and sampling preflight."""

        return plan_particle_extraction(
            box_size_pix=box_size_pix,
            source_pixel_size_A=source_pixel_size_A,
            particle_extent_A=particle_extent_A,
            fourier_crop_size_pix=fourier_crop_size_pix,
            target_resolution_A=target_resolution_A,
            flexible=flexible,
            allow_small_box=allow_small_box,
            small_box_rationale=small_box_rationale,
        )

    def extract_particles(
        self,
        micrographs,
        particles,
        box_size_pix,
        bin_size_pix=None,
        micrographs_output="micrographs",
        particles_output="particles",
        output_f16=True,
        gpus=1,
        title="Extract Particles",
        source_pixel_size_A=None,
        particle_extent_A=None,
        target_resolution_A=None,
        flexible=False,
        allow_small_box=False,
        small_box_rationale=None,
        desc="",
        **params,
    ):
        extraction_plan = self.plan_particle_extraction(
            box_size_pix=box_size_pix,
            source_pixel_size_A=source_pixel_size_A,
            particle_extent_A=particle_extent_A,
            fourier_crop_size_pix=bin_size_pix,
            target_resolution_A=target_resolution_A,
            flexible=flexible,
            allow_small_box=allow_small_box,
            small_box_rationale=small_box_rationale,
        )
        for warning_text in extraction_plan["warnings"]:
            warnings.warn(warning_text, UserWarning, stacklevel=2)

        params.update(
            _clean(
                {
                    "box_size_pix": box_size_pix,
                    "bin_size_pix": bin_size_pix,
                    "output_f16": output_f16,
                    "compute_num_gpus": gpus,
                }
            )
        )
        plan_description = (
            "cryopilot_extraction_plan_v1: "
            + json.dumps(extraction_plan, sort_keys=True, separators=(",", ":"))
            + "\nPost-extraction review required: inspect representative particles and "
            "all 2D classes for density near the image boundary. Re-extract before "
            "downstream reconstruction if compact, extended, end-on, or unusual "
            "views are clipped. Do not reject particles solely because a prior box "
            "clipped them."
        )
        return self.create_job(
            "extract_micrographs_multi",
            connections={
                "micrographs": self._source(micrographs, micrographs_output),
                "particles": self._source(particles, particles_output),
            },
            params=params,
            title=title,
            desc=(f"{desc.rstrip()}\n\n" if desc.strip() else "") + plan_description,
        )

    def class_2d(self, particles, particles_output="particles", classes=200, max_resolution_A=12, zero_pad_factor=1, random_seed=0, gpus=1, title="2D Classification", **params):
        params.update(
            {
                "class2D_K": classes,
                "class2D_max_res": max_resolution_A,
                "class2D_zp_factor": zero_pad_factor,
                "random_seed": random_seed,
                "compute_num_gpus": gpus,
            }
        )
        return self.create_job(
            "class_2D_new",
            connections={"particles": self._source(particles, particles_output)},
            params=params,
            title=title,
        )

    def select_2d(self, particles, templates, particles_output="particles", templates_output="class_averages", title="Select 2D Classes", **params):
        return self.create_job(
            "select_2D",
            connections={
                "particles": self._source(particles, particles_output),
                "templates": self._source(templates, templates_output),
            },
            params=params,
            title=title,
        )

    def select_2d_classes(
        self,
        class_job,
        class_indices,
        particles_output="particles",
        templates_output="class_averages",
        title=None,
        lane=None,
        hostname=None,
        gpus=None,
        cluster_vars=None,
        wait=True,
        timeout=None,
        error_on_incomplete=True,
        project_uid=None,
    ):
        source_job = class_job if hasattr(class_job, "model") else self.find_job(class_job, project_uid=project_uid)
        indices = _parse_class_indices(class_indices)
        selected_templates = ",".join(str(idx) for idx in indices)
        title = title or f"Select 2D classes from {source_job.uid}: {selected_templates}"
        select_job = self.select_2d(
            source_job,
            source_job,
            particles_output=particles_output,
            templates_output=templates_output,
            title=title,
            selected_templates=selected_templates,
        )
        select_job.queue(
            lane=lane,
            hostname=hostname,
            gpus=gpus or [],
            cluster_vars=cluster_vars or {},
        )
        if wait:
            select_job.wait_for_done(timeout=timeout, error_on_incomplete=error_on_incomplete)
            select_job.refresh()
        return select_job

    def template_pick(self, templates, micrographs, diameter_A, templates_output="templates_selected", micrographs_output="exposures_accepted", title="Template Picker", **params):
        params.update({"diameter": diameter_A})
        return self.create_job(
            "template_picker_gpu",
            connections={
                "templates": self._source(templates, templates_output),
                "micrographs": self._source(micrographs, micrographs_output),
            },
            params=params,
            title=title,
        )

    def abinit(self, particles, particles_output="particles", classes=3, num_particles=None, symmetry="C1", random_seed=0, title="Ab-Initio Reconstruction", **params):
        # Leaving num_particles unset lets ab-initio use, and classify, the
        # whole input stack. A set value limits the outputs to that subset.
        params.update(
            _clean(
                {
                    "abinit_K": classes,
                    "abinit_num_particles": num_particles,
                    "abinit_symmetry": symmetry,
                    "random_seed": random_seed,
                }
            )
        )
        return self.create_job(
            "homo_abinit",
            connections={"particles": self._source(particles, particles_output)},
            params=params,
            title=title,
        )

    def hetero_refine(self, particles, volumes, particles_output="particles", volume_output="volumes_all_classes", classes=None, symmetry="C1", force_hard=True, random_seed=0, title="Heterogeneous Refinement", **params):
        params.update(
            _clean(
                {
                    "multirefine_N": classes,
                    "multirefine_symmetry": symmetry,
                    "multirefine_force_hard_class": force_hard,
                    "random_seed": random_seed,
                }
            )
        )
        return self.create_job(
            "hetero_refine",
            connections={
                "particles": self._source(particles, particles_output),
                "volume": self._source_list(volumes, volume_output),
            },
            params=params,
            title=title,
        )

    def homogeneous_refine(self, particles, volume, particles_output="particles", volume_output="volume", symmetry="C1", random_seed=0, title="Homogeneous Refinement", **params):
        params.update({"refine_symmetry": symmetry, "random_seed": random_seed})
        return self.create_job(
            "homo_refine_new",
            connections={
                "particles": self._source(particles, particles_output),
                "volume": self._source(volume, volume_output),
            },
            params=params,
            title=title,
        )

    def nonuniform_refine(self, particles, volume, particles_output="particles", volume_output="volume", symmetry="C1", dynamic_mask=False, dynamic_mask_start_A=None, minimize_scale=True, random_seed=0, title="Non-Uniform Refinement", **params):
        params.update(
            _clean(
                {
                    "refine_symmetry": symmetry,
                    "refine_dynamic_mask_start_res": dynamic_mask_start_A,
                    "refine_scale_min": minimize_scale,
                    "random_seed": random_seed,
                }
            )
        )
        if not dynamic_mask:
            # CryoSPARC v5 has an explicit dynamic-mask switch. Older releases
            # only disable it with an unrealistically fine start resolution.
            job_type = self.resolve_job_type("nonuniform_refine_new", title="nonuniform_refine_new")
            switch = self._param_name(
                job_type,
                ("refine_dynamic_mask_use", "refine_use_dynamic_mask", "Use dynamic refinement mask"),
            )
            if switch is not None:
                params.setdefault(switch, False)
            else:
                params.setdefault("refine_dynamic_mask_start_res", 0.1)
        return self.create_job(
            "nonuniform_refine_new",
            connections={
                "particles": self._source(particles, particles_output),
                "volume": self._source(volume, volume_output),
            },
            params=params,
            title=title,
        )

    def global_ctf_refine(self, particles, volume, particles_output="particles", volume_output="volume", title="Global CTF Refinement", **params):
        return self.create_job(
            "ctf_refine_global",
            connections={
                "particles": self._source(particles, particles_output),
                "volume": self._source(volume, volume_output),
            },
            params=params,
            title=title,
        )

    def local_ctf_refine(self, particles, volume, particles_output="particles", volume_output="volume", title="Local CTF Refinement", **params):
        return self.create_job(
            "ctf_refine_local",
            connections={
                "particles": self._source(particles, particles_output),
                "volume": self._source(volume, volume_output),
            },
            params=params,
            title=title,
        )

    def orientation_diagnostics(self, volume, particles, volume_output="volume", particles_output="particles", title="Orientation Diagnostics", **params):
        return self.create_job(
            "orientation_diagnostics",
            connections={
                "volume": self._source(volume, volume_output),
                "particles": self._source(particles, particles_output),
            },
            params=params,
            title=title,
            require_all_inputs=False,
        )

    def validation(self, volume, mask=None, volume_output="volume", mask_output="mask_fsc_auto", title="Validation (FSC)", **params):
        connections = {"volume": self._source(volume, volume_output)}
        if mask is not None:
            connections["mask"] = self._source(mask, mask_output)
        return self.create_job("validation", connections=connections, params=params, title=title)

    def sharpen(self, volume, mask=None, volume_output="volume", mask_output="mask_fsc_auto", bfactor=None, title="Sharpening Tools", **params):
        params.update(_clean({"sharp_bfactor": bfactor}))
        connections = {"volume": self._source(volume, volume_output)}
        if mask is not None:
            connections["mask"] = self._source(mask, mask_output)
        return self.create_job("sharpen", connections=connections, params=params, title=title)

    # The wrappers below intentionally provide connection plumbing only. Their
    # scientific parameters are forwarded verbatim and the returned jobs remain
    # in "building" state until the caller explicitly queues them.

    def micrograph_denoiser(
        self,
        exposures,
        model=None,
        exposures_output="exposures",
        model_output="denoise_model",
        title="Micrograph Denoiser",
        **params,
    ):
        return self._create_wrapper_job(
            "micrograph_denoiser",
            [
                (("exposures", "micrographs"), exposures, exposures_output),
                (("denoise_model", "model"), model, model_output),
            ],
            title,
            params,
        )

    def micrograph_junk_detector(
        self,
        exposures,
        particles=None,
        exposures_output="exposures",
        particles_output="particles",
        title="Micrograph Junk Detector",
        **params,
    ):
        return self._create_wrapper_job(
            "micrograph_junk_detector",
            [
                (("exposures", "micrographs"), exposures, exposures_output),
                (("particles",), particles, particles_output),
            ],
            title,
            params,
        )

    def exposure_groups(
        self,
        exposures=None,
        particles=None,
        exposures_output="exposures",
        particles_output="particles",
        title="Exposure Group Utilities",
        **params,
    ):
        if exposures is None and particles is None:
            raise ValueError("exposure_groups requires exposures and/or particles.")
        return self._create_wrapper_job(
            "exposure_groups",
            [
                (("exposures", "exposure", "micrographs"), exposures, exposures_output),
                (("particles", "particle"), particles, particles_output),
            ],
            title,
            params,
        )

    def reference_motion_correction(
        self,
        exposures,
        particles,
        volumes,
        masks=None,
        hyperparameters=None,
        exposures_output="exposures",
        particles_output="particles",
        volume_output="volume",
        mask_output="mask",
        hyperparameters_output="hyperparameters",
        title="Reference Based Motion Correction",
        **params,
    ):
        particle_values = self._source_values(particles)
        volume_values = self._source_values(volumes)
        if len(particle_values) != len(volume_values):
            raise ValueError(
                "reference_motion_correction requires one particle stack per reference volume."
            )
        if masks is not None and len(self._source_values(masks)) != len(volume_values):
            raise ValueError(
                "reference_motion_correction masks must match the reference-volume count."
            )

        job_type = self._wrapper_job_type("reference_motion_correction")
        params = dict(params)
        self._set_structural_param(
            job_type,
            params,
            (
                "ref_num_volumes",
                "num_ref_volumes",
                "number_of_reference_volumes",
                "number_of_ref_volumes",
                "Number of reference volumes",
            ),
            len(volume_values),
            "multiple particle/reference sets are supplied",
        )
        job_type, job = self._create_parameterized_wrapper_job(
            "reference_motion_correction", title, params
        )
        self._connect_job_sources(
            job, job_type, ("exposures", "micrographs", "movies"), exposures, exposures_output
        )
        self._connect_job_sources(job, job_type, ("particles",), particle_values, particles_output)
        self._connect_job_sources(
            job,
            job_type,
            ("volume", "volumes", "reference_volume", "reference_volumes"),
            volume_values,
            volume_output,
        )
        self._connect_job_sources(
            job, job_type, ("mask", "masks", "static_mask"), masks, mask_output
        )
        self._connect_job_sources(
            job,
            job_type,
            ("hyperparameters", "hyperparameter"),
            hyperparameters,
            hyperparameters_output,
        )
        return job

    def subset_particles(
        self,
        particles=None,
        initial_particles=None,
        final_particles=None,
        particles_output="particles",
        initial_particles_output="particles",
        final_particles_output="particles",
        title="Subset Particles by Statistic",
        **params,
    ):
        if particles is not None and (initial_particles is not None or final_particles is not None):
            raise ValueError("Use either particles or the initial_particles/final_particles pair, not both.")
        if particles is None and (initial_particles is None or final_particles is None):
            raise ValueError("Provide particles or both initial_particles and final_particles.")
        return self._create_wrapper_job(
            "subset_particles",
            [
                (("particles",), particles, particles_output),
                (("initial_particles", "particles_initial"), initial_particles, initial_particles_output),
                (("final_particles", "particles_final"), final_particles, final_particles_output),
            ],
            title,
            params,
        )

    def rebalance_orientations(
        self,
        particles,
        particles_output="particles",
        title="Rebalance Orientations",
        **params,
    ):
        return self._create_wrapper_job(
            "rebalance_orientations",
            [(("particles",), particles, particles_output)],
            title,
            params,
        )

    def homogeneous_reconstruct(
        self,
        particles,
        mask=None,
        particles_output="particles",
        mask_output="mask",
        title="Homogeneous Reconstruction Only",
        **params,
    ):
        return self._create_wrapper_job(
            "homogeneous_reconstruct",
            [
                (("particles",), particles, particles_output),
                (("mask",), mask, mask_output),
            ],
            title,
            params,
        )

    def heterogeneous_reconstruct(
        self,
        particles,
        mask=None,
        particles_output="particles_all_classes",
        mask_output="mask",
        title="Heterogeneous Reconstruction Only",
        **params,
    ):
        return self._create_wrapper_job(
            "heterogeneous_reconstruct",
            [
                (("particles",), particles, particles_output),
                (("mask",), mask, mask_output),
            ],
            title,
            params,
        )

    def class_3d(
        self,
        particles,
        volumes=None,
        solvent_mask=None,
        focus_mask=None,
        particles_output="particles",
        volume_output="volume",
        solvent_mask_output="mask",
        focus_mask_output="mask",
        title="3D Classification",
        **params,
    ):
        return self._create_wrapper_job(
            "class_3d",
            [
                (("particles",), particles, particles_output),
                (("volume", "volumes", "initial_volumes"), volumes, volume_output),
                (("solvent_mask", "mask"), solvent_mask, solvent_mask_output),
                (("focus_mask",), focus_mask, focus_mask_output),
            ],
            title,
            params,
        )

    def regroup_3d(
        self,
        particles,
        volumes,
        mask=None,
        particles_output="particles_all_classes",
        volume_output="volumes_all_classes",
        mask_output="mask",
        title="Regroup 3D Classes",
        **params,
    ):
        return self._create_wrapper_job(
            "regroup_3d",
            [
                (("particles",), particles, particles_output),
                (("volume", "volumes"), volumes, volume_output),
                (("mask",), mask, mask_output),
            ],
            title,
            params,
        )

    def volume_tools(
        self,
        volume=None,
        mask=None,
        volume_output="volume",
        mask_output="mask",
        title="Volume Tools",
        **params,
    ):
        if (volume is None) == (mask is None):
            raise ValueError("volume_tools requires exactly one of volume or mask.")
        return self._create_wrapper_job(
            "volume_tools",
            [
                (("volume",), volume, volume_output),
                (("mask",), mask, mask_output),
            ],
            title,
            params,
        )

    def volume_alignment_tools(
        self,
        volume,
        mask=None,
        particles=None,
        volume_output="volume",
        mask_output="mask",
        particles_output="particles",
        title="Volume Alignment Tools",
        **params,
    ):
        return self._create_wrapper_job(
            "volume_alignment_tools",
            [
                (("volume",), volume, volume_output),
                (("mask",), mask, mask_output),
                (("particles",), particles, particles_output),
            ],
            title,
            params,
        )

    def align_3d_maps(
        self,
        reference,
        volumes=None,
        *,
        volumes_group=None,
        individual_volumes=None,
        reference_mask=None,
        masks=None,
        particles=None,
        reference_output="volume",
        reference_mask_output="mask",
        volume_output="volume",
        mask_output="mask",
        particles_output="particles",
        title="Align 3D Maps",
        **params,
    ):
        if volumes is not None:
            if volumes_group is not None or individual_volumes is not None:
                raise ValueError(
                    "Use volumes, volumes_group, or individual_volumes, not more than one."
                )
            if isinstance(volumes, list):
                individual_volumes = volumes
            else:
                volumes_group = volumes
        if (volumes_group is None) == (individual_volumes is None):
            raise ValueError(
                "align_3d_maps requires exactly one of volumes_group or individual_volumes."
            )

        individual_mode = individual_volumes is not None
        map_values = self._source_values(
            individual_volumes if individual_mode else volumes_group
        )
        if not map_values:
            raise ValueError("At least one map to align is required.")
        if individual_mode:
            if particles is not None and len(self._source_values(particles)) != len(map_values):
                raise ValueError(
                    "Individual-map particle inputs must match the individual-volume count."
                )
            if masks is not None and len(self._source_values(masks)) != len(map_values):
                raise ValueError(
                    "Individual-map mask inputs must match the individual-volume count."
                )

        job_type = self._wrapper_job_type("align_3d_maps")
        params = dict(params)
        if particles is not None:
            self._set_structural_param(
                job_type,
                params,
                (
                    "align_update_particles",
                    "update_particle_alignments",
                    "update_particles",
                    "Update particle alignments",
                ),
                True,
                "particle inputs are supplied",
            )
        if masks is not None:
            self._set_structural_param(
                job_type,
                params,
                (
                    "align_create_mask_inputs",
                    "create_mask_inputs",
                    "Create mask inputs",
                ),
                True,
                "mask inputs are supplied",
            )

        job_type, job = self._create_parameterized_wrapper_job(
            "align_3d_maps", title, params
        )
        self._connect_job_sources(
            job,
            job_type,
            ("reference", "reference_map", "reference_volume", "volume_ref"),
            reference,
            reference_output,
        )
        self._connect_job_sources(
            job,
            job_type,
            ("reference_mask", "mask_ref"),
            reference_mask,
            reference_mask_output,
        )
        if individual_mode:
            self._connect_job_sources(
                job,
                job_type,
                ("volume", "maps_to_align_individual_volumes", "individual_volumes"),
                map_values,
                volume_output,
            )
            self._connect_job_sources(
                job,
                job_type,
                ("particles", "particles_map_to_align"),
                particles,
                particles_output,
            )
            self._connect_job_sources(
                job,
                job_type,
                ("mask", "mask_map_to_align"),
                masks,
                mask_output,
            )
        else:
            self._connect_job_sources(
                job,
                job_type,
                ("volumes", "volumes_group", "maps_to_align_volumes_group"),
                volumes_group,
                volume_output,
            )
            self._connect_job_sources(
                job,
                job_type,
                ("particles_all", "particles"),
                particles,
                particles_output,
            )
            self._connect_job_sources(
                job,
                job_type,
                (
                    "masks_all_classes",
                    "masks_all_classes_individual_masks",
                    "Masks all classes (individual masks)",
                    "masks",
                    "mask",
                ),
                masks,
                mask_output,
            )
        return job

    def symmetry_expand(
        self,
        particles,
        particles_output="particles",
        title="Symmetry Expansion",
        **params,
    ):
        return self._create_wrapper_job(
            "symmetry_expand",
            [(("particles",), particles, particles_output)],
            title,
            params,
        )

    def particle_subtract(
        self,
        particles,
        volume,
        mask,
        particles_output="particles",
        volume_output="volume",
        mask_output="mask",
        title="Particle Subtraction",
        **params,
    ):
        return self._create_wrapper_job(
            "particle_subtract",
            [
                (("particles",), particles, particles_output),
                (("volume",), volume, volume_output),
                (("mask",), mask, mask_output),
            ],
            title,
            params,
        )

    def local_refine(
        self,
        particles,
        volume,
        mask,
        particles_output="particles",
        volume_output="volume",
        mask_output="mask",
        title="Local Refinement",
        **params,
    ):
        return self._create_wrapper_job(
            "local_refine",
            [
                (("particles",), particles, particles_output),
                (("volume",), volume, volume_output),
                (("mask",), mask, mask_output),
            ],
            title,
            params,
        )

    def local_resolution(
        self,
        volume,
        mask=None,
        volume_output="volume",
        mask_output="mask",
        title="Local Resolution Estimation",
        **params,
    ):
        return self._create_wrapper_job(
            "local_resolution",
            [
                (("volume",), volume, volume_output),
                (("mask",), mask, mask_output),
            ],
            title,
            params,
        )

    def local_filter(
        self,
        volume,
        volume_output="volume",
        title="Local Filtering",
        **params,
    ):
        return self._create_wrapper_job(
            "local_filter",
            [(("volume",), volume, volume_output)],
            title,
            params,
        )

    def variability_3d(
        self,
        particles,
        mask=None,
        particles_output="particles",
        mask_output="mask",
        title="3D Variability",
        **params,
    ):
        return self._create_wrapper_job(
            "variability_3d",
            [
                (("particles",), particles, particles_output),
                (("mask",), mask, mask_output),
            ],
            title,
            params,
        )

    def variability_3d_display(
        self,
        particles,
        volumes,
        particles_output="particles",
        volume_output="components",
        title="3D Variability Display",
        **params,
    ):
        return self._create_wrapper_job(
            "variability_3d_display",
            [
                (("particles",), particles, particles_output),
                (("volume", "volumes", "components"), volumes, volume_output),
            ],
            title,
            params,
        )

    def flex_data_prep(
        self,
        particles,
        volume,
        particles_output="particles",
        volume_output="volume",
        title="3D Flex Data Prep",
        **params,
    ):
        return self._create_wrapper_job(
            "flex_data_prep",
            [
                (("particles",), particles, particles_output),
                (("volume",), volume, volume_output),
            ],
            title,
            params,
        )

    def flex_mesh_prep(
        self,
        volume,
        mask,
        volume_output="volume",
        mask_output="mask",
        title="3D Flex Mesh Prep",
        **params,
    ):
        return self._create_wrapper_job(
            "flex_mesh_prep",
            [
                (("volume",), volume, volume_output),
                (("mask", "segmentation"), mask, mask_output),
            ],
            title,
            params,
        )

    def flex_train(
        self,
        particles,
        mesh,
        particles_output="particles",
        mesh_output="flex_mesh",
        title="3D Flex Training",
        **params,
    ):
        return self._create_wrapper_job(
            "flex_train",
            [
                (("particles",), particles, particles_output),
                (("mesh", "flex_mesh"), mesh, mesh_output),
            ],
            title,
            params,
        )

    def flex_generate(
        self,
        model,
        volume=None,
        model_output="flex_model",
        volume_output="volume",
        title="3D Flex Generator",
        **params,
    ):
        return self._create_wrapper_job(
            "flex_generate",
            [
                (("model", "flex_model", "checkpoint"), model, model_output),
                (("volume", "full_res_volume"), volume, volume_output),
            ],
            title,
            params,
        )

    def flex_reconstruct(
        self,
        particles,
        model,
        particles_output="particles",
        model_output="flex_model",
        title="3D Flex Reconstruction",
        **params,
    ):
        return self._create_wrapper_job(
            "flex_reconstruct",
            [
                (("particles",), particles, particles_output),
                (("model", "flex_model", "checkpoint"), model, model_output),
            ],
            title,
            params,
        )

    def _source(self, value, output):
        if isinstance(value, tuple):
            return value
        return self.out(value, output)

    def _source_list(self, value, output):
        if isinstance(value, list):
            return [self._source(item, output) for item in value]
        return self._source(value, output)


def connect_cryosparc(base_url=DEFAULT_URL, email=None, password=None, project_uid=None, workspace_uid=None, timeout=300):
    base_url = base_url.rstrip("/")
    email = email or os.environ.get("CRYOSPARC_EMAIL")
    password = password or os.environ.get("CRYOSPARC_PASSWORD")
    if not email:
        email = input("cryoSPARC email: ")
    if not password:
        password = getpass.getpass("cryoSPARC password: ")
    cs = CryoSPARC(base_url, email=email, password=password, timeout=timeout)
    cs.test_connection()
    return CryoSPARCWorkflow(cs, project_uid=project_uid, workspace_uid=workspace_uid)


def connect_cryosparc_from_env(project_uid=None, workspace_uid=None, timeout=300):
    return connect_cryosparc(
        base_url=os.environ.get("CRYOSPARC_BASE_URL", DEFAULT_URL),
        project_uid=project_uid,
        workspace_uid=workspace_uid,
        timeout=timeout,
    )


def _positive_min(value):
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def job_summary(job):
    if hasattr(job, "refresh"):
        job.refresh()
    model = job.model
    workspace_uids = getattr(model, "workspace_uids", None) or []
    spec = getattr(model, "spec", None) or {}
    if hasattr(spec, "model_dump"):
        spec = spec.model_dump()
    return {
        "uid": model.uid,
        "type": spec.get("type") or getattr(model, "type", None),
        "status": getattr(model, "status", None),
        "project_uid": getattr(model, "project_uid", None),
        "workspace_uid": getattr(model, "workspace_uid", None) or (workspace_uids[0] if workspace_uids else None),
        "title": getattr(model, "title", None),
    }


CRYOSPARC_HELP = """
Available cryoSPARC wrappers and REPL conventions:

REPL state and connection
- `csw`: active `CryoSPARCWorkflow` bound to the session's one project and
  `Automated Reconstruction` workspace. Reuse it; do not reconnect or switch
  project/workspace inside the agent session.
- `connect_cryosparc(base_url=..., email=..., password=..., project_uid=None,
  workspace_uid=None, timeout=300)`: connect and return a `CryoSPARCWorkflow`.
  Use only when `csw` is missing.
- `connect_cryosparc_from_env(project_uid=None, workspace_uid=None, timeout=300)`:
  connect from `CRYOSPARC_BASE_URL`, `CRYOSPARC_EMAIL`, and `CRYOSPARC_PASSWORD`.
  Avoid this inside the harness unless all env vars are definitely set.
- `job_summary(job)`: return a compact dict with UID, type, status, project,
  workspace, and title. Use after every create/queue/wait step.

General conventions
- Most `csw.*` job wrappers create a cryoSPARC job and return a `JobController`;
  they do not queue the job unless the wrapper explicitly says it queues.
- Chain outputs with `csw.out(job, "output_name")` or by passing a job object to
  high-level wrappers that know their default output names.
- Use `csw.job_spec(job_type)` before generic `csw.create_job(...)` when unsure
  about live cryoSPARC input, output, or parameter keys.
- Use `csw.validate_job(...)` before generic job creation. It has no side effects.
- Prefer `csw.queue_and_wait(job)` over `csw.queue(job, wait=True)` when running
  jobs. It refreshes the job, waits for completion, checks `has_error` and
  `has_warning`, and includes the job log tail on failures/warnings.
- Common terminal statuses are `completed`, `failed`, and `killed`. Interactive
  jobs may pass through `waiting`, but wrappers such as `select_2d_classes` hide
  the interactive step when possible.
- Never wait only for a transient status such as `waiting`, `queued`, or
  `running` without also checking terminal statuses. Prefer
  `csw.wait_for_status(...)` for transient states so the wait returns if the job
  has already completed, failed, or been killed.

Project, workspace, and job lookup
- `csw.use_project(project_uid)`: select an existing project like `"P15"` and
  return `csw`.
- `csw.create_project(title, project_dir, desc=None)`: create a project under
  the required server-visible parent directory, select it, and return the
  project controller.
- `csw.use_workspace(workspace_uid, project_uid=None)`: select an existing
  workspace like `"W1"` and return `csw`.
- `csw.create_workspace(title, project_uid=None, desc=None)`: create/select a
  workspace in the current or given project and return the workspace controller.
- `csw.list_projects()`: return `[{"uid", "title"}, ...]` for visible projects.
- `csw.list_workspaces(project_uid=None)`: return workspace summaries. Uses the
  current project when one is selected.
- `csw.list_jobs(project_uid=None, workspace_uid=None, limit=20)`: return recent
  job summaries. Uses the current workspace/project when selected.
- `csw.audit_jobs(project_uid=None, workspace_uid=None, limit=50,
  include_ok=False)`: return recent failed, killed, warning, and error-marked
  jobs with status, `has_error`, `has_warning`, queue message, and stored error
  fields. Set `include_ok=True` to include normal jobs too.
- `csw.find_job(job_uid, project_uid=None)`: return a live `JobController` for
  a job such as `"J33"`. Uses the current project unless `project_uid` is given.

Generic job discovery and control
- `csw.job_types(search=None)`: list available job type keys. Pass a search term
  such as `"refine"` or `"select"` to filter.
- `csw.resolve_job_type(candidates, title=None, category=None)`: resolve an
  active installed type from ordered internal-key candidates or a unique exact
  title/category match. Legacy and obsolete jobs are rejected; fuzzy matches
  are reported only as error hints.
- `csw.job_spec(job_type)`: return type/title/description/category/stability,
  input/output counts and types, parameter types/defaults/enums/ranges,
  compatibility name lists, and full live `input_specs`, `output_specs`, and
  `param_specs` metadata.
- `csw.out(job_or_uid, output)`: return `(job_uid, output)` for low-level
  connection dicts, e.g. `csw.out(j33, "particles")`.
- `csw.validate_job(job_type, connections=None, params=None,
  require_all_inputs=True)`: return an `ok` bool plus unknown/missing inputs and
  params, primitive parameter types, enums, and numeric bounds when exposed by
  the installed registry. Does not create a job.
- `csw.create_job(job_type, connections=None, params=None, title="", desc="",
  validate=True, require_all_inputs=True)`: create a generic job in the selected
  workspace. It validates by default and returns a `JobController`.
- `csw.queue(job, lane=None, hostname=None, gpus=None, cluster_vars=None,
  wait=False, timeout=None)`: queue a job and return a job summary, or wait for
  completion when `wait=True`. Prefer `queue_and_wait` for actual runs because
  it catches failures and warnings better.
- `csw.queue_and_wait(job, lane=None, hostname=None, gpus=None,
  cluster_vars=None, timeout=None, error_on_failed=True, log_lines=80)`: queue a
  building job if needed, always wait until terminal status, refresh, and return
  a summary with `has_error` and `has_warning`. If the job failed/killed or
  `has_error=True`, attach `log_tail`; when `error_on_failed=True`, raise a
  `RuntimeError` containing the useful log tail. If the job has warnings, return
  `log_tail` without raising.
- `csw.wait(job, timeout=None, error_on_incomplete=False)`: wait until a job is
  completed, failed, or killed, then return `{"uid", "status"}`.
- `csw.wait_for_status(job_or_uid, statuses, stop_statuses=("completed",
  "failed", "killed"), poll_seconds=5, project_uid=None)`: wait until a job
  reaches one of `statuses`, but return early with `ok=False` if it reaches a
  stop/terminal status first. Use this instead of raw `run_and_wait` for
  conditions like "wait until J10 is waiting" because J10 may already be
  completed.
- `csw.job_log_tail(job_or_uid, lines=80, project_uid=None)`: return the last
  lines of `job.log` as `{"uid", "status", "has_error", "has_warning",
  "source", "lines", "text"}`. Use immediately after a failed or warning job to
  understand cryoSPARC runtime errors such as assertion failures.
- `csw.status(job)`: refresh and summarize a job.
- `csw.clear(job)`: clear a queued/building job back to building state.
- `csw.kill(job)`: kill a queued/running job.
- `csw.delete(job_or_uid)`: delete a job from the current project.
- `csw.connect(target_job, target_input, source_job, source_output)`: connect an
  output from one job into an input on another existing job.

Assets, plots, and 2D class visual inspection
- `csw.export_png_assets(job_or_uid, out_dir="cryosparc_png_assets",
  project_uid=None)`: download PNG assets from a job database record. Returns
  `{"job_uid", "count", "paths"}`; use `result["paths"]` for image paths.
- `csw.render_2d_class_sheet(job_or_uid_or_dir, output=None,
  sort_mode="similarity", eca_stat="median", cols=10, scale_bar_a=120.0,
  project_uid=None, cache_dir="cryosparc_class_sheets",
  contrast_percentiles=None)`: render a contact sheet
  from a 2D classification job or exported job directory. It prints each
  0-based class index as `#N` on the tile and returns `{"path", "n_classes",
  "sort", "contrast", "class_order", "counts", "res_A"}`.
- `csw.render_2d_class_batches(job_or_uid_or_dir, batch_size=8,
  class_indices=None, sort_mode="similarity", cols=4,
  contrast_percentiles=None, ...)`: render all classes,
  or only `class_indices`, into numbered review images. Choose a practical
  batch size of about 5-20 classes. Returns `paths` plus the exact class indices
  and particle count in each batch.
- `csw.preview_2d_class_selection(job_or_uid_or_dir, selected_indices,
  batch_size=8, sort_mode="similarity", cols=4,
  contrast_percentiles=None, ...)`: render separate green
  SELECTED and red REJECTED image batches for a proposed selection. Returns
  both groups' class lists, paths, particle counts, and selected-particle
  fraction. This does not create or queue a CryoSPARC job.
- `csw.select_2d_classes(class_job, class_indices, wait=True, ...)`: create,
  queue, and usually complete a `select_2D` job from a completed 2D
  classification job. `class_indices` may be a list like `[0, 7, 22]` or a
  string like `"0,7,22"`. Returns the created `select_2D` `JobController`.
  Outputs are `particles_selected`, `templates_selected`, `particles_excluded`,
  and `templates_excluded`.

All three renderers default to each class's full finite intensity range with
linear contrast: no percentile clipping, filtering, or changes to source data.
The PNG labels and returned `contrast` metadata identify the display setting.
If extreme pixels obscure detail, re-render with an explicit percentile window,
e.g. `contrast_percentiles=(0.2, 99.8)`, and compare against the unclipped view.
Identical percentile limits fall back to the full range. Do not mistake a
contrast-saturated white core for a featureless particle or amplified background
for structure; check alternative contrast and single-class views before deciding.
Intensities are scaled per class, so brightness is not comparable between classes.

Before `select_2d_classes`, review the class batches and make a draft selection.
Then call `preview_2d_class_selection`, visualize both the selected and rejected
groups, and revise the selection if needed. Iterate until the split looks
correct, then pass the verified `preview["selected"]["class_indices"]` to
`select_2d_classes`.

Import and preprocessing wrappers
- `csw.import_movies(blob_paths, psize_A, accel_kv, cs_mm,
  total_dose_e_per_A2, gainref_path=None, title="Import Movies", **params)`:
  create `import_movies`. Main output is usually `imported_movies`.
- `csw.import_micrographs(blob_paths, psize_A, accel_kv, cs_mm,
  total_dose_e_per_A2, title="Import Micrographs", **params)`: create
  `import_micrographs` for already motion-corrected exposures.
- `csw.import_particle_stack(particle_meta_path, particle_data_path,
  source_exposures=None, exposures_output="imported_micrographs", psize_A=None,
  accel_kv=None, cs_mm=None, total_dose_e_per_A2=None, data_sign=None,
  ignore_raw_data=None, ignore_pose_data=None, title="Import Particle Stack",
  **params)`: create-only wrapper for the installed Import Particle Stack job.
  It resolves live parameter names, imports `.mrc`/`.mrcs` particle images with
  `.star` metadata, and commonly outputs `imported_particles`; it never queues.
  Pose and CTF metadata are retained unless their ignore options are explicitly
  enabled. `source_exposures` is optional and is needed only for downstream jobs
  that require exposure linkage. CryoSPARC links rather than copies particle
  files, so keep their server-visible paths stable and visually verify data sign.
- `csw.import_volume(volume_blob_path, volume_out_name="imported_volume",
  volume_psize=None, title="Import Volume", **params)`: create
  `import_volumes`. Default output for chaining is `imported_volume`.
- `csw.create_templates(volume, volume_output="imported_volume",
  n_templates=50, title="Create Templates", **params)`: create templates from a
  volume job/output. Default input source is `volume#imported_volume`.
- `csw.patch_motion(movies, movies_output="imported_movies", output_f16=True,
  random_seed=0, gpus=1, title="Patch Motion Correction", **params)`: create
  `patch_motion_correction_multi`. Typical output for CTF is `micrographs`.
- `csw.patch_ctf(exposures, exposures_output="micrographs", gpus=1,
  title="Patch CTF Estimation", **params)`: create `patch_ctf_estimation_multi`.
  Typical output for curation/picking is `exposures`.
- `csw.curate_exposures(exposures, exposures_output="exposures",
  title="Curate Exposures", **params)`: create `curate_exposures_v2`. Typical
  accepted output is `exposures_accepted`.

Particle picking and extraction wrappers
- `csw.blob_pick(micrographs, diameter_A, micrographs_output="exposures_accepted",
  diameter_max_A=None, title="Blob Picker", **params)`: create
  `blob_picker_gpu`. Output is usually `particles`.
- `csw.inspect_picks(particles, micrographs=None, particles_output="particles",
  micrographs_output="micrographs", title="Inspect Picks", **params)`: create
  `inspect_picks_v2` for manual/interactive pick inspection.
- `csw.remove_duplicates(particles, micrographs, particles_output="particles",
  micrographs_output="micrographs", min_distance_A=50, title=..., **params)`:
  create `remove_duplicate_particles`.
- `csw.plan_particle_extraction(box_size_pix, source_pixel_size_A,
  particle_extent_A, fourier_crop_size_pix=None, target_resolution_A=None,
  flexible=False, allow_small_box=False, small_box_rationale=None)`: mandatory
  preflight for every extraction. Print and inspect this result before creating
  the job. `particle_extent_A` is the largest plausible extent, not merely a
  compact-state diameter. The plan reports physical field of view,
  box-to-particle ratio, output sampling, Nyquist limit, and warnings.
- `csw.extract_particles(micrographs, particles, box_size_pix, bin_size_pix=None,
  micrographs_output="micrographs", particles_output="particles",
  output_f16=True, gpus=1, title="Extract Particles",
  source_pixel_size_A=..., particle_extent_A=...,
  target_resolution_A=None, flexible=False, allow_small_box=False,
  small_box_rationale=None, **params)`: create `extract_micrographs_multi`.
  `source_pixel_size_A` must be the actual upstream motion-corrected micrograph
  sampling, not an assumed raw-movie value. The wrapper rejects a physical box
  below 2x particle extent unless an explicit override and rationale are given,
  warns below 2.5x for flexible particles, checks Fourier-cropped Nyquist
  sampling, and records the audit in the job description. Output is usually
  `particles`. After the first extraction and 2D classification, MUST render and
  inspect representative particles and every 2D class for clipped compact,
  extended, end-on, rare, or unusual views. If meaningful density enters the
  outer 10-15% of the box, re-extract before reconstruction and do not reject
  particles merely because the prior box clipped them.

2D classification and template picking wrappers
- `csw.class_2d(particles, particles_output="particles", classes=200,
  max_resolution_A=12, zero_pad_factor=1, random_seed=0, gpus=1,
  title="2D Classification", **params)`: create `class_2D_new`. Outputs include
  `particles`, `particles_rejected`, and `class_averages`.
- `csw.select_2d(particles, templates, particles_output="particles",
  templates_output="class_averages", title="Select 2D Classes", **params)`:
  create raw `select_2D`. Prefer `select_2d_classes(...)` when selecting by
  known class indices.
- `csw.template_pick(templates, micrographs, diameter_A,
  templates_output="templates_selected", micrographs_output="exposures_accepted",
  title="Template Picker", **params)`: create `template_picker_gpu` using
  selected 2D templates.

3D reconstruction and refinement wrappers
- `csw.abinit(particles, particles_output="particles", classes=3,
  num_particles=None, symmetry="C1", random_seed=0, title=..., **params)`:
  create `homo_abinit`. Outputs commonly include `particles_all_classes` and
  `volumes_all_classes`. Leave `num_particles` unset for multi-class runs:
  a set value limits the classified outputs to that subset.
- `csw.hetero_refine(particles, volumes, particles_output="particles",
  volume_output="volumes_all_classes", classes=None, symmetry="C1",
  force_hard=True, random_seed=0, title=..., **params)`: create
  `hetero_refine` from particles plus one or more reference volumes.
- `csw.homogeneous_refine(particles, volume, particles_output="particles",
  volume_output="volume", symmetry="C1", random_seed=0, title=..., **params)`:
  create `homo_refine_new`.
- `csw.nonuniform_refine(particles, volume, particles_output="particles",
  volume_output="volume", symmetry="C1", dynamic_mask=False,
  dynamic_mask_start_A=None, minimize_scale=True, random_seed=0, title=...,
  **params)`: create `nonuniform_refine_new`. With `dynamic_mask=False` the
  wrapper turns off `Use dynamic refinement mask` (CryoSPARC v5), or sets
  `Dynamic mask start resolution (A)` to 0.1 on older releases. The FSC mask
  stays dynamic either way. When masking is off, connect `mask_fsc_auto` to
  later Orientation Diagnostics, CTF refinement and RBMC jobs, or they use a
  whole-box mask.
- `csw.global_ctf_refine(particles, volume, particles_output="particles",
  volume_output="volume", title="Global CTF Refinement", **params)`: create
  `ctf_refine_global`.
- `csw.local_ctf_refine(particles, volume, particles_output="particles",
  volume_output="volume", title="Local CTF Refinement", **params)`: create
  `ctf_refine_local`.

Validation and map post-processing wrappers
- `csw.orientation_diagnostics(volume, particles, volume_output="volume",
  particles_output="particles", title="Orientation Diagnostics", **params)`:
  create `orientation_diagnostics`.
- `csw.validation(volume, mask=None, volume_output="volume",
  mask_output="mask_fsc_auto", title="Validation (FSC)", **params)`: create
  `validation`, optionally with a mask.
- `csw.sharpen(volume, mask=None, volume_output="volume",
  mask_output="mask_fsc_auto", bfactor=None, title="Sharpening Tools",
  **params)`: create `sharpen`; pass `bfactor` to set `sharp_bfactor`.

Additional preprocessing and particle-curation wrappers (all create-only)
- `csw.micrograph_denoiser(exposures, model=None, **params)`: connect
  CTF-estimated Patch Motion exposures and an optional denoise model; commonly
  outputs denoised exposures and a model. Denoised images aid viewing/picking
  but add no reconstruction signal; extraction still uses raw micrographs.
- `csw.micrograph_junk_detector(exposures, particles=None, **params)`: connect
  exposures and optional picks; commonly outputs annotated/filtered exposures
  or particles. Inspect annotations because contaminants and true particles can
  be confused.
- `csw.exposure_groups(exposures=None, particles=None, **params)`: connect at
  least one exposure or particle group and create Exposure Group Utilities;
  outputs regrouped/split data. Group only on real acquisition differences.
- `csw.reference_motion_correction(exposures, particles, volumes, masks=None,
  hyperparameters=None, **params)`: supports repeated particle/half-map
  references and masks; outputs corrected particles/exposures and motion or
  dose hyperparameters. The wrapper derives the live reference-count parameter,
  creates the job, then connects its materialized numbered inputs. Use
  high-quality half-map references with matched poses.
- `csw.subset_particles(particles=..., **params)` or
  `csw.subset_particles(initial_particles=..., final_particles=..., **params)`:
  output statistic-based particle subsets. Per-particle scores may correlate
  with orientation or ice, so compare maps before discarding a population.
- `csw.rebalance_orientations(particles, **params)`: output an orientation-
  reweighted particle subset. It can reduce overrepresented views but cannot
  manufacture missing views and may discard valuable rare particles.

Additional reconstruction and local-processing wrappers (all create-only)
- `csw.homogeneous_reconstruct(particles, mask=None, **params)`: reconstruct
  aligned particles without pose refinement; outputs a volume/halfmaps and
  particles. Supplying symmetry-expanded particles requires careful symmetry
  settings.
- `csw.heterogeneous_reconstruct(particles, mask=None, **params)`: reconstruct
  class-posterior particles without alignment; outputs all-class particles and
  a volumes group. Confirm the class-posterior prefix matches the upstream job.
- `csw.class_3d(particles, volumes=None, solvent_mask=None, focus_mask=None,
  **params)`: classify already aligned particles; commonly outputs all-class
  particles and volumes. 3D Classification does not realign poses; use
  Heterogeneous Refinement when pose updates are needed.
- `csw.regroup_3d(particles, volumes, mask=None, **params)`: regroup an
  all-class particle stack and volumes group into superclasses; outputs
  regrouped particles/volumes. Small or incoherent classes can distort grouping.
- `csw.volume_tools(volume=...|mask=..., **params)`: accept exactly one volume
  or mask and output the transformed object. Downstream masks must have a soft
  edge; thresholding, dilation, filtering, and resampling order matters.
- `csw.volume_alignment_tools(volume, mask=None, particles=None, **params)`:
  output a rigidly transformed volume/mask and consistently transformed particle
  poses. Reassigning particle UIDs breaks upstream low-level result overrides.
- `csw.align_3d_maps(reference, volumes_group=...|individual_volumes=[...],
  reference_mask=None, masks=None, particles=None, **params)`: explicitly choose
  a volumes-group or individual-map mode. The wrapper connects maps first, then
  resolves the parameter-dependent particle/mask inputs on the created job;
  individual particles and masks must match map order. Outputs aligned volumes
  and particles. Verify chirality and difference-map diagnostics.
- `csw.symmetry_expand(particles, **params)`: output expanded particle
  observations. The inputs must
  already use CryoSPARC's conventional symmetry axes; expansion multiplies
  observations, not independent experimental evidence.
- `csw.particle_subtract(particles, volume, mask, **params)`: output subtracted
  particles. It requires gold-standard half-set particles, a volume containing
  matching halfmaps, a soft mask, and matching boxes/pixel sizes.
- `csw.local_refine(particles, volume, mask, **params)`: output locally refined
  particles and volume. It requires aligned gold-standard half sets, matching
  halfmaps, and a biologically appropriate soft mask; inspect for overfitting.
- `csw.local_resolution(volume, mask=None, **params)`: output a local-resolution
  map from halfmaps and an optional soft mask. Treat the map as spatial
  diagnostic evidence rather than a substitute for half-map FSC validation.
- `csw.local_filter(volume, **params)`: accept the single Local
  Resolution volume containing `map_half_A`, `map_half_B`, and `map_locres`
  (plus its optional `mask_refine` slot) and output `map_filtered`. The mask is
  not a separate job input. Compare against the unfiltered map for sharpening
  or local-filter artifacts.

Heterogeneity and 3D Flex wrappers (all create-only)
- `csw.variability_3d(particles, mask=None, **params)`: output particles with
  variability coordinates and component volumes. 3DVA is exploratory; modes
  can mix compositional and continuous changes and do not prove a mechanism.
- `csw.variability_3d_display(particles, volumes, **params)`: render clusters,
  intermediates, or simple-mode trajectories from matched 3DVA outputs;
  commonly outputs volume series and particle subsets. Display choices can
  exaggerate apparent motion, so compare modes and refinements.
- `csw.flex_data_prep(particles, volume, **params)`: prepare aligned
  particles and a matching consensus volume for 3D Flex. Its outputs are
  training data, not a final reconstruction; keep the training box within the
  installed version's limits.
- `csw.flex_mesh_prep(volume, mask, **params)`: output a tetrahedral mesh from
  the prepared consensus map and required matching solvent mask (with optional
  segmentation supplied through live parameters). Inspect that the mesh
  follows the moving biological density rather than solvent or micelle.
- `csw.flex_train(particles, mesh, **params)`: output training checkpoints/a
  flex model from matched prepared particles and mesh. Training is exploratory
  and should be checked for convergence, reproducibility, and implausible
  deformations.
- `csw.flex_generate(model, volume=None, **params)`: generate volume series
  from a training checkpoint and optional full-resolution volume. Generated
  trajectories visualize the learned manifold; they are not independent
  structural classes.
- `csw.flex_reconstruct(particles, model, **params)`: create high-resolution
  3D Flex reconstruction from prepared high-resolution particles and a matched
  training checkpoint; commonly outputs halfmaps. Validate the density with
  half-map and biological diagnostics.
""".strip()
