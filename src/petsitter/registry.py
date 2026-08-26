"""Community trick index. Resolve, fetch, and install shared tricks.

There is no registry server.  The index is a static ``index.json`` built
hourly by a GitHub Action in day50-dev/tricks, which crawls repos carrying
the ``petsitter-trick`` topic.  This module is the whole client: it fetches
that file, caches it, and downloads tricks to

    <config_dir>/tricks/<owner>/<slug>/<version>.py

A trickset refers to an installed trick as ``pkg:<owner>/<slug>@<version>``
so tricksets stay portable between machines. See ``src/loader.py``.

Point somewhere else with ``PET_REGISTRY_INDEX`` (an https:// or file:// URL)
or a ``registry_index`` key in config.json.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_INDEX = "https://day50-dev.github.io/tricks/index.json"
CACHE_TTL = 3600  # the index is rebuilt hourly; no point asking more often
FAIL_TTL = 300    # after a failed fetch, don't retry for this long

# When the index is unreachable (no network, or it simply isn't published yet)
# every dashboard poll would otherwise spend a fresh 20s timeout finding that
# out again. Remember the failure and fail fast instead.
_last_failure: float = 0.0
_last_failure_reason: str = ""
PKG_RE = re.compile(r"^pkg:(?P<owner>[\w.-]+)/(?P<slug>[\w.-]+?)(?:@(?P<version>[\w.\-+]+))?$")
NAME_RE = re.compile(r"^(?P<owner>[\w.-]+)/(?P<slug>[\w.-]+)$")


class RegistryError(Exception):
    """Anything that went wrong talking to or reading the index."""


# ---------------------------------------------------------------------------
# locations
# ---------------------------------------------------------------------------


def index_url(config: dict | None = None) -> str:
    env = os.environ.get("PET_REGISTRY_INDEX")
    if env:
        return env
    if config and config.get("registry_index"):
        return str(config["registry_index"])
    return DEFAULT_INDEX


def installed_root(config_dir: Path) -> Path:
    return Path(config_dir) / "tricks"


def installed_path(config_dir: Path, name: str, version: str) -> Path:
    owner, slug = split_name(name)
    return installed_root(config_dir) / owner / slug / f"{version}.py"


def split_name(name: str) -> tuple[str, str]:
    m = NAME_RE.match(name.strip())
    if not m:
        raise RegistryError(
            f"'{name}' is not a package name. Expected <owner>/<name>, e.g. dana/ollama-ctx"
        )
    return m.group("owner"), m.group("slug")


def slugify_stem(stem: str) -> str:
    """``ollama_ctx`` -> ``ollama-ctx``.  Must match crawl.py's slugify()."""
    s = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    return re.sub(r"-{2,}", "-", s)


def parse_pkg_spec(spec: str) -> tuple[str, str | None] | None:
    """``pkg:owner/slug@1.2.3`` -> ``("owner/slug", "1.2.3")``; else None."""
    m = PKG_RE.match(spec.strip())
    if not m:
        return None
    return f"{m.group('owner')}/{m.group('slug')}", m.group("version")


def pkg_spec(name: str, version: str) -> str:
    return f"pkg:{name}@{version}"


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------


def _cache_path(config_dir: Path) -> Path:
    return Path(config_dir) / "registry-cache.json"


