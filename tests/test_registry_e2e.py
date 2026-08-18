"""End-to-end: metadata extraction -> index -> install -> checksum -> load."""
import hashlib, json, os, pathlib, shutil, sys, tempfile
from pathlib import Path

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tricks-index"))   # crawl.py lives there

from petsitter import registry, loader
import crawl

TRICK = '''
"""A test trick."""
from petsitter.trick import Trick

class OllamaCtxTrick(Trick):
    __version__ = "0.1.0"
    __brief__ = "Clamps num_ctx for ollama backends"
    __display_name__ = "Ollama Context Clamp"
    keywords = ["ctx", "ollama"]
    prompt_keyword = "ctx"

    def system_prompt(self, to_add: str) -> str:
        return "Keep it short."
'''

fails = []
def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("  " + detail if detail and not cond else ""))
    if not cond:
        fails.append(label)

tmp = Path(tempfile.mkdtemp())
cfg = tmp / "config"; cfg.mkdir()
src_dir = tmp / "repo"; src_dir.mkdir()
trick_file = src_dir / "ollama_ctx.py"
trick_file.write_text(TRICK)
blob = trick_file.read_bytes()

print("\n1. ast extraction (crawl.py) — no import, no exec")
meta = crawl.extract_trick(blob)
check("finds the Trick subclass", meta is not None)
check("__version__", meta.get("__version__") == "0.1.0", str(meta))
check("__brief__", meta.get("__brief__") == "Clamps num_ctx for ollama backends")
check("__display_name__", meta.get("__display_name__") == "Ollama Context Clamp")
check("keywords", meta.get("keywords") == ["ctx", "ollama"])
check("prompt_keyword", meta.get("prompt_keyword") == "ctx")
check("class_name", meta.get("class_name") == "OllamaCtxTrick")

print("\n1b. hostile input must not execute or crash")
check("no Trick subclass -> None", crawl.extract_trick(b"import os\nos.system('echo PWNED')\n") is None)
check("syntax error -> None", crawl.extract_trick(b"def (((") is None)
check("non-utf8 -> None", crawl.extract_trick(b"\xff\xfe\x00bad") is None)
check("slugify matches client", crawl.slugify("ollama_ctx") == registry.slugify_stem("ollama_ctx"))

print("\n2. index round trip")
sha = hashlib.sha256(blob).hexdigest()
index = {"schema": 1, "count": 1, "tricks": [{
    "name": "dana/ollama-ctx", "version": "0.1.0",
    "brief": meta["__brief__"], "display_name": meta["__display_name__"],
    "keywords": meta["keywords"], "prompt_keyword": meta["prompt_keyword"],
    "required_models": ["default"], "repo": "https://github.com/dana/x",
    "url": trick_file.as_uri(), "sha256": sha, "stars": 4, "featured": True,
}]}
index_file = tmp / "index.json"
index_file.write_text(json.dumps(index))
os.environ["PET_REGISTRY_INDEX"] = index_file.as_uri()

got = registry.fetch_index(cfg)
check("fetch_index over file://", got["count"] == 1)
check("cache written", (cfg / "registry-cache.json").exists())
check("search by name", len(registry.search(got, "ollama")) == 1)
check("search by keyword", len(registry.search(got, "ctx")) == 1)
check("search miss", len(registry.search(got, "zzzz")) == 0)
entry = registry.resolve(got, "dana/ollama-ctx")
check("resolve", entry["version"] == "0.1.0")

try:
    registry.resolve(got, "dana/nope"); check("unknown name raises", False)
except registry.RegistryError as e:
    check("unknown name raises", True)
try:
    registry.resolve(got, "dana/ollama-ctx", "9.9.9"); check("bad version raises", False)
except registry.RegistryError:
    check("bad version raises", True)

