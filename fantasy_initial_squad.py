r"""
FIFA WC2026 Fantasy — INITIAL squad for matchday 1 (pre-data heuristic).

WHY THIS EXISTS
---------------
The initial squad locks at the first kickoff, before any live stats exist.
Best available objective TODAY is a transparent heuristic:

    xPts(player) = Price * TeamMult(nation)

  - Price is FIFA's own quality/popularity signal (the only per-player
    information we have pre-tournament).
  - TeamMult encodes team strength: stronger teams -> more goals, more clean
    sheets, deeper runs. Tiers are grounded in the FIFA World Ranking
    (April/June 2026): top-6 and 7-11 are verified from the published list;
    a third tier reflects ranking ~12-20 (verified); everything else is
    NEUTRAL 1.00 because I could not verify ranks beyond the top ~20 —
    edit MULT_OVERRIDES to apply your own judgment.

This is NOT a model. It ignores fixtures, minutes risk, set-piece duty,
and player form. It exists to beat (a) random squads and (b) pure
price-maximisation, and to be replaced from matchday 2 by real xPts.

Run:  python3 fantasy_initial_squad.py
Then enter the squad manually at play.fifa.com before the deadline.
"""

import pandas as pd
import fantasy_ilp_baseline as ilp

POOL = ilp.INPUT_XLSX   # same source as the optimiser

# Tiers grounded in the published FIFA ranking (see chat citations).
TIER1 = {"ARG", "FRA", "ESP", "ENG", "POR", "BRA"}            # ranks 1-6
TIER2 = {"NED", "MAR", "BEL", "GER", "CRO"}                   # ranks 7-11
TIER3 = {"COL", "SEN", "MEX", "USA", "URU", "JPN", "SUI"}     # ranks ~13-19

MULT = {1: 1.30, 2: 1.18, 3: 1.08}
DEFAULT_MULT = 1.00     # all unverified nations stay neutral

# Your judgment calls go here, e.g. {"NOR": 1.08, "KOR": 1.05}
MULT_OVERRIDES: dict[str, float] = {}


def team_mult(code: str) -> float:
    if code in MULT_OVERRIDES:
        return MULT_OVERRIDES[code]
    if code in TIER1:
        return MULT[1]
    if code in TIER2:
        return MULT[2]
    if code in TIER3:
        return MULT[3]
    return DEFAULT_MULT


def main():
    df = pd.read_excel(POOL)
    for c in ("Player Name", "Nation Short Code", "Nation", "Position"):
        df[c] = df[c].astype(str).str.strip()
    df["xPts"] = df["Price ($M)"].astype(float) * df["Nation Short Code"].map(team_mult)
    df["pid"] = df.index

    ilp.USE_PLACEHOLDER = False          # use OUR xPts column
    out = ilp.optimise(df)
    ilp.validate(out["squad"])
    print("Constraint validation: PASSED  (heuristic objective)\n")

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
                f"{r['Nation Short Code']:<4} ${r['Price ($M)']:>4.1f}M  "
                f"xPts {r['xPts']:>5.1f}{c}")

    print("=" * 64)
    print("  INITIAL SQUAD — heuristic objective (price x team tier)")
    print("=" * 64)
    print(f"\n  Starting XI  (formation {form}):")
    for _, r in xi.iterrows():
        print(line(r))
    print("\n  Bench:")
    for _, r in bench.iterrows():
        print(line(r))
    print("\n" + "-" * 64)
    print(f"  Cost: ${res['Price ($M)'].sum():.1f}M / $100M   "
          f"Nations: {res['Nation Short Code'].nunique()}, "
          f"max {res['Nation Short Code'].value_counts().max()}/nation")
    print("-" * 64)


if __name__ == "__main__":
    main()
