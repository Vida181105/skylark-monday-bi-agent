"""Throwaway connectivity check: confirms the API token and both board IDs work
and that normalization survives the live data. Run: python scripts_smoke_test.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv()

from skylark import monday_client as mc  # noqa: E402
from skylark import normalize as N  # noqa: E402

print("Boards visible to this token:")
for name, meta in mc.board_meta().items():
    print(f"  {name} (id={meta['id']}) - {len(meta['columns'])} columns")
    print(f"    {meta['columns']}")

raw_deals = mc.fetch_board("deals")
raw_wos = mc.fetch_board("work_orders")
print(f"\nRaw rows: deals={len(raw_deals)} work_orders={len(raw_wos)}")

deals = N.normalize_deals(raw_deals)
wos = N.normalize_work_orders(raw_wos)
print(f"Clean rows: deals={len(deals)} work_orders={len(wos)}")
print(f"Dropped from deals (corrupted header rows): {len(raw_deals) - len(deals)}")

joined = N.join_boards(deals, wos)
matched = joined["Serial #"].notna().sum() if "Serial #" in joined else 0
print(f"Joined rows: {len(joined)} ({matched} deal-rows matched a work order)")
print("\nDeal Status:\n", deals["Deal Status"].value_counts(dropna=False))
print("\nSector/service:\n", deals["Sector/service"].value_counts(dropna=False))