print("\n3. install + checksum enforcement")
path, fresh = registry.install(entry, cfg)
check("installed fresh", fresh)
check("lands at owner/slug/version.py",
      path == cfg / "tricks" / "dana" / "ollama-ctx" / "0.1.0.py", str(path))
check("bytes match", path.read_bytes() == blob)
check("receipt written", path.with_suffix(".json").exists())
_, again = registry.install(entry, cfg)
check("second install is a no-op", not again)

bad = dict(entry); bad["sha256"] = "0" * 64
bad_cfg = tmp / "cfg2"; bad_cfg.mkdir()
try:
    registry.install(bad, bad_cfg)
    check("checksum mismatch refuses", False)
except registry.RegistryError as e:
    check("checksum mismatch refuses", "mismatch" in str(e))
check("nothing written on mismatch",
      not (bad_cfg / "tricks" / "dana" / "ollama-ctx" / "0.1.0.py").exists())

print("\n4. list / uninstall")
items = registry.list_installed(cfg)
check("list_installed", len(items) == 1 and items[0]["spec"] == "pkg:dana/ollama-ctx@0.1.0")
check("brief survives via receipt", items[0]["brief"] == meta["__brief__"])

print("\n5. loader resolves pkg: specs")
loader.set_config_dir(cfg)
check("resolve_path pinned", loader.resolve_path("pkg:dana/ollama-ctx@0.1.0") == path)
check("resolve_path unpinned picks newest", loader.resolve_path("pkg:dana/ollama-ctx") == path)
cls = loader.load_trick_from_path("pkg:dana/ollama-ctx@0.1.0")
check("loads the class", cls.__name__ == "OllamaCtxTrick")
check("stamps __package_name__", getattr(cls, "__package_name__", None) == "dana/ollama-ctx")
check("instance works", cls().system_prompt("") == "Keep it short.")

print("\n5b. two owners, same filename, no collision")
other = tmp / "repo2"; other.mkdir()
o = other / "ollama_ctx.py"
o.write_text(TRICK.replace("OllamaCtxTrick", "OtherTrick").replace("Keep it short.", "Different."))
e2 = dict(entry); e2["name"] = "eve/ollama-ctx"; e2["url"] = o.as_uri()
e2["sha256"] = hashlib.sha256(o.read_bytes()).hexdigest()
registry.install(e2, cfg)
c1 = loader.load_trick_from_path("pkg:dana/ollama-ctx@0.1.0")
c2 = loader.load_trick_from_path("pkg:eve/ollama-ctx@0.1.0")
check("both load distinctly", c1().system_prompt("") == "Keep it short."
                              and c2().system_prompt("") == "Different.")

print("\n5c. missing package gives an actionable error")
try:
    loader.resolve_path("pkg:nobody/nothing@1.0.0"); check("missing raises", False)
except FileNotFoundError as e:
    check("missing raises with a fix", "pet install nobody/nothing" in str(e), str(e))

print("\n6. existing path forms still work")
check("relative path", loader.resolve_path("tricks/json_mode.py").name == "json_mode.py")
cls = loader.load_trick_from_path("tricks/no_emdash.py")
check("built-in still loads", cls.__name__ == "NoEmDashTrick")

print("\n7. stale cache beats a dead network")
os.environ["PET_REGISTRY_INDEX"] = "https://127.0.0.1:1/nope.json"
os.utime(cfg / "registry-cache.json", (0, 0))
stale = registry.fetch_index(cfg)
check("falls back to stale cache", stale["count"] == 1)
shutil.rmtree(cfg / "registry-cache.json", ignore_errors=True)
(cfg / "registry-cache.json").unlink(missing_ok=True)
try:
    registry.fetch_index(cfg); check("cold cache + dead net raises", False)
except registry.RegistryError:
    check("cold cache + dead net raises", True)

removed = registry.uninstall(cfg, "dana/ollama-ctx")
check("uninstall removes", len(removed) == 1 and not path.exists())

shutil.rmtree(tmp)
print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
