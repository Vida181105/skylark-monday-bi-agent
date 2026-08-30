"""Tests for the normalization layer."""

import pandas as pd
import pytest

from skylark import normalize as N


def _deal(**over):
    row = {
        "Deal Name": "Deal 1",
        "Owner code": "OWNER_001",
        "Client Code": "COMPANY017",
        "Deal Status": "Open",
        "Close Date (A)": None,
        "Closure Probability": "High",
        "Masked Deal value": "1,200.50",
        "Tentative Close Date": "2025-09-30",
        "Deal Stage": "C. Proposal Sent",
        "Product deal": "Dock + DMO",
        "Sector/service": "Mining",
        "Created Date": "2025-01-15",
    }
    row.update(over)
    return row


CORRUPT_ROWS = [
    {c: c for c in N.DEAL_COLUMNS} | {"Deal Name": "Nezuko"},
    {c: c for c in N.DEAL_COLUMNS} | {"Deal Name": "Bugs Bunny"},
]


def test_drops_both_corrupted_header_rows():
    df = N.normalize_deals([_deal(), *CORRUPT_ROWS, _deal(**{"Deal Name": "Deal 2"})])
    assert len(df) == 2
    assert set(df["Deal Name"]) == {"Deal 1", "Deal 2"}


def test_keeps_row_that_merely_mentions_a_column_name():
    # One echoed field is not enough to call a row corrupted.
    legit = _deal(**{"Deal Name": "Deal Stage", "Product deal": "Spectra"})
    df = N.normalize_deals([legit, *CORRUPT_ROWS])
    assert len(df) == 1
    assert df.loc[0, "Deal Name"] == "Deal Stage"


def test_corrupted_rows_do_not_poison_dtypes():
    df = N.normalize_deals([_deal(), *CORRUPT_ROWS])
    assert pd.api.types.is_numeric_dtype(df["Masked Deal value"])
    assert pd.api.types.is_datetime64_any_dtype(df["Created Date"])
    assert df.loc[0, "Masked Deal value"] == pytest.approx(1200.50)


def test_null_dates_and_probabilities_are_preserved_not_imputed():
    df = N.normalize_deals([_deal(**{"Close Date (A)": None, "Closure Probability": ""})])
    assert pd.isna(df.loc[0, "Close Date (A)"])
    assert pd.isna(df.loc[0, "Closure Probability"])


def test_unparseable_date_coerces_to_nat_instead_of_raising():
    df = N.normalize_deals([_deal(**{"Tentative Close Date": "45231.0"}), _deal()])
    assert len(df) == 2


def test_stage_order_follows_letter_prefix_not_alphabetical_text():
    stages = ["G. Project Won", "A. Lead Generated", "C. Proposal Sent"]
    ordered = sorted(stages, key=N.stage_order)
    assert ordered == ["A. Lead Generated", "C. Proposal Sent", "G. Project Won"]
    # Alphabetising the text after the letter gives the wrong order.
    assert sorted(stages, key=lambda s: s.split(". ", 1)[1]) != ordered
    assert pd.isna(N.stage_order("Not relevant"))
    assert N.stage_order("N,O. Not relevant") == 14.0


# --- work order header realignment -----------------------------------------

RAW_SHEET = [
    ["Work Order Tracker", None, None, None],           # banner row
    ["Serial #", "Customer Name Code", "Sector", "Amount in Rupees (Excl of GST)"],
    ["SDPLDEAL-075", "WOCOMPANY_017", "Mining", "1,00,000"],
    ["SDPLDEAL-076", "WOCOMPANY_004", "Railways", "-2500"],
]


def test_real_header_is_second_row_of_sheet():
    df = N.realign_header_row(RAW_SHEET)
    assert list(df.columns) == RAW_SHEET[1]
    assert len(df) == 2
    assert df.iloc[0]["Serial #"] == "SDPLDEAL-075"


def test_negative_money_is_surfaced_not_clamped():
    df = N.normalize_work_orders(N.realign_header_row(RAW_SHEET))
    assert df.loc[1, "Amount in Rupees (Excl of GST)"] == -2500


