"""pet — a command-line companion to the petsitter dashboard.

Everything petsitter persists lives in JSON files under ``~/.config/petsitter/``
(``config.json`` for global model settings, plus one ``tricksets/<name>.json``
per trickset).  ``pet`` reads and writes those same files directly, so the CLI
and the web dashboard always see the same state — no server needs to be running.

The commands mirror the dashboard tabs:

    pet ls                          # list tricksets (look at tricksets)
    pet show <trickset>             # full detail for one trickset
    pet tricks                      # list available trick modules
    pet new <name>                  # create a trickset
    pet delete <name>               # delete a trickset
    pet rename <old> <new>          # rename a trickset
    pet add <trickset> <trick>      # add a trick to a trickset
    pet rm <trickset> <trick>       # remove a trick from a trickset
    pet enable <trickset> <trick>   # activate a trick
    pet disable <trickset> <trick>  # deactivate a trick
    pet reorder <trickset> <trick> <index>
    pet keyword <trickset> <trick> [kw]
    pet config <trickset> <trick> key=value...
    pet param <trickset> key=value...
    pet filter <trickset> [--x-title G] [--model G]
    pet install <trick>             # run a trick's install() hook
    pet uninstall <trick>           # run a trick's uninstall() hook
    pet lifecycle <trick> <hook>
    pet models [trickset]           # show model config
    pet model <key> <url> [opts]    # set a model entry (--trickset to scope)
    pet logfile <trickset> [path]
    pet loglevel <trickset> [level]
    pet examples [--force]          # install the example tricksets
    pet agents [list|register|unregister]
"""

import json
import os
import tomllib
from pathlib import Path
from typing import Any

import click

from src.loader import load_trick_from_path
from src.trick import Trick
from src.trickset import SCHEMA, Trickset

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILTIN_TRICKS_DIR = REPO_ROOT / "tricks"

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


# --------------------------------------------------------------------------
# file access helpers — everything below works directly on the JSON files
# --------------------------------------------------------------------------


def config_dir() -> Path:
    return Path(os.environ.get("PET_CONFIG_DIR", str(Path.home() / ".config" / "petsitter")))


def config_path() -> Path:
    return config_dir() / "config.json"


def tricksets_dir() -> Path:
    return config_dir() / "tricksets"


def load_config() -> dict:
    p = config_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_config(cfg: dict) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2) + "\n")


def _ts_path(name: str) -> Path:
    return tricksets_dir() / f"{name}.json"


def _save_ts(ts: Trickset) -> None:
    if not ts.file_path:
        ts.file_path = str(_ts_path(ts.name))
    ts.save()


def _load_ts(name: str) -> Trickset:
    """Load a trickset from disk; create ``_default`` on first use."""
    p = _ts_path(name)
    if p.exists():
        return Trickset.load_from_file(str(p))
    if name == "_default":
        ts = Trickset("_default", SCHEMA, {"X-Title": "*", "Model": "*"}, [], file_path=str(p))
        ts.save()
        return ts
    raise click.ClickException(
        f"trickset '{name}' not found in {tricksets_dir()} (create it with 'pet new {name}')"
    )


def _list_ts_files() -> list[Path]:
    d = tricksets_dir()
    if not d.exists():
        return []
    return sorted(d.glob("*.json"))


def _tricks_dir() -> Path | None:
    cwd = Path("tricks")
    if cwd.is_dir():
        return cwd
    if BUILTIN_TRICKS_DIR.is_dir():
        return BUILTIN_TRICKS_DIR
    return None


