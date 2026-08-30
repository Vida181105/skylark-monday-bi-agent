"""Query engine behind the agent's tools.

One filter/group/aggregate primitive over three datasets covers the question
space, and keeps null and join semantics from drifting per question.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from . import monday_client as mc
from . import normalize as N

DATASETS = ("deals", "work_orders", "joined")

# Above this many raw rows, warn the model not to tally them by hand.
ROW_TALLY_WARNING_THRESHOLD = 10

# Columns worth enumerating distinct values for.
_CATEGORICAL_HINTS = (
    "status",
    "stage",
    "sector",
    "probability",
    "owner",
    "nature of work",
    "type of work",
    "document type",
    "service",
)


def load(dataset: str) -> pd.DataFrame:
    """Fetch live board data and return the cleaned frame for a dataset."""
    if dataset == "deals":
        return N.normalize_deals(mc.fetch_board("deals"))
    if dataset == "work_orders":
        return N.normalize_work_orders(mc.fetch_board("work_orders"))
    if dataset == "joined":
        return N.join_boards(
            N.normalize_deals(mc.fetch_board("deals")),
            N.normalize_work_orders(mc.fetch_board("work_orders")),
        )
    raise ValueError(f"Unknown dataset '{dataset}'. Expected one of {DATASETS}.")


# --- filtering --------------------------------------------------------------


def _coerce_operand(series: pd.Series, value: Any) -> Any:
    if pd.api.types.is_datetime64_any_dtype(series):
        return pd.to_datetime(value, errors="coerce")
    if pd.api.types.is_numeric_dtype(series) and isinstance(value, str):
        return pd.to_numeric(value, errors="coerce")
    return value


def _apply_filter(df: pd.DataFrame, f: dict[str, Any]) -> pd.DataFrame:
    col, op = f.get("column"), str(f.get("op", "eq")).lower()
    if col not in df.columns:
        raise ValueError(f"Unknown column '{col}'. Call describe_data for valid column names.")
    s = df[col]
    val = f.get("value")

    if op in {"is_null", "isnull"}:
        return df[s.isna()]
    if op in {"not_null", "notnull"}:
        return df[s.notna()]
    if op == "in":
        vals = [_coerce_operand(s, v) for v in (val if isinstance(val, list) else [val])]
        return df[s.isin(vals)]
    if op == "not_in":
        vals = [_coerce_operand(s, v) for v in (val if isinstance(val, list) else [val])]
        return df[~s.isin(vals)]
    if op == "between":
        lo, hi = (_coerce_operand(s, v) for v in val)
        return df[s.between(lo, hi)]
    if op == "contains":
        return df[s.astype("string").str.contains(str(val), case=False, na=False)]
    if op == "not_contains":
        return df[~s.astype("string").str.contains(str(val), case=False, na=False)]

    v = _coerce_operand(s, val)
    if op == "eq":
        # Case-insensitive so the caller need not guess casing.
        if not pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_datetime64_any_dtype(s):
            return df[s.astype("string").str.casefold() == str(v).casefold()]
        return df[s == v]
    if op == "ne":
        if not pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_datetime64_any_dtype(s):
            return df[s.astype("string").str.casefold() != str(v).casefold()]
        return df[s != v]
    ops = {"gt": s.gt, "gte": s.ge, "lt": s.lt, "lte": s.le}
    if op in ops:
        return df[ops[op](v).fillna(False)]
    raise ValueError(f"Unsupported operator '{op}'.")


# --- grouping / aggregation -------------------------------------------------


def _group_series(df: pd.DataFrame, spec: str) -> tuple[str, pd.Series]:
    """Support ``"Created Date:quarter"`` style date bucketing."""
    if ":" in spec:
        col, part = spec.rsplit(":", 1)
        part = part.lower()
    else:
        col, part = spec, None
    if col not in df.columns:
        raise ValueError(f"Unknown group_by column '{col}'.")
    s = df[col]
    if part is None:
        return col, s.astype(object).where(s.notna(), "(not set)")
    if not pd.api.types.is_datetime64_any_dtype(s):
        raise ValueError(f"Cannot take '{part}' of non-date column '{col}'.")
    fmt = {"year": "%Y", "quarter": None, "month": "%Y-%m", "day": "%Y-%m-%d"}
    if part not in fmt:
        raise ValueError(f"Unsupported date part '{part}'. Use year, quarter, month or day.")
    if part == "quarter":
        out = s.dt.year.astype("Int64").astype("string") + "-Q" + s.dt.quarter.astype("Int64").astype("string")
    else:
        out = s.dt.strftime(fmt[part])
    return f"{col}:{part}", out.astype(object).where(s.notna(), "(no date)")


_AGGS = {
    "sum": "sum",
    "mean": "mean",
    "avg": "mean",
    "median": "median",
    "min": "min",
    "max": "max",
    "count": "size",
    "count_non_null": "count",
    "nunique": "nunique",
}


def _sort_echo(column: str, ascending: bool) -> dict[str, str]:
    """State the order applied, so a caller can catch its own mistake.

    `ascending` defaults to False, which is right for "largest" and wrong for
    "oldest"; a silent default is how a newest-first list gets read as
    oldest-first.
    """
    return {
        "column": column,
        "order": "ascending" if ascending else "descending",
        "first_row_is": ("smallest/oldest" if ascending else "largest/newest") + f" by {column}",
    }


def _grand_total(df: pd.DataFrame, aggregations: list[dict[str, str]]) -> dict[str, Any]:
    """Apply the aggregations to the whole frame, ignoring any grouping."""
    row: dict[str, Any] = {}
    for a in aggregations:
        col, raw_func = a.get("column", "*"), str(a.get("func", "count")).lower()
        func = _AGGS.get(raw_func)
        if func is None:
            raise ValueError(f"Unsupported aggregation '{a.get('func')}'. Use one of {sorted(_AGGS)}.")
        if col in ("*", None) or func == "size":
            row["count"] = len(df)
            continue
        if col not in df.columns:
            raise ValueError(f"Unknown aggregation column '{col}'. Call describe_data for valid column names.")
        row[f"{func}_{col}"] = getattr(df[col], func)()
    return row


def query(
    dataset: str,
    filters: list[dict[str, Any]] | None = None,
    group_by: list[str] | None = None,
    aggregations: list[dict[str, str]] | None = None,
    columns: list[str] | None = None,
    sort_by: str | None = None,
    ascending: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """Filter, then either aggregate by group or return raw rows."""
    for a in aggregations or []:
        if not isinstance(a, dict):
            raise ValueError(
                f"Each aggregation must be an object like "
                f"{{'column': 'Masked Deal value', 'func': 'sum'}}, got {a!r}."
            )

    df = load(dataset)
    total_before = len(df)

    for f in filters or []:
        df = _apply_filter(df, f)
    matched = len(df)

    touched = {f["column"] for f in (filters or []) if f.get("column") in df.columns}
    touched |= {c for c in (columns or []) if c in df.columns}
    touched |= {a["column"] for a in (aggregations or []) if a.get("column") in df.columns}
    touched |= {g.split(":")[0] for g in (group_by or []) if g.split(":")[0] in df.columns}

    result: dict[str, Any] = {
        "dataset": dataset,
        "rows_in_dataset": total_before,
        "rows_matching_filters": matched,
        "data_quality": _quality_notes(df, sorted(touched)),
    }

    if group_by:
        keys = []
        tmp = df.copy()
        for spec in group_by:
            name, series = _group_series(tmp, spec)
            tmp[name] = series
            keys.append(name)
        aggs = aggregations or [{"column": "*", "func": "count"}]
        grouped = tmp.groupby(keys, dropna=False)
        frames = []
        for a in aggs:
            if not isinstance(a, dict):
                raise ValueError(
                    f"Each aggregation must be an object like "
                    f"{{'column': 'Masked Deal value', 'func': 'sum'}}, got {a!r}."
                )
            col, func = a.get("column", "*"), _AGGS.get(str(a.get("func", "count")).lower())
            if func is None:
                raise ValueError(f"Unsupported aggregation '{a.get('func')}'. Use one of {sorted(_AGGS)}.")
            if col in ("*", None) or func == "size":
                frames.append(grouped.size().rename("count"))
            else:
                if col not in tmp.columns:
                    raise ValueError(
                        f"Unknown aggregation column '{col}'. Call describe_data for valid column names."
                    )
                frames.append(getattr(grouped[col], func)().rename(f"{func}_{col}"))
        out = pd.concat(frames, axis=1).reset_index()
        if sort_by and sort_by in out.columns:
            out = out.sort_values(sort_by, ascending=ascending)
            result["sorted"] = _sort_echo(sort_by, ascending)
        elif len(out.columns) > len(keys):
            out = out.sort_values(out.columns[len(keys)], ascending=False)
        result["group_count"] = len(out)
        result["results"] = _records(out.head(limit))
        # The overall figure, so the caller never has to add the groups up.
        result["totals"] = _records(pd.DataFrame([_grand_total(df, aggs)]))[0]
        return result

    if aggregations:
        # Without a group_by, aggregate to a grand total over the matched rows
        # rather than falling through to the raw-row path.
        result["results"] = _records(pd.DataFrame([_grand_total(df, aggregations)]))
        return result

    view = df[columns] if columns else df
    if sort_by and sort_by in view.columns:
        view = view.sort_values(sort_by, ascending=ascending, na_position="last")
        result["sorted"] = _sort_echo(sort_by, ascending)
    result["returned"] = min(limit, len(view))
    result["results"] = _records(view.head(limit))
    if result["returned"] > ROW_TALLY_WARNING_THRESHOLD:
        # Models will happily count a row listing by eye and get it wrong. Say
        # so where they are about to do it.
        result["note"] = (
            "This is a row listing, not an aggregate. Do not count, sum or rank these "
            "rows yourself - re-run the same filters with group_by/aggregations to get "
            "counts and totals, or sort_by with a small limit for the largest items."
        )
    return result


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    out = []
    for rec in df.to_dict(orient="records"):
        clean = {}
        for k, v in rec.items():
            if v is None or (not isinstance(v, (list, dict)) and pd.isna(v)):
                clean[str(k)] = None
            elif isinstance(v, pd.Timestamp):
                clean[str(k)] = v.date().isoformat()
            elif hasattr(v, "item"):
                clean[str(k)] = v.item()
            else:
                clean[str(k)] = v
        out.append(clean)
    return out


def _quality_notes(df: pd.DataFrame, cols: list[str]) -> dict[str, Any]:
    """Null coverage for the columns this query touched, for caveating answers."""
    n = len(df)
    notes = {}
    for c in cols:
        if c not in df.columns:
            continue
        nulls = int(df[c].isna().sum())
        if nulls:
            notes[c] = {
                "null_in_matched_rows": nulls,
                "null_pct": round(nulls / n * 100, 1) if n else 0.0,
            }
    return notes


# --- schema description -----------------------------------------------------


def describe(dataset: str) -> dict[str, Any]:
    df = load(dataset)
    cols = []
    for c in df.columns:
        if str(c).startswith("_"):
            continue
        s = df[c]
        info: dict[str, Any] = {
            "name": str(c),
            "type": "date"
            if pd.api.types.is_datetime64_any_dtype(s)
            else "number"
            if pd.api.types.is_numeric_dtype(s)
            else "bool"
            if pd.api.types.is_bool_dtype(s)
            else "text",
            "null_pct": round(float(s.isna().mean()) * 100, 1) if len(s) else 0.0,
        }
        if info["null_pct"] == 100.0:
            # Nothing to describe, but keep the name so callers know it exists.
            info["empty"] = True
            cols.append(info)
            continue
        if info["type"] == "date" and s.notna().any():
            info["range"] = [str(s.min().date()), str(s.max().date())]
        if info["type"] == "number" and s.notna().any():
            info["min"], info["max"] = float(s.min()), float(s.max())
        if info["type"] == "text":
            nun = int(s.nunique(dropna=True))
            info["distinct_count"] = nun
            if nun <= 15 or any(h in str(c).lower() for h in _CATEGORICAL_HINTS):
                top = [str(v) for v in s.dropna().value_counts().head(15).index]
                info["values"] = top
                if nun > len(top):
                    info["values_truncated_of"] = nun
        cols.append(info)
    return {"dataset": dataset, "row_count": len(df), "columns": cols}
