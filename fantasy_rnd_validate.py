r"""
Validate a hand-edited rnd<N>_points.csv against the pool.

Catches the mistakes hand-editing introduces: bad/duplicate pool_row,
position mismatch vs the pool, out-of-range points, malformed rows.
Read-only; touches nothing. Run after you add manual rows.

  python3 fantasy_rnd_validate.py 1
"""
from __future__ import annotations
import sys
import pandas as pd
from fantasy_name_bridge import load_pool, _read_csv

POINTS_MIN, POINTS_MAX = -10, 40   # sane fantasy range; widen if needed


def validate(round_no: int):
    csv_path = f"rnd{round_no}_points.csv"
    pool = {p.row: p for p in load_pool()}
    try:
        df = _read_csv(csv_path)
    except FileNotFoundError:
        print(f"Missing {csv_path}. Run fantasy_rnd_points.py {round_no} first.")
        return

    issues, ok = [], 0
    seen: dict[int, str] = {}
    for i, r in df.iterrows():
        line = i + 2  # 1-based + header
        try:
            pr = int(r["pool_row"])
        except (ValueError, TypeError):
            issues.append(f"line {line}: pool_row not an integer ({r['pool_row']!r})")
            continue
        p = pool.get(pr)
        if p is None:
            issues.append(f"line {line}: pool_row {pr} not in pool ({r.get('player_name')})")
            continue
        if pr in seen:
            issues.append(f"line {line}: pool_row {pr} ({p.display}) DUPLICATE "
                          f"— also '{seen[pr]}'")
            continue
        seen[pr] = str(r.get("player_name"))
        if str(r.get("position")).strip() != p.position:
            issues.append(f"line {line}: position {r.get('position')!r} != pool "
                          f"{p.position!r} for {p.display} (row {pr})")
        try:
            pts = float(r["points"])
            if not (POINTS_MIN <= pts <= POINTS_MAX):
                issues.append(f"line {line}: points {pts} out of range "
                              f"[{POINTS_MIN},{POINTS_MAX}]")
        except (ValueError, TypeError):
            issues.append(f"line {line}: points not numeric ({r['points']!r})")
        ok += 1

    print(f"{csv_path}: {len(df)} rows, {ok} structurally valid, "
          f"{len(seen)} unique pool players.")
    if issues:
        print(f"\n{len(issues)} ISSUE(S) to fix:")
        for x in issues:
            print(f"  {x}")
    else:
        print("No issues — the points table is clean and ready for the xPts model.")


if __name__ == "__main__":
    validate(int(sys.argv[1]) if len(sys.argv) > 1 else 1)