def _introspect(path: Path) -> dict:
    """Extract display_name, brief, keywords, prompt_keyword from a trick module."""
    info = {
        "path": str(path),
        "display_name": None,
        "brief": None,
        "keywords": [],
        "prompt_keyword": "",
        "required_models": ["default"],
        "config_fields": [],
        "mtime": path.stat().st_mtime_ns,
    }
    try:
        import importlib.util as _util
        spec = _util.spec_from_file_location(path.stem, str(path))
        if spec and spec.loader:
            mod = _util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            for name in dir(mod):
                obj = getattr(mod, name)
                if isinstance(obj, type) and issubclass(obj, Trick) and obj is not Trick:
                    info["display_name"] = getattr(obj, "__display_name__", None) or name
                    info["brief"] = getattr(obj, "__brief__", "")
                    info["keywords"] = list(getattr(obj, "keywords", []) or [])
                    info["prompt_keyword"] = getattr(obj, "prompt_keyword", "") or ""
                    info["required_models"] = list(getattr(obj, "required_models", ["default"]) or ["default"])
                    info["config_fields"] = list(getattr(obj, "config_fields", []) or [])
                    break
    except Exception:
        pass
    return info


def _available_tricks() -> list[dict]:
    d = _tricks_dir()
    if not d:
        return []
    result = []
    for f in sorted(d.glob("*.py")):
        if f.name == "__init__.py":
            continue
        result.append(_introspect(f))
    return result


def _resolve_trick_spec(spec: str) -> str:
    """Resolve a trick name/path to a loadable ``.py`` path.

    ``json_mode`` -> ``tricks/json_mode.py``; paths pass through unchanged.
    """
    s = spec.strip()
    if not s:
        raise click.UsageError("trick path is empty")
    if s.endswith(".py"):
        return s
    for base in ("tricks", str(BUILTIN_TRICKS_DIR)):
        cand = f"{base}/{s}.py"
        if Path(cand).exists():
            return cand
    return s


def _trick_exists(path: str) -> bool:
    if Path(path).exists():
        return True
    if (REPO_ROOT / path).exists():
        return True
    return False


def _find_trick_index(ts: Trickset, spec: str) -> int | None:
    """Locate a trick by file path, class name, filename, or display name."""
    spec_l = spec.lower()
    resolved = _resolve_trick_spec(spec).lower()
    for i, path in enumerate(ts.trick_paths):
        names = {path.lower(), Path(path).stem.lower()}
        if i < len(ts.tricks) and ts.tricks[i] is not None:
            t = ts.tricks[i]
            names.add(type(t).__name__.lower())
            dn = getattr(t, "__display_name__", "") or ""
            if dn:
                names.add(dn.lower())
        if spec_l in names or resolved in names:
            return i
    return None


def _trick_label(ts: Trickset, i: int) -> str:
    if i >= len(ts.tricks) or ts.tricks[i] is None:
        return Path(ts.trick_paths[i]).stem
    t = ts.tricks[i]
    return getattr(t, "__display_name__", "") or type(t).__name__


def _parse_value(val: str) -> Any:
    """Parse a CLI value: JSON literals (3, true, null) else a string."""
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return val


def _model_entry_display(entry: dict, indent: str = "    ") -> str:
    url = entry.get("url", "")
    model = entry.get("model", "")
    key = entry.get("key", "")
    parts = []
    if url:
        parts.append(f"url={url}")
    if model is not False and model != "":
        parts.append(f"model={model}")
    elif model is False:
        parts.append("model=passthrough")
    if key is not False and key != "":
        parts.append("key=****")
    elif key is False:
        parts.append("key=passthrough")
    return indent + "  ".join(parts) if parts else indent + "(empty)"


def _version() -> str:
    try:
        with open(REPO_ROOT / "pyproject.toml", "rb") as f:
            return tomllib.load(f).get("project", {}).get("version", "0.0.0")
    except Exception:
        return "0.0.0"


# --------------------------------------------------------------------------
# output helpers
# --------------------------------------------------------------------------


def _print_trickset_list(ts: Trickset, json_out: bool = False) -> None:
    if json_out:
        click.echo(json.dumps(ts.to_dict(), indent=2))
        return
    filters = "  ".join(f"{k}={v}" for k, v in ts.filters.items())
    click.echo(f"{ts.name}  ({filters})")
    if not ts.trick_paths:
        click.echo("    (no tricks)")
        return
    for i, path in enumerate(ts.trick_paths):
        enabled = i < len(ts.trick_enabled) and ts.trick_enabled[i]
        mark = "on " if enabled else "off"
        label = _trick_label(ts, i)
        kw = ts.trick_keywords[i] if i < len(ts.trick_keywords) and ts.trick_keywords[i] else None
        suffix = f"  keyword={kw}" if kw else ""
        click.echo(f"  {mark:<4} {label:<24} {path}{suffix}")


