"""Physical-coordinate MRC handling shared by DAQ and map handedness tools."""
from pathlib import Path

import mrcfile
import numpy as np


def read_mrc(path: Path):
    with mrcfile.open(path, permissive=True) as mrc:
        data = np.asarray(mrc.data, dtype=np.float32).copy()
        axes = [int(mrc.header.maps), int(mrc.header.mapr), int(mrc.header.mapc)]
        if data.ndim != 3 or sorted(axes) != [1, 2, 3]:
            raise ValueError("Expected a 3D MRC with a valid MAPC/MAPR/MAPS axis permutation.")
        data = np.transpose(data, [axes.index(axis) for axis in (3, 2, 1)])
        if not np.isfinite(data).all():
            raise ValueError("Map density must be finite.")
        step = tuple(float(mrc.voxel_size[axis]) for axis in ("x", "y", "z"))
        if not all(np.isfinite(value) and value > 0 for value in step):
            raise ValueError("Map voxel spacing must be finite and positive.")
        origin = tuple(float(mrc.header.origin[axis]) for axis in ("x", "y", "z"))
        if all(abs(value) < 1e-6 for value in origin):
            starts = [int(mrc.header.nzstart), int(mrc.header.nystart), int(mrc.header.nxstart)]
            origin = tuple(starts[axes.index(axis)] * step[axis - 1] for axis in (1, 2, 3))
        if not all(np.isfinite(value) for value in origin):
            raise ValueError("Map origin must be finite.")
    return np.ascontiguousarray(data), origin, step


def resample_to_1a(data, origin, step):
    if np.allclose(step, (1, 1, 1), rtol=0, atol=1e-6):
        return data, origin, (1.0, 1.0, 1.0)
    from scipy.ndimage import affine_transform

    spacing = np.asarray(step[::-1])
    shape = tuple((np.floor((np.asarray(data.shape) - 1) * spacing + 1e-6).astype(int) + 1).tolist())
    result = affine_transform(data, np.diag(1 / spacing), output_shape=shape,
                              order=1, mode="nearest", prefilter=False)
    return result.astype(np.float32), origin, (1.0, 1.0, 1.0)


def write_mrc(path: Path, data, origin, step):
    with mrcfile.new(path, overwrite=True) as mrc:
        mrc.set_data(np.ascontiguousarray(data, dtype=np.float32))
        mrc.voxel_size = step
        mrc.header.origin = origin
        mrc.update_header_stats()


def flip_map_copy(source: Path, destination: Path, axis: str = "z"):
    if source.resolve() == destination.resolve():
        raise ValueError("A handedness flip must not overwrite the original map.")
    if axis not in ("x", "y", "z"):
        raise ValueError("Flip axis must be x, y, or z.")
    data, origin, step = read_mrc(source)
    physical_axis = ("x", "y", "z").index(axis)
    array_axis = 2 - physical_axis
    write_mrc(destination, np.flip(data, array_axis), origin, step)
    transform = np.eye(4)
    transform[physical_axis, physical_axis] = -1
    transform[physical_axis, 3] = 2 * origin[physical_axis] + (data.shape[array_axis] - 1) * step[physical_axis]
    return {"map_path": str(destination.resolve()), "source_map": str(source.resolve()),
            "axis": axis, "reflection_matrix": transform.tolist(),
            "note": "Map-only reflection about its physical center; model coordinates are unchanged."}
