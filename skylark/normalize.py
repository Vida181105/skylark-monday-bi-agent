"""Cleaning rules for the two monday.com boards.

Every query runs through here so the rules live in one place. Nulls are
preserved rather than imputed: they are usually semantic (a deal that has not
closed has no close date).
"""

from __future__ import annotations

import re
import warnings
from typing import Any, Iterable

import pandas as pd

# --- canonical column names -------------------------------------------------

DEAL_COLUMNS = [
    "Deal Name",
    "Owner code",
    "Client Code",
    "Deal Status",
    "Close Date (A)",
    "Closure Probability",
    "Masked Deal value",
    "Tentative Close Date",
    "Deal Stage",
    "Product deal",
    "Sector/service",
    "Created Date",
]

DEAL_DATE_COLUMNS = ["Close Date (A)", "Tentative Close Date", "Created Date"]
DEAL_NUMERIC_COLUMNS = ["Masked Deal value"]

# Added by monday.com to every board; empty after these imports.
MONDAY_DEFAULT_COLUMNS = ["Person", "Status", "Date"]

WORK_ORDER_DATE_COLUMNS = [
    "Data Delivery Date",
    "Date of PO/LOI",
    "Probable Start Date",
    "Probable End Date",
    "Probable Start/End Date",
    "Last invoice date",
    "Collection Date",
    "Expected Billing Month",
    "Actual Billing Month",
    "Actual Collection Month",
]

# Matched by pattern: the work order board has 38 columns with long,
# inconsistent money and quantity names.
WORK_ORDER_NUMERIC_PATTERNS = [
    r"amount",
    r"value",
    r"billed",
    r"collected",
    r"receivable",
    r"qty",
    r"quantity",
    r"gst",
    r"rupees",
]

# The letter prefix on a Deal Stage is the funnel order; sort on it, not on the
# text after it.
_STAGE_PREFIX = re.compile(r"^\s*([A-Z])(?:\s*,\s*[A-Z])*\s*\.")
_NUMERIC_SUFFIX = re.compile(r"(\d+)\s*$")
_MONEY_JUNK = re.compile(r"[^0-9eE.\-+]")
_TZ_NAME_SUFFIX = re.compile(r"\s*\([^)]*\)\s*$")
_BLANK_TOKENS = {"", "-", "--", "n/a", "N/A", "NA", "null", "NULL", "None", "nan"}


# --- helpers ----------------------------------------------------------------


def _to_frame(records: Iterable[dict[str, Any]] | pd.DataFrame) -> pd.DataFrame:
    if isinstance(records, pd.DataFrame):
        return records.copy()
    return pd.DataFrame(list(records))


def _blank_to_na(df: pd.DataFrame) -> pd.DataFrame:
    """monday.com returns unset cells as empty strings; treat them as null."""
    for col in df.columns:
        # pandas 2 reports these as `object`, pandas 3 as `str`.
        if not (pd.api.types.is_object_dtype(df[col]) or pd.api.types.is_string_dtype(df[col])):
            continue
        df[col] = (
            df[col]
            .astype(object)
            .apply(
                lambda v: pd.NA
                if v is None
                or (isinstance(v, str) and v.strip() in _BLANK_TOKENS)
                else (v.strip() if isinstance(v, str) else v)
            )
        )
    return df


def drop_header_echo_rows(df: pd.DataFrame, min_matches: int = 2) -> tuple[pd.DataFrame, int]:
    """Drop rows that are a header row re-injected as data.

    Such a row has its cells equal to the column names themselves. Requiring
    ``min_matches`` echoes avoids dropping a legitimate row that merely contains
    one such string. Returns the frame and the number of rows dropped.
    """
    if df.empty:
        return df, 0

    def echo_count(row: pd.Series) -> int:
        return sum(
            1
            for col in df.columns
            if isinstance(row[col], str) and row[col].strip().casefold() == str(col).strip().casefold()
        )

    mask = df.apply(echo_count, axis=1) >= min_matches
    return df.loc[~mask].copy(), int(mask.sum())


