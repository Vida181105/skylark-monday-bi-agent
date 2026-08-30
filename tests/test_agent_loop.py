"""Tests for the tool-use loop, with the Gemini client faked.

The loop is what turns tool results into an answer, and its failure modes only
appear over several rounds, so it is exercised here without touching the API.
"""

import types as pytypes

import pytest

from skylark import agent, monday_client
from tests.test_analytics import DEAL_ROWS, WO_ROWS


@pytest.fixture(autouse=True)
def stub_monday(monkeypatch):
    monkeypatch.setattr(
        monday_client, "fetch_board",
        lambda board, use_cache=True: DEAL_ROWS if board == "deals" else WO_ROWS,
    )


class FakeCall:
    def __init__(self, name, args):
        self.name, self.args = name, args


class FakePart:
    def __init__(self, text=None, function_call=None, thought=False):
        self.text, self.function_call, self.thought = text, function_call, thought


def chunk(*parts):
    content = pytypes.SimpleNamespace(parts=list(parts))
    return pytypes.SimpleNamespace(candidates=[pytypes.SimpleNamespace(content=content)])


class FakeModels:
    """Replays a scripted sequence of turns and records every request."""

    def __init__(self, turns, no_tool_reply="Final answer from cached data."):
        self.turns, self.no_tool_reply = list(turns), no_tool_reply
        self.calls_with_tools = 0
        self.calls_without_tools = 0

    def generate_content_stream(self, model, contents, config):
        if not getattr(config, "tools", None):
            self.calls_without_tools += 1
            return iter([chunk(FakePart(text=self.no_tool_reply))])
        self.calls_with_tools += 1
        turn = self.turns[min(self.calls_with_tools - 1, len(self.turns) - 1)]
        return iter([chunk(*turn)])


def build(monkeypatch, models):
    monkeypatch.setattr(
        agent.genai, "Client", lambda **kw: pytypes.SimpleNamespace(models=models)
    )
    return agent.BIAgent(api_key="fake")


def texts(events):
    return "".join(e["text"] for e in events if e["type"] == "text")


def test_a_plain_answer_ends_the_loop_in_one_round(monkeypatch):
    models = FakeModels([[FakePart(text="49 open deals.")]])
    events = list(build(monkeypatch, models).ask("how many open deals?"))
    assert texts(events) == "49 open deals."
    assert models.calls_with_tools == 1
    assert models.calls_without_tools == 0


def test_thinking_parts_are_not_shown_to_the_user(monkeypatch):
    models = FakeModels([[FakePart(text="internal", thought=True), FakePart(text="Answer.")]])
    assert texts(list(build(monkeypatch, models).ask("q"))) == "Answer."


def test_a_tool_call_is_executed_and_fed_back(monkeypatch):
    call = FakeCall("query_board", {"dataset": "deals", "aggregations": [{"column": "*", "func": "count"}]})
    models = FakeModels([[FakePart(function_call=call)], [FakePart(text="3 deals.")]])
    events = list(build(monkeypatch, models).ask("count deals"))
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["output"]["results"] == [{"count": 3}]
    assert texts(events) == "3 deals."


def test_exhausting_the_round_budget_still_produces_an_answer(monkeypatch):
    # Regression: the loop used to give up with "stopped after too many tool
    # rounds", so a dozen paid requests returned nothing the user could read.
    call = FakeCall("query_board", {"dataset": "deals"})
    models = FakeModels([[FakePart(function_call=call)]])  # never stops calling
    events = list(build(monkeypatch, models).ask("something broad"))
    assert models.calls_with_tools == agent.MAX_TOOL_ROUNDS
    assert models.calls_without_tools == 1  # the forced finish
    assert "Final answer from cached data." in texts(events)
    assert "too many tool rounds" not in texts(events)


def test_withdrawing_the_tools_is_what_ends_the_loop(monkeypatch):
    call = FakeCall("query_board", {"dataset": "deals"})
    models = FakeModels([[FakePart(function_call=call)]])
    seen = {}
    original = models.generate_content_stream

    def spy(model, contents, config):
        seen["last_had_tools"] = bool(getattr(config, "tools", None))
        return original(model, contents, config)

    models.generate_content_stream = spy
    list(build(monkeypatch, models).ask("q"))
    assert seen["last_had_tools"] is False


