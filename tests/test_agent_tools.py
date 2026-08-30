"""Tests for the seam between Gemini's function-call arguments and the query
engine. The model itself is not called."""

import pytest

from skylark import agent, monday_client
from tests.test_analytics import DEAL_ROWS, WO_ROWS


@pytest.fixture(autouse=True)
def stub_monday(monkeypatch):
    monkeypatch.setattr(
        monday_client, "fetch_board",
        lambda board, use_cache=True: DEAL_ROWS if board == "deals" else WO_ROWS,
    )


def test_gemini_split_operands_fold_back_into_one_value():
    # Gemini cannot express a union-typed operand, so lists arrive in `values`.
    out = agent._normalize_filters(
        {"filters": [
            {"column": "Created Date", "op": "between", "values": ["2025-04-01", "2026-03-31"]},
            {"column": "Deal Status", "op": "in", "values": ["Open", "Won"]},
            {"column": "Sector/service", "op": "eq", "value": "Mining"},
        ]}
    )
    assert out["filters"][0]["value"] == ["2025-04-01", "2026-03-31"]
    assert out["filters"][1]["value"] == ["Open", "Won"]
    assert out["filters"][2]["value"] == "Mining"
    assert all("values" not in f for f in out["filters"])


def test_between_from_values_actually_filters():
    res = agent._run_tool("query_board", {
        "dataset": "deals",
        "filters": [{"column": "Created Date", "op": "between", "values": ["2025-07-01", "2025-09-30"]}],
    })
    assert res["rows_matching_filters"] == 1
    assert res["results"][0]["Deal Name"] == "B"


def test_null_valued_optional_args_are_dropped():
    # Unset optional parameters arrive as null and must not reach pandas.
    res = agent._run_tool("query_board", {
        "dataset": "deals", "filters": None, "group_by": None,
        "aggregations": None, "columns": None, "sort_by": None, "limit": None,
    })
    assert res["rows_in_dataset"] == 3


def test_limit_is_capped():
    res = agent._run_tool("query_board", {"dataset": "work_orders", "limit": 5000})
    assert res["returned"] <= 200


def test_tool_error_comes_back_as_a_message_not_an_exception():
    res = agent._run_tool("query_board", {"dataset": "deals", "filters": [{"column": "Nope", "op": "eq", "value": "x"}]})
    assert "error" in res and "Nope" in res["error"]


def test_unknown_tool_name_is_reported():
    assert "error" in agent._run_tool("drop_board", {})


def test_system_prompt_states_the_fiscal_year_assumption():
    import datetime as dt
    p = agent.system_prompt(dt.date(2026, 1, 15))
    assert "2025-04-01" in p  # FY starting April of the prior calendar year
    assert "Indian FY" in p


def test_oversized_results_are_row_capped_not_field_stripped():
    big = {"dataset": "deals", "data_quality": {"Masked Deal value": {"null_pct": 5.0}},
           "results": [{"Deal Name": "x" * 300} for _ in range(300)]}
    out = agent._cap_result(big)
    assert len(out["results"]) < 300
    assert "300 rows" in out["results_truncated"]
    assert out["data_quality"] == big["data_quality"]  # the caveat block survives


def test_small_results_are_untouched():
    small = {"dataset": "deals", "results": [{"a": 1}]}
    assert agent._cap_result(small) == small


def test_retry_delay_read_from_the_429_payload():
    assert agent._retry_delay_seconds(Exception("{'retryDelay': '27s'}")) == 28.0
    assert agent._retry_delay_seconds(Exception("Please retry in 5.5s.")) == 6.5
    assert agent._retry_delay_seconds(Exception("no delay here")) == 30.0
    assert agent._retry_delay_seconds(Exception("retryDelay: '600s'")) == 90.0  # clamped


def test_default_row_limit_is_modest_to_protect_the_token_budget():
    res = agent._run_tool("query_board", {"dataset": "work_orders"})
    assert res["returned"] <= 25


def test_bare_function_name_aggregation_is_paired_with_columns():
    # Observed live: aggregations=["sum"] with the column in `columns`.
    out = agent._normalize_aggregations(
        {"aggregations": ["sum"], "columns": ["Masked Deal value", "stage_order"]}
    )
    assert out["aggregations"] == [
        {"column": "Masked Deal value", "func": "sum"},
        {"column": "stage_order", "func": "sum"},
    ]
    assert "columns" not in out


def test_bare_function_name_without_columns_counts_rows():
    out = agent._normalize_aggregations({"aggregations": ["count"]})
    assert out["aggregations"] == [{"column": "*", "func": "count"}]