def _print_trickset_detail(ts: Trickset, json_out: bool = False) -> None:
    if json_out:
        click.echo(json.dumps(ts.to_dict(), indent=2))
        return
    click.echo(f"name:     {ts.name}")
    click.echo(f"schema:   {ts.schema}")
    click.echo(f"file:     {ts.file_path or '(not persisted)'}")
    click.echo(f"filters:  {'  '.join(f'{k}={v}' for k, v in ts.filters.items())}")
    click.echo(f"logfile:  {ts.logfile}")
    click.echo(f"loglevel: {ts.loglevel}")
    if ts.parameters:
        click.echo(f"parameters: {json.dumps(ts.parameters)}")
    else:
        click.echo("parameters: {}")
    if ts.models:
        click.echo("models:")
        for k, entry in sorted(ts.models.items()):
            click.echo(f"  {k}")
            click.echo(_model_entry_display(entry if isinstance(entry, dict) else {}))
    else:
        click.echo("models: {}")
    click.echo(f"tricks ({len(ts.trick_paths)}):")
    for i, path in enumerate(ts.trick_paths):
        enabled = i < len(ts.trick_enabled) and ts.trick_enabled[i]
        mark = "on " if enabled else "off"
        label = _trick_label(ts, i)
        tid = ts.trick_ids[i] if i < len(ts.trick_ids) else "?"
        kw = ts.trick_keywords[i] if i < len(ts.trick_keywords) and ts.trick_keywords[i] else None
        kw_s = f"  keyword={kw}" if kw else ""
        cfg = ts.trick_configs.get(tid)
        cfg_s = f"  config={json.dumps(cfg)}" if cfg else ""
        click.echo(f"  {mark:<4} {label:<24} {path}{kw_s}{cfg_s}")


def _print_available_tricks(tricks: list[dict], json_out: bool = False) -> None:
    if json_out:
        click.echo(json.dumps(tricks, indent=2))
        return
    if not tricks:
        click.echo("No tricks found.")
        return
    rows = []
    for t in tricks:
        name = t["display_name"] or Path(t["path"]).stem
        kw = ",".join(t["keywords"])
        if t["prompt_keyword"]:
            kw = (kw + "," if kw else "") + f"({t['prompt_keyword']})"
        rows.append((name, t["path"], t["brief"] or "", kw))
    name_w = max(len(r[0]) for r in rows) + 2
    path_w = max(len(r[1]) for r in rows) + 2
    for name, path, brief, kw in rows:
        line = f"{name:<{name_w}}{path:<{path_w}}{brief}"
        if kw:
            line += f"  [{kw}]"
        click.echo(line)


def _print_models(global_cfg: dict, scoped: dict, json_out: bool = False) -> None:
    keys = sorted(set(global_cfg.keys()) | set(scoped.keys()))
    if json_out:
        click.echo(json.dumps({
            "global": global_cfg,
            "scoped": scoped,
            "keys": keys,
        }, indent=2))
        return
    if not keys:
        click.echo("No models configured.")
        return
    for k in keys:
        g = global_cfg.get(k, {}) if isinstance(global_cfg.get(k), dict) else {}
        s = scoped.get(k, {}) if isinstance(scoped.get(k), dict) else {}
        entry = s if s else g
        scoped_mark = "  (scoped)" if s else ""
        click.echo(f"{k}{scoped_mark}")
        click.echo(_model_entry_display(entry))


# --------------------------------------------------------------------------
# lifecycle helpers
# --------------------------------------------------------------------------


def _run_lifecycle(path: str, func: str) -> None:
    if not _trick_exists(path):
        raise click.ClickException(f"Trick file not found: {path}")
    try:
        cls = load_trick_from_path(path)
    except Exception as e:
        raise click.ClickException(f"Could not load trick {path}: {e}")
    trick = cls()
    method = getattr(trick, func, None)
    if method is None:
        raise click.ClickException(f"Trick {path} has no hook '{func}'")
    method()
    click.echo(f"Ran {func}() on {path}")


