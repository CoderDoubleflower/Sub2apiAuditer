"""Tests for the rules file trick."""

from pathlib import Path

from petsitter.trick import Trick
from tricks.rules_file import RulesFileTrick


def _write_rules(dirpath: Path, text: str) -> Path:
    path = dirpath / "rules.md"
    path.write_text(text, encoding="utf-8")
    return path


class TestRulesFileTrick:
    def test_declares_config_field(self):
        field = RulesFileTrick.config_fields[0]
        assert field["key"] == "rules_path"
        assert field["label"]
        assert field["type"] == "path"

    def test_no_path_injects_nothing(self):
        t = RulesFileTrick()
        assert t.system_prompt("base") == ""
        assert t.info({}) == {}

    def test_configure_loads_and_injects(self, tmp_path):
        p = _write_rules(tmp_path, "# Rules\n\n- always use tabs\n")
        t = RulesFileTrick()
        t.configure({"rules_path": str(p)})
        out = t.system_prompt("base")
        assert "always use tabs" in out
        assert out.startswith("The following rules MUST be followed")

    def test_info_declares_loaded_rules(self, tmp_path):
        p = _write_rules(tmp_path, "rule one")
        t = RulesFileTrick(rules_path=str(p))
        t.startup()
        capabilities = {}
        t.info(capabilities)
        assert capabilities["rules_file"] == str(p)

    def test_keyword_switches_file(self, tmp_path):
        p1 = _write_rules(tmp_path, "rule one")
        p2 = tmp_path / "other.md"
        p2.write_text("rule two")

        t = RulesFileTrick(rules_path=str(p1))
        r = t.handle_prompt_keyword("")
        assert "Rules loaded" in r["content"]
        assert "rule one" in t.system_prompt("")

        r = t.handle_prompt_keyword(str(p2))
        assert "rule two" in t.system_prompt("")
        assert "rule one" not in t.system_prompt("")

    def test_keyword_missing_file(self, tmp_path):
        t = RulesFileTrick()
        r = t.handle_prompt_keyword(str(tmp_path / "nope.md"))
        assert "No rules loaded" in r["content"]

    def test_keyword_reports_no_config(self, tmp_path):
        t = RulesFileTrick()
        r = t.handle_prompt_keyword("")
        assert "No rules file configured" in r["content"]