def test_persistent_tool_failure_surfaces_the_real_cause(monkeypatch):
    # A bad monday token used to hide behind "try a narrower question".
    def boom(board, use_cache=True):
        raise monday_client.MondayError("monday.com rejected the API token (401).")

    monkeypatch.setattr(monday_client, "fetch_board", boom)
    call = FakeCall("query_board", {"dataset": "deals"})
    models = FakeModels([[FakePart(function_call=call)]], no_tool_reply="The board rejected the token.")
    a = build(monkeypatch, models)
    list(a.ask("how's pipeline?"))
    nudge = a.contents[-2].parts[0].text  # the instruction before the final reply
    assert "Every data lookup failed" in nudge
    assert "401" in nudge


def test_partial_success_asks_for_a_best_effort_answer_not_an_error(monkeypatch):
    call = FakeCall("query_board", {"dataset": "deals"})
    models = FakeModels([[FakePart(function_call=call)]])
    a = build(monkeypatch, models)
    list(a.ask("q"))
    nudge = a.contents[-2].parts[0].text
    assert "used the whole tool budget" in nudge
    assert "Every data lookup failed" not in nudge


def test_a_failure_in_the_forced_finish_is_reported_not_raised(monkeypatch):
    call = FakeCall("query_board", {"dataset": "deals"})
    models = FakeModels([[FakePart(function_call=call)]])
    original = models.generate_content_stream

    def flaky(model, contents, config):
        if not getattr(config, "tools", None):
            raise RuntimeError("network gone")
        return original(model, contents, config)

    models.generate_content_stream = flaky
    events = list(build(monkeypatch, models).ask("q"))
    assert "Could not finish the answer" in texts(events)


def test_the_forced_answer_is_kept_in_history_for_follow_ups(monkeypatch):
    call = FakeCall("query_board", {"dataset": "deals"})
    models = FakeModels([[FakePart(function_call=call)]])
    a = build(monkeypatch, models)
    list(a.ask("q"))
    assert a.contents[-1].role == "model"
    assert a.contents[-1].parts[0].text == "Final answer from cached data."


# --- transient API failures -------------------------------------------------


def api_error(code, message="boom"):
    from google.genai import errors

    cls = errors.ClientError if code < 500 else errors.ServerError
    return cls(code, {"error": {"code": code, "message": message}})


class FlakyModels(FakeModels):
    """Raises the given errors on the first calls, then behaves normally."""

    def __init__(self, errors_to_raise, turns, mid_stream=False):
        super().__init__(turns)
        self.to_raise = list(errors_to_raise)
        self.mid_stream = mid_stream

    def generate_content_stream(self, model, contents, config):
        if self.to_raise:
            exc = self.to_raise.pop(0)
            if not self.mid_stream:
                raise exc

            def partial():
                yield chunk(FakePart(text="partial "))
                raise exc

            return partial()
        return super().generate_content_stream(model, contents, config)


def test_a_503_is_retried_rather_than_shown_to_the_user(monkeypatch):
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    models = FlakyModels([api_error(503, "high demand")], [[FakePart(text="Recovered.")]])
    events = list(build(monkeypatch, models).ask("q"))
    assert "Recovered." in texts(events)
    assert "The model is busy" in texts(events)


def test_a_429_reports_rate_limiting_not_a_server_error(monkeypatch):
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    models = FlakyModels([api_error(429, "quota")], [[FakePart(text="Recovered.")]])
    assert "Rate limited" in texts(list(build(monkeypatch, models).ask("q")))


def test_retries_are_bounded_and_the_error_surfaces(monkeypatch):
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    from google.genai import errors

    models = FlakyModels([api_error(503) for _ in range(agent.STREAM_RETRIES)], [[FakePart(text="never")]])
    with pytest.raises(errors.ServerError):
        list(build(monkeypatch, models).ask("q"))


def test_a_non_retryable_error_is_raised_immediately(monkeypatch):
    from google.genai import errors

    models = FlakyModels([api_error(400, "bad request")], [[FakePart(text="never")]])
    with pytest.raises(errors.ClientError):
        list(build(monkeypatch, models).ask("q"))
    assert models.calls_with_tools == 0  # no retry was attempted


def test_a_failure_after_streaming_starts_is_not_retried(monkeypatch):
    # Retrying here would repeat text the user has already seen.
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    from google.genai import errors

    models = FlakyModels([api_error(503)], [[FakePart(text="x")]], mid_stream=True)
    with pytest.raises(errors.ServerError):
        list(build(monkeypatch, models).ask("q"))


def test_backoff_grows_and_is_capped():
    assert agent._backoff_seconds(0) == 2.0
    assert agent._backoff_seconds(1) == 6.0
    assert agent._backoff_seconds(9) == 30.0
