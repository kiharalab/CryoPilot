"""Small, cached previews of large density maps for the in-browser viewer.

CryoSPARC maps are often 320-512 voxels a side (130-540 MB), too much to send
to Mol* in a browser. ``preview`` mean-pools a map by an integer factor so its
longest side is at most ``MAX_PREVIEW_SIZE`` voxels and writes the result as a
new MRC file whose voxel size, cell, and origin describe the binned grid, so
it overlays the full map and any model built into it. The full map's
statistics (min, max, mean, rms) are computed on the same pass and cached
next to the preview.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
from pathlib import Path
from typing import Any

import numpy as np

from .config import APP_HOME

PREVIEW_DIR = APP_HOME / "cache" / "volume-previews"
MAX_PREVIEW_SIZE = 256
PREVIEW_CACHE_BYTES = 4 * 1024**3

_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


class VolumeError(ValueError):
    pass


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.Lock()
        return lock


def _cache_key(source: Path, max_size: int) -> str:
    stat = source.stat()
    raw = f"{source.resolve()}|{stat.st_mtime_ns}|{stat.st_size}|{max_size}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def bin_factor(shape: tuple[int, ...] | list[int], max_size: int | None = None) -> int:
    """The smallest integer factor that brings every side to ``max_size`` or less."""

    max_size = max_size or MAX_PREVIEW_SIZE
    longest = max(int(n) for n in shape) if shape else 0
    return max(1, math.ceil(longest / max_size)) if longest else 1


def _origin_xyz(header: Any, voxel_size: tuple[float, float, float]) -> np.ndarray:
    """Position of voxel (0, 0, 0) in Å, following ChimeraX: ORIGIN if set, else NxSTART."""

    origin = np.array([float(header.origin.x), float(header.origin.y), float(header.origin.z)])
    if np.any(origin != 0):
        return origin
    start = np.array([float(header.nxstart), float(header.nystart), float(header.nzstart)])
    return start * np.array(voxel_size)


def _stats(total: float, total_sq: float, count: int, low: float, high: float) -> dict[str, float]:
    mean = total / count if count else 0.0
    variance = max(total_sq / count - mean * mean, 0.0) if count else 0.0
    return {"min": float(low), "max": float(high), "mean": float(mean), "rms": float(math.sqrt(variance))}


def _pool(source: Path, target: Path, factor: int) -> dict[str, Any]:
    """Mean-pool ``source`` by ``factor`` into ``target``; return the full map's stats."""

    import mrcfile

    with mrcfile.mmap(str(source), mode="r", permissive=True) as mrc:
        data = mrc.data
        if data is None or data.ndim != 3:
            raise VolumeError(f"{source.name} is not a 3D map")
        nz, ny, nx = (int(n) for n in data.shape)
        voxel = (float(mrc.voxel_size.x), float(mrc.voxel_size.y), float(mrc.voxel_size.z))
        origin = _origin_xyz(mrc.header, voxel)
        axis_order = (int(mrc.header.mapc), int(mrc.header.mapr), int(mrc.header.maps))
        out_z, out_y, out_x = nz // factor, ny // factor, nx // factor
        if factor > 1 and min(out_z, out_y, out_x) < 1:
            raise VolumeError(f"{source.name} is too small to bin by {factor}")
        binned = np.empty((out_z, out_y, out_x), dtype=np.float32) if factor > 1 else None
        total = total_sq = 0.0
        low, high = math.inf, -math.inf
        count = 0
        # Read a slab of `factor` sections at a time so memory stays near one
        # slab even for very large maps; the statistics cover the whole map.
        step = factor if factor > 1 else max(1, 16 * 1024**2 // max(ny * nx * 4, 1))
        for z0 in range(0, nz, step):
            slab = np.asarray(data[z0 : z0 + step], dtype=np.float64)
            finite = slab[np.isfinite(slab)]
            if finite.size:
                total += float(finite.sum())
                total_sq += float(np.square(finite).sum())
                low = min(low, float(finite.min()))
                high = max(high, float(finite.max()))
                count += int(finite.size)
            if binned is not None:
                k = z0 // factor
                if k >= out_z or slab.shape[0] < factor:
                    continue
                block = slab[:, : out_y * factor, : out_x * factor]
                block = np.nan_to_num(block, nan=0.0, posinf=0.0, neginf=0.0)
                binned[k] = block.reshape(factor, out_y, factor, out_x, factor).mean(axis=(0, 2, 4))
    stats = _stats(total, total_sq, count, low if count else 0.0, high if count else 0.0)
    stats.update({"shape": [nx, ny, nz], "voxel_size": list(voxel)})
    if binned is None:
        return stats

    # The first binned voxel is the mean of voxels 0..factor-1, so its centre
    # sits (factor - 1) / 2 original voxels further along every axis.
    new_origin = origin + (factor - 1) / 2.0 * np.array(voxel)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with mrcfile.new(str(tmp), overwrite=True) as out:
        out.set_data(binned)
        out.voxel_size = tuple(v * factor for v in voxel)
        out.header.mapc, out.header.mapr, out.header.maps = axis_order
        out.header.nxstart = out.header.nystart = out.header.nzstart = 0
        out.header.origin.x, out.header.origin.y, out.header.origin.z = (float(v) for v in new_origin)
        out.update_header_stats()
        out.add_label(f"CryoPilot preview: {source.name} mean-pooled by {factor}")
    os.replace(tmp, target)
    stats.update(
        {
            "bin_factor": factor,
            "preview_shape": [out_x, out_y, out_z],
            "preview_voxel_size": [v * factor for v in voxel],
        }
    )
    return stats


def _prune(directory: Path, max_bytes: int, keep: set[Path]) -> None:
    """Delete the least recently used previews until the cache fits ``max_bytes``."""

    try:
        files = [path for path in directory.iterdir() if path.is_file() and not path.name.startswith(".")]
    except OSError:
        return
    entries = []
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((stat.st_mtime, stat.st_size, path))
    used = sum(size for _, size, _ in entries)
    for _, size, path in sorted(entries):
        if used <= max_bytes:
            break
        if path in keep:
            continue
        try:
            path.unlink()
            used -= size
        except OSError:
            continue


def _touch(path: Path) -> None:
    try:
        os.utime(path)
    except OSError:
        pass


def preview(source: Path, max_size: int | None = None) -> tuple[Path, dict[str, Any]]:
    """Return a map file of at most ``max_size`` voxels a side, plus the full map's stats.

    Maps that are already small enough are returned as they are. Results are
    cached by source path, modification time, and size.
    """

    source = Path(source)
    max_size = max_size or MAX_PREVIEW_SIZE
    if not source.is_file():
        raise VolumeError(f"Map file not found: {source.name}")
    key = _cache_key(source, max_size)
    target = PREVIEW_DIR / f"{key}.mrc"
    stats_path = PREVIEW_DIR / f"{key}.json"
    with _lock_for(key):
        stats = _read_json(stats_path)
        if stats is not None:
            if stats.get("bin_factor", 1) <= 1:
                return source, stats
            if target.is_file():
                _touch(target)
                _touch(stats_path)
                return target, stats
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        factor = bin_factor(_shape(source), max_size)
        stats = _pool(source, target, factor)
        stats.setdefault("bin_factor", 1)
        _write_json(stats_path, stats)
        _prune(PREVIEW_DIR, PREVIEW_CACHE_BYTES, keep={target, stats_path})
    return (target if factor > 1 else source), stats


def cached_stats(source: Path, max_size: int | None = None) -> dict[str, Any] | None:
    """Stats recorded by an earlier ``preview`` of this exact file, if any."""

    try:
        key = _cache_key(Path(source), max_size or MAX_PREVIEW_SIZE)
    except OSError:
        return None
    stats = _read_json(PREVIEW_DIR / f"{key}.json")
    if stats is None:
        return None
    return {name: stats[name] for name in ("min", "max", "mean", "rms") if name in stats}


def _shape(source: Path) -> tuple[int, int, int]:
    import mrcfile

    with mrcfile.open(str(source), mode="r", permissive=True, header_only=True) as mrc:
        header = mrc.header
        return int(header.nx), int(header.ny), int(header.nz)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(value), encoding="utf-8")
    os.replace(tmp, path)


def stats_header(stats: dict[str, Any] | None) -> str:
    """Compact JSON for the ``X-Map-Stats`` response header."""

    if not stats:
        return ""
    keys = ("min", "max", "mean", "rms", "bin_factor")
    return json.dumps({key: stats[key] for key in keys if key in stats}, separators=(",", ":"))
