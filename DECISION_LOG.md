# Decision Log: Skylark Drones monday.com BI Agent

## 1. Assumptions

The brief left several things open. Rather than stall on them, the agent picks a
reading and states it in the answer, so a reader can correct it.

| Ambiguity | What I assumed | Why |
|---|---|---|
| "this quarter" | Indian fiscal year, 1 Apr to 31 Mar. The agent states this the first time it matters, and switches to calendar quarters if asked. | Skylark is India-based. Asking a clarifying question on every date question would be tedious, and stating the convention is just as correctable. |
| "pipeline" | `Deal Status = Open`. Won and Dead are excluded; On Hold only when the agent says so. | Stage alone can't separate a live deal from a dead one sitting at the same stage. |
| "energy sector" and similar phrasing | Mapped to the nearest `Sector/service` values (Renewables, Powerline), with the mapping named in the answer. | The 12 categories don't use founder vocabulary. Naming the mapping keeps it auditable. |
| Deal values | Masked but internally consistent, so sums and comparisons hold. Not real rupees, and the agent says "masked value". | The brief says so, and presenting masked magnitudes as revenue would mislead. |
| Which nulls are a problem | Late-lifecycle nulls (close date, billing month, collection status) are semantic: that stage hasn't been reached. The real caveats are sparse core fields, mainly Closure Probability at 75% null and Masked Deal value at 52%. | Caveating everything is the same as caveating nothing. The agent warns only where sparsity weakens the number it just gave. |
| Cross-board join | `COMPANY###` to `WOCOMPANY_###` on the numeric suffix, left-outer. | Not documented in the brief; I derived it. 50 of 51 work-order customer codes match. Left-outer keeps deals that never converted, which are still real pipeline. |
| Write access | None. The agent only reads. | A BI tool that can mutate the source of truth is a risk nobody asked for. |

## 2. Trade-offs

**Streamlit, one process, instead of a FastAPI and React split.** The brief asks
for a conversational interface and a hosted prototype that needs no local setup.
Streamlit Community Cloud gives both from a single repo with no infrastructure.
The costs are real: no multi-user auth, cold starts, and a ceiling on how the UI
can look. Within the time budget, a working end-to-end flow was worth more than a
better-looking half-built one.

**The monday.com GraphQL API directly, rather than the monday.com MCP server.**
MCP would mean hosting a second process for capability the API already covers in
about 120 lines, and the direct client keeps the read surface explicitly
read-only. The MCP server is still the better tool for exploring the board schema
interactively during development, where a live tool loop beats writing throwaway
scripts. That use doesn't justify hosting it in production.

**Gemini Flash-Lite on the free tier.** The agent needs function calling, room
for long tool results, and reasonable instruction following. Flash has all three
at no marginal cost, which matters for a prototype strangers will click through
without a billing relationship. The provider is confined to one module:
`skylark/agent.py` owns the SDK and the loop, while the tools, cleaning and query
engine know nothing about it, so changing models is a one-file change.

The specific model came out of measurement rather than preference. The free
tier's binding limit is requests per day, and each tool round costs one request,
so `gemini-3.6-flash` at 20 a day allows roughly three questions for the whole
app. That is unusable for a public demo. `gemini-3.5-flash-lite` has a much
larger allowance and handled the canonical questions well, so it is the default,
with `GEMINI_MODEL` overriding it on a paid key. Three things in the code follow
from this: tool results are capped, `describe_data` is trimmed and discouraged in
the prompt, and rate-limit responses are retried using the delay the server
returns.

That limit is a live constraint, not just a design note. Gemini's free tier caps
daily requests, which can surface as busy or rate-limited errors under real
usage. With more time I'd add a paid-tier fallback, or a second free provider
such as Groq to fail over to.

**One general query tool instead of a tool per canned question.** The model gets
`describe_data` and `query_board`, which filters, groups, aggregates and joins,
rather than `get_pipeline_by_sector()` and a dozen siblings. Hardcoded tools only
answer the questions I thought of in advance. The cost is that the model can
compose a query that is subtly wrong, so every result carries row counts and null
coverage, and errors come back as messages it can correct from rather than as
crashes.

**Aggregation in pandas, not in the model's context.** Tools return computed
aggregates rather than raw rows, so answers cover all 342 deals and 176 work
orders instead of whatever sample fits in a prompt. This turned out to matter
more than expected: in testing, the model would otherwise tally a returned row
list by eye and get it wrong. Grouped results now also carry an overall total, so
there is never a reason to add the groups up by hand.

**One normalization module, tested before anything else was built.** The
corrupted header rows, the work-order header on the second row, monday's
JavaScript date strings and the numeric join key are all pinned by tests written
against the known defects. Cleaning in one place cannot drift between queries,
which is what stops one answer counting 344 deals and the next counting 342.

**Null coverage returned with every result.** Data-quality caveats are computed
rather than remembered. The agent quotes the actual percentage for the rows a
query matched, so "closure probability wasn't set on 40% of open deals in this
sector" is a fact about that query rather than boilerplate.

## 3. What I'd do differently with more time

- **Caching and request budget.** Both boards are re-read on a 60-second TTL, and
  the system prompt plus every prior tool result is resent each round. Context
  caching on the stable prefix, a longer board TTL, and trimming old tool results
  from history would cut both cost and the request count the free tier limits.
- **An evaluation set.** Around 20 founder questions with hand-checked answers,
  run as a regression suite. Correctness currently rests on the unit tests plus
  manual spot-checks, and the spot-checks found real errors that the unit tests
  could not.
- **Charts.** Trend and funnel questions want a picture. Streamlit renders
  DataFrames natively, so a third tool returning a chart spec would be a small
  change with a large readability payoff.
- **Confirm the sector taxonomy with the business.** The energy-sector mapping is
  my inference from category names, not a definition anyone agreed to.
- **Scoped write-back.** "Flag these six stalled deals for review" is the obvious
  next ask, and it needs a deliberate permission model rather than a broader
  token.
- **Multi-user auth with per-user monday tokens**, so the agent sees only the
  boards a given user is allowed to see.

## 4. How I read "leadership updates"

Not a data dump, and not a chart pack. Someone reading between meetings wants to
know where things stand, what moved, what's at risk, and what needs them.

The leadership summary produces a short digest: headline pipeline and win
position, movement by sector, execution and billing risk, and the two or three
items needing a decision. It is built entirely from live tool output, with a
caveat attached only where it changes how much to trust a number. Ordinary
answers follow the same shape, leading with the answer, then the supporting
numbers, then what they mean. A number with no interpretation attached just moves
the work to the reader.
