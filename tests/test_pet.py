"""Tests for the `pet` command-line interface."""

import json

import pytest
from click.testing import CliRunner

from src.pet import cli
from src.trickset import Trickset


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def pet_env(tmp_path, monkeypatch):
    """Point PET_CONFIG_DIR at an isolated temp dir."""
    monkeypatch.setenv("PET_CONFIG_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_config_override():
    """-c sets module globals; clear them so tests stay isolated."""
    from src import pet

    pet._override_config_dir = None
    pet._override_config_path = None
    yield
    pet._override_config_dir = None
    pet._override_config_path = None


def _invoke(runner, pet_env, *args):
    result = runner.invoke(cli, args)
    assert result.exit_code == 0, result.output
    return result


class TestConfigFlag:
    """pet -c <path> points all commands at an alternate config area."""

    def test_config_dir_arg_used(self, runner, tmp_path):
        """A directory arg sets config dir and default config.json path."""
        cfg_dir = tmp_path / "custom"
        result = runner.invoke(cli, ["-c", str(cfg_dir), "ls"])
        assert result.exit_code == 0, result.output
        from src import pet
        assert pet._override_config_dir == cfg_dir
        assert pet._override_config_path == cfg_dir / "config.json"

    def test_config_file_arg_used(self, runner, tmp_path):
        """A file arg sets config path directly; base dir is its parent."""
        cfg_file = tmp_path / "another_petsitter_config.conf.json"
        result = runner.invoke(cli, ["-c", str(cfg_file), "ls"])
        assert result.exit_code == 0, result.output
        from src import pet
        assert pet._override_config_path == cfg_file
        assert pet._override_config_dir == tmp_path

    def test_new_writes_to_config_dir(self, runner, tmp_path):
        """pet new with -c writes tricksets under the chosen base dir."""
        cfg_dir = tmp_path / "custom"
        _invoke(runner, tmp_path, "-c", str(cfg_dir), "new", "demo", "-t", "json_mode")
        assert (cfg_dir / "tricksets" / "demo.json").exists()

    def test_model_writes_to_config_file(self, runner, tmp_path):
        """pet model with -c writes the config file at the given path."""
        cfg_file = tmp_path / "custom" / "config.json"
        _invoke(runner, tmp_path, "-c", str(cfg_file), "model", "default",
                "http://localhost:11434", "--model", "gemma4")
        data = json.loads(cfg_file.read_text())
        assert data["model_url"] == "http://localhost:11434"
        assert data["model_name"] == "gemma4"

    def test_env_var_still_default(self, runner, pet_env):
        """Without -c, PET_CONFIG_DIR still selects the base dir."""
        _invoke(runner, pet_env, "new", "demo", "-t", "json_mode")
        assert (pet_env / "tricksets" / "demo.json").exists()


class TestBasics:
    def test_ls_empty(self, runner, pet_env):
        result = _invoke(runner, pet_env, "ls")
        assert "No tricksets yet" in result.output

    def test_new_creates_file(self, runner, pet_env):
        _invoke(runner, pet_env, "new", "demo", "--x-title", "opencode*", "-t", "json_mode")
        path = pet_env / "tricksets" / "demo.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["name"] == "demo"
        assert data["filters"]["X-Title"] == "opencode*"
        assert data["tricks"][0]["file"] == "tricks/json_mode.py"

    def test_show_reports_created_trickset(self, runner, pet_env):
        _invoke(runner, pet_env, "new", "demo", "-t", "json_mode")
        result = _invoke(runner, pet_env, "show", "demo")
        assert "demo" in result.output
        assert "JSON Mode" in result.output

    def test_show_without_name_is_table(self, runner, pet_env):
        _invoke(runner, pet_env, "new", "demo", "--x-title", "demo*", "-t", "json_mode")
        _invoke(runner, pet_env, "new", "other", "-t", "kennel")
        result = _invoke(runner, pet_env, "show")
        assert "NAME" in result.output
        assert "FILTERS" in result.output
        assert "demo" in result.output
        assert "demo*" in result.output
        assert "other" in result.output

    def test_default_filter_when_no_title(self, runner, pet_env):
        _invoke(runner, pet_env, "new", "demo", "-t", "json_mode")
        data = json.loads((pet_env / "tricksets" / "demo.json").read_text())
        assert data["filters"]["X-Title"] == "*"
        assert data["filters"]["Model"] == "*"


class TestTricks:
    def test_add_runs_install_and_enables(self, runner, pet_env):
        _invoke(runner, pet_env, "new", "demo", "-t", "json_mode")
        result = _invoke(runner, pet_env, "add", "demo", "kennel")
        assert "Kennel" in result.output
        data = json.loads((pet_env / "tricksets" / "demo.json").read_text())
        assert [t["file"] for t in data["tricks"]] == [
            "tricks/json_mode.py",
            "tricks/kennel.py",
        ]
        assert all(t["enabled"] for t in data["tricks"])

    def test_add_disabled_with_keyword(self, runner, pet_env):
        _invoke(runner, pet_env, "new", "demo", "-t", "json_mode")
        _invoke(runner, pet_env, "add", "demo", "kennel", "--disable", "--keyword", "k")
        data = json.loads((pet_env / "tricksets" / "demo.json").read_text())
        kennel = data["tricks"][1]
        assert kennel["enabled"] is False
        assert kennel["keyword"] == "k"

    def test_add_unknown_trick_fails(self, runner, pet_env):
        _invoke(runner, pet_env, "new", "demo")
        result = runner.invoke(cli, ["add", "demo", "no_such_trick"])
        assert result.exit_code != 0

    def test_enable_disable(self, runner, pet_env):
        _invoke(runner, pet_env, "new", "demo", "-t", "json_mode")
        _invoke(runner, pet_env, "disable", "demo", "json_mode")
        data = json.loads((pet_env / "tricksets" / "demo.json").read_text())
        assert data["tricks"][0]["enabled"] is False
        _invoke(runner, pet_env, "enable", "demo", "json_mode")
        data = json.loads((pet_env / "tricksets" / "demo.json").read_text())
        assert data["tricks"][0]["enabled"] is True

    def test_keyword(self, runner, pet_env):
        _invoke(runner, pet_env, "new", "demo", "-t", "json_mode")
        _invoke(runner, pet_env, "keyword", "demo", "json_mode", "go")
        data = json.loads((pet_env / "tricksets" / "demo.json").read_text())
        assert data["tricks"][0]["keyword"] == "go"

    def test_reorder(self, runner, pet_env):
        _invoke(runner, pet_env, "new", "demo", "-t", "json_mode", "-t", "kennel")
        _invoke(runner, pet_env, "reorder", "demo", "kennel", "0")
        data = json.loads((pet_env / "tricksets" / "demo.json").read_text())
        assert data["tricks"][0]["file"] == "tricks/kennel.py"

    def test_config_sets_fields(self, runner, pet_env):
        _invoke(runner, pet_env, "new", "demo", "-t", "mcp_tools")
        _invoke(runner, pet_env, "config", "demo", "mcp_tools", "mcp_path=/tmp/mcp.json")
        data = json.loads((pet_env / "tricksets" / "demo.json").read_text())
        assert data["tricks"][0]["config"] == {"mcp_path": "/tmp/mcp.json"}

    def test_rm_removes_trick(self, runner, pet_env):
        _invoke(runner, pet_env, "new", "demo", "-t", "json_mode", "-t", "kennel")
        _invoke(runner, pet_env, "rm", "demo", "kennel")
        data = json.loads((pet_env / "tricksets" / "demo.json").read_text())
        assert [t["file"] for t in data["tricks"]] == ["tricks/json_mode.py"]


class TestParameters:
    def test_param_parses_literals(self, runner, pet_env):
        _invoke(runner, pet_env, "new", "demo", "-t", "json_mode")
        _invoke(runner, pet_env, "param", "demo", "retries=3", "name=foo", "debug=true")
        data = json.loads((pet_env / "tricksets" / "demo.json").read_text())
        assert data["parameters"] == {"retries": 3, "name": "foo", "debug": True}

    def test_param_clears(self, runner, pet_env):
        _invoke(runner, pet_env, "new", "demo", "-t", "json_mode")
        _invoke(runner, pet_env, "param", "demo", "retries=3")
        _invoke(runner, pet_env, "param", "demo", "--clear")
        data = json.loads((pet_env / "tricksets" / "demo.json").read_text())
        assert data["parameters"] == {}


class TestModels:
    def test_model_global_default(self, runner, pet_env):
        result = _invoke(runner, pet_env, "model", "default", "http://localhost:11434", "--model", "gemma4")
        assert "Global model 'default' saved" in result.output
        cfg = json.loads((pet_env / "config.json").read_text())
        assert cfg["modelset"]["default"] == {"url": "http://localhost:11434", "model": "gemma4"}
        assert cfg["model_url"] == "http://localhost:11434"
        assert cfg["model_name"] == "gemma4"

    def test_model_scoped_to_trickset(self, runner, pet_env):
        _invoke(runner, pet_env, "new", "demo", "-t", "json_mode")
        _invoke(runner, pet_env, "model", "default", "http://127.0.0.1:123", "--trickset", "demo")
        data = json.loads((pet_env / "tricksets" / "demo.json").read_text())
        assert data["models"] == {"default": {"url": "http://127.0.0.1:123"}}

    def test_model_remove(self, runner, pet_env):
        _invoke(runner, pet_env, "model", "default", "http://localhost:11434")
        _invoke(runner, pet_env, "model", "default", "--remove")
        cfg = json.loads((pet_env / "config.json").read_text())
        assert "default" not in cfg.get("modelset", {})
        assert cfg["model_url"] == ""

    def test_model_requires_url_or_remove(self, runner, pet_env):
        result = runner.invoke(cli, ["model", "default"])
        assert result.exit_code != 0
        assert "url is required" in result.output


class TestTricksetOps:
    def test_rename(self, runner, pet_env):
        _invoke(runner, pet_env, "new", "demo", "-t", "json_mode")
        _invoke(runner, pet_env, "rename", "demo", "renamed")
        assert (pet_env / "tricksets" / "renamed.json").exists()
        assert not (pet_env / "tricksets" / "demo.json").exists()

    def test_delete(self, runner, pet_env):
        _invoke(runner, pet_env, "new", "demo", "-t", "json_mode")
        _invoke(runner, pet_env, "delete", "demo")
        assert not (pet_env / "tricksets" / "demo.json").exists()

    def test_delete_default_protected(self, runner, pet_env):
        result = runner.invoke(cli, ["delete", "_default"])
        assert result.exit_code != 0
        assert "Cannot delete the _default trickset" in result.output

    def test_lifecycle_hooks_run(self, runner, pet_env):
        result = _invoke(runner, pet_env, "install", "json_mode")
        assert "Ran install()" in result.output
        result = _invoke(runner, pet_env, "uninstall", "json_mode")
        assert "Ran uninstall()" in result.output

    def test_output_is_server_loadable(self, runner, pet_env):
        _invoke(runner, pet_env, "new", "demo", "--x-title", "opencode*", "-t", "json_mode", "-t", "kennel")
        _invoke(runner, pet_env, "disable", "demo", "kennel")
        _invoke(runner, pet_env, "keyword", "demo", "json_mode", "jm")
        _invoke(runner, pet_env, "param", "demo", "retries=3")
        _invoke(runner, pet_env, "model", "default", "http://127.0.0.1:1", "--trickset", "demo")
        ts = Trickset.load_from_file(str(pet_env / "tricksets" / "demo.json"))
        assert [type(t).__name__ for t in ts.tricks] == ["JsonModeTrick", "KennelTrick"]
        assert ts.trick_enabled == [True, False]
        assert ts.parameters == {"retries": 3}
        assert ts.models == {"default": {"url": "http://127.0.0.1:1"}}
