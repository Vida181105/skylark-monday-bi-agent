# Skylark Drones — monday.com BI Agent

A conversational BI agent that answers founder-style questions ("How's pipeline
looking for the energy sector this quarter?") by reading **live** from two
monday.com boards — the **Deal tracker** (sales pipeline) and the **Work order
tracker** (project execution and billing) — cleaning them, joining across them
when needed, and answering with interpreted insight plus honest data caveats.

> **Live demo:** https://skylark-monday-bi-agent-kwpfmutktekeken97dy8kn.streamlit.app/

No board data is bundled into the app. Every question triggers a fresh
GraphQL read of both boards (cached for 60 seconds).

---

## Architecture

```
                 ┌──────────────────────────────────────────┐
  Founder ──────▶│  app.py — Streamlit chat UI              │
                 │  st.chat_message + streaming answer      │
                 └───────────────┬──────────────────────────┘
                                 │
                 ┌───────────────▼──────────────────────────┐
                 │  skylark/agent.py — Gemini tool-use loop │
                 │  gemini-3.5-flash-lite, streaming       │
                 └───────────────┬──────────────────────────┘
                    describe_data│query_board
                 ┌───────────────▼──────────────────────────┐
                 │  skylark/analytics.py — query engine     │
                 │  filter · group · aggregate · join       │
                 └───────────────┬──────────────────────────┘
                 ┌───────────────▼──────────────────────────┐
                 │  skylark/normalize.py — cleaning layer   │
                 │  drop corrupted rows · coerce · join key │
                 └───────────────┬──────────────────────────┘
                 ┌───────────────▼──────────────────────────┐
                 │  skylark/monday_client.py — GraphQL v2   │
                 │  read-only · paginated · 60s TTL cache   │
                 └───────────────┬──────────────────────────┘
                                 ▼
                    monday.com — Deal tracker + Work order tracker
```

| Module | Responsibility |
|---|---|
| [`skylark/monday_client.py`](skylark/monday_client.py) | Read-only monday.com GraphQL API v2 client. Cursor pagination, 60-second TTL cache, no writes. |
| [`skylark/normalize.py`](skylark/normalize.py) | The single cleaning layer every query passes through — drops corrupted rows, coerces dates and money, derives the cross-board join key and the funnel `stage_order`. |
| [`skylark/analytics.py`](skylark/analytics.py) | One filter/group/aggregate primitive over three datasets (`deals`, `work_orders`, `joined`), returning results plus per-column null coverage. |
| [`skylark/agent.py`](skylark/agent.py) | The Gemini tool-use loop: system prompt encoding the business semantics, two tools, streaming, tool-error recovery, rate-limit retry, and tool-payload capping. |
| [`app.py`](app.py) | Streamlit chat UI, example prompts, leadership-summary mode, inspectable tool calls. |
| [`tests/`](tests/) | 50 offline tests covering the messy-data cases and the query engine. |

**Why this shape.** A single Streamlit process is both the conversational
interface and the backend, so "hosted and testable with zero local setup" costs
one deploy rather than a frontend/backend split. Analysis lives in tools rather
than in the model's context, so answers are computed from all 346 + 176 rows
instead of a sample that happens to fit in a prompt. Cleaning lives in exactly
one module, so null and join semantics cannot drift per question.

---

## The data, and what's wrong with it

Handled in [`skylark/normalize.py`](skylark/normalize.py) and pinned by tests:

| Issue | Handling |
|---|---|
| Deal tracker has 2 corrupted rows that re-inject the header ("Nezuko", "Bugs Bunny") with every field equal to its column name | Dropped: any row echoing ≥2 of its own column names. A row echoing just one (a deal legitimately named "Deal Stage") is kept. |
| monday.com serves dates as JS `Date.toString()` strings (`Fri Dec 26 2025 … (Coordinated Universal Time)`) | The timezone name is stripped and the value parsed to a naive timestamp. Pandas' mixed-format inference turns these into `NaT` instead — which silently empties *every* date column. |
| The live Deals board carries an empty copy of the whole work-order schema (43 dead columns, incl. an empty `Sector` beside the real `Sector/service`) | Columns that are entirely empty **and** foreign to the board's own schema are pruned. A board's own sparse columns are always kept — "nothing has reached that stage yet" is a real answer. |
| Work order sheet's real header is its **second** row | `realign_header_row()` picks the header row by distinct-value count; verified on import. Via the API, column titles arrive correctly — this covers raw-file reads. |
| `Deal Stage` is alphabetically prefixed and the letter is the funnel order | `stage_order` column: `A.` → 1 … `G.` → 7. Never sorted by the stage text. |
| `Close Date (A)` 92% null, work-order billing/collection columns 85–100% null | Semantic, not missing — never imputed and never reported as a data-quality problem. |
| `Closure Probability` 75% null, `Masked Deal value` 52% null, `Tentative Close Date` 21% null | Real caveats. Every tool result carries null coverage for the columns the query touched, and the agent is instructed to state the percentage when it weakens an estimate. |
| `Deals.Client Code` = `COMPANY###` vs `WorkOrders.Customer Name Code` = `WOCOMPANY_###` | Joined on the numeric suffix only (50 of 51 work-order customers match). |
| Owner code sets differ across boards (`OWNER_007` deals-only, `OWNER_008` work-orders-only) | Left-outer join; unmatched rows kept and flagged in the answer. |
| Negative computed amounts (e.g. `Amount to be billed`) | Preserved and surfaced as real over-billing/timing artifacts, never clamped to zero. |
| Deal values are masked | Internally consistent, so sums and ratios are meaningful; the agent says "masked value" rather than implying real revenue. |

---

## monday.com setup

1. **Create the boards.** In monday.com: *Add board → Import from Excel* for
   `Deal_funnel_Data.xlsx` (→ "Deal tracker") and `Work_Order_Tracker_Data.xlsx`
   (→ "work order tracker"). For the work order file, confirm the importer
   picked the **second** row as the header — column names should read
   `Serial #`, `Customer Name Code`, … and not `Unnamed: 1`.
2. **Note the board IDs** — the numeric segment of each board's URL
   (`.../boards/5030966982`).
3. **Generate a read-only personal API token** — avatar → *Administration* →
   *API* → *Copy/Regenerate*. The app only issues `query` operations; it never
   mutates a board.
4. **Configure the app** (below). Never commit the token — `.env` is gitignored.

---

## Running it

### Locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in the two tokens
python scripts/smoke_test.py  # confirms both boards read and normalize cleanly
streamlit run app.py
```

`.env`:

```
MONDAY_API_TOKEN=<your monday.com personal API token>
GEMINI_API_KEY=<your Google AI Studio API key>
MONDAY_DEALS_BOARD_ID=5030966982
MONDAY_WORK_ORDERS_BOARD_ID=5030967295
# optional - defaults to gemini-3.5-flash-lite
GEMINI_MODEL=
```

> **Model choice is driven by the free tier's daily request cap.** Each tool
> round is one API request, so full `gemini-3.6-flash` (20 requests/day) allows
> roughly three questions per day for the whole app. The default is
> `gemini-3.5-flash-lite`, which carries a far larger free allowance and answered
> the canonical questions well in testing. On a paid key, set
> `GEMINI_MODEL=gemini-3.6-flash` for stronger reasoning.
>
> Note that `gemini-2.5-flash` and `gemini-2.5-flash-lite` are retired for newly
> issued API keys — they still appear in `models.list()` but return 404 on
> `generateContent`, so the listing is not a reliable availability signal.

### Tests

```bash
pytest -q     # 50 tests, fully offline (monday.com is stubbed)
```

### Deploying to Streamlit Community Cloud

1. Push this repo to GitHub.
2. [share.streamlit.io](https://share.streamlit.io) → *New app* → pick the repo,
   branch, and `app.py`.
3. *Advanced settings → Secrets*, paste:

   ```toml
   MONDAY_API_TOKEN = "..."
   GEMINI_API_KEY = "..."
   MONDAY_DEALS_BOARD_ID = "5030966982"
   MONDAY_WORK_ORDERS_BOARD_ID = "5030967295"
   ```

4. Deploy, then smoke-test the public URL with one pipeline question and one
   billing question.

---

## What it can answer

- **Pipeline** — open value and deal count by sector, owner, stage or quarter;
  where deals stall in the funnel; what's due to close this fiscal quarter.
- **Revenue & wins** — won value by sector or owner, win rates, conversion from
  pipeline to executed work.
- **Operations** — work orders delivered but unbilled, outstanding receivables,
  negative billing artifacts, execution status by sector.
- **Cross-board** — which clients have both an open deal and live work orders;
  sectors that sell but never convert into executed work.
- **Leadership summary** — one-click digest of pipeline health, sector movement,
  execution/collection risk, and the decisions needing attention.

Assumptions it states rather than stalls on: fiscal year is **Indian FY,
1 April – 31 March**; "pipeline" means `Deal Status = Open`.

**On the data snapshot:** the boards' records stop around January 2026, and no
open deal has a tentative close date in the future. Questions scoped to the
current period therefore match nothing — the agent reports that honestly and
offers the unscoped view, but the sample questions deliberately avoid
"this quarter"/"this week" phrasing so the demo shows substantive answers. It asks a
clarifying question only when the readings would give materially different
answers.

See [`DECISION_LOG.md`](DECISION_LOG.md) for the trade-offs behind these choices.
