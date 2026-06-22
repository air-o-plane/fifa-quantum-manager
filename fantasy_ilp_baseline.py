#!/usr/bin/env python3
r"""
FIFA Men's World Cup 2026 Fantasy — ILP baseline squad optimiser.

PURPOSE
-------
This is the *classical baseline* for the project. It encodes the real
fantasy constraints as an integer linear program (ILP) and returns the
provably optimal squad for a given objective. Its job is twofold:

  1. Prove the constraint encoding is correct (every returned squad is legal).
  2. Provide a known-optimal benchmark to measure the Classiq QAOA branch
     against on identical inputs.

OBJECTIVE / xPts
----------------
The optimiser maximises the sum of expected points (xPts) over the
STARTING XI. We do NOT yet have a real xPts model — that comes from the
form/news data feed. Until then this script uses a clearly-labelled
PLACEHOLDER objective (see build_placeholder_xpts). Swapping in real xPts
is a one-line change: provide a column named 'xPts' in the input and set
USE_PLACEHOLDER = False.

MODEL
-----
Two layers of binary decision variables per player i:
  squad_i  = 1 if player i is in the 15-man squad
  start_i  = 1 if player i is in the starting XI   (start_i <= squad_i)
  capt_i   = 1 if player i is the captain          (capt_i  <= start_i)

Squad layer (budget applies to all 15):
  - exactly 15 players
  - position quotas: GK=2, DEF=5, MID=5, FWD=3
  - sum(price * squad) <= BUDGET
  - per-nation: sum(squad) <= NATION_CAP

Starting-XI layer (points accrue here):
  - exactly 11 starters
  - GK=1; DEF in [3,5]; MID in [2,5]; FWD in [1,3]; outfield total = 10
    (this range encoding spans every valid formation: 3-4-3, 3-5-2,
     4-3-3, 4-4-2, 4-5-1, 5-3-2, 5-4-1, 5-2-3, ...)
  - exactly 1 captain, drawn from the starters

Objective:
  maximise  sum(xPts * start)  +  sum(xPts * capt)
            \------ XI points -----/   \-- captain's points counted twice --/
"""

from pathlib import Path
import pandas as pd
import pulp

# ----------------------------------------------------------------------
# Configuration  (group-stage values; R32 onward: BUDGET=105, NATION_CAP rises)
# ----------------------------------------------------------------------
INPUT_XLSX   = "FIFA_Men_s_World_Cup_2026_Player_Pool.xlsx"
BUDGET       = 100.0          # $M, group stage
NATION_CAP   = 3              # max players per nation, group stage & R32
SQUAD_SIZE   = 15
SQUAD_QUOTA  = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
XI_SIZE      = 11
XI_BOUNDS    = {"GK": (1, 1), "DEF": (3, 5), "MID": (2, 5), "FWD": (1, 3)}
USE_PLACEHOLDER = True        # set False once a real 'xPts' column is supplied


def load_pool(path: str) -> pd.DataFrame:
    df = pd.read_excel(path)
    # Defensive cleaning (idempotent — the source is already clean).
    for c in ("Player Name", "Nation", "Position"):
        df[c] = df[c].astype(str).str.strip()
    df["Price ($M)"] = pd.to_numeric(df["Price ($M)"], errors="raise")
    # (Player Name, Nation) is unique in the cleaned pool — use it as the key.
    df = df.reset_index(drop=True)
    df["pid"] = df.index  # stable integer id keying the ILP variables
    return df


def build_placeholder_xpts(df: pd.DataFrame) -> pd.Series:
    """
    PLACEHOLDER ONLY — NOT a real points projection.

    Uses price as a crude proxy for quality so the budget constraint
    actually binds (pricier players look 'better', forcing real trade-offs).
    This exists solely to exercise the constraint machinery. Replace with
    a genuine xPts model from the form/news feed before trusting any squad.
    """
    return df["Price ($M)"].astype(float)