def _install_agent_manager():
    agents_dir = REPO_ROOT / "agents"
    from src.agent_manager import AgentManager
    return AgentManager(config_dir=str(config_dir()), agents_dir=str(agents_dir))


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------


@click.group()
@click.version_option(_version(), prog_name="pet")
def cli() -> None:
    """Manage petsitter tricksets, tricks, and models from the command line.
    """


@cli.command("ls")
@click.argument("name", required=False)
@click.option("--json", "json_out", is_flag=True, help="Emit raw JSON")
def ls_cmd(name: str | None, json_out: bool) -> None:
    """List tricksets (and their tricks). Give a name to inspect one."""
    if name:
        _print_trickset_detail(_load_ts(name), json_out=json_out)
        return
    files = _list_ts_files()
    if not files:
        click.echo("No tricksets yet. Create one with 'pet new <name>' or run 'pet examples'.")
        return
    if json_out:
        click.echo(json.dumps([json.loads(f.read_text()) for f in files], indent=2))
        return
    for f in files:
        ts = Trickset.load_from_file(str(f))
        _print_trickset_list(ts)
        click.echo("")


@cli.command("show")
@click.argument("name")
@click.option("--json", "json_out", is_flag=True, help="Emit raw JSON")
def show_cmd(name: str, json_out: bool) -> None:
    """Show full detail for a trickset."""
    _print_trickset_detail(_load_ts(name), json_out=json_out)


@cli.command("tricks")
@click.option("--json", "json_out", is_flag=True, help="Emit raw JSON")
def tricks_cmd(json_out: bool) -> None:
    """List available trick modules."""
    _print_available_tricks(_available_tricks(), json_out=json_out)


@cli.command("new")
@click.argument("name")
@click.option("--x-title", default="*", show_default=True, help="X-Title filter glob")
@click.option("--model", "model_filter", default="*", show_default=True, help="Model filter glob")
@click.option("-t", "--trick", "tricks", multiple=True, help="Trick path/name to seed (repeatable)")
def new_cmd(name: str, x_title: str, model_filter: str, tricks: tuple[str, ...]) -> None:
    """Create a new trickset."""
    if _ts_path(name).exists():
        raise click.ClickException(f"Trickset '{name}' already exists")
    ts = Trickset(name, SCHEMA, {"X-Title": x_title, "Model": model_filter}, [], file_path=str(_ts_path(name)))
    for t in tricks:
        resolved = _resolve_trick_spec(t)
        if not _trick_exists(resolved):
            raise click.ClickException(f"Trick file not found: {t}")
        ts.add_trick(resolved)
    _save_ts(ts)
    click.echo(f"Created trickset '{name}' -> {ts.file_path}")


@cli.command("delete")
@click.argument("name")
def delete_cmd(name: str) -> None:
    """Delete a trickset file."""
    if name == "_default":
        raise click.ClickException("Cannot delete the _default trickset")
    p = _ts_path(name)
    if not p.exists():
        raise click.ClickException(f"Trickset '{name}' not found")
    p.unlink()
    click.echo(f"Deleted trickset '{name}'")


@cli.command("rename")
@click.argument("old")
@click.argument("new")
def rename_cmd(old: str, new: str) -> None:
    """Rename a trickset (moves its JSON file)."""
    ts = _load_ts(old)
    if _ts_path(new).exists():
        raise click.ClickException(f"Trickset '{new}' already exists")
    old_path = Path(ts.file_path) if ts.file_path else _ts_path(old)
    new_path = _ts_path(new)
    ts.name = new
    old_path.rename(new_path)
    ts.file_path = str(new_path)
    _save_ts(ts)
    click.echo(f"Renamed '{old}' -> '{new}'")


