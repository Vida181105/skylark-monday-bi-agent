"""Read-only monday.com GraphQL API v2 client.

Boards are queried at request time; nothing is bundled into the app. A short
TTL cache avoids re-fetching a board on every tool call.
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests

API_URL = "https://api.monday.com/v2"
API_VERSION = "2024-10"
PAGE_SIZE = 250
CACHE_TTL_SECONDS = 60

_cache: dict[str, tuple[float, Any]] = {}


class MondayError(RuntimeError):
    pass


def _token() -> str:
    token = os.environ.get("MONDAY_API_TOKEN", "").strip()
    if not token:
        raise MondayError(
            "MONDAY_API_TOKEN is not set. Add it to .env locally, or to the "
            "app's secrets when deployed."
        )
    return token


def board_ids() -> dict[str, str]:
    return {
        "deals": os.environ.get("MONDAY_DEALS_BOARD_ID", "5030966982").strip(),
        "work_orders": os.environ.get("MONDAY_WORK_ORDERS_BOARD_ID", "5030967295").strip(),
    }


def _post(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    resp = requests.post(
        API_URL,
        json={"query": query, "variables": variables},
        headers={
            "Authorization": _token(),
            "API-Version": API_VERSION,
            "Content-Type": "application/json",
        },
        timeout=60,
    )
    if resp.status_code == 401:
        raise MondayError("monday.com rejected the API token (401). Check MONDAY_API_TOKEN.")
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        raise MondayError(f"monday.com API error: {payload['errors']}")
    if payload.get("error_message"):
        raise MondayError(f"monday.com API error: {payload['error_message']}")
    return payload["data"]


_FIRST_PAGE = """
query ($ids: [ID!], $limit: Int!) {
  boards(ids: $ids) {
    id
    name
    columns { id title type }
    items_page(limit: $limit) {
      cursor
      items { id name column_values { id text } }
    }
  }
}
"""

_NEXT_PAGE = """
query ($cursor: String!, $limit: Int!) {
  next_items_page(cursor: $cursor, limit: $limit) {
    cursor
    items { id name column_values { id text } }
  }
}
"""


def _flatten(items: list[dict], titles: dict[str, str], name_title: str) -> list[dict[str, Any]]:
    rows = []
    for item in items:
        row: dict[str, Any] = {name_title: item.get("name"), "_item_id": item.get("id")}
        for cv in item.get("column_values", []):
            title = titles.get(cv["id"], cv["id"])
            row[title] = cv.get("text")
        rows.append(row)
    return rows


def fetch_board(board: str, use_cache: bool = True) -> list[dict[str, Any]]:
    """Fetch every item of a board as a list of ``{column title: text}`` dicts.

    Raw and uncleaned; callers pass the result through :mod:`skylark.normalize`.
    """
    ids = board_ids()
    if board not in ids:
        raise MondayError(f"Unknown board '{board}'. Expected one of {list(ids)}.")
    board_id = ids[board]

    cached = _cache.get(board_id)
    if use_cache and cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    data = _post(_FIRST_PAGE, {"ids": [board_id], "limit": PAGE_SIZE})
    boards = data.get("boards") or []
    if not boards:
        raise MondayError(
            f"Board {board_id} returned no data. Check the ID and that the token's "
            "account can see the board."
        )
    b = boards[0]
    titles = {c["id"]: c["title"] for c in b.get("columns", [])}
    # An item's `name` is the board's first column.
    name_title = "Deal Name" if board == "deals" else "Serial #"

    page = b["items_page"]
    rows = _flatten(page["items"], titles, name_title)
    cursor = page.get("cursor")
    while cursor:
        nxt = _post(_NEXT_PAGE, {"cursor": cursor, "limit": PAGE_SIZE})["next_items_page"]
        rows.extend(_flatten(nxt["items"], titles, name_title))
        cursor = nxt.get("cursor")

    _cache[board_id] = (time.time(), rows)
    return rows


def board_meta() -> dict[str, Any]:
    """Board names, ids and column titles; used by the smoke test."""
    ids = board_ids()
    data = _post(_FIRST_PAGE, {"ids": list(ids.values()), "limit": 1})
    return {
        b["name"]: {"id": b["id"], "columns": [c["title"] for c in b["columns"]]}
        for b in data.get("boards", [])
    }


def clear_cache() -> None:
    _cache.clear()
