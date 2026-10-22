"""Map and map-model images drawn with numpy and matplotlib.

Headless compute nodes have no OpenGL, so PyMOL and ChimeraX fall back to slow
software rendering for isosurfaces. These views need no OpenGL: thresholded
projections and slices for contour checks, and density slabs with model atoms
for fit inspection. Each image takes about a second.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402

AXES = ("x", "y", "z")
# Projection along one axis, viewed with the remaining two (array index order is z, y, x).
VIEWS = (("front", 0, ("x", "y")), ("side", 2, ("y", "z")), ("top", 1, ("x", "z")))
DAQ_CMAP = LinearSegmentedColormap.from_list("daq", ["#c0392b", "#ffffff", "#2b5aa8"])
DAQ_RANGE = 1.5


def _extent(origin, step, shape, plane):
    """Image extent in Å for a projection onto `plane` (two of x, y, z)."""
    size = {"x": shape[2], "y": shape[1], "z": shape[0]}
    lo = {a: origin[i] for i, a in enumerate(AXES)}
    sp = {a: step[i] for i, a in enumerate(AXES)}
    h, v = plane
    return [lo[h], lo[h] + size[h] * sp[h], lo[v], lo[v] + size[v] * sp[v]]


def _plane(volume2d, plane):
    """Return a 2D array oriented for imshow(origin='lower') with plane = (horizontal, vertical)."""
    # After projecting out one axis the remaining array axes are in z, y, x order.
    remaining = [a for a in ("z", "y", "x") if a in plane]
    if remaining == [plane[1], plane[0]]:
        return volume2d
    return volume2d.T


def component_stats(mask: np.ndarray) -> dict:
    """Connected components of the thresholded map: how much of it is one body versus noise."""
    from scipy import ndimage

    labels, count = ndimage.label(mask)
    if count == 0:
        return {"components": 0, "largest_fraction": 0.0, "voxels_above": 0}
    sizes = np.bincount(labels.ravel())[1:]
    return {"components": int(count), "largest_fraction": float(sizes.max() / sizes.sum()),
            "voxels_above": int(sizes.sum())}


def contour_views(data, origin, step, levels, output: Path, title: str = "") -> list[dict]:
    """One row per contour level: three thresholded projections and one central slice."""
    stats = []
    fig, axes = plt.subplots(len(levels), 4, figsize=(13, 3.3 * len(levels)), squeeze=False)
    from scipy import ndimage

    weights = np.clip(data, 0, None)
    com = np.array(data.shape) // 2
    if weights.sum() > 0:
        com = np.clip(np.round(ndimage.center_of_mass(weights)).astype(int), 0, np.array(data.shape) - 1)
    for row, level in enumerate(levels):
        mask = data > level
        st = {"level": float(level), **component_stats(mask)}
        stats.append(st)
        for col, (name, axis, plane) in enumerate(VIEWS):
            proj = _plane(mask.mean(axis=axis), plane)
            ax = axes[row][col]
            ax.imshow(proj, origin="lower", cmap="Greys", vmin=0, vmax=max(proj.max(), 1e-6),
                      extent=_extent(origin, step, data.shape, plane))
            ax.set_title(f"{name}: projection above {level:.4g}", fontsize=9)
            ax.set_xlabel(f"{plane[0]} (Å)", fontsize=8)
            ax.set_ylabel(f"{plane[1]} (Å)", fontsize=8)
            ax.tick_params(labelsize=7)
        sl = _plane(data[com[0]], ("x", "y"))
        ax = axes[row][3]
        lo, hi = np.percentile(data, [1, 99.9])
        ext = _extent(origin, step, data.shape, ("x", "y"))
        ax.imshow(sl, origin="lower", cmap="Greys", vmin=lo, vmax=hi, extent=ext)
        if (sl > level).any() and (sl < level).any():
            ax.contour(sl, levels=[level], colors="#d35400", linewidths=0.8, origin="lower", extent=ext)
        ax.set_title(f"central z slice; contour {level:.4g}", fontsize=9)
        ax.tick_params(labelsize=7)
        axes[row][0].text(0.01, 0.99, f"{st['components']} components; largest {st['largest_fraction']:.1%}",
                          transform=axes[row][0].transAxes, va="top", fontsize=8, color="#c0392b",
                          bbox=dict(facecolor="white", alpha=0.7, edgecolor="none", pad=1))
    if title:
        fig.suptitle(title, fontsize=11)
    fig.tight_layout()
    fig.savefig(output, dpi=110)
    plt.close(fig)
    return stats


def parse_selection(selection: str, atoms) -> np.ndarray:
    """Boolean mask for 'all', 'chain A', 'resi 10-40', or terms joined with 'and'."""
    keep = np.ones(len(atoms["chain"]), dtype=bool)
    text = selection.strip()
    if text.lower() in ("", "all"):
        return keep
    for term in re.split(r"\s+and\s+", text, flags=re.I):
        m = re.fullmatch(r"chain\s+(\S+)", term.strip(), flags=re.I)
        if m:
            keep &= atoms["chain"] == m.group(1)
            continue
        m = re.fullmatch(r"resi\s+(-?\d+)(?:\s*-\s*(-?\d+))?", term.strip(), flags=re.I)
        if m:
            a = int(m.group(1)); b = int(m.group(2)) if m.group(2) else a
            keep &= (atoms["resi"] >= min(a, b)) & (atoms["resi"] <= max(a, b))
            continue
        raise ValueError(f"Unsupported selection term {term!r}; use 'all', 'chain X', 'resi a-b', joined by 'and'.")
    return keep


def read_atoms(path: Path) -> dict:
    import gemmi

    structure = gemmi.read_structure(str(path))
    if len(structure) != 1:
        raise ValueError("Render one candidate model at a time.")
    rows = [(ch.name, res.seqid.num, a.pos.x, a.pos.y, a.pos.z, a.b_iso, a.occ, a.name)
            for ch in structure[0] for res in ch for a in res]
    if not rows:
        raise ValueError("Model has no atoms.")
    chain, resi, x, y, z, b, occ, name = zip(*rows)
    return {"chain": np.array(chain), "resi": np.array(resi), "xyz": np.c_[x, y, z],
            "b": np.array(b, dtype=float), "occ": np.array(occ, dtype=float), "name": np.array(name)}


def overlay_views(data, origin, step, atoms, contour, outputs, selection="all", color_by_bfactor=False,
                  slab=12.0) -> dict:
    """Three orthogonal density slabs through the selection centre with the model's atoms in the slab."""
    focus = parse_selection(selection, atoms)
    if not focus.any():
        raise ValueError("Focus selection is empty.")
    xyz = atoms["xyz"]
    centre = xyz[focus].mean(0)
    origin_a = np.asarray(origin, float); step_a = np.asarray(step, float)
    span = np.ptp(xyz[focus], axis=0) + 2 * 8.0
    for (name, axis, plane), out in zip(VIEWS, outputs):
        depth_axis = "zyx"[axis]
        d = AXES.index(depth_axis)
        c_idx = int(round((centre[d] - origin_a[d]) / step_a[d]))
        half = max(1, int(round(slab / 2 / step_a[d])))
        lo_i, hi_i = max(0, c_idx - half), min(data.shape[axis], c_idx + half + 1)
        sl = [slice(None)] * 3
        sl[axis] = slice(lo_i, hi_i)
        proj = _plane(data[tuple(sl)].max(axis=axis), plane)
        ext = _extent(origin, step, data.shape, plane)
        fig, ax = plt.subplots(figsize=(8, 6.4))
        vmax = np.percentile(data, 99.9)
        ax.imshow(proj, origin="lower", cmap="Greys", vmin=min(contour, vmax) * 0.5, vmax=vmax, extent=ext)
        if (proj > contour).any() and (proj < contour).any():
            ax.contour(proj, levels=[contour], colors="#7f8c8d", linewidths=0.6, origin="lower", extent=ext)
        zlo, zhi = origin_a[d] + lo_i * step_a[d], origin_a[d] + hi_i * step_a[d]
        inside = focus & (xyz[:, d] >= zlo) & (xyz[:, d] <= zhi)
        h, v = (AXES.index(p) for p in plane)
        pts = xyz[inside]
        if color_by_bfactor:
            scored = atoms["occ"][inside] > 0
            ax.scatter(pts[~scored, h], pts[~scored, v], s=6, c="#9a9a9a", linewidths=0)
            ax.scatter(pts[scored, h], pts[scored, v], s=6, c=atoms["b"][inside][scored], cmap=DAQ_CMAP,
                       vmin=-DAQ_RANGE, vmax=DAQ_RANGE, linewidths=0.2, edgecolors="#333333")
        else:
            ax.scatter(pts[:, h], pts[:, v], s=6, c="#2b5aa8", linewidths=0)
        ca = inside & (atoms["name"] == "CA")
        ax.scatter(xyz[ca, h], xyz[ca, v], s=10, facecolors="none", edgecolors="#111111", linewidths=0.4)
        ax.set_xlim(centre[h] - span[h] / 2, centre[h] + span[h] / 2)
        ax.set_ylim(centre[v] - span[v] / 2, centre[v] + span[v] / 2)
        ax.set_aspect("equal")
        ax.set_xlabel(f"{plane[0]} (Å)"); ax.set_ylabel(f"{plane[1]} (Å)")
        scale = ("DAQ: red -1.5 / white 0 / blue +1.5; unscored gray" if color_by_bfactor else "model atoms blue")
        ax.set_title(f"{name}: {slab:g} Å slab at {depth_axis} = {centre[d]:.1f} Å | contour {contour:g} | "
                     f"{scale} | {int(inside.sum())} atoms", fontsize=8)
        fig.tight_layout()
        fig.savefig(out, dpi=150)
        plt.close(fig)
    return {"centre": [float(v) for v in centre], "slab_A": slab, "focus_atoms": int(focus.sum())}