def test_work_order_serial_is_a_safe_primary_key():
    df = N.normalize_work_orders(N.realign_header_row(RAW_SHEET))
    assert df["Serial #"].is_unique


# --- cross-board join -------------------------------------------------------


def test_join_key_is_numeric_suffix_only():
    assert N.join_key("COMPANY017") == N.join_key("WOCOMPANY_017") == "17"
    assert N.join_key("COMPANY7") == N.join_key("WOCOMPANY_007") == "7"
    assert pd.isna(N.join_key(None))
    assert pd.isna(N.join_key("NOCODE"))


def test_join_is_left_outer_and_keeps_unmatched_deals():
    deals = N.normalize_deals([
        _deal(**{"Deal Name": "Matched", "Client Code": "COMPANY017"}),
        _deal(**{"Deal Name": "Unconverted", "Client Code": "COMPANY999"}),
    ])
    wos = N.normalize_work_orders(N.realign_header_row(RAW_SHEET))
    joined = N.join_boards(deals, wos)
    assert set(joined["Deal Name"]) == {"Matched", "Unconverted"}
    matched = joined[joined["Deal Name"] == "Matched"].iloc[0]
    assert matched["Serial #"] == "SDPLDEAL-075"
    assert pd.isna(joined[joined["Deal Name"] == "Unconverted"].iloc[0]["Serial #"])


def test_join_disambiguates_columns_present_on_both_boards():
    deals = N.normalize_deals([_deal(**{"Sector/service": "Mining"})])
    wos = N.normalize_work_orders(N.realign_header_row(RAW_SHEET))
    joined = N.join_boards(deals, wos)
    assert "WO: Sector" in joined.columns or "Sector" in joined.columns


def test_data_quality_report_flags_sparse_columns():
    df = N.normalize_deals([_deal(), _deal(**{"Closure Probability": None})])
    rep = N.data_quality_report(df, "deals")
    assert rep["row_count"] == 2
    assert rep["columns"]["Closure Probability"]["null_pct"] == 50.0


# --- live monday.com quirks -------------------------------------------------

JS_DATE = "Fri Dec 26 2025 00:00:00 GMT+0000 (Coordinated Universal Time)"


def test_monday_javascript_date_strings_parse():
    # The API serves dates as JS Date.toString(); mixed-format inference turns
    # these into NaT and empties the column.
    df = N.normalize_deals([_deal(**{"Created Date": JS_DATE, "Tentative Close Date": JS_DATE})])
    assert df.loc[0, "Created Date"] == pd.Timestamp("2025-12-26")
    assert df.loc[0, "Tentative Close Date"] == pd.Timestamp("2025-12-26")


def test_parsed_dates_are_naive_so_plain_date_filters_compare():
    df = N.normalize_deals([_deal(**{"Created Date": JS_DATE})])
    assert df["Created Date"].dt.tz is None
    assert df.loc[0, "Created Date"] >= pd.Timestamp("2025-04-01")


def test_empty_foreign_columns_are_pruned_from_deals():
    # The live Deals board carries an empty copy of the work-order schema.
    rows = [_deal(**{"Sector": "", "Amount Receivable (Masked)": "", "Person": "", "Date": ""})]
    df = N.normalize_deals(rows)
    assert "Sector" not in df.columns
    assert "Amount Receivable (Masked)" not in df.columns
    assert "Person" not in df.columns
    assert "Sector/service" in df.columns


def test_a_populated_foreign_column_is_kept():
    rows = [_deal(**{"Extra note": "real value"}), _deal(**{"Extra note": ""})]
    df = N.normalize_deals(rows)
    assert "Extra note" in df.columns


def test_sparse_but_real_deal_column_is_never_pruned():
    # ~92% null by design; must survive an entirely empty slice.
    df = N.normalize_deals([_deal(**{"Close Date (A)": None})])
    assert "Close Date (A)" in df.columns


def test_work_order_lifecycle_nulls_survive_pruning():
    rows = [{"Serial #": "SDPLDEAL-001", "Customer Name Code": "WOCOMPANY_017",
             "Collection status": "", "Collection Date": "", "Person": "", "Status": ""}]
    df = N.normalize_work_orders(rows)
    assert "Collection status" in df.columns
    assert "Person" not in df.columns
