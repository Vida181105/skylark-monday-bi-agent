"""Gemini tool-use agent loop.

Two tools over the live boards: one to inspect the schema, one to
filter/group/aggregate. Aggregation happens in the tools rather than in the
model's context, so answers cover every row instead of a sample.
"""

from __future__ import annotations

import datetime as dt
import os
import json
import logging
import re
import time
from typing import Any, Iterator

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from . import analytics

# The SDK logs an automatic-function-calling notice on every streamed call.
logging.getLogger("google_genai.models").setLevel(logging.ERROR)

# Flash-Lite: the free tier's daily request cap is the binding constraint, and
# each tool round costs one request. Full Flash allows 20/day. Override with
# GEMINI_MODEL; a paid key should prefer gemini-3.6-flash.
DEFAULT_MODEL = "gemini-3.5-flash-lite"


def resolve_model() -> str:
    """The model id, read at call time so config loaded after import applies."""
    return os.environ.get("GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
MAX_TOOL_ROUNDS = 12
# The whole conversation is resent each round and the free tier caps input
# tokens per minute, so tool payloads are capped.
MAX_TOOL_RESULT_CHARS = 12000
# 429 is the free tier's quota; 5xx is Gemini being busy. Both are transient
# and worth waiting out rather than surfacing to the user.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
STREAM_RETRIES = 3

# Gemini's Schema dialect: uppercase type names, no `additionalProperties`, and
# every property needs a concrete type. The filter operand is therefore split
# into a scalar `value` and a list `values`.
_DATASET_ENUM = list(analytics.DATASETS)

TOOL_DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "describe_data",
        "description": (
            "Inspect a dataset's schema before querying it: exact column names, "
            "inferred types, null percentage, date ranges, numeric ranges, and the "
            "distinct values of categorical columns. Call this first whenever you "
            "are unsure of a column name or of how a category is spelled."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "dataset": {
                    "type": "STRING",
                    "enum": _DATASET_ENUM,
                    "description": "deals | work_orders | joined",
                }
            },
            "required": ["dataset"],
        },
    },
    {
        "name": "query_board",
        "description": (
            "Filter, group and aggregate live monday.com board data. Use "
            "'deals' for pipeline questions, 'work_orders' for execution / "
            "billing / collection questions, and 'joined' only when a question "
            "genuinely needs both boards (it left-joins work orders onto deals on "
            "the numeric customer-code suffix, so it fans out one deal row per "
            "matching work order - never sum deal values on the joined dataset). "
            "Omit group_by to get raw rows back. Every response also reports how "
            "many of the matched rows are null in the columns you touched - use "
            "that to caveat your answer."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "dataset": {"type": "STRING", "enum": _DATASET_ENUM},
                "filters": {
                    "type": "ARRAY",
                    "description": "Filters, ANDed together.",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "column": {"type": "STRING", "description": "Exact column name."},
                            "op": {
                                "type": "STRING",
                                "enum": [
                                    "eq", "ne", "in", "not_in", "contains",
                                    "not_contains", "gt", "gte", "lt", "lte",
                                    "between", "is_null", "not_null",
                                ],
                            },
                            "value": {
                                "type": "STRING",
                                "description": (
                                    "Single operand for eq/ne/contains/not_contains/gt/gte/lt/lte. "
                                    "Dates as YYYY-MM-DD; numbers as digits. Omit for is_null/not_null."
                                ),
                            },
                            "values": {
                                "type": "ARRAY",
                                "items": {"type": "STRING"},
                                "description": (
                                    "Operand list for 'in' and 'not_in', or exactly [low, high] "
                                    "for 'between'. Use instead of `value` for those operators."
                                ),
                            },
                        },
                        "required": ["column", "op"],
                    },
                },
                "group_by": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"},
                    "description": (
                        "Column names. A date column may be bucketed with a suffix: "
                        "'Created Date:quarter', ':month', ':year', ':day'."
                    ),
                },
                "aggregations": {
                    "type": "ARRAY",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "column": {"type": "STRING", "description": "'*' for a row count."},
                            "func": {
                                "type": "STRING",
                                "enum": ["sum", "mean", "median", "min", "max", "count", "count_non_null", "nunique"],
                            },
                        },
                        "required": ["column", "func"],
                    },
                },
                "columns": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"},
                    "description": "Columns to return when not grouping. Keep it short.",
                },
                "sort_by": {"type": "STRING", "description": "Column to sort by."},
                "ascending": {
                    "type": "BOOLEAN",
                    "description": (
                        "Defaults to false, which puts the LARGEST or NEWEST value first. "
                        "For 'oldest', 'earliest', 'smallest' or 'longest-standing' "
                        "questions you must set this to true, or you will get the opposite "
                        "record. The response echoes the order actually applied - check it."
                    ),
                },
                "limit": {"type": "INTEGER", "description": "Default 50, max 200."},
            },
            "required": ["dataset"],
        },
    },
]


