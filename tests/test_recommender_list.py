"""Tests for the recommender list trick."""

from petsitter.tricks.recommender_list import RecommenderListTrick, _parse_one


def _write_list(dirpath, text):
    path = dirpath / "recommend.txt"
    path.write_text(text, encoding="utf-8")
    return path


class TestParsing:
    def test_category_choice(self):
        e = _parse_one("database: postgres")
        assert e == {"kind": "prefer", "category": "database", "choice": "postgres", "note": ""}

    def test_equals_form_and_note(self):
        e = _parse_one("package manager = uv (fast)")
        assert e["category"] == "package manager"
        assert e["choice"] == "uv"
        assert e["note"] == "fast"

    def test_bare_preference(self):
        e = _parse_one("ripgrep")
        assert e["kind"] == "prefer"
        assert e["category"] == ""
        assert e["choice"] == "ripgrep"

    def test_avoid_forms(self):
        for line in ("avoid: mongodb", "!mongodb", "never mongodb"):
            e = _parse_one(line)
            assert e["kind"] == "avoid", line
            assert e["choice"] == "mongodb", line

    def test_comments_and_blanks_ignored(self):
        assert _parse_one("") is None
        assert _parse_one("# a comment") is None
        assert _parse_one("uv  # trailing note") == {
            "kind": "prefer", "category": "", "choice": "uv", "note": "",
        }

    def test_hash_inside_a_name_survives(self):
        assert _parse_one("language: c#")["choice"] == "c#"

    def test_words_starting_with_a_negation_are_not_avoids(self):
        assert _parse_one("notification queue: sqs")["kind"] == "prefer"
        assert _parse_one("no-color")["kind"] == "prefer"


class TestRecommenderListTrick:
    def test_declares_config_fields(self):
        keys = [f["key"] for f in RecommenderListTrick.config_fields]
        assert keys == ["recommender_path", "recommendations", "strict"]

    def test_empty_injects_nothing(self):
        t = RecommenderListTrick()
        assert t.system_prompt("base") == ""
        assert t.info({}) == {}

    def test_inline_recommendations(self):
        t = RecommenderListTrick(recommendations="database: postgres; avoid: mongodb")
        out = t.system_prompt("")
        assert "Preferred:" in out
        assert "database: postgres" in out
        assert "Avoid unless the user explicitly asks" in out
        assert "mongodb" in out

    def test_file_recommendations(self, tmp_path):
        p = _write_list(tmp_path, "# my stack\ndatabase: postgres\npackage manager: uv\n")
        t = RecommenderListTrick(recommender_path=str(p))
        out = t.system_prompt("")
        assert "database: postgres" in out
        assert "package manager: uv" in out

    def test_missing_file_is_dormant(self, tmp_path):
        t = RecommenderListTrick(recommender_path=str(tmp_path / "nope.txt"))
        assert t.system_prompt("") == ""

    def test_inline_overrides_file_per_category(self, tmp_path):
        p = _write_list(tmp_path, "database: mysql\n")
        t = RecommenderListTrick(recommender_path=str(p), recommendations="database: postgres")
        out = t.system_prompt("")
        assert "postgres" in out
        assert "mysql" not in out

    def test_strict_changes_the_instruction(self):
        t = RecommenderListTrick(recommendations="database: postgres")
        assert "say which entry you" in t.system_prompt("")
        t.configure({"strict": True})
        assert "Do not propose anything outside this list" in t.system_prompt("")

    def test_info_declares_the_list(self, tmp_path):
        p = _write_list(tmp_path, "database: postgres\nripgrep\n")
        t = RecommenderListTrick(recommender_path=str(p))
        caps = {}
        t.info(caps)
        assert caps["recommender_list"]["count"] == 2
        assert caps["recommender_list"]["categories"] == ["database"]

    def test_keyword_lists_when_empty(self):
        r = RecommenderListTrick().handle_prompt_keyword("")
        assert "recommender list is empty" in r["content"]

    def test_keyword_lists_entries(self):
        t = RecommenderListTrick(recommendations="database: postgres")
        assert "database: postgres" in t.handle_prompt_keyword("")["content"]

    def test_keyword_adds_and_persists(self, tmp_path):
        p = _write_list(tmp_path, "database: postgres\n")
        t = RecommenderListTrick(recommender_path=str(p))
        r = t.handle_prompt_keyword("package manager = uv")
        assert "package manager = uv" in r["content"]
        assert str(p) in r["content"]
        assert "package manager: uv" in p.read_text()
        # and it round-trips back through a fresh load
        assert "package manager: uv" in RecommenderListTrick(recommender_path=str(p)).system_prompt("")

    def test_keyword_add_replaces_the_category(self, tmp_path):
        p = _write_list(tmp_path, "database: mysql\n")
        t = RecommenderListTrick(recommender_path=str(p))
        t.handle_prompt_keyword("database: postgres")
        out = t.system_prompt("")
        assert "postgres" in out
        assert "mysql" not in out

    def test_keyword_add_without_a_file_stays_in_memory(self):
        t = RecommenderListTrick()
        r = t.handle_prompt_keyword("avoid mongodb")
        assert "avoid mongodb" in r["content"]
        assert "Saved to" not in r["content"]
        assert "mongodb" in t.system_prompt("")

    def test_keyword_drops_by_category_or_choice(self, tmp_path):
        p = _write_list(tmp_path, "database: postgres\nripgrep\n")
        t = RecommenderListTrick(recommender_path=str(p))
        assert "Dropped" in t.handle_prompt_keyword("drop database")["content"]
        assert "postgres" not in t.system_prompt("")
        assert "Dropped" in t.handle_prompt_keyword("remove ripgrep")["content"]
        assert t.system_prompt("") == ""

    def test_keyword_drop_miss(self):
        t = RecommenderListTrick(recommendations="database: postgres")
        assert "Nothing in the recommender list" in t.handle_prompt_keyword("drop redis")["content"]

    def test_keyword_reload_picks_up_file_edits(self, tmp_path):
        p = _write_list(tmp_path, "database: postgres\n")
        t = RecommenderListTrick(recommender_path=str(p))
        p.write_text("database: sqlite\n", encoding="utf-8")
        assert "Reloaded" in t.handle_prompt_keyword("reload")["content"]
        assert "sqlite" in t.system_prompt("")

    def test_keyword_junk_explains_itself(self):
        r = RecommenderListTrick().handle_prompt_keyword("#")
        assert "Nothing to add" in r["content"]