def optimise(df: pd.DataFrame) -> dict:
    if USE_PLACEHOLDER or "xPts" not in df.columns:
        df = df.assign(xPts=build_placeholder_xpts(df))

    pids   = df["pid"].tolist()
    pos    = dict(zip(df["pid"], df["Position"]))
    price  = dict(zip(df["pid"], df["Price ($M)"]))
    nation = dict(zip(df["pid"], df["Nation"]))
    xpts   = dict(zip(df["pid"], df["xPts"]))

    prob = pulp.LpProblem("WC2026_Fantasy_Squad", pulp.LpMaximize)

    squad = pulp.LpVariable.dicts("squad", pids, cat="Binary")
    start = pulp.LpVariable.dicts("start", pids, cat="Binary")
    capt  = pulp.LpVariable.dicts("capt",  pids, cat="Binary")

    # Objective: starting-XI points, with the captain counted a second time.
    prob += pulp.lpSum(xpts[i] * start[i] for i in pids) \
          + pulp.lpSum(xpts[i] * capt[i]  for i in pids)

    # --- Squad layer ---
    prob += pulp.lpSum(squad[i] for i in pids) == SQUAD_SIZE
    for p, q in SQUAD_QUOTA.items():
        prob += pulp.lpSum(squad[i] for i in pids if pos[i] == p) == q
    prob += pulp.lpSum(price[i] * squad[i] for i in pids) <= BUDGET
    for nat in set(nation.values()):
        prob += pulp.lpSum(squad[i] for i in pids if nation[i] == nat) <= NATION_CAP

    # --- Starting-XI layer ---
    prob += pulp.lpSum(start[i] for i in pids) == XI_SIZE
    for p, (lo, hi) in XI_BOUNDS.items():
        sel = pulp.lpSum(start[i] for i in pids if pos[i] == p)
        prob += sel >= lo
        prob += sel <= hi
    for i in pids:                      # a starter must be in the squad
        prob += start[i] <= squad[i]

    # --- Captain ---
    prob += pulp.lpSum(capt[i] for i in pids) == 1
    for i in pids:                      # captain must be a starter
        prob += capt[i] <= start[i]

    status = prob.solve(pulp.PULP_CBC_CMD(msg=0))
    if pulp.LpStatus[prob.status] != "Optimal":
        raise RuntimeError(f"Solver status: {pulp.LpStatus[prob.status]}")

    chosen = [i for i in pids if squad[i].value() > 0.5]
    res = df[df["pid"].isin(chosen)].copy()
    res["starter"] = res["pid"].map(lambda i: start[i].value() > 0.5)
    res["captain"] = res["pid"].map(lambda i: capt[i].value() > 0.5)
    return {"squad": res, "objective": pulp.value(prob.objective)}


def validate(res: pd.DataFrame):
    """Independently re-check every constraint on the returned squad."""
    assert len(res) == SQUAD_SIZE, "squad size"
    for p, q in SQUAD_QUOTA.items():
        assert (res["Position"] == p).sum() == q, f"squad quota {p}"
    assert res["Price ($M)"].sum() <= BUDGET + 1e-6, "budget"
    assert res["Nation"].value_counts().max() <= NATION_CAP, "nation cap"
    xi = res[res["starter"]]
    assert len(xi) == XI_SIZE, "XI size"
    for p, (lo, hi) in XI_BOUNDS.items():
        assert lo <= (xi["Position"] == p).sum() <= hi, f"XI bound {p}"
    assert res["captain"].sum() == 1, "one captain"
    assert res.loc[res["captain"], "starter"].all(), "captain is a starter"


def report(out: dict):
    res = out["squad"].copy()
    order = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}
    res = res.sort_values(by=["starter", "Position", "Price ($M)"],
                          key=lambda s: s.map(order) if s.name == "Position" else s,
                          ascending=[False, True, False])
    xi = res[res["starter"]]
    bench = res[~res["starter"]]
    form = "-".join(str((xi["Position"] == p).sum()) for p in ("DEF", "MID", "FWD"))

    def line(r):
        c = "  (C)" if r["captain"] else ""
        return f"   {r['Position']:<3} {r['Player Name']:<22} {r['Nation']:<4} ${r['Price ($M)']:>4.1f}M{c}"

    print("=" * 60)
    print("  OPTIMAL SQUAD  —  PLACEHOLDER OBJECTIVE (price proxy)")
    print("=" * 60)
    print(f"\n  Starting XI   (formation {form}):")
    for _, r in xi.iterrows():
        print(line(r))
    print(f"\n  Bench:")
    for _, r in bench.iterrows():
        print(line(r))
    print("\n" + "-" * 60)
    print(f"  Squad cost : ${res['Price ($M)'].sum():.1f}M / ${BUDGET:.0f}M")
    print(f"  Nations    : {res['Nation'].nunique()} used, max "
          f"{res['Nation'].value_counts().max()}/nation (cap {NATION_CAP})")
    print(f"  Objective  : {out['objective']:.1f}  (placeholder units)")
    print("-" * 60)


if __name__ == "__main__":
    pool = load_pool(INPUT_XLSX)
    print(f"Loaded {len(pool)} players, {pool['Nation'].nunique()} nations.\n")
    out = optimise(pool)
    validate(out["squad"])
    print("Constraint validation: PASSED\n")
    report(out)
