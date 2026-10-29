from __future__ import annotations

"""Portable PyQGIS runtime discovery for Grid Reprojector.

The application is designed to run with the Python interpreter supplied by, or
compatible with, an installed QGIS distribution.  No installation directory is
hard-coded.  Runtime discovery uses, in order:

1. ``--qgis-prefix`` command-line option;
2. ``GRIDREPROJECTOR_QGIS_PREFIX`` environment variable;
3. ``QGIS_PREFIX_PATH`` or ``QGIS_PREFIX`` environment variables;
4. an already importable ``qgis`` Python package;
5. ``OSGEO4W_ROOT`` and ``OSGEO4W_HOME`` environment variables;
6. QGIS executables discoverable on ``PATH``.

The prefix must identify a recognizable QGIS application installation.
"""

import importlib.util
import os
import shutil
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Union


_DLL_HANDLES: List[object] = []
_CONFIGURED_PREFIX: Optional[Path] = None


def _normalise_path(value: Union[str, os.PathLike]) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value)))).resolve()


def _is_qgis_prefix(path: Path) -> bool:
    if not path.is_dir():
        return False
    return any(
        candidate.is_dir()
        for candidate in (
            path / "python" / "qgis",
            path / "share" / "qgis" / "python" / "qgis",
            path / "Resources" / "python" / "qgis",
            path.parent / "Resources" / "python" / "qgis",
        )
    )


def _first_valid_prefix(candidates: Iterable[Path]) -> Optional[Path]:
    seen = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)
        if _is_qgis_prefix(resolved):
            return resolved
    return None


def _prefixes_under_root(root: Path) -> List[Path]:
    candidates: List[Path] = []
    if _is_qgis_prefix(root):
        candidates.append(root)

    apps = root / "apps"
    if apps.is_dir():
        preferred = [apps / "qgis", apps / "qgis-ltr"]
        candidates.extend(preferred)
        try:
            candidates.extend(sorted(apps.glob("qgis*")))
        except OSError:
            pass
    return candidates


def _prefix_from_importable_qgis() -> Optional[Path]:
    try:
        spec = importlib.util.find_spec("qgis")
    except (ImportError, AttributeError, ValueError):
        return None
    if spec is None:
        return None

    # When PyQGIS is already importable, QGIS itself is the best authority for
    # its compiled installation prefix. This covers system Linux and macOS
    # layouts that do not resemble OSGeo4W.
    try:
        from qgis.core import QgsApplication  # type: ignore

        compiled_prefix = QgsApplication.prefixPath()
        if compiled_prefix:
            candidate = _normalise_path(compiled_prefix)
            if candidate.is_dir():
                return candidate
    except Exception:
        pass

    locations: List[Path] = []
    if spec.origin and spec.origin not in {"built-in", "frozen"}:
        locations.append(Path(spec.origin))
    if spec.submodule_search_locations:
        locations.extend(Path(item) for item in spec.submodule_search_locations)

    for location in locations:
        current = location if location.is_dir() else location.parent
        for ancestor in [current, *current.parents]:
            if _is_qgis_prefix(ancestor):
                return ancestor
    return None


def _prefixes_from_executable(executable: Path) -> List[Path]:
    candidates: List[Path] = []
    current = executable.parent
    for ancestor in [current, *list(current.parents)[:5]]:
        candidates.extend(_prefixes_under_root(ancestor))
        if ancestor.name.lower() == "bin":
            candidates.extend(_prefixes_under_root(ancestor.parent))
    return candidates


def _command_line_prefix(argv: List[str]) -> Optional[str]:
    """Extract and remove --qgis-prefix so QGIS itself never receives it."""
    for index, argument in enumerate(list(argv)):
        if argument == "--qgis-prefix":
            if index + 1 >= len(argv):
                raise RuntimeError("--qgis-prefix requires a directory argument.")
            value = argv[index + 1]
            del argv[index : index + 2]
            return value
        if argument.startswith("--qgis-prefix="):
            value = argument.split("=", 1)[1]
            del argv[index]
            if not value:
                raise RuntimeError("--qgis-prefix requires a directory argument.")
            return value
    return None


def discover_qgis_prefix(explicit_prefix: Optional[str] = None) -> Path:
    """Locate the QGIS application prefix without assuming an installation path."""
    candidates: List[Path] = []

    if explicit_prefix:
        explicit_path = _normalise_path(explicit_prefix)
        candidates.append(explicit_path)
        candidates.extend(_prefixes_under_root(explicit_path))

    for variable in (
        "GRIDREPROJECTOR_QGIS_PREFIX",
        "QGIS_PREFIX_PATH",
        "QGIS_PREFIX",
    ):
        value = os.environ.get(variable, "").strip()
        if value:
            environment_path = _normalise_path(value)
            candidates.append(environment_path)
            candidates.extend(_prefixes_under_root(environment_path))

    importable = _prefix_from_importable_qgis()
    if importable is not None:
        candidates.append(importable)

    for variable in ("OSGEO4W_ROOT", "OSGEO4W_HOME"):
        value = os.environ.get(variable, "").strip()
        if value:
            candidates.extend(_prefixes_under_root(_normalise_path(value)))

    for executable_name in (
        "qgis-bin",
        "qgis-bin.exe",
        "qgis_process",
        "qgis_process.exe",
        "qgis",
        "qgis.exe",
    ):
        executable = shutil.which(executable_name)
        if executable:
            candidates.extend(_prefixes_from_executable(_normalise_path(executable)))

    prefix = _first_valid_prefix(candidates)
    if prefix is not None:
        return prefix

    if explicit_prefix:
        raise RuntimeError(
            "The supplied QGIS prefix is not valid. It must point to a recognizable "
            "QGIS application prefix or installation root. Supplied value: " + explicit_prefix
        )

    raise RuntimeError(
        "A usable PyQGIS installation could not be discovered. Run Grid Reprojector "
        "from the Python environment supplied by QGIS/OSGeo4W, set the environment "
        "variable GRIDREPROJECTOR_QGIS_PREFIX to the QGIS application prefix, or use "
        "'python main.py --qgis-prefix <QGIS_PREFIX>'. The prefix is the directory "
        "that contains the QGIS application resources. The Python interpreter must be ABI-compatible "
        "with the installed QGIS build."
    )


