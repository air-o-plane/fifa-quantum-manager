r"""
FIFA WC2026 Fantasy — build the real squad from xPts via the verified ILP.

Loads the pool, joins the model's xpts.csv (keyed by pool_row == the ILP's
pid), and runs the SAME two-layer ILP from fantasy_ilp_baseline with the real
expected-points objective instead of the price placeholder. All constraint
machinery (15-man squad, 2/5/5/3, $100M cap, nation caps, formation-legal XI,
captain) is unchanged and re-validated.

USAGE:
  python3 fantasy_build_squad.py            # group-stage budget/caps
  python3 fantasy_build_squad.py 105 4      # R32: budget 105, nation cap 4
"""

from __future__ import annotations
import sys
import pandas as pd

import fantasy_ilp_baseline as ilp
from fantasy_name_bridge import _read_csv

POOL_XLSX = "FIFA_Men_s_World_Cup_2026_Player_Pool.xlsx"
XPTS_CSV  = "xpts.csv"


def main(budget: float = 100.0, nation_cap: int = 3):
    pool = ilp.load_pool(POOL_XLSX)           # adds 'pid' = 0-based row index
    xp = _read_csv(XPTS_CSV)[["pool_row", "xpts"]]

    merged = pool.merge(xp, left_on="pid", right_on="pool_row", how="left")
    missing = merged["xpts"].isna().sum()
    if missing:
        # Players the model didn't score (shouldn't happen — model covers all) :
        # default to 0 so they're simply never selected.
        merged["xpts"] = merged["xpts"].fillna(0.0)
        print(f"Note: {missing} players had no xPts; set to 0.")
    merged["xPts"] = merged["xpts"]           # the column name the ILP expects

    # Apply the requested budget / nation cap to the shared ILP config.
    ilp.BUDGET = float(budget)
    ilp.NATION_CAP = int(nation_cap)
    ilp.USE_PLACEHOLDER = False               # use OUR xPts, not the price proxy

    out = ilp.optimise(merged)
    ilp.validate(out["squad"])
    print(f"Constraint validation: PASSED  (real xPts objective, "
          f"budget ${ilp.BUDGET:.0f}M, max {ilp.NATION_CAP}/nation)\n")

    res = out["squad"].copy()
    order = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}
    res = res.sort_values(by=["starter", "Position", "xPts"],
                          key=lambda s: s.map(order) if s.name == "Position" else s,
                          ascending=[False, True, False])
    xi, bench = res[res["starter"]], res[~res["starter"]]
    form = "-".join(str((xi["Position"] == p).sum()) for p in ("DEF", "MID", "FWD"))

    def line(r):
        c = "  (C)" if r["captain"] else ""
        return (f"   {r['Position']:<3} {r['Player Name']:<22} "
                f"{r['Nation']:<4} ${r['Price ($M)']:>4.1f}M  xPts {r['xPts']:>5.2f}{c}")

    print("=" * 66)
    print("  OPTIMAL SQUAD — real xPts objective")
    print("=" * 66)
    print(f"\n  Starting XI  (formation {form}):")
    for _, r in xi.iterrows():
        print(line(r))
    print("\n  Bench:")
    for _, r in bench.iterrows():
        print(line(r))
    print("\n" + "-" * 66)
    print(f"  Squad cost : ${res['Price ($M)'].sum():.1f}M / ${ilp.BUDGET:.0f}M")
    print(f"  Nations    : {res['Nation'].nunique()} used, max "
          f"{res['Nation'].value_counts().max()}/nation (cap {ilp.NATION_CAP})")
    print(f"  Total xPts (XI + captain): {out['objective']:.2f}")
    print("-" * 66)


if __name__ == "__main__":
    b = float(sys.argv[1]) if len(sys.argv) > 1 else 100.0
    nc = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    main(b, nc)