def realign_header_row(rows: list[list[Any]]) -> pd.DataFrame:
    """Build a frame from raw sheet rows, picking the real header row.

    The work order sheet's header is its second row, under a banner row. The
    row with the most distinct non-empty cells wins. Only needed for raw file
    reads; the API returns column titles directly.
    """
    if not rows:
        return pd.DataFrame()
    scan = min(len(rows), 5)
    best_idx, best_score = 0, -1
    for i in range(scan):
        cells = [str(c).strip() for c in rows[i] if c is not None and str(c).strip() != ""]
        score = len(set(cells))
        if score > best_score:
            best_idx, best_score = i, score
    header = [str(c).strip() for c in rows[best_idx]]
    return pd.DataFrame(rows[best_idx + 1 :], columns=header)


def coerce_dates(df: pd.DataFrame, columns: Iterable[str]) -> pd.DataFrame:
    """Parse date columns, coercing unparseable values to NaT.

    monday.com serves dates as JavaScript ``Date.toString()`` values, e.g.
    ``"Fri Dec 26 2025 00:00:00 GMT+0000 (Coordinated Universal Time)"``.
    Pandas' mixed-format inference turns those into NaT, so the trailing
    timezone name is stripped first. Results are naive UTC so they compare
    against plain ``YYYY-MM-DD`` operands.
    """
    for col in columns:
        if col not in df.columns:
            continue
        text = df[col].astype(object).apply(
            lambda v: _TZ_NAME_SUFFIX.sub("", v).strip() if isinstance(v, str) else v
        )
        with warnings.catch_warnings():
            # Mixed date formats are expected here; per-element parsing is the point.
            warnings.simplefilter("ignore", UserWarning)
            parsed = pd.to_datetime(text, errors="coerce", utc=True)
        df[col] = parsed.dt.tz_localize(None)
    return df


def coerce_numeric(df: pd.DataFrame, columns: Iterable[str]) -> pd.DataFrame:
    """Parse money and quantity columns, stripping symbols, commas and stray text.

    Negatives are preserved: a negative 'Amount to be billed' is a real
    over-billing or timing artifact, not an error.
    """
    for col in columns:
        if col not in df.columns:
            continue
        df[col] = pd.to_numeric(
            df[col].apply(
                lambda v: _MONEY_JUNK.sub("", v) if isinstance(v, str) else v
            ),
            errors="coerce",
        )
    return df


def stage_order(stage: Any) -> float:
    """Funnel position from a Deal Stage's letter prefix: 'A.' -> 1, 'G.' -> 7.

    Unprefixed or blank stages sort last.
    """
    if not isinstance(stage, str):
        return float("nan")
    m = _STAGE_PREFIX.match(stage)
    if not m:
        return float("nan")
    return float(ord(m.group(1).upper()) - ord("A") + 1)


def join_key(code: Any) -> Any:
    """Cross-board join key: the numeric suffix of a client/customer code.

    Deals carry ``COMPANY###``, work orders ``WOCOMPANY_###``; only the numeric
    suffix is shared.
    """
    if code is None or (isinstance(code, float) and pd.isna(code)):
        return pd.NA
    m = _NUMERIC_SUFFIX.search(str(code))
    return str(int(m.group(1))) if m else pd.NA


def numeric_columns_for(df: pd.DataFrame, patterns: Iterable[str]) -> list[str]:
    return [
        c
        for c in df.columns
        if any(re.search(p, str(c), re.IGNORECASE) for p in patterns)
    ]


def drop_empty_foreign_columns(df: pd.DataFrame, known: Iterable[str]) -> pd.DataFrame:
    """Drop columns that are entirely empty and foreign to the board's schema.

    The live Deals board carries an empty copy of the work order column set, so
    a blank ``Sector`` sits beside the real ``Sector/service``. A board's own
    columns are kept however sparse.
    """
    known = set(known)
    dead = [c for c in df.columns if c not in known and not str(c).startswith("_") and df[c].isna().all()]
    return df.drop(columns=dead)