def _discover_qt_root(installation_root: Path, prefix: Path) -> Optional[Path]:
    candidates = [
        installation_root / "apps" / "Qt6",
        installation_root / "apps" / "Qt5",
        prefix.parent / "Qt6",
        prefix.parent / "Qt5",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _discover_grass_root(installation_root: Path) -> Optional[Path]:
    candidates: List[Path] = []
    grass_parent = installation_root / "apps" / "grass"
    if grass_parent.is_dir():
        try:
            candidates.extend(sorted(grass_parent.glob("grass*"), reverse=True))
        except OSError:
            pass
    apps = installation_root / "apps"
    if apps.is_dir():
        try:
            candidates.extend(sorted(apps.glob("grass*"), reverse=True))
        except OSError:
            pass
    return next((item for item in candidates if item.is_dir()), None)


def _prepend_environment_path(variable: str, paths: Iterable[Path]) -> None:
    entries: List[str] = []
    existing = os.environ.get(variable, "")
    existing_parts = [part for part in existing.split(os.pathsep) if part]
    seen = {os.path.normcase(part) for part in existing_parts}

    for path in paths:
        if not path.is_dir():
            continue
        value = str(path)
        key = os.path.normcase(value)
        if key not in seen:
            entries.append(value)
            seen.add(key)
    entries.extend(existing_parts)
    if entries:
        os.environ[variable] = os.pathsep.join(entries)


def configure_qgis_environment(explicit_prefix: Optional[str] = None) -> str:
    """Configure paths required to import and initialize PyQGIS.

    Calling this function more than once is safe.  It does not install QGIS and
    cannot make an incompatible Python interpreter compatible with QGIS.
    """
    global _CONFIGURED_PREFIX

    if _CONFIGURED_PREFIX is not None:
        if explicit_prefix:
            requested = discover_qgis_prefix(explicit_prefix)
            if requested != _CONFIGURED_PREFIX:
                raise RuntimeError(
                    "PyQGIS is already configured from a different prefix: "
                    f"{_CONFIGURED_PREFIX}"
                )
        return str(_CONFIGURED_PREFIX)

    prefix = discover_qgis_prefix(explicit_prefix)
    installation_root = prefix.parent.parent if prefix.parent.name.lower() == "apps" else prefix
    qt_root = _discover_qt_root(installation_root, prefix)
    grass_root = _discover_grass_root(installation_root)

    python_roots = [
        prefix / "python",
        prefix / "share" / "qgis" / "python",
        prefix / "Resources" / "python",
        prefix.parent / "Resources" / "python",
    ]
    python_paths: List[Path] = []
    for python_root in python_roots:
        python_paths.extend([python_root, python_root / "plugins"])
    for python_path in reversed(python_paths):
        if python_path.is_dir():
            value = str(python_path)
            if value not in sys.path:
                sys.path.insert(0, value)

    path_entries = [prefix / "bin", installation_root / "bin"]
    if qt_root is not None:
        path_entries.append(qt_root / "bin")
    if grass_root is not None:
        path_entries.extend([grass_root / "bin", grass_root / "lib"])
    _prepend_environment_path("PATH", path_entries)

    qt_plugin_entries: List[Path] = [prefix / "qtplugins", prefix / "plugins"]
    if qt_root is not None:
        qt_plugin_entries.insert(0, qt_root / "plugins")
        platform_plugins = qt_root / "plugins" / "platforms"
        if platform_plugins.is_dir():
            os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", str(platform_plugins))
    _prepend_environment_path("QT_PLUGIN_PATH", qt_plugin_entries)

    os.environ["QGIS_PREFIX"] = str(prefix)
    os.environ["QGIS_PREFIX_PATH"] = str(prefix)
    os.environ.setdefault("MAX_CACHE_LAYERS", "100000")

    gdal_data = installation_root / "share" / "gdal"
    if gdal_data.is_dir():
        os.environ.setdefault("GDAL_DATA", str(gdal_data))
    proj_data = installation_root / "share" / "proj"
    if proj_data.is_dir():
        os.environ.setdefault("PROJ_DATA", str(proj_data))
        os.environ.setdefault("PROJ_LIB", str(proj_data))
    if grass_root is not None:
        os.environ.setdefault("GISBASE", str(grass_root))

    if os.name == "nt" and hasattr(os, "add_dll_directory"):
        for entry in path_entries:
            if not entry.is_dir():
                continue
            try:
                _DLL_HANDLES.append(os.add_dll_directory(str(entry)))
            except OSError:
                pass

    _CONFIGURED_PREFIX = prefix
    return str(prefix)


def configure_from_argv(argv: Optional[List[str]] = None) -> str:
    """Configure PyQGIS, accepting an optional --qgis-prefix command-line option."""
    arguments = sys.argv if argv is None else argv
    explicit = _command_line_prefix(arguments)
    return configure_qgis_environment(explicit)
