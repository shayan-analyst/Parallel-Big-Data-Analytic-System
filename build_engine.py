"""Build the native Cython/OpenMP extension for the current operating system."""
from __future__ import annotations
import sys
from setuptools import Extension, setup
from Cython.Build import cythonize
import numpy

if sys.platform == "win32":
    compile_args = ["/O2", "/openmp"]
    link_args = []
elif sys.platform.startswith("linux"):
    compile_args = ["-O3", "-fopenmp"]
    link_args = ["-fopenmp"]
else:
    raise SystemExit(
        "Automatic OpenMP build is configured for Windows (MSVC) and Linux (GCC/Clang). "
        "macOS requires a separately configured libomp toolchain."
    )

extension = Extension(
    name="app.engine.openmp_kernels",
    sources=["app/engine/openmp_kernels.pyx"],
    include_dirs=[numpy.get_include()],
    extra_compile_args=compile_args,
    extra_link_args=link_args,
)

setup(
    name="parallel-big-data-analytics-engine",
    ext_modules=cythonize(
        [extension],
        force=True,  # Never reuse generated C from an older .pyx revision.
        compiler_directives={"language_level": 3, "boundscheck": False, "wraparound": False},
    ),
)