@cli.command("add")
@click.argument("name")
@click.argument("trick")
@click.option("--disable", is_flag=True, help="Add the trick disabled")
@click.option("--keyword", "keyword", default=None, help="Override prompt keyword")
@click.option("--no-install", is_flag=True, help="Skip the trick's install() hook")
def add_cmd(name: str, trick: str, disable: bool, keyword: str | None, no_install: bool) -> None:
    """Add a trick to a trickset. Runs its install() hook like the dashboard."""
    ts = _load_ts(name)
    resolved = _resolve_trick_spec(trick)
    if not _trick_exists(resolved):
        raise click.ClickException(f"Trick file not found: {trick}")
    t = ts.add_trick(resolved, enabled=not disable, keyword=keyword)
    if not no_install:
        try:
            t.install()
        except Exception as e:
            click.echo(f"Warning: install() failed for {resolved}: {e}", err=True)
    _save_ts(ts)
    click.echo(f"Added {type(t).__name__} ({resolved}) to '{name}'")


@cli.command("rm")
@click.argument("name")
@click.argument("trick")
def rm_cmd(name: str, trick: str) -> None:
    """Remove a trick from a trickset. Runs its uninstall() hook."""
    ts = _load_ts(name)
    idx = _find_trick_index(ts, trick)
    if idx is None:
        raise click.ClickException(f"Trick '{trick}' not found in '{name}'")
    tid = ts.trick_ids[idx]
    if idx < len(ts.tricks) and ts.tricks[idx] is not None:
        try:
            ts.tricks[idx].uninstall()
        except Exception as e:
            click.echo(f"Warning: uninstall() failed for {trick}: {e}", err=True)
    ts.remove_trick(tid)
    _save_ts(ts)
    click.echo(f"Removed {trick} from '{name}'")


@cli.command("enable")
@click.argument("name")
@click.argument("trick")
def enable_cmd(name: str, trick: str) -> None:
    """Activate a trick in a trickset."""
    _set_enabled(name, trick, True)


@cli.command("disable")
@click.argument("name")
@click.argument("trick")
def disable_cmd(name: str, trick: str) -> None:
    """Deactivate a trick in a trickset."""
    _set_enabled(name, trick, False)


def _set_enabled(name: str, trick: str, enabled: bool) -> None:
    ts = _load_ts(name)
    idx = _find_trick_index(ts, trick)
    if idx is None:
        raise click.ClickException(f"Trick '{trick}' not found in '{name}'")
    while len(ts.trick_enabled) <= idx:
        ts.trick_enabled.append(True)
    ts.trick_enabled[idx] = enabled
    _save_ts(ts)
    state = "enabled" if enabled else "disabled"
    click.echo(f"{_trick_label(ts, idx)}: {state} in '{name}'")


@cli.command("reorder")
@click.argument("name")
@click.argument("trick")
@click.argument("index", type=int)
def reorder_cmd(name: str, trick: str, index: int) -> None:
    """Move a trick to a new position (0-based) in the execution order."""
    ts = _load_ts(name)
    idx = _find_trick_index(ts, trick)
    if idx is None:
        raise click.ClickException(f"Trick '{trick}' not found in '{name}'")
    tid = ts.trick_ids[idx]
    if not ts.reorder_trick(tid, index):
        raise click.ClickException(f"Could not reorder '{trick}'")
    _save_ts(ts)
    click.echo(f"Moved {trick} to position {index} in '{name}'")


@cli.command("keyword")
@click.argument("name")
@click.argument("trick")
@click.argument("keyword", required=False)
def keyword_cmd(name: str, trick: str, keyword: str | None) -> None:
    """Set (or clear) a prompt keyword override for a trick."""
    ts = _load_ts(name)
    idx = _find_trick_index(ts, trick)
    if idx is None:
        raise click.ClickException(f"Trick '{trick}' not found in '{name}'")
    kw = keyword.strip() if keyword and keyword.strip() else None
    while len(ts.trick_keywords) <= idx:
        ts.trick_keywords.append(None)
    ts.trick_keywords[idx] = kw
    _save_ts(ts)
    if kw:
        click.echo(f"keyword for {_trick_label(ts, idx)} set to '{kw}'")
    else:
        click.echo(f"keyword override for {_trick_label(ts, idx)} cleared")