def _fetch(url: str, timeout: int = 20) -> bytes:
    if url.startswith("file://"):
        return Path(urllib.parse.urlparse(url).path).read_bytes()
    if not url.startswith(("http://", "https://")):
        return Path(url).read_bytes()
    req = urllib.request.Request(url, headers={"User-Agent": "petsitter"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        raise RegistryError(f"could not fetch {url}: {e}") from e


def fetch_index(config_dir: Path, config: dict | None = None,
                force: bool = False) -> dict[str, Any]:
    """Return the index, from cache when it's fresh enough.

    A stale cache is better than an error: if the network is down but we have
    an old copy, use it and say nothing.  Only a cold cache can fail.
    """
    global _last_failure, _last_failure_reason
    cache = _cache_path(config_dir)
    if not force and cache.exists():
        age = time.time() - cache.stat().st_mtime
        if age < CACHE_TTL:
            try:
                return json.loads(cache.read_text())
            except (json.JSONDecodeError, OSError):
                pass  # corrupt cache; fall through and refetch

    if not force and _last_failure and (time.time() - _last_failure) < FAIL_TTL:
        raise RegistryError(_last_failure_reason or "the index was unreachable")

    url = index_url(config)
    try:
        raw = _fetch(url)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict) or "tricks" not in data:
            raise RegistryError(f"{url} is not a petsitter index")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(raw)
        _last_failure = 0.0
        _last_failure_reason = ""
        return data
    except (RegistryError, json.JSONDecodeError, UnicodeDecodeError) as e:
        if cache.exists():
            try:
                return json.loads(cache.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        _last_failure = time.time()
        _last_failure_reason = str(e)
        raise RegistryError(str(e)) from e


def search(index: dict, query: str = "", featured_only: bool = False) -> list[dict]:
    q = query.strip().lower()
    out = []
    for e in index.get("tricks", []):
        if featured_only and not e.get("featured"):
            continue
        if q:
            hay = " ".join([
                e.get("name", ""), e.get("brief", ""), e.get("display_name", ""),
                " ".join(e.get("keywords", []) or []), e.get("prompt_keyword", "") or "",
            ]).lower()
            if q not in hay:
                continue
        out.append(e)
    out.sort(key=lambda e: (not e.get("featured"), -int(e.get("stars", 0)), e.get("name", "")))
    return out


def resolve(index: dict, name: str, version: str | None = None) -> dict:
    """Find one entry by name, optionally pinned to a version."""
    name = name.strip()
    matches = [e for e in index.get("tricks", []) if e.get("name") == name]
    if not matches:
        near = [e["name"] for e in index.get("tricks", [])
                if name.split("/")[-1] in e.get("name", "")][:5]
        hint = f"  Did you mean: {', '.join(near)}?" if near else ""
        raise RegistryError(f"'{name}' is not in the index.{hint}")
    entry = matches[0]
    if version and entry.get("version") != version:
        raise RegistryError(
            f"the index has {name}@{entry.get('version')}, not {version}. "
            f"Older versions aren't re-indexed; if you have it installed it still works."
        )
    return entry


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def install(entry: dict, config_dir: Path, force: bool = False) -> tuple[Path, bool]:
    """Download and verify a trick.  Returns ``(path, newly_written)``.

    The checksum is not optional.  A mismatch means the bytes behind a pinned
    commit URL are not what the crawler saw, which should be impossible, so we
    refuse rather than guess.
    """
    name = entry["name"]
    version = entry["version"]
    dest = installed_path(config_dir, name, version)

    if dest.exists() and not force:
        if _sha256_file(dest) == entry.get("sha256"):
            return dest, False
        # Same version, different bytes. The disk copy was edited or is stale.
        if not force:
            return dest, False

    blob = _fetch(entry["url"], timeout=30)
    got = hashlib.sha256(blob).hexdigest()
    want = entry.get("sha256", "")
    if want and got != want:
        raise RegistryError(
            f"checksum mismatch for {name}@{version}\n"
            f"  expected {want}\n  got      {got}\n"
            f"Refusing to install. Report this at {entry.get('repo', '(unknown repo)')}."
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".py.part")
    tmp.write_bytes(blob)
    tmp.replace(dest)
    _write_receipt(dest, entry)
    return dest, True


def _write_receipt(dest: Path, entry: dict) -> None:
    """Drop the index entry next to the file so we know where it came from."""
    try:
        meta = dict(entry)
        meta["installed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        dest.with_suffix(".json").write_text(json.dumps(meta, indent=2) + "\n")
    except OSError:
        pass  # the trick itself landed; the receipt is a nicety


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def list_installed(config_dir: Path) -> list[dict]:
    """Everything under <config_dir>/tricks, newest version per package first."""
    root = installed_root(config_dir)
    if not root.is_dir():
        return []
    out = []
    for py in sorted(root.glob("*/*/*.py")):
        version = py.stem
        slug = py.parent.name
        owner = py.parent.parent.name
        meta = {}
        receipt = py.with_suffix(".json")
        if receipt.exists():
            try:
                meta = json.loads(receipt.read_text())
            except (json.JSONDecodeError, OSError):
                meta = {}
        out.append({
            "name": f"{owner}/{slug}",
            "version": version,
            "path": str(py),
            "spec": pkg_spec(f"{owner}/{slug}", version),
            "brief": meta.get("brief", ""),
            "display_name": meta.get("display_name", slug),
            "repo": meta.get("repo", ""),
        })
    return out


def uninstall(config_dir: Path, name: str, version: str | None = None) -> list[Path]:
    """Remove installed copies.  Returns what was deleted."""
    owner, slug = split_name(name)
    d = installed_root(config_dir) / owner / slug
    if not d.is_dir():
        return []
    removed = []
    for py in sorted(d.glob("*.py")):
        if version and py.stem != version:
            continue
        receipt = py.with_suffix(".json")
        py.unlink(missing_ok=True)
        receipt.unlink(missing_ok=True)
        removed.append(py)
    # Tidy up empty directories, but never the shared root.
    for p in (d, d.parent):
        try:
            if p.is_dir() and not any(p.iterdir()):
                p.rmdir()
        except OSError:
            pass
    return removed
