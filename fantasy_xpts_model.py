r"""
FIFA WC2026 Fantasy — xPts model (Option 2: form-weighted, with a fixture hook).

WHAT IT PRODUCES
----------------
One expected-points number per pool player for the UPCOMING round, written to
xpts.csv (pool_row, player_name, nation_code, position, price, xpts, basis).
Both optimisers (ILP baseline, Classiq QAOA) consume this column.

THE MODEL (deliberately simple and interpretable)
-------------------------------------------------
Each player's xPts is a shrinkage blend of two signals:

    xpts = w * form_estimate  +  (1 - w) * prior

  - prior          : expected points implied by PRICE and POSITION. Price is
                     FIFA's own quality signal; we calibrate a points-per-$
                     baseline from the ACTUAL RND1 points we have, per position
                     (so the prior is fitted to reality, not guessed).
  - form_estimate  : the player's own recent FIFA points (from rnd*_points.csv),
                     regressed toward their positional mean to tame one-game
                     variance.
  - w (form weight): grows with how many rounds of evidence we have. After ONE
                     round, evidence is thin, so w is LOW (prior dominates).
                     w = rounds_played / (rounds_played + K).  K=2 by default,
                     so MD2 leans ~1/3 on form, MD3 ~1/2, etc.

HONEST LIMITS
-------------
- One round of data is mostly noise; this model says so by keeping w low early.
- A blank in rnd*_points (didn't feature) is treated as "no form signal", NOT
  as zero points — those players fall back toward the prior, with a small
  minutes-risk haircut since not playing last round is mild negative signal.
- FIXTURE ADJUSTMENT (Option 3) is stubbed: fixture_multiplier() returns 1.0
  for everyone now; wire in opponent strength next and xpts scales by it.

USAGE
-----
  python3 fantasy_xpts_model.py            # uses rounds found in the workbook
  -> writes xpts.csv, prints the top 25 and basis breakdown
"""

from __future__ import annotations
import sys
import os
import csv
import glob
import statistics as st

import pandas as pd

from fantasy_name_bridge import load_pool, _read_csv

K_SHRINK_FORM   = 2.0    # form-weight half-saturation (rounds)
K_SHRINK_PLAYER = 1.5    # baseline per-player regression-to-mean strength
# Backtest after RND2 (corr +0.282, MAE 2.58) showed the model over-punishes
# players with a single low-scoring observation — many under-forecasts
# (Gakpo, Ueda, Brobbey...) were 1-game blankers who exploded next round.
# So for nobs=1, shrink HARDER toward the positional mean; ease off with more
# games, where the player's own form is genuinely informative.
K_SHRINK_BY_NOBS = {1: 2.5, 2: 1.5, 3: 1.0}      # cap drops as games accumulate
NO_PLAY_HAIRCUT = 0.85   # multiplier on prior for players with no recent minutes


# ----------------------------------------------------------------------
# Inputs
# ----------------------------------------------------------------------
def load_round_points() -> dict[int, dict[int, float]]:
    """Read every rnd<N>_points.csv present -> {round_no: {pool_row: points}}."""
    rounds: dict[int, dict[int, float]] = {}
    for path in sorted(glob.glob("rnd*_points.csv")):
        try:
            n = int("".join(c for c in path if c.isdigit()))
        except ValueError:
            continue
        df = _read_csv(path)
        rounds[n] = {int(r["pool_row"]): float(r["points"])
                     for _, r in df.iterrows()
                     if pd.notna(r.get("pool_row")) and pd.notna(r.get("points"))}
    return rounds


# ----------------------------------------------------------------------
# Fixture adjustment (Option 3) — position-aware, from fixture_multipliers.csv
# ----------------------------------------------------------------------
def load_fixture_mult():
    """{code: (attack_mult, defence_mult)} or {} if not generated yet."""
    try:
        from fantasy_fixture_difficulty import load_multipliers
        return load_multipliers()
    except Exception:
        return {}


def fixture_multiplier(nation_code: str, position: str, table: dict) -> float:
    """Attackers/mids scale by attack_mult (opponent defence); defenders/keepers
    by defence_mult (opponent attack). 1.0 if no fixture data yet."""
    mm = table.get(nation_code)
    if not mm:
        return 1.0
    attack_mult, defence_mult = mm
    return attack_mult if position in ("MID", "FWD") else defence_mult