@cli.command("config")
@click.argument("name")
@click.argument("trick")
@click.argument("settings", nargs=-1)
def config_cmd(name: str, trick: str, settings: tuple[str, ...]) -> None:
    """Set per-trick config fields: 'pet config <ts> <trick> key=value ...'."""
    if not settings:
        ts = _load_ts(name)
        idx = _find_trick_index(ts, trick)
        if idx is None:
            raise click.ClickException(f"Trick '{trick}' not found in '{name}'")
        tid = ts.trick_ids[idx]
        cfg = ts.trick_configs.get(tid, {})
        click.echo(json.dumps(cfg, indent=2) if cfg else "(no config set)")
        return
    ts = _load_ts(name)
    idx = _find_trick_index(ts, trick)
    if idx is None:
        raise click.ClickException(f"Trick '{trick}' not found in '{name}'")
    tid = ts.trick_ids[idx]
    config = dict(ts.trick_configs.get(tid, {}))
    fields = []
    t = ts.tricks[idx] if idx < len(ts.tricks) else None
    if t is not None:
        fields = [f.get("key") for f in getattr(type(t), "config_fields", []) or []]
    for setting in settings:
        if "=" not in setting:
            raise click.UsageError(f"Expected key=value, got '{setting}'")
        k, _, v = setting.partition("=")
        if fields and k not in fields:
            click.echo(f"Note: '{k}' is not a declared config field for {_trick_label(ts, idx)}", err=True)
        config[k] = _parse_value(v)
    ts.trick_configs[tid] = config
    if t is not None:
        try:
            t.configure(config)
        except Exception as e:
            click.echo(f"Warning: configure() failed: {e}", err=True)
    _save_ts(ts)
    click.echo(f"config for {_trick_label(ts, idx)} saved: {json.dumps(config)}")


@cli.command("param")
@click.argument("name")
@click.argument("settings", nargs=-1)
@click.option("--clear", is_flag=True, help="Remove all parameters")
def param_cmd(name: str, settings: tuple[str, ...], clear: bool) -> None:
    """Set trickset parameters: 'pet param <ts> key=value ...'."""
    ts = _load_ts(name)
    if not settings and not clear:
        click.echo(json.dumps(ts.parameters, indent=2) if ts.parameters else "(no parameters set)")
        return
    if clear:
        ts.parameters = {}
    for setting in settings:
        if "=" not in setting:
            raise click.UsageError(f"Expected key=value, got '{setting}'")
        k, _, v = setting.partition("=")
        ts.parameters[k] = _parse_value(v)
    _save_ts(ts)
    click.echo(f"parameters for '{name}': {json.dumps(ts.parameters)}")


@cli.command("filter")
@click.argument("name")
@click.option("--x-title", default=None, help="X-Title filter glob")
@click.option("--model", "model_filter", default=None, help="Model filter glob")
def filter_cmd(name: str, x_title: str | None, model_filter: str | None) -> None:
    """Set a trickset's routing filters (globs, '*' matches everything)."""
    ts = _load_ts(name)
    if x_title is not None:
        ts.filters["X-Title"] = x_title
    if model_filter is not None:
        ts.filters["Model"] = model_filter
    if x_title is None and model_filter is None:
        raise click.UsageError("Pass --x-title and/or --model")
    _save_ts(ts)
    click.echo(f"filters for '{name}': {'  '.join(f'{k}={v}' for k, v in ts.filters.items())}")


@cli.command("install")
@click.argument("trick")
def install_cmd(trick: str) -> None:
    """Run a trick's install() hook (e.g. 'pet install swapharness')."""
    _run_lifecycle(_resolve_trick_spec(trick), "install")


@cli.command("uninstall")
@click.argument("trick")
def uninstall_cmd(trick: str) -> None:
    """Run a trick's uninstall() hook."""
    _run_lifecycle(_resolve_trick_spec(trick), "uninstall")