# --- board normalizers ------------------------------------------------------


def normalize_deals(records: Iterable[dict[str, Any]] | pd.DataFrame) -> pd.DataFrame:
    """Clean the Deal tracker board into an analysis-ready frame."""
    df = _to_frame(records)
    if df.empty:
        return pd.DataFrame(columns=DEAL_COLUMNS + ["join_key", "stage_order", "is_won", "is_open"])

    df, _ = drop_header_echo_rows(df)
    # Canonical rule, in case a corrupted row echoed only one column name.
    for col in ("Deal Status", "Deal Stage"):
        if col in df.columns:
            df = df[~(df[col].astype(str).str.strip().str.casefold() == col.casefold())]

    df = _blank_to_na(df)
    df = coerce_dates(df, DEAL_DATE_COLUMNS)
    df = coerce_numeric(df, DEAL_NUMERIC_COLUMNS)

    for col in ("Deal Status", "Deal Stage", "Sector/service", "Closure Probability"):
        if col in df.columns:
            df[col] = df[col].apply(lambda v: v.strip() if isinstance(v, str) else v)

    if "Deal Stage" in df.columns:
        df["stage_order"] = df["Deal Stage"].apply(stage_order)
    if "Client Code" in df.columns:
        df["join_key"] = df["Client Code"].apply(join_key)
    df = drop_empty_foreign_columns(df, DEAL_COLUMNS)

    if "Deal Status" in df.columns:
        status = df["Deal Status"].astype("string").str.casefold()
        df["is_won"] = status.eq("won").fillna(False)
        df["is_open"] = status.eq("open").fillna(False)

    return df.reset_index(drop=True)


def normalize_work_orders(records: Iterable[dict[str, Any]] | pd.DataFrame) -> pd.DataFrame:
    """Clean the Work order tracker board into an analysis-ready frame."""
    df = _to_frame(records)
    if df.empty:
        return pd.DataFrame(columns=["Serial #", "join_key"])

    df, _ = drop_header_echo_rows(df)
    df = _blank_to_na(df)
    df = coerce_dates(df, WORK_ORDER_DATE_COLUMNS)
    df = coerce_numeric(df, numeric_columns_for(df, WORK_ORDER_NUMERIC_PATTERNS))

    df = df.drop(columns=[c for c in MONDAY_DEFAULT_COLUMNS if c in df.columns])

    if "Customer Name Code" in df.columns:
        df["join_key"] = df["Customer Name Code"].apply(join_key)
    if "Serial #" in df.columns:
        df["Serial #"] = df["Serial #"].apply(lambda v: v.strip() if isinstance(v, str) else v)

    return df.reset_index(drop=True)


def join_boards(deals: pd.DataFrame, work_orders: pd.DataFrame) -> pd.DataFrame:
    """Left-outer join work orders onto deals on the numeric customer suffix.

    Left-outer because a deal that never converted to executed work is still a
    valid pipeline row.
    """
    if deals.empty or work_orders.empty:
        return deals.copy()
    left = deals.copy()
    right = work_orders.copy()
    overlap = (set(left.columns) & set(right.columns)) - {"join_key"}
    right = right.rename(columns={c: f"WO: {c}" for c in overlap})
    return left.merge(right, on="join_key", how="left", suffixes=("", "_wo"))


def data_quality_report(df: pd.DataFrame, label: str) -> dict[str, Any]:
    """Null coverage stats used to caveat an answer."""
    total = len(df)
    nulls = {
        str(c): {
            "null_count": int(df[c].isna().sum()),
            "null_pct": round(float(df[c].isna().mean()) * 100, 1) if total else 0.0,
        }
        for c in df.columns
    }
    return {"board": label, "row_count": total, "columns": nulls}
