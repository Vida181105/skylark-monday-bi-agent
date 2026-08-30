"""Query-engine tests. monday.com is stubbed, so these run offline."""

import pytest

from skylark import analytics, monday_client

DEAL_ROWS = [
    {"Deal Name": "A", "Owner code": "OWNER_001", "Client Code": "COMPANY017", "Deal Status": "Open",
     "Deal Stage": "C. Proposal Sent", "Masked Deal value": "100", "Sector/service": "Mining",
     "Closure Probability": "High", "Created Date": "2025-05-10", "Close Date (A)": None},
    {"Deal Name": "B", "Owner code": "OWNER_001", "Client Code": "COMPANY004", "Deal Status": "Won",
     "Deal Stage": "G. Project Won", "Masked Deal value": "300", "Sector/service": "Mining",
     "Closure Probability": None, "Created Date": "2025-08-01", "Close Date (A)": "2025-09-02"},
    {"Deal Name": "C", "Owner code": "OWNER_002", "Client Code": "COMPANY099", "Deal Status": "Open",
     "Deal Stage": "A. Lead Generated", "Masked Deal value": None, "Sector/service": "Railways",
     "Closure Probability": None, "Created Date": "2025-11-20", "Close Date (A)": None},
    # corrupted header re-injection row
    {"Deal Name": "Nezuko", "Owner code": "Owner code", "Client Code": "Client Code",
     "Deal Status": "Deal Status", "Deal Stage": "Deal Stage", "Masked Deal value": "Masked Deal value",
     "Sector/service": "Sector/service", "Closure Probability": "Closure Probability",
     "Created Date": "Created Date", "Close Date (A)": "Close Date (A)"},
]

WO_ROWS = [
    {"Serial #": "SDPLDEAL-001", "Customer Name Code": "WOCOMPANY_017", "Sector": "Mining",
     "Execution Status": "Completed", "Amount Receivable": "500", "Date of PO/LOI": "2025-06-01"},
    {"Serial #": "SDPLDEAL-002", "Customer Name Code": "WOCOMPANY_017", "Sector": "Mining",
     "Execution Status": "In Progress", "Amount Receivable": "-50", "Date of PO/LOI": "2025-07-15"},
    {"Serial #": "SDPLDEAL-003", "Customer Name Code": "WOCOMPANY_555", "Sector": "Railways",
     "Execution Status": "In Progress", "Amount Receivable": None, "Date of PO/LOI": None},
]


@pytest.fixture(autouse=True)
def stub_monday(monkeypatch):
    monkeypatch.setattr(
        monday_client, "fetch_board",
        lambda board, use_cache=True: DEAL_ROWS if board == "deals" else WO_ROWS,
    )


def test_corrupted_row_never_reaches_a_query():
    out = analytics.query("deals", group_by=["Deal Status"], aggregations=[{"column": "*", "func": "count"}])
    assert out["rows_in_dataset"] == 3
    assert {r["Deal Status"] for r in out["results"]} == {"Open", "Won"}


def test_filter_is_case_insensitive_on_text():
    out = analytics.query("deals", filters=[{"column": "Deal Status", "op": "eq", "value": "open"}])
    assert out["rows_matching_filters"] == 2


def test_group_and_sum_ignores_nulls_but_reports_them():
    out = analytics.query(
        "deals",
        filters=[{"column": "Deal Status", "op": "eq", "value": "Open"}],
        group_by=["Sector/service"],
        aggregations=[{"column": "Masked Deal value", "func": "sum"}, {"column": "*", "func": "count"}],
    )
    by_sector = {r["Sector/service"]: r for r in out["results"]}
    assert by_sector["Mining"]["sum_Masked Deal value"] == 100
    # The Railways deal has no value; the caveat is surfaced.
    assert out["data_quality"]["Masked Deal value"]["null_pct"] == 50.0


def test_date_between_and_quarter_bucketing():
    out = analytics.query(
        "deals",
        filters=[{"column": "Created Date", "op": "between", "value": ["2025-04-01", "2026-03-31"]}],
        group_by=["Created Date:quarter"],
        aggregations=[{"column": "*", "func": "count"}],
    )
    assert out["rows_matching_filters"] == 3
    assert {r["Created Date:quarter"] for r in out["results"]} == {"2025-Q2", "2025-Q3", "2025-Q4"}


def test_null_grouping_is_labelled_not_dropped():
    out = analytics.query("deals", group_by=["Closure Probability"], aggregations=[{"column": "*", "func": "count"}])
    labels = {r["Closure Probability"]: r["count"] for r in out["results"]}
    assert labels["(not set)"] == 2


def test_joined_dataset_matches_on_numeric_suffix():
    out = analytics.query(
        "joined",
        filters=[{"column": "Deal Name", "op": "eq", "value": "A"}],
        columns=["Deal Name", "Serial #"],
    )
    assert {r["Serial #"] for r in out["results"]} == {"SDPLDEAL-001", "SDPLDEAL-002"}


def test_negative_receivable_survives_to_the_answer():
    out = analytics.query("work_orders", columns=["Serial #", "Amount Receivable"], sort_by="Amount Receivable", ascending=True)
    assert out["results"][0]["Amount Receivable"] == -50


def test_describe_exposes_distinct_categories_and_null_pct():
    d = analytics.describe("deals")
    cols = {c["name"]: c for c in d["columns"]}
    assert d["row_count"] == 3
    assert set(cols["Deal Stage"]["values"]) == {"C. Proposal Sent", "G. Project Won", "A. Lead Generated"}
    assert cols["Close Date (A)"]["type"] == "date"
    assert cols["Closure Probability"]["null_pct"] == pytest.approx(66.7, abs=0.1)