@cli.command("lifecycle")
@click.argument("trick")
@click.argument("hook", type=click.Choice(["install", "startup", "shutdown", "uninstall"]))
def lifecycle_cmd(trick: str, hook: str) -> None:
    """Run an arbitrary lifecycle hook on a trick."""
    _run_lifecycle(_resolve_trick_spec(trick), hook)


@cli.command("models")
@click.argument("name", required=False)
@click.option("--json", "json_out", is_flag=True, help="Emit raw JSON")
def models_cmd(name: str | None, json_out: bool) -> None:
    """Show model config. Without a name, shows the global config.json modelset."""
    cfg = load_config()
    if not name or name == "_default":
        global_models = dict(cfg.get("modelset", {}))
        if "default" not in global_models:
            global_models["default"] = {
                "url": cfg.get("model_url", ""),
                "model": cfg.get("model_name", ""),
                "key": cfg.get("api_key", ""),
            }
        _print_models(global_models, {}, json_out=json_out)
        return
    ts = _load_ts(name)
    scoped = {k: v for k, v in ts.models.items() if isinstance(v, dict)}
    _print_models(dict(cfg.get("modelset", {})), scoped, json_out=json_out)


@cli.command("model")
@click.argument("key")
@click.argument("url", required=False)
@click.option("--trickset", "ts_name", default="_default", show_default=True,
              help="Scope: a trickset name, or _default for the global config.json modelset")
@click.option("--model", "model_name", default=None, help="Model name to send upstream")
@click.option("--key", "api_key", default=None, help="API key for the upstream")
@click.option("--model-passthrough", is_flag=True, help="Pass through the client model name (store false)")
@click.option("--key-passthrough", is_flag=True, help="Pass through the client API key (store false)")
@click.option("--remove", is_flag=True, help="Remove this model key")
def model_cmd(key: str, url: str | None, ts_name: str, model_name: str | None,
              api_key: str | None, model_passthrough: bool, key_passthrough: bool, remove: bool) -> None:
    """Set a model config entry for a model key.

    Without --trickset the entry is stored in the global config.json modelset;
    with --trickset <name> it is stored on that trickset (overriding the
    global entry for that key).

    Examples:
      pet model default http://localhost:11434 --model llama3:8b
      pet model thinker http://localhost:11434 --model lfm2.5:latest
      pet model default http://localhost:11434 --model llama3:8b --trickset opencode
      pet model toolcall --remove
    """
    if not url and not remove:
        raise click.UsageError("url is required (or pass --remove)")

    entry: dict[str, Any] = {}
    if url is not None:
        entry["url"] = url.rstrip("/")
    if model_passthrough:
        entry["model"] = False
    elif model_name is not None:
        entry["model"] = model_name
    if key_passthrough:
        entry["key"] = False
    elif api_key is not None:
        entry["key"] = api_key

    if ts_name == "_default":
        cfg = load_config()
        modelset = dict(cfg.get("modelset", {}))
        if remove:
            modelset.pop(key, None)
            if key == "default":
                cfg["model_url"] = ""
                cfg["model_name"] = ""
                cfg["api_key"] = ""
            cfg["modelset"] = modelset
            save_config(cfg)
            click.echo(f"Removed model key '{key}' from global config")
            return
        modelset[key] = entry
        cfg["modelset"] = modelset
        if key == "default":
            cfg["model_url"] = entry.get("url", "")
            cfg["model_name"] = "" if entry.get("model") is False else (entry.get("model") or "")
            cfg["api_key"] = "" if entry.get("key") is False else (entry.get("key") or "")
        save_config(cfg)
        click.echo(f"Global model '{key}' saved: {json.dumps(entry)}")
        return

    ts = _load_ts(ts_name)
    if remove:
        ts.models.pop(key, None)
    else:
        ts.models[key] = entry
    _save_ts(ts)
    click.echo(f"Model '{key}' saved on trickset '{ts_name}': {json.dumps(entry)}")


