r"""
FIFA WC2026 Fantasy — RND points joiner.

Attaches the actual FIFA fantasy points from the spreadsheet's RND<N>_Points
tab to pool rows, producing a clean  pool_row -> points  table that the xPts
model uses as ground truth.

Reuses the verified matching logic from fantasy_name_bridge (accent-insensitive
norm + tie-aware candidate selection) so the RND-tab surname forms ("Martínez",
"Díaz") resolve to the same pool rows the API bridge already agreed on — no
second, divergent matcher.

STRATEGY (within the same nation AND position only):
  1. exact normalised match on display name, full name, or last name
     -> if it resolves to ONE pool row, accept.
  2. otherwise fuzzy; a clear winner (>=0.88 and >=0.03 ahead of #2) accepts,
     anything else is REVIEW (never guessed).
Position is part of the key here (the RND tab's position agrees with the pool),
which cleanly separates same-surname cases like the two ARG 'Martínez'.

OUTPUT:
  rnd<N>_points.csv : pool_row, player_name, nation_code, position, points, method
  + a printed REVIEW/UNMATCHED list for your manual pass.

USAGE:
  python3 fantasy_rnd_points.py 1          # join the RND1_Points tab
"""

from __future__ import annotations
import sys
import csv
from difflib import SequenceMatcher

import pandas as pd

import fantasy_name_bridge as _b
from fantasy_name_bridge import norm, load_pool
PLAYER_POOL = "FIFA_Men_s_World_Cup_2026_Player_Pool.xlsx"

FUZZY_ACCEPT = 0.88
FUZZY_MARGIN = 0.03


def load_rnd_tab(round_no: int, path: str = PLAYER_POOL) -> pd.DataFrame:
    tab = f"RND{round_no}_Points"
    df = pd.read_excel(path, sheet_name=tab)
    need = ["Player Name", "Nation Short Code", "Position", f"RND{round_no}_Points"]
    for c in need:
        if c not in df.columns:
            raise RuntimeError(f"Tab {tab!r} missing column {c!r}. Found: "
                               f"{list(df.columns)}")
    df = df[need].copy()
    for c in ("Player Name", "Nation Short Code", "Position"):
        df[c] = df[c].astype(str).str.strip()
    df = df.rename(columns={f"RND{round_no}_Points": "points"})
    df["points"] = pd.to_numeric(df["points"], errors="coerce")
    return df.dropna(subset=["points"])


def join_round(round_no: int):
    pool = load_pool()
    rnd = load_rnd_tab(round_no)

    # index pool by (nation, position) -> candidate PoolPlayers
    by_np: dict[tuple, list] = {}
    for p in pool:
        by_np.setdefault((p.nation_code, p.position), []).append(p)

    out_rows, review = [], []
    used_pool_rows: set[int] = set()

    for _, r in rnd.iterrows():
        n = norm(r["Player Name"])
        cands = by_np.get((r["Nation Short Code"], r["Position"]), [])

        # 1) exact on display / full / last, unioned (one player or bust)
        union = {}
        for p in cands:
            if (norm(p.display) == n or (p.full and norm(p.full) == n)
                    or norm(p.last) == n):
                union[p.row] = p
        if len(union) == 1:
            p = next(iter(union.values()))
            method = "exact"
        else:
            # 2) fuzzy within candidates, tie-aware
            best, best_r, second_r = None, 0.0, 0.0
            for p in cands:
                pr = max(SequenceMatcher(None, n, norm(k)).ratio()
                         for k in (p.display, p.full, p.last) if k)
                if pr > best_r:
                    best, second_r, best_r = p, best_r, pr
                elif pr > second_r:
                    second_r = pr
            if (best is not None and best_r >= FUZZY_ACCEPT
                    and best_r - second_r >= FUZZY_MARGIN):
                p, method = best, f"fuzzy:{best_r:.2f}"
            else:
                review.append((r, "REVIEW" if best else "UNMATCHED",
                               best, round(best_r, 2)))
                continue

        if p.row in used_pool_rows:
            review.append((r, "DUP", p, 1.0))   # two RND rows -> one pool player
            continue
        used_pool_rows.add(p.row)
        out_rows.append({
            "pool_row": p.row, "player_name": p.display,
            "nation_code": p.nation_code, "position": p.position,
            "points": int(r["points"]), "method": method,
        })

    out_csv = f"rnd{round_no}_points.csv"
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["pool_row", "player_name",
                          "nation_code", "position", "points", "method"])
        w.writeheader()
        w.writerows(sorted(out_rows, key=lambda x: -x["points"]))

    print(f"Joined {len(out_rows)}/{len(rnd)} RND{round_no} rows to pool "
          f"-> {out_csv}")
    if review:
        print(f"\n{len(review)} need manual review. For each, the candidate pool "
              f"rows in the SAME nation+position are listed — copy the right "
              f"pool_row into {out_csv}:\n")
        for r, kind, best, score in review:
            print(f"  [{kind}] RND row: {r['Player Name']!r} {r['Nation Short Code']} "
                  f"{r['Position']}  {int(r['points'])}pts")
            cands = by_np.get((r["Nation Short Code"], r["Position"]), [])
            taken = [p for p in cands if p.row in used_pool_rows]
            free  = [p for p in cands if p.row not in used_pool_rows]
            for p in free:
                print(f"        -> pool_row {p.row:<5} {p.display:<28} "
                      f"{p.position}  ${p.price}   [FREE]")
            for p in taken:
                print(f"           pool_row {p.row:<5} {p.display:<28} "
                      f"{p.position}  ${p.price}   (already used)")
            if not cands:
                print("        (no pool player in this nation+position — likely "
                      "a non-pool call-up; leave unmapped)")
            print()
        print(f"To add a resolved row to {out_csv}, append a line:")
        print(f"  <pool_row>,<player_name>,<nation>,<position>,<points>,manual")


if __name__ == "__main__":
    rn = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    join_round(rn)
