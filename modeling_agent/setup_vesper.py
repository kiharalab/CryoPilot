#!/usr/bin/env python3
"""Install a pinned VESPER build into the agent's ignored .deps directory."""
import platform
import shutil
import subprocess
from pathlib import Path

from vesper_align import VESPER_REVISION


def main():
    root = Path(__file__).resolve().parent / ".deps" / "vesper"
    root.parent.mkdir(parents=True, exist_ok=True)
    if not root.exists():
        subprocess.run(["git", "clone", "--filter=blob:none", "--sparse", "https://github.com/kiharalab/VESPER.git", str(root)], check=True)
        subprocess.run(["git", "sparse-checkout", "set", "VESPER_code"], cwd=root, check=True)
    subprocess.run(["git", "fetch", "--depth", "1", "origin", VESPER_REVISION], cwd=root, check=True)
    subprocess.run(["git", "checkout", "--detach", VESPER_REVISION], cwd=root, check=True)
    flags = ["-O3", "-std=gnu99"]
    if platform.system() == "Darwin":
        brew = shutil.which("brew")
        if not brew:
            raise RuntimeError("Install Homebrew FFTW and libomp first, or configure VESPER_EXECUTABLE.")
        prefixes = {name: subprocess.check_output([brew, "--prefix", name], text=True).strip() for name in ("fftw", "libomp")}
        compiler = "clang"
        flags += ["-Xpreprocessor", "-fopenmp", "-Wno-error=implicit-function-declaration", "-Wno-error=incompatible-pointer-types"]
        for prefix in prefixes.values():
            flags += [f"-I{prefix}/include", f"-L{prefix}/lib", f"-Wl,-rpath,{prefix}/lib"]
        flags += ["-lomp"]
    else:
        compiler = "gcc"
        flags += ["-fopenmp"]
    sources = [str(root / "VESPER_code" / name) for name in ("main.c", "chkcmdline.c", "mrc.c", "readpdb.c", "mrcfft.c")]
    subprocess.run([compiler, *flags, *sources, "-lfftw3_threads", "-lfftw3f", "-lm", "-o", str(root / "VESPER")], check=True)
    print(f"Installed VESPER {VESPER_REVISION} at {root / 'VESPER'}")


if __name__ == "__main__":
    main()
