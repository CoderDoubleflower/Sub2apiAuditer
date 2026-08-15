"""Dynamic loading of trick modules."""

import importlib.util
import os
import sys
from pathlib import Path
from typing import Type

from src.trick import Trick

# Set by the server / CLI once the -c/--config option has been resolved, so
# ``pkg:`` specs land in the same config area everything else uses.
_config_dir: Path | None = None


def set_config_dir(path: str | Path) -> None:
    """Tell the loader where installed tricks live."""
    global _config_dir
    _config_dir = Path(path)


def config_dir() -> Path:
    if _config_dir is not None:
        return _config_dir
    return Path(os.environ.get("PET_CONFIG_DIR", str(Path.home() / ".config" / "petsitter")))


def resolve_path(path: str) -> Path:
    """Turn a trickset entry into a real file on disk.

    Three forms are accepted:

    * ``pkg:<owner>/<slug>@<version>`` — a trick installed from the community
      index, resolved under ``<config_dir>/tricks/``.  Portable between
      machines, which plain paths are not.
    * an absolute or relative path to a ``.py``
    * a path relative to the repo root (how the built-ins are referenced)
    """
    from src import registry  # local import: keeps registry optional at import time

    spec = registry.parse_pkg_spec(path)
    if spec is not None:
        name, version = spec
        if version is None:
            # No pin — take the newest installed version of that package.
            candidates = [e for e in registry.list_installed(config_dir())
                          if e["name"] == name]
            if not candidates:
                raise FileNotFoundError(
                    f"{path} is not installed. Run: pet install {name}"
                )
            version = _newest(c["version"] for c in candidates)
        resolved = registry.installed_path(config_dir(), name, version)
        if not resolved.exists():
            raise FileNotFoundError(
                f"{path} is not installed. Run: pet install {name}"
            )
        return resolved

    trick_path = Path(path).expanduser()
    if trick_path.exists():
        return trick_path
    alt = Path(__file__).parent.parent / path
    if alt.exists():
        return alt
    raise FileNotFoundError(f"Trick file not found: {path}")


def _newest(versions) -> str:
    def key(v: str):
        try:
            return tuple(int(x) for x in v.split("-")[0].split("."))
        except ValueError:
            return (0,)
    return max(versions, key=key)


def load_trick_from_path(path: str) -> Type[Trick]:
    """Load a Trick class from a trick reference.

    Args:
        path: A ``pkg:owner/slug@version`` spec or a file path
            (e.g. ``tricks/tools.py``).

    Returns:
        The Trick subclass defined in the module.

    Raises:
        FileNotFoundError: If the path doesn't exist or the package isn't installed.
        ImportError: If no Trick subclass is found.
    """
    trick_path = resolve_path(path)

    # Installed packages are namespaced by owner so two authors can both ship
    # a ``json_mode.py`` without clobbering each other in sys.modules.
    module_name = trick_path.stem
    pkg = None
    from src import registry
    spec_parts = registry.parse_pkg_spec(path)
    if spec_parts is not None:
        owner, slug = spec_parts[0].split("/")
        module_name = f"petsitter_pkg_{owner}_{slug}".replace("-", "_")
        pkg = spec_parts[0]

    spec = importlib.util.spec_from_file_location(module_name, trick_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load module: {path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    # Find the Trick subclass (not the base Trick itself)
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if (
            isinstance(attr, type)
            and issubclass(attr, Trick)
            and attr is not Trick
        ):
            if pkg:
                # Remember where it came from so the dashboard can show it.
                attr.__package_name__ = pkg
            return attr

    raise ImportError(f"No Trick subclass found in {path}")


def load_tricks(paths: list[str]) -> list[Trick]:
    """Load multiple tricks from file paths or pkg: specs.

    Args:
        paths: List of trick references.

    Returns:
        List of instantiated Trick objects.
    """
    tricks = []
    for path in paths:
        trick_class = load_trick_from_path(path)
        tricks.append(trick_class())
    return tricks