# ----------------------------------------------------------------------
# Model
# ----------------------------------------------------------------------
def build_xpts():
    pool = load_pool()
    rounds = load_round_points()
    n_rounds = len(rounds)
    if n_rounds == 0:
        print("No rnd*_points.csv found. Run the joiner first.")
        return

    # Per-position price->points baseline, fitted from ACTUAL points we have.
    # Aggregate every (price, points) observation across all rounds by position.
    obs: dict[str, list[tuple[float, float]]] = {p: [] for p in ("GK","DEF","MID","FWD")}
    pool_by_row = {p.row: p for p in pool}
    for rnd in rounds.values():
        for row, pts in rnd.items():
            pp = pool_by_row.get(row)
            if pp:
                obs[pp.position].append((pp.price, pts))

    # Simple per-position rate: mean points per $ (robust, no overfitting).
    ppd: dict[str, float] = {}
    pos_mean: dict[str, float] = {}
    for posn, xs in obs.items():
        if xs:
            ppd[posn] = sum(p for _, p in xs) / sum(pr for pr, _ in xs)
            pos_mean[posn] = st.mean(p for _, p in xs)
        else:
            ppd[posn] = 0.5      # fallback if a position had no scorers
            pos_mean[posn] = 2.0

    # OPTION 2 — stream-derived points as fallback for players with no
    # hand-curated rnd<N>_points.csv row. Activates automatically when
    # stream_points.csv is present (written by fantasy_stream_consumer.py).
    # These players currently fall all the way back to the prior; stream data
    # gives them a real, validated form signal instead.
    # The stream's per-match average uses the same nobs-dependent shrinkage as
    # hand-curated form, since it's similarly noisy (MAE ~0.55 per round).
    stream_fallback: dict[int, tuple[float, int]] = {}   # pool_row -> (avg, nmatches)
    stream_csv = "stream_points.csv"
    if os.path.exists(stream_csv):
        try:
            sdf = _read_csv(stream_csv)
            for _, r in sdf.iterrows():
                pr = int(r["pool_row"])
                matches = int(r.get("matches", 1) or 1)
                avg = float(r.get("stream_points", 0) or 0) / matches
                stream_fallback[pr] = (avg, matches)
        except Exception as e:
            print(f"  (stream fallback skipped: {e})")

    w_form = n_rounds / (n_rounds + K_SHRINK_FORM)   # global form weight
    fixture_table = load_fixture_mult()

    # Per-player recent form: mean of their available round points, regressed
    # toward the positional mean (shrinkage tuned by # observations).
    def player_form(row: int, posn: str):
        vals = [rounds[r][row] for r in rounds if row in rounds[r]]
        if vals:
            m = st.mean(vals)
            nobs = len(vals)
            k = K_SHRINK_BY_NOBS.get(nobs, 0.75)
            return (nobs * m + k * pos_mean[posn]) / (nobs + k), nobs, "curated"
        # No hand-curated data — try stream-derived fallback
        if row in stream_fallback:
            avg, nm = stream_fallback[row]
            k = K_SHRINK_BY_NOBS.get(nm, 0.75)
            shrunk = (nm * avg + k * pos_mean[posn]) / (nm + k)
            return shrunk, nm, "stream"
        return None, 0, "none"

    out = []
    n_stream_used = 0
    for p in pool:
        prior = ppd[p.position] * p.price
        form, nobs, source = player_form(p.row, p.position)
        if form is None:
            # no curated OR stream data -> lean on prior with a mild haircut
            xpts = prior * NO_PLAY_HAIRCUT
            basis = "prior(no-form)"
        else:
            xpts = w_form * form + (1 - w_form) * prior
            if source == "stream":
                basis = f"stream({nobs})+prior"
                n_stream_used += 1
            else:
                basis = f"form({nobs})+prior"
        xpts *= fixture_multiplier(p.nation_code, p.position, fixture_table)
        out.append({"pool_row": p.row, "player_name": p.display,
                    "nation_code": p.nation_code, "position": p.position,
                    "price": p.price, "xpts": round(xpts, 2), "basis": basis})

    out.sort(key=lambda r: -r["xpts"])
    with open("xpts.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)

    print(f"Wrote xpts.csv for {len(out)} players "
          f"({n_rounds} round(s) of data, form weight w={w_form:.2f}).")
    print("Fixture adjustment: " + (f"APPLIED ({len(fixture_table)} nations)"
          if fixture_table else "none (run fantasy_fixture_difficulty.py first)"))
    print(f"Per-position points-per-$ baseline: "
          f"{ {k: round(v,2) for k,v in ppd.items()} }")
    print("\nTop 25 by xPts:")
    print(f"  {'player':<24}{'nat':<5}{'pos':<5}{'$':>6}{'xPts':>7}  basis")
    for r in out[:25]:
        print(f"  {r['player_name']:<24}{r['nation_code']:<5}{r['position']:<5}"
              f"{r['price']:>6}{r['xpts']:>7}  {r['basis']}")
    nf = sum(1 for r in out if r["basis"].startswith("prior"))
    nc = sum(1 for r in out if r["basis"].startswith("form"))
    ns = n_stream_used
    print(f"\n{nc} players have curated form signal; "
          f"{ns} use stream-derived fallback; "
          f"{nf} fall back to the prior.")


if __name__ == "__main__":
    build_xpts()