def test_unknown_column_is_a_clear_error_the_model_can_recover_from():
    with pytest.raises(ValueError, match="describe_data"):
        analytics.query("deals", filters=[{"column": "Sector", "op": "eq", "value": "Mining"}])


def test_stage_order_column_is_available_for_funnel_sorting():
    out = analytics.query("deals", columns=["Deal Stage", "stage_order"], sort_by="stage_order", ascending=True)
    assert [r["Deal Stage"] for r in out["results"]][0] == "A. Lead Generated"


def test_aggregation_without_group_by_returns_a_grand_total():
    # Regression: these were silently dropped and raw rows came back instead.
    out = analytics.query(
        "deals",
        filters=[{"column": "Deal Status", "op": "eq", "value": "Open"}],
        aggregations=[{"column": "Masked Deal value", "func": "sum"}, {"column": "*", "func": "count"}],
    )
    assert out["results"] == [{"sum_Masked Deal value": 100.0, "count": 2}]
    # The raw-row path reports `returned` and one dict per matched row, which
    # is easily mistaken for a total.
    assert "returned" not in out
    assert len(out["results"]) == 1


def test_unknown_aggregation_column_errors_instead_of_returning_rows():
    with pytest.raises(ValueError, match="Amount Payable"):
        analytics.query("work_orders", aggregations=[{"column": "Amount Payable / Deal Value", "func": "sum"}])


def test_unknown_aggregation_column_errors_when_grouping_too():
    with pytest.raises(ValueError, match="describe_data"):
        analytics.query("deals", group_by=["Sector/service"], aggregations=[{"column": "Nope", "func": "sum"}])


def test_a_long_row_listing_warns_against_hand_tallying(monkeypatch):
    # The model counted 48 returned rows by eye and got every stage wrong, so
    # the warning rides along with the data it applies to. The stub boards are
    # small, so the threshold is lowered rather than the fixtures inflated.
    monkeypatch.setattr(analytics, "ROW_TALLY_WARNING_THRESHOLD", 2)
    out = analytics.query("work_orders", columns=["Serial #"], limit=200)
    small = analytics.query("work_orders", columns=["Serial #"], limit=2)
    assert out["returned"] == 3
    assert "not an aggregate" in out["note"]
    assert "note" not in small


def test_aggregate_results_carry_no_such_warning():
    out = analytics.query("work_orders", group_by=["Sector"], aggregations=[{"column": "*", "func": "count"}])
    assert "note" not in out


def test_grouped_results_carry_the_overall_total():
    # The model added up its own group rows for the headline and got a total
    # 710k off. The engine now supplies it.
    out = analytics.query(
        "deals",
        group_by=["Sector/service"],
        aggregations=[{"column": "Masked Deal value", "func": "sum"}, {"column": "*", "func": "count"}],
    )
    assert out["totals"] == {"sum_Masked Deal value": 400.0, "count": 3}
    assert sum(r["sum_Masked Deal value"] or 0 for r in out["results"]) == out["totals"]["sum_Masked Deal value"]


def test_totals_cover_every_matched_row_not_just_the_returned_page():
    # With limit truncating the group list, the total must still be complete.
    out = analytics.query(
        "deals",
        group_by=["Deal Name"],
        aggregations=[{"column": "Masked Deal value", "func": "sum"}],
        limit=1,
    )
    assert len(out["results"]) == 1
    assert out["group_count"] == 3
    assert out["totals"]["sum_Masked Deal value"] == 400.0


def test_totals_respect_the_filters():
    out = analytics.query(
        "deals",
        filters=[{"column": "Deal Status", "op": "eq", "value": "Open"}],
        group_by=["Sector/service"],
        aggregations=[{"column": "Masked Deal value", "func": "sum"}],
    )
    assert out["totals"]["sum_Masked Deal value"] == 100.0


def test_sort_direction_is_echoed_so_a_wrong_default_is_visible():
    # Asked for the oldest open deal, the model sorted without `ascending` and
    # named the newest one. The applied order now comes back with the rows.
    newest = analytics.query("deals", columns=["Deal Name", "Created Date"], sort_by="Created Date")
    oldest = analytics.query(
        "deals", columns=["Deal Name", "Created Date"], sort_by="Created Date", ascending=True
    )
    assert newest["sorted"]["order"] == "descending"
    assert "largest/newest" in newest["sorted"]["first_row_is"]
    assert oldest["sorted"]["order"] == "ascending"
    assert newest["results"][0]["Created Date"] > oldest["results"][0]["Created Date"]


def test_ascending_actually_returns_the_earliest_record():
    out = analytics.query(
        "deals", columns=["Deal Name", "Created Date"], sort_by="Created Date", ascending=True
    )
    assert out["results"][0]["Deal Name"] == "A"  # created 2025-05-10, the earliest stub row


def test_grouped_results_echo_their_sort_too():
    out = analytics.query(
        "deals", group_by=["Sector/service"],
        aggregations=[{"column": "Masked Deal value", "func": "sum"}],
        sort_by="sum_Masked Deal value", ascending=True,
    )
    assert out["sorted"]["order"] == "ascending"


def test_unsorted_queries_carry_no_sort_block():
    assert "sorted" not in analytics.query("deals", columns=["Deal Name"])
