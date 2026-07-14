r"""
Permutation experiments for knockout-stage squad selection.

Quick wrapper that runs the SAME ILP optimiser as fantasy_build_squad.py,
but under different strategic constraints, so you can produce alternative
squads for your other two fantasy teams. Each permutation reflects a real
strategic theory, not just numerical tweaks.

  python3 fantasy_permutations.py             # runs all enabled permutations
  python3 fantasy_permutations.py C           # runs just C
  python3 fantasy_permutations.py C D         # runs C and D

PERMUTATIONS:
  A = pure model output (baseline; equivalent to fantasy_build_squad.py)
  C = 4-3-3 only (4 DEF + 3 MID + 3 FWD); reduces clean-sheet exposure
  D = eggs-in-one-basket: concentrate on heavy favourites (Brazil, Spain,
      Argentina, France) by boosting their xPts; lock-in approach

Each permutation prints its own squad and a one-line summary. Restores all
mutated state at the end — your normal xpts.csv and ILP config are unchanged.
"""
from __future__ import annotations
import sys
import os
import shutil
import tempfile

import pandas as pd

import fantasy_ilp_baseline as ilp
import fantasy_build_squad as bs

# Heavy-favourite nations for permutation D — these are the teams the betting
# markets price as most likely to advance deep. NOT a guarantee; this is a
# strategic bet, not a prediction.
HEAVY_FAVOURITES = ["Brazil", "Spain", "Argentina", "France", "Portugal", "England"]
D_BOOST_FACTOR   = 1.15        # 15% xPts boost for players from favourites

ENABLED_DEFAULT = ["A", "C", "D"]


def run_permutation(label: str, budget: float, nation_cap: int):
    print("\n" + "=" * 70)
    print(f"  PERMUTATION {label}")
    print("=" * 70)
    bs.main(budget=budget, nation_cap=nation_cap)


def perm_A(budget: float, cap: int):
    """Pure model output (baseline)."""
    run_permutation("A — pure model output (baseline)", budget, cap)


def perm_C(budget: float, cap: int):
    """4-3-3 only: 4 DEF, 3 MID, 3 FWD starters; squad shifts MID→DEF stays."""
    saved_xi = dict(ilp.XI_BOUNDS)
    try:
        # Force exactly 4 DEF + 3 MID + 3 FWD + 1 GK = 11
        ilp.XI_BOUNDS = {"GK": (1, 1), "DEF": (4, 4), "MID": (3, 3), "FWD": (3, 3)}
        run_permutation("C — 4-3-3 only (risk diversification)", budget, cap)
    finally:
        ilp.XI_BOUNDS = saved_xi


def perm_D(budget: float, cap: int, pool_xlsx: str = bs.POOL_XLSX,
           xpts_csv: str = bs.XPTS_CSV):
    """Eggs-in-one-basket: boost xPts for heavy-favourite nations, so the
    optimiser concentrates picks there. Restore xpts.csv at the end."""
    tmp = tempfile.mkdtemp(prefix="perm_d_")
    backup = os.path.join(tmp, "xpts.csv.bak")
    try:
        shutil.copy2(xpts_csv, backup)
        # Look up which players are from the favourites via the pool
        pool = pd.read_excel(pool_xlsx, sheet_name="Player Pool")
        pool["pool_row"] = pool.index
        fav_rows = pool[pool["Nation"].isin(HEAVY_FAVOURITES)]["pool_row"].tolist()
        print(f"\n[Permutation D] Boosting xPts by {(D_BOOST_FACTOR-1)*100:.0f}% "
              f"for {len(fav_rows)} players from {HEAVY_FAVOURITES}.")
        xp = pd.read_csv(xpts_csv)
        mask = xp["pool_row"].isin(fav_rows)
        xp.loc[mask, "xpts"] = xp.loc[mask, "xpts"] * D_BOOST_FACTOR
        xp.to_csv(xpts_csv, index=False)
        run_permutation("D — eggs-in-one-basket (heavy favourites)", budget, cap)
    finally:
        if os.path.exists(backup):
            shutil.copy2(backup, xpts_csv)
        shutil.rmtree(tmp, ignore_errors=True)


def perm_E(budget: float, cap: int):
    """3-4-3: exactly 3 DEF + 4 MID + 3 FWD in the starting XI.
    Trades two defenders for two midfielders vs the model's preferred 5-2-3.
    Strategic rationale: the RND3 backtest showed defenders at -0.05 correlation
    (weakest-predicting position); midfielders predicted better. The model shows
    a lower total xPts because it is constrained away from cheap-defender stacking,
    but that number may understate real-world performance if clean sheets are less
    repeatable than the model believes.
    Bench composition: 1 GK + 2 DEF + 1 MID + 0 FWD."""
    saved_xi = dict(ilp.XI_BOUNDS)
    try:
        ilp.XI_BOUNDS = {"GK": (1, 1), "DEF": (3, 3), "MID": (4, 4), "FWD": (3, 3)}
        run_permutation("E — 3-4-3 (midfield-heavy, reduces defender dependency)", budget, cap)
    finally:
        ilp.XI_BOUNDS = saved_xi


PERMUTATIONS = {"A": perm_A, "C": perm_C, "D": perm_D, "E": perm_E}


def main():
    # Parse args: any uppercase letter is a permutation label; the first
    # numeric arg is budget, the second is nation cap. So all of these work:
    #   fantasy_permutations.py
    #   fantasy_permutations.py C D
    #   fantasy_permutations.py 105
    #   fantasy_permutations.py A C D 105 3
    budget, cap = 100.0, 3
    enabled = []
    nums = []
    for a in sys.argv[1:]:
        if a.replace(".", "").isdigit():
            nums.append(float(a))
        else:
            enabled.append(a.upper())
    if len(nums) >= 1: budget = nums[0]
    if len(nums) >= 2: cap = int(nums[1])
    if not enabled:
        enabled = ENABLED_DEFAULT
    unknown = [x for x in enabled if x not in PERMUTATIONS]
    if unknown:
        print(f"Unknown permutation(s): {unknown}. "
              f"Available: {sorted(PERMUTATIONS)}")
        return
    print(f"Running permutations: {enabled}    "
          f"(budget ${budget:.0f}M, max {cap}/nation)")
    for label in enabled:
        PERMUTATIONS[label](budget, cap)
    print("\n" + "=" * 70)
    print("  All permutations complete. Original xpts.csv and ILP config restored.")
    print("=" * 70)


if __name__ == "__main__":
    main()