def test_well_formed_aggregations_are_left_alone():
    args = {"aggregations": [{"column": "Masked Deal value", "func": "sum"}], "columns": ["Deal Name"]}
    assert agent._normalize_aggregations(args) == args


def test_the_shorthand_now_runs_end_to_end():
    res = agent._run_tool("query_board", {
        "dataset": "deals", "aggregations": ["sum"], "columns": ["Masked Deal value"],
    })
    assert res["results"] == [{"sum_Masked Deal value": 400.0}]


def test_a_nonsense_aggregation_gives_the_model_a_readable_error():
    res = agent._run_tool("query_board", {"dataset": "deals", "aggregations": [123], "group_by": ["Deal Status"]})
    assert "error" in res and "Unsupported aggregation" in res["error"]


def test_query_engine_rejects_non_object_aggregations_directly():
    # The agent coerces shorthand first; the engine itself refuses to guess.
    from skylark import analytics
    with pytest.raises(ValueError, match="must be an object"):
        analytics.query("deals", aggregations=["sum"])


def test_model_is_resolved_at_call_time_not_import_time(monkeypatch):
    # On Streamlit Cloud, GEMINI_MODEL lands in os.environ only after the module
    # is imported, so resolving it at import would ignore the setting.
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    assert agent.resolve_model() == agent.DEFAULT_MODEL
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.6-flash")
    assert agent.resolve_model() == "gemini-3.6-flash"
    monkeypatch.setenv("GEMINI_MODEL", "   ")
    assert agent.resolve_model() == agent.DEFAULT_MODEL


@pytest.mark.parametrize(
    "today,label,start,end",
    [
        ("2026-08-30", "FY2026-27 Q2", "2026-07-01", "2026-09-30"),
        ("2026-04-01", "FY2026-27 Q1", "2026-04-01", "2026-06-30"),
        ("2026-12-31", "FY2026-27 Q3", "2026-10-01", "2026-12-31"),
        ("2026-02-15", "FY2025-26 Q4", "2026-01-01", "2026-03-31"),  # Q4 falls in the next calendar year
        ("2026-06-30", "FY2026-27 Q1", "2026-04-01", "2026-06-30"),  # last day of a quarter
    ],
)
def test_indian_fiscal_quarter_bounds(today, label, start, end):
    import datetime as dt

    got_label, got_start, got_end = agent.fiscal_quarter(dt.date.fromisoformat(today))
    assert (got_label, got_start.isoformat(), got_end.isoformat()) == (label, start, end)


def test_prompt_states_exact_quarter_bounds_rather_than_leaving_them_implicit():
    # The model mapped a "2026-Q1" group-by label onto "this quarter" and
    # reported deals that close in the past as closing now.
    import datetime as dt

    p = agent.system_prompt(dt.date(2026, 8, 30))
    assert "2026-07-01 to 2026-09-30" in p
    assert "FY2026-27 Q2" in p
    assert "matches zero rows, report zero" in p


def test_prompt_forbids_counting_rows_by_hand():
    import datetime as dt

    p = agent.system_prompt(dt.date(2026, 8, 30))
    assert "Never tally, sum, average or rank rows yourself" in p


def test_prompt_points_at_the_totals_block_for_headline_figures():
    import datetime as dt

    p = agent.system_prompt(dt.date(2026, 8, 30))
    assert "not even by adding up the rows of a grouped result" in p
    assert "`totals` block" in p


def test_prompt_requires_rankings_to_be_sorted_by_the_ranking_metric():
    # It called Construction a top converter at 50% with Railways at 57.7%
    # sitting above it in the same unsorted table.
    import datetime as dt

    p = agent.system_prompt(dt.date(2026, 8, 30))
    assert "sort the table by the metric you are ranking on" in p
    assert "do not call something a \\\nleader when a row above it scores higher" in p or "leader when a row above it scores higher" in p


def test_prompt_requires_disclosing_reuse_of_earlier_figures():
    import datetime as dt

    p = agent.system_prompt(dt.date(2026, 8, 30))
    assert "from the figures pulled" in p


def test_ascending_flag_is_documented_as_a_trap():
    schema = next(t for t in agent.TOOL_DECLARATIONS if t["name"] == "query_board")
    desc = schema["parameters"]["properties"]["ascending"]["description"]
    assert "must set this to true" in desc
    assert "LARGEST or NEWEST" in desc


def test_prompt_covers_oldest_style_questions():
    import datetime as dt

    p = agent.system_prompt(dt.date(2026, 8, 30))
    assert "oldest, earliest, smallest or longest-standing" in p