def fiscal_quarter(today: dt.date) -> tuple[str, dt.date, dt.date]:
    """Indian fiscal quarter containing ``today``: Apr-Jun is Q1, Jan-Mar is Q4.

    Returns a label and the inclusive start and end dates, so the prompt can
    hand the model exact bounds instead of leaving it to derive them.
    """
    start_month = {1: 1, 2: 1, 3: 1, 4: 4, 5: 4, 6: 4, 7: 7, 8: 7, 9: 7, 10: 10, 11: 10, 12: 10}[today.month]
    start = dt.date(today.year, start_month, 1)
    end_month = start_month + 2
    last_day = (dt.date(today.year + (end_month == 12), (end_month % 12) + 1, 1) - dt.timedelta(days=1)).day
    end = dt.date(today.year, end_month, last_day)
    quarter = {4: 1, 7: 2, 10: 3, 1: 4}[start_month]
    fy_start_year = today.year if today.month >= 4 else today.year - 1
    return f"FY{fy_start_year}-{str(fy_start_year + 1)[-2:]} Q{quarter}", start, end


def system_prompt(today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    fy_start = dt.date(today.year if today.month >= 4 else today.year - 1, 4, 1)
    q_label, q_start, q_end = fiscal_quarter(today)
    return f"""You are the Skylark Drones BI agent. Founders and sales leadership ask you \
natural-language questions about the business; you answer them from two live monday.com \
boards via your tools.

Today is {today.isoformat()}.

# The data
**deals** - the Deal tracker sales pipeline (~344 usable rows). Key columns: Deal Name, \
Owner code, Client Code, Deal Status (Open / On Hold / Dead / Won), Deal Stage, \
Close Date (A), Closure Probability (High/Medium/Low), Masked Deal value, \
Tentative Close Date, Product deal, Sector/service, Created Date. Two corrupted rows that \
re-injected the header have already been dropped for you.

**work_orders** - the Work order tracker, project execution and billing (~176 rows). Key \
columns: Serial # (unique work-order ID), Deal name masked, Customer Name Code, \
Nature of Work, Execution Status, Date of PO/LOI, Data Delivery Date, Sector, \
Type of Work, BD/KAM Personnel code, plus amount / billed / collected / receivable \
columns and billing & collection status columns.

**joined** - work orders left-joined onto deals on the numeric suffix of the customer code \
(deals use COMPANY###, work orders use WOCOMPANY_###). It fans out to one row per \
deal-workorder pair, so counts and sums of *deal* fields are inflated on it. Use it only \
for questions that truly span both boards, and prefer counting distinct Serial # or \
Deal Name over raw row counts.

# How to read the data
- **Deal Stage is ordered by its letter prefix**, not alphabetically. The ladder is: \
A. Lead Generated -> B. Sales Qualified Leads -> C. Demo Done -> D. Feasibility -> \
E. Proposal/Commercials Sent -> F. Negotiations -> **G. Project Won** (the win) -> \
H. Work Order Received -> I. POC -> J. Invoice sent -> K. Amount Accrued. Off the ladder: \
L. Project Lost, M. Projects On Hold, N. Not relevant at the moment, O. Not Relevant at \
all, and "Project Completed" (no prefix - it sorts last and has a null `stage_order`). \
A `stage_order` column (1 = A ... 7 = G ... 11 = K) is available for sorting. Never \
describe stage progression using alphabetical order of the stage text, and remember that \
H-K are *post-win* execution and billing stages, not pipeline.
- **Nulls are usually semantic, not missing data.** Close Date (A) is null until a deal \
actually closes (~92% null - expected). Late-lifecycle work-order columns (billing month, \
collection status/date) are 85-100% null because those work orders have not reached that \
stage. Do NOT flag those as data-quality problems.
- **Do flag** sparse fields that materially weaken your answer (live rates on 342 deals): \
Closure Probability unset on 75%, Masked Deal value on 52%, Product deal on 49%, \
Sector/service on 2%. **Tentative Close Date is unset on 21% of deals** - so any \
"closing this quarter" figure necessarily excludes those deals and understates the true \
number; say so when you scope pipeline by date. \
When an aggregate rests on a sparse column, say so in one plain sentence with the actual \
percentage from the tool's `data_quality` block - e.g. "closure probability wasn't set on \
40% of open deals in this sector, so this estimate may be optimistic".
- Deal values are **masked/anonymised magnitudes**: internally consistent, so ratios, sums \
and comparisons are meaningful, but the absolute rupee figures are not real. Say "masked \
value" rather than implying real revenue.
- Negative computed amounts on work orders (e.g. Amount to be billed) are real \
over-billing or timing artifacts. Report them, do not silently drop them.
- Some owner codes exist on only one board (OWNER_007 has no work orders; OWNER_008 has no \
deals). Mention it if it affects an owner comparison.

# Using the tools
- **Every tool round costs a request against a small daily quota, so be frugal.** Issue \
independent queries *in parallel in one turn* rather than one per turn, and plan the whole \
analysis before you start rather than exploring incrementally.
- The column lists above are complete and accurate - you do not need `describe_data` just \
to learn column names. Call it only when you need distinct values or null rates you do not \
already have, and prefer folding that need into a `query_board` group-by instead.
- Filters are ANDed. Put a single operand in `value`; for `in`, `not_in` and `between` use \
`values` instead (`between` takes exactly two, low then high). `is_null` / `not_null` take \
neither.
- If a tool returns an error, read it and correct the call - usually a column name that \
`describe_data` will give you exactly.

# Conventions and clarifying questions
- Fiscal year: **Indian FY, 1 April - 31 March** (Skylark Drones is India-based). The \
current fiscal year began {fy_start.isoformat()}. State this assumption the first time a \
question depends on it. If the user says they use calendar quarters, adopt that instead.
- **"This quarter" means {q_label}, i.e. {q_start.isoformat()} to {q_end.isoformat()} \
inclusive.** To scope a question to any period, apply an explicit `between` filter on the \
relevant date column using those exact bounds. Never decide that a deal falls in a period \
from a quarter label, from a group-by bucket, or from memory - the buckets are calendar \
labels and say nothing about which one is current.
- **If a date-scoped filter matches zero rows, report zero.** Say plainly that nothing \
falls in the window, then use `describe_data` to state the column's actual date range and \
offer the unscoped or overdue view as a clearly-labelled alternative. Never present an \
unscoped figure as though it were the scoped one - that is the single worst error you can \
make here, because it looks like a confident answer and is simply false. Forecast dates in \
this dataset are often stale, so an empty current-quarter window is a real and useful \
finding: it means the close dates need updating.
- "Pipeline" = deals whose status is Open (optionally On Hold, if you say so). Won/Dead \
deals are not pipeline.
- Ask a clarifying question only when a question is genuinely ambiguous *and* the readings \
lead to materially different answers. Otherwise state your assumption in one line and \
answer. Never stall a whole answer on a question you could reasonably assume through.

# How to answer
1. Call tools to compute the numbers. Never estimate from memory, and never invent a figure \
a tool did not return.
2. Lead with the answer in one or two sentences, then the supporting numbers (a small \
markdown table when comparing categories), then what it means - the trend, the outlier, the \
risk, the thing to do next.
3. When a question asks for the **oldest, earliest, smallest or longest-standing** record, \
pass `ascending: true`; the default puts the newest or largest first. Each response echoes \
the order it applied in a `sorted` block - read it before naming a record.
4. **Every number you state must come from a tool result. Never tally, sum, average or \
rank rows yourself - not even by adding up the rows of a grouped result.** A grouped \
response carries a `totals` block holding the overall figure for exactly the same filters; \
quote that for any headline number. If you need a breakdown, run a `group_by` query; if you need the \
biggest items, run `sort_by` with a `limit`. Counting a row listing by eye is the most \
likely way you will produce a confidently wrong answer - a returned list of rows is \
evidence for naming individual records, never for a total.
5. **A bare count is rarely the whole answer.** When you report how many records match, \
also break them down by the most useful dimension (sector, owner or stage) and state the \
value at stake. That is a second, grouped query - worth the round.
6. **When you rank or compare, sort the table by the metric you are ranking on**, and make \
sure the opening sentence names the actual top of that sort - do not call something a \
leader when a row above it scores higher. If you leave categories out (tiny sample sizes, \
say), name them and say why in one clause.
7. You may reuse figures already returned by a tool earlier in this conversation when the \
filters match exactly, and if you do, say so in one clause ("from the figures pulled \
earlier"). If the earlier result does not cover exactly what is being asked now, query \
again - a wrong number costs far more than one request.
8. Keep it tight: a founder skimming on a phone. No preamble, no restating the question.
9. Close with a one-line data caveat only when a sparse column actually affects the answer.
10. If asked for an exec / leadership summary, produce a short digest: headline pipeline and \
win position, movement by sector, execution and collection risk, and 2-3 things needing a \
decision. Still driven entirely by tool output.
"""


def _normalize_filters(args: dict[str, Any]) -> dict[str, Any]:
    """Fold the split `value` / `values` operands back into one."""
    out = dict(args)
    folded = []
    for f in out.get("filters") or []:
        f = dict(f)
        values = f.pop("values", None)
        if values is not None and f.get("value") in (None, ""):
            f["value"] = list(values)
        folded.append(f)
    if folded:
        out["filters"] = folded
    return out


def _cap_result(out: dict[str, Any]) -> dict[str, Any]:
    """Keep a tool result inside the token budget by dropping rows, not fields.

    Aggregates and the data_quality block carry the answer; the tail of a long
    row list rarely does. Truncation is reported so the caller knows.
    """
    if len(json.dumps(out, default=str)) <= MAX_TOOL_RESULT_CHARS:
        return out
    rows = out.get("results")
    if not isinstance(rows, list) or not rows:
        return out
    capped = dict(out)
    keep = len(rows)
    while keep > 1:
        keep = max(1, keep // 2)
        capped["results"] = rows[:keep]
        capped["results_truncated"] = (
            f"showing {keep} of {len(rows)} rows - narrow the filters, group, or "
            f"request fewer columns for the full picture"
        )
        if len(json.dumps(capped, default=str)) <= MAX_TOOL_RESULT_CHARS:
            break
    return capped


def _normalize_aggregations(args: dict[str, Any]) -> dict[str, Any]:
    """Accept ``aggregations: ["sum"]`` with the column carried in `columns`.

    The model emits this shorthand; pairing them here saves a retry round.
    """
    aggs = args.get("aggregations")
    if not aggs or all(isinstance(a, dict) for a in aggs):
        return args
    out = dict(args)
    cols = out.get("columns") or []
    expanded: list[dict[str, str]] = []
    for a in aggs:
        if isinstance(a, dict):
            expanded.append(a)
        elif cols:
            expanded.extend({"column": c, "func": str(a)} for c in cols)
        else:
            expanded.append({"column": "*", "func": str(a)})
    out["aggregations"] = expanded
    if cols:
        # `columns` selects raw output; it is meaningless once aggregating.
        out.pop("columns", None)
    return out


def _run_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        if name == "describe_data":
            return _cap_result(analytics.describe(args["dataset"]))
        if name == "query_board":
            args = _normalize_aggregations(_normalize_filters(args))
            args["limit"] = min(int(args.get("limit") or 25), 200)
            args = {k: v for k, v in args.items() if v is not None}
            return _cap_result(analytics.query(**args))
        return {"error": f"Unknown tool '{name}'."}
    except Exception as exc:  # surfaced to the model so it can correct itself
        return {"error": f"{type(exc).__name__}: {exc}"}


def _retry_delay_seconds(exc: Exception, default: float = 30.0) -> float:
    """The suggested retry delay from a 429 payload, or ``default``."""
    m = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", str(exc))
    if m:
        return min(float(m.group(1)) + 1.0, 90.0)
    m = re.search(r"retry in (\d+(?:\.\d+)?)s", str(exc))
    return min(float(m.group(1)) + 1.0, 90.0) if m else default


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff for 5xx, which carries no suggested delay."""
    return min(2.0 * (3 ** attempt), 30.0)


class BIAgent:
    """Holds one conversation. Streams text and reports tool activity."""

    def __init__(self, api_key: str | None = None):
        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        self.client = genai.Client(api_key=key)
        self.model = resolve_model()
        self.config = types.GenerateContentConfig(
            system_instruction=system_prompt(),
            tools=[types.Tool(function_declarations=TOOL_DECLARATIONS)],
            # The loop below is driven here, one round at a time.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        self.contents: list[types.Content] = []


    def _stream_round(self) -> Iterator[tuple[str, Any]]:
        """One model turn, retrying transient API failures.

        Only retries while nothing has been streamed yet: once tokens have
        reached the user a retry would duplicate them.
        """
        for attempt in range(STREAM_RETRIES):
            produced = False
            try:
                stream = self.client.models.generate_content_stream(
                    model=self.model, contents=self.contents, config=self.config
                )
                for chunk in stream:
                    produced = True
                    yield "chunk", chunk
                return
            except genai_errors.APIError as exc:
                code = getattr(exc, "code", None)
                if code not in RETRYABLE_STATUS or produced or attempt == STREAM_RETRIES - 1:
                    raise
                if code == 429:
                    delay = _retry_delay_seconds(exc)
                    reason = "Rate limited"
                else:
                    delay = _backoff_seconds(attempt)
                    reason = "The model is busy"
                yield "note", f"_{reason}. Retrying in {delay:.0f}s._\n\n"
                time.sleep(delay)

    def ask(self, user_message: str) -> Iterator[dict[str, Any]]:
        """Yield tool and text events as the turn unfolds."""
        self.contents.append(types.Content(role="user", parts=[types.Part(text=user_message)]))

        tool_errors: list[str] = []
        tool_successes = 0

        for _ in range(MAX_TOOL_ROUNDS):
            calls: list[Any] = []
            reply_parts: list[types.Part] = []

            for kind, payload in self._stream_round():
                if kind == "note":
                    yield {"type": "text", "text": payload}
                    continue
                chunk = payload
                for candidate in chunk.candidates or []:
                    for part in (candidate.content.parts if candidate.content else []) or []:
                        if getattr(part, "thought", False):
                            continue  # reasoning tokens, not shown
                        if part.function_call:
                            calls.append(part.function_call)
                            reply_parts.append(part)
                        elif part.text:
                            yield {"type": "text", "text": part.text}
                            reply_parts.append(types.Part(text=part.text))

            # Record the model's turn before answering its tool calls.
            self.contents.append(types.Content(role="model", parts=reply_parts or [types.Part(text="")]))

            if not calls:
                return

            results = []
            for call in calls:
                args = dict(call.args or {})
                yield {"type": "tool", "name": call.name, "input": args}
                out = _run_tool(call.name, args)
                if "error" in out:
                    tool_errors.append(str(out["error"]))
                else:
                    tool_successes += 1
                yield {"type": "tool_result", "name": call.name, "output": out}
                results.append(
                    types.Part.from_function_response(name=call.name, response={"result": out})
                )
            # All function responses for one turn go back together.
            self.contents.append(types.Content(role="user", parts=results))

        yield from self._answer_without_tools(tool_errors, tool_successes)

    def _answer_without_tools(
        self, tool_errors: list[str], tool_successes: int
    ) -> Iterator[dict[str, Any]]:
        """Force a reply once the tool-round budget is spent.

        Withdrawing the tools is what ends the loop: with nothing to call, the
        model has to answer from what it already has. Spending one more request
        on that beats ending the turn with nothing after a dozen.
        """
        if tool_errors and not tool_successes:
            nudge = (
                "Every data lookup failed. The last error was: "
                f"{tool_errors[-1]}. Tell the user plainly what went wrong and what to "
                "check. Do not invent any figures."
            )
        else:
            nudge = (
                "You have used the whole tool budget for this question. Answer now from "
                "the data you have already retrieved, and say which part is incomplete."
            )
        self.contents.append(types.Content(role="user", parts=[types.Part(text=nudge)]))

        no_tools = types.GenerateContentConfig(system_instruction=self.config.system_instruction)
        answer = ""
        try:
            for chunk in self.client.models.generate_content_stream(
                model=self.model, contents=self.contents, config=no_tools
            ):
                for candidate in chunk.candidates or []:
                    for part in (candidate.content.parts if candidate.content else []) or []:
                        if getattr(part, "thought", False):
                            continue
                        if part.text:
                            answer += part.text
                            yield {"type": "text", "text": part.text}
        except Exception as exc:
            yield {"type": "text", "text": f"\n\n_Could not finish the answer: {exc}_"}
            return
        if not answer:
            yield {"type": "text", "text": "\n\n_No answer was produced. Try a narrower question._"}
            return
        # Keep the answer in history so a follow-up question can build on it.
        self.contents.append(types.Content(role="model", parts=[types.Part(text=answer)]))
