"""Tests for the reference check trick."""

import asyncio
import json

import pytest

from petsitter import observability
from petsitter.tricks import reference_check as rc
from petsitter.tricks.reference_check import ReferenceCheckTrick, _classify, _split_refs


@pytest.fixture(autouse=True)
def request_envelope():
    """Every hook here runs inside a request, as it does in the proxy."""
    token = observability.start_request_meta()
    yield observability.request_meta()
    observability.reset_request_meta(token)


def _tools(*specs):
    return [{"type": "function", "function": dict(s)} for s in specs]


SEARCH_TOOL = _tools({"name": "search_docs", "description": "Search the manual"})


def _convo(tool_content, call_id="call_1", name="search_docs"):
    return [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Who was the 16th president?"},
        {"role": "assistant", "tool_calls": [
            {"id": call_id, "type": "function",
             "function": {"name": name, "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": call_id, "content": tool_content},
    ]


def _ids(trick, context):
    return trick._issued_ids(context)


class TestToolMatching:
    def test_matches_on_name(self):
        t = ReferenceCheckTrick()
        assert t._reference_tools(_tools({"name": "search_docs"})) == {"search_docs"}

    def test_matches_on_description(self):
        """MCP tools are named badly; the description is what gives them away."""
        t = ReferenceCheckTrick()
        tools = _tools({"name": "mcp__ctx7__get", "description": "Search documentation"})
        assert t._reference_tools(tools) == {"mcp__ctx7__get"}

    def test_ignores_unrelated_tools(self):
        t = ReferenceCheckTrick()
        tools = _tools({"name": "write_file", "description": "Write to disk"})
        assert t._reference_tools(tools) == set()

    def test_patterns_are_configurable(self):
        t = ReferenceCheckTrick()
        t.configure({"tool_patterns": "oracle"})
        assert t._reference_tools(SEARCH_TOOL) == set()
        assert t._reference_tools(_tools({"name": "oracle_ask"})) == {"oracle_ask"}


class TestStamping:
    def test_dormant_without_reference_tools(self):
        t = ReferenceCheckTrick()
        ctx = _convo("Lincoln was the 16th president.")
        out = t.pre_hook(ctx, {"tools": _tools({"name": "write_file"})})
        assert "ref_id" not in out[-1]["content"]
        assert "reference-checked" not in out[0]["content"]

    def test_stamps_prose_and_states_the_contract(self):
        t = ReferenceCheckTrick()
        ctx = t.pre_hook(_convo("Lincoln was the 16th president."), {"tools": SEARCH_TOOL})
        assert rc.ID_RE.search(ctx[-1]["content"])
        assert "You are being reference-checked." in ctx[0]["content"]
        assert "ref_id:none" in ctx[0]["content"]

    def test_stamps_each_paragraph_separately(self):
        t = ReferenceCheckTrick()
        ctx = t.pre_hook(_convo("Doc one text.\n\nDoc two text."), {"tools": SEARCH_TOOL})
        assert len(_ids(t, ctx)) == 3  # two documents plus the whole result

    def test_json_results_keep_their_shape(self):
        body = json.dumps({"results": [{"title": "a", "text": "x"}, {"title": "b", "text": "y"}]})
        t = ReferenceCheckTrick()
        ctx = t.pre_hook(_convo(body), {"tools": SEARCH_TOOL})
        parsed = json.loads(ctx[-1]["content"])
        assert [r["title"] for r in parsed["results"]] == ["a", "b"]
        assert all("ref_id" in r for r in parsed["results"])
        assert len({r["ref_id"] for r in parsed["results"]}) == 2

    def test_bare_json_list_is_stamped(self):
        body = json.dumps([{"text": "x"}, {"text": "y"}])
        t = ReferenceCheckTrick()
        ctx = t.pre_hook(_convo(body), {"tools": SEARCH_TOOL})
        assert all("ref_id" in r for r in json.loads(ctx[-1]["content"]))

    def test_non_reference_tool_output_is_left_alone(self):
        t = ReferenceCheckTrick()
        ctx = _convo("some shell output", call_id="c2", name="run_bash")
        ctx.append({"role": "assistant", "tool_calls": [
            {"id": "c3", "type": "function", "function": {"name": "search_docs", "arguments": "{}"}}]})
        ctx.append({"role": "tool", "tool_call_id": "c3", "content": "retrieved text"})
        out = t.pre_hook(ctx, {"tools": SEARCH_TOOL + _tools({"name": "run_bash"})})
        assert "ref_id" not in out[3]["content"]
        assert rc.ID_RE.search(out[-1]["content"])

    def test_stamping_is_idempotent_across_turns(self):
        """The harness resends its own unstamped transcript every turn."""
        t = ReferenceCheckTrick()
        first = t.pre_hook(_convo("Lincoln was the 16th president."), {"tools": SEARCH_TOOL})
        again = t.pre_hook(list(first), {"tools": SEARCH_TOOL})
        assert first[-1]["content"] == again[-1]["content"]

    def test_ids_are_unguessable_across_instances(self):
        a, b = ReferenceCheckTrick(), ReferenceCheckTrick()
        assert a._id("call_1", "text") != b._id("call_1", "text")
        assert a._id("call_1", "text") == a._id("call_1", "text")


class TestRefsParsing:
    def test_splits_and_strips(self):
        body, cited = _split_refs("Abraham Lincoln.\n<refs>\nclaim -> ref_id:abc123abc123\n</refs>")
        assert body == "Abraham Lincoln."
        assert cited == ["abc123abc123"]

    def test_tolerates_missing_closing_tag(self):
        body, cited = _split_refs("Answer.\n<refs>\nx -> ref_id:none")
        assert body == "Answer."
        assert cited == ["none"]

    def test_captures_a_forged_id_rather_than_skipping_it(self):
        _, cited = _split_refs("<refs>\nclaim -> <reference_id: #131>\n</refs>")
        assert cited == ["131"]

    def test_classify(self):
        valid, forged, none = _classify(["aaaaaaaaaaaa", "131", "none"], {"aaaaaaaaaaaa"})
        assert valid == ["aaaaaaaaaaaa"]
        assert forged == ["131"]
        assert none == 1


class TestVerification:
    """post_hook, with the challenge call stubbed out."""

    @pytest.fixture
    def armed(self, monkeypatch):
        calls = []

        def fake(context, message="", *a, **kw):
            calls.append(message)
            reply = replies.pop(0) if replies else {"role": "assistant", "content": "still nothing"}
            return list(context) + [{"role": "user", "content": message}, reply]

        replies = []
        monkeypatch.setattr(rc, "callmodel_sync", fake)
        return calls, replies

    def _prepared(self, content="Lincoln was the 16th president."):
        t = ReferenceCheckTrick()
        ctx = t.pre_hook(_convo(content), {"tools": SEARCH_TOOL})
        return t, ctx

    def test_dormant_when_no_reference_tools_were_in_scope(self, armed):
        calls, _ = armed
        t = ReferenceCheckTrick()
        t.pre_hook(_convo("x"), {"tools": _tools({"name": "write_file"})})
        ctx = _convo("x") + [{"role": "assistant", "content": "unsourced claim"}]
        assert t.post_hook(ctx)[-1]["content"] == "unsourced claim"
        assert calls == []

    def test_valid_citation_passes_and_block_is_stripped(self, armed):
        calls, _ = armed
        t, ctx = self._prepared()
        good = sorted(_ids(t, ctx))[0]
        ctx.append({"role": "assistant",
                    "content": f"Abraham Lincoln.\n<refs>\n16th president -> ref_id:{good}\n</refs>"})
        out = t.post_hook(ctx)
        assert out[-1]["content"] == "Abraham Lincoln."
        assert calls == []

    def test_forged_id_is_challenged(self, armed):
        calls, replies = armed
        t, ctx = self._prepared()
        good = sorted(_ids(t, ctx))[0]
        replies.append({"role": "assistant",
                        "content": f"Abraham Lincoln.\n<refs>\nx -> ref_id:{good}\n</refs>"})
        ctx.append({"role": "assistant",
                    "content": "The cuttlefish.\n<refs>\nx -> <reference_id: #131>\n</refs>"})
        out = t.post_hook(ctx)
        assert len(calls) == 1
        assert "131" in calls[0]
        assert "never issued" in calls[0]
        assert out[-1]["content"] == "Abraham Lincoln."
        assert t._stats["forged"] == 1

    def test_unattributed_answer_is_challenged_with_the_content_again(self, armed):
        calls, replies = armed
        t, ctx = self._prepared()
        good = sorted(_ids(t, ctx))[0]
        replies.append({"role": "assistant", "content": f"Lincoln.\n<refs>\nx -> ref_id:{good}\n</refs>"})
        ctx.append({"role": "assistant", "content": "Lincoln, obviously."})
        out = t.post_hook(ctx)
        assert len(calls) == 1
        assert good in calls[0]  # the retrieved material, re-presented
        assert out[-1]["content"] == "Lincoln."

    def test_ref_id_none_is_accepted_without_challenge(self, armed):
        calls, _ = armed
        t, ctx = self._prepared()
        ctx.append({"role": "assistant",
                    "content": "I could not find that.\n<refs>\nx -> ref_id:none\n</refs>"})
        out = t.post_hook(ctx)
        assert calls == []
        assert out[-1]["content"] == "I could not find that."
        assert t._stats["none"] == 1

    def test_gives_up_after_max_rounds_and_passes_through_clean(self, armed):
        calls, _ = armed
        t, ctx = self._prepared()
        t.configure({"max_rounds": 2})
        ctx.append({"role": "assistant", "content": "Bare assertion."})
        out = t.post_hook(ctx)
        assert len(calls) == 2
        assert out[-1]["content"] == "still nothing"
        assert t._stats["gave_up"] == 1

    def test_no_annotation_is_ever_added_to_the_answer(self, armed):
        """The response body must be exactly what the model produced."""
        calls, _ = armed
        t, ctx = self._prepared()
        t.configure({"max_rounds": 0})
        ctx.append({"role": "assistant", "content": '{"president": "Lincoln"}'})
        out = t.post_hook(ctx)
        assert out[-1]["content"] == '{"president": "Lincoln"}'
        assert json.loads(out[-1]["content"]) == {"president": "Lincoln"}

    def test_missing_call_is_challenged(self, armed):
        calls, replies = armed
        t = ReferenceCheckTrick()
        ctx = t.pre_hook([{"role": "user", "content": "Who was the 16th president?"}],
                         {"tools": SEARCH_TOOL})
        replies.append({"role": "assistant",
                        "content": "I have no source for that.\n<refs>\nx -> ref_id:none\n</refs>"})
        ctx.append({"role": "assistant", "content": "Lincoln, from memory."})
        out = t.post_hook(ctx)
        assert len(calls) == 1
        assert "without consulting" in calls[0]
        assert out[-1]["content"] == "I have no source for that."

    def test_an_answer_that_never_complies_burns_every_round(self, armed):
        calls, _ = armed
        t = ReferenceCheckTrick()
        ctx = t.pre_hook([{"role": "user", "content": "?"}], {"tools": SEARCH_TOOL})
        ctx.append({"role": "assistant", "content": "Lincoln, from memory."})
        t.post_hook(ctx)
        assert len(calls) == 3
        assert t._stats["gave_up"] == 1

    def test_missing_call_check_can_be_turned_off(self, armed):
        calls, _ = armed
        t = ReferenceCheckTrick(challenge_missing_call=False)
        ctx = t.pre_hook([{"role": "user", "content": "?"}], {"tools": SEARCH_TOOL})
        ctx.append({"role": "assistant", "content": "Lincoln, from memory."})
        assert t.post_hook(ctx)[-1]["content"] == "Lincoln, from memory."
        assert calls == []

    def test_a_challenge_answered_with_a_tool_call_goes_to_the_harness(self, armed):
        calls, replies = armed
        t = ReferenceCheckTrick()
        ctx = t.pre_hook([{"role": "user", "content": "?"}], {"tools": SEARCH_TOOL})
        replies.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": "c9", "type": "function",
             "function": {"name": "search_docs", "arguments": "{}"}}]})
        ctx.append({"role": "assistant", "content": "Lincoln, from memory."})
        out = t.post_hook(ctx)
        assert out[-1]["tool_calls"][0]["function"]["name"] == "search_docs"

    def test_in_flight_tool_call_is_not_checked(self, armed):
        calls, _ = armed
        t, ctx = self._prepared()
        ctx.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": "c4", "type": "function", "function": {"name": "search_docs", "arguments": "{}"}}]})
        t.post_hook(ctx)
        assert calls == []

    def test_challenge_failure_passes_the_answer_through(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("upstream down")
        monkeypatch.setattr(rc, "callmodel_sync", boom)
        t, ctx = self._prepared()
        ctx.append({"role": "assistant", "content": "Unsourced."})
        assert t.post_hook(ctx)[-1]["content"] == "Unsourced."


class TestRequestScoping:
    """Per-request state rides the envelope, not the trick instance."""

    def test_pre_hook_publishes_what_it_found(self):
        t = ReferenceCheckTrick()
        t.pre_hook(_convo("text"), {"tools": SEARCH_TOOL})
        assert observability.request_meta()["reference_tools"] == {"search_docs"}

    def test_post_hook_falls_back_to_the_payload_on_the_envelope(self, monkeypatch):
        """pre_hook never ran, but the proxy parked the request's tools."""
        calls = []
        monkeypatch.setattr(rc, "callmodel_sync", lambda c, m="", *a, **k: (
            calls.append(m) or list(c) + [{"role": "assistant", "content": "x\n<refs>\ny -> ref_id:none\n</refs>"}]))
        observability.request_meta()["tools"] = SEARCH_TOOL
        t = ReferenceCheckTrick()
        ctx = _convo("text") + [{"role": "assistant", "content": "unsourced"}]
        t.post_hook(ctx)
        assert len(calls) == 1

    def test_no_state_leaks_between_instances_of_the_same_trick(self):
        """The trick object is shared across requests; the verdict must not be."""
        t = ReferenceCheckTrick()

        async def one(tools, answer):
            token = observability.start_request_meta()
            try:
                ctx = t.pre_hook(_convo("Retrieved text."), {"tools": tools})
                ctx.append({"role": "assistant", "content": answer})
                await asyncio.sleep(0)  # let the other request interleave here
                return t.post_hook(ctx)[-1]["content"]
            finally:
                observability.reset_request_meta(token)

        async def both():
            return await asyncio.gather(
                one(_tools({"name": "write_file"}), "no tools in scope"),
                one(SEARCH_TOOL, "sourced\n<refs>\nx -> ref_id:none\n</refs>"),
            )

        unarmed, armed = asyncio.run(both())
        assert unarmed == "no tools in scope"   # dormant, block never stripped
        assert armed == "sourced"               # armed, block stripped


class TestReporting:
    def test_info_makes_no_claim_about_the_answer(self):
        assert ReferenceCheckTrick().info({}) == {"reference_check": True}

    def test_keyword_before_any_traffic(self):
        r = ReferenceCheckTrick().handle_prompt_keyword("")
        assert "has not seen a retrieval turn yet" in r["content"]

    def test_keyword_reports_the_tally(self):
        t = ReferenceCheckTrick()
        t._stats.update({"checked": 4, "challenged": 2, "forged": 1, "none": 1, "gave_up": 0})
        assert "4 answers checked" in t.handle_prompt_keyword("")["content"]