@cli.command("logfile")
@click.argument("name")
@click.argument("path", required=False)
def logfile_cmd(name: str, path: str | None) -> None:
    """Show or set a trickset's log file path."""
    ts = _load_ts(name)
    if path is None:
        click.echo(ts.logfile)
        return
    ts.logfile = str(Path(path).expanduser())
    _save_ts(ts)
    click.echo(f"logfile for '{name}': {ts.logfile}")


@cli.command("loglevel")
@click.argument("name")
@click.argument("level", required=False, type=click.Choice(LOG_LEVELS))
def loglevel_cmd(name: str, level: str | None) -> None:
    """Show or set a trickset's log level (DEBUG/INFO/WARNING/ERROR)."""
    ts = _load_ts(name)
    if level is None:
        click.echo(ts.loglevel)
        return
    ts.loglevel = level.upper()
    _save_ts(ts)
    click.echo(f"loglevel for '{name}': {ts.loglevel}")


@cli.command("examples")
@click.option("--force", is_flag=True, help="Overwrite existing examples (backs up first)")
def examples_cmd(force: bool) -> None:
    """Install the example tricksets into the config dir."""
    from src.server import install_examples
    results = install_examples(force=force)
    if not results:
        click.echo("No example tricksets found.")
        return
    for r in results:
        if r["result"]:
            click.echo(f"  installed {r['name']}")
        else:
            click.echo(f"  skipped {r['name']} ({r.get('errmsg', '')})")


@cli.group()
def agents() -> None:
    """Manage harness agents (route tools through petsitter)."""


@agents.command("list")
def agents_list() -> None:
    """List harness agents and their registration state."""
    mgr = _install_agent_manager()
    registered = mgr.get_registered().get("agents", {})
    agents_data = mgr.get_agents()
    if not agents_data:
        click.echo("No agent harnesses found.")
        return
    for agent_id, a in sorted(agents_data.items()):
        d = a.get("detect", {})
        status = d.get("status", "?")
        reg = "registered" if registered.get(agent_id, {}).get("status") == "registered" else "not registered"
        click.echo(f"{agent_id}  [{status} / {reg}]  {a.get('display_name', '')}")
        click.echo(f"    {a.get('description', '')}")
        for p in a.get("config_paths", []):
            click.echo(f"    config: {p}")


@agents.command("register")
@click.argument("agent_id")
def agents_register(agent_id: str) -> None:
    """Register an agent: create its trickset and patch its config."""
    mgr = _install_agent_manager()
    try:
        success, log = mgr.register(agent_id)
    except KeyError as e:
        raise click.ClickException(str(e))
    for entry in log:
        lvl = entry.get("level", "INFO")
        click.echo(f"[{lvl}] {entry.get('message', '')}")
    if not success:
        raise click.ClickException(f"Registration of '{agent_id}' failed")
    click.echo(f"Registered agent '{agent_id}'")


@agents.command("unregister")
@click.argument("agent_id")
def agents_unregister(agent_id: str) -> None:
    """Unregister an agent and restore its original config."""
    mgr = _install_agent_manager()
    try:
        success, log = mgr.unregister(agent_id)
    except KeyError as e:
        raise click.ClickException(str(e))
    for entry in log:
        lvl = entry.get("level", "INFO")
        click.echo(f"[{lvl}] {entry.get('message', '')}")
    click.echo(f"Unregistered agent '{agent_id}'")


@cli.command("status")
def status_cmd() -> None:
    """Show a summary of the current configuration."""
    cfg = load_config()
    url = cfg.get("model_url", "")
    model = cfg.get("model_name", "")
    click.echo("petsitter status")
    click.echo(f"  config dir: {config_dir()}")
    click.echo(f"  model:      {url or '(not set)'}" + (f"  ({model})" if model else ""))
    files = _list_ts_files()
    click.echo(f"  tricksets:  {len(files)}")
    for f in files:
        ts = Trickset.load_from_file(str(f))
        enabled = sum(1 for i in range(len(ts.trick_paths))
                      if i < len(ts.trick_enabled) and ts.trick_enabled[i])
        click.echo(f"    {ts.name}  ({enabled}/{len(ts.trick_paths)} tricks enabled)")


if __name__ == "__main__":
    cli()
