r"""
xPts BACKTEST — does the model trained on all PRIOR rounds predict the
LATEST round's actuals?

  forecast = xPts rebuilt from rnd1..rnd<N-1>_points.csv  (no fixture adj.)
  actuals  = rnd<N>_points.csv
  where N is the highest round number you have a points file for.

So as the tournament progresses, this automatically tests "RND2 forecast → RND3
actuals" once you have RND3, then "RND3 → RND4", etc. — always the most useful
"does the current state of the model predict the next round" question.

Fixture multipliers are excluded for two honest reasons: (a) the saved file
was built pointing at whatever was "next" at fixture-script-time, which is now
stale; (b) it isolates the test of FORM + PRIOR signal from the fixture layer.
So this is the FLOOR of model quality. With fixture adjustment, real predictive
power should be a bit higher.

Procedure (safe by construction):
  1. Snapshot xpts.csv so we can restore it.
  2. Temporarily HIDE rnd<N>_points.csv + fixture_multipliers.csv.
  3. Rebuild xPts on rnd1..rnd<N-1> — this is the FORECAST.
  4. Restore everything.
  5. Join forecast against rnd<N> actuals on pool_row; report metrics.

  python3 fantasy_xpts_backtest.py
"""
from __future__ import annotations
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile

import pandas as pd

from fantasy_name_bridge import _read_csv


def _discover_rounds() -> list[int]:
    rounds = []
    for path in glob.glob("rnd*_points.csv"):
        m = re.match(r"rnd(\d+)_points\.csv$", path)
        if m:
            rounds.append(int(m.group(1)))
    return sorted(rounds)


def _safe_move(src: str, dst: str) -> bool:
    if os.path.exists(src):
        shutil.move(src, dst)
        return True
    return False


def main():
    rounds = _discover_rounds()
    if len(rounds) < 2:
        print(f"Found rounds: {rounds}. Need at least 2 to backtest "
              f"(prior rounds -> latest round). Add more rnd<N>_points.csv first.")
        return

    target = rounds[-1]
    train  = rounds[:-1]
    files_to_hide = [f"rnd{target}_points.csv", "fixture_multipliers.csv"]

    print(f"Discovered rounds with points data: {rounds}")
    print(f"Forecast trained on: {train}    Actuals from: rnd{target}")
    print(f"(Fixture adjustment excluded - measures FORM+PRIOR floor.)\n")

    tmp = tempfile.mkdtemp(prefix="xpts_backtest_")
    moved = []
    saved_xpts = None
    try:
        if os.path.exists("xpts.csv"):
            saved_xpts = os.path.join(tmp, "xpts.csv.current")
            shutil.copy2("xpts.csv", saved_xpts)

        for f in files_to_hide:
            dst = os.path.join(tmp, f)
            if _safe_move(f, dst):
                moved.append((f, dst))

        print(f"Rebuilding xPts using rounds {train} (no fixtures) - the FORECAST...")
        r = subprocess.run([sys.executable, "fantasy_xpts_model.py"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("xPts model failed:\n", r.stderr); return
        forecast = _read_csv("xpts.csv")[["pool_row", "xpts", "basis"]].copy()
        forecast = forecast.rename(columns={"xpts": "xpts_forecast"})
        if saved_xpts:
            shutil.copy2(saved_xpts, "xpts.csv")
    finally:
        for orig, hidden in moved:
            if os.path.exists(hidden) and not os.path.exists(orig):
                shutil.move(hidden, orig)
        shutil.rmtree(tmp, ignore_errors=True)

    actuals = _read_csv(f"rnd{target}_points.csv").rename(columns={"points": "actual"})
    df = actuals.merge(forecast, on="pool_row", how="left")
    df["xpts_forecast"] = df["xpts_forecast"].fillna(0.0)
    df["basis"] = df["basis"].fillna("prior(no-form)")
    df["error"] = df["actual"] - df["xpts_forecast"]

    n = len(df)
    corr = df["xpts_forecast"].corr(df["actual"])
    mae = df["error"].abs().mean()
    rmse = (df["error"] ** 2).mean() ** 0.5
    mean_actual = df["actual"].mean()
    naive_mae = (df["actual"] - mean_actual).abs().mean()

    train_label = "+".join(f"R{r}" for r in train)
    print("\n" + "=" * 70)
    print(f"  xPts BACKTEST - forecast({train_label}) vs RND{target} actuals")
    print("=" * 70)
    print(f"  players in BOTH RND{target} actuals and the forecast : {n}")
    print(f"  Pearson correlation (forecast vs actual)             : {corr:+.3f}")
    print(f"  MAE forecast                                          : {mae:.2f} pts")
    print(f"  RMSE forecast                                         : {rmse:.2f} pts")
    print(f"  MAE of a naive 'predict the mean' baseline            : {naive_mae:.2f} pts")
    skill = (naive_mae - mae) / naive_mae if naive_mae > 0 else 0
    print(f"  skill score vs naive baseline                         : {skill:+.1%}")
    print("  (positive skill = model beats predicting the average; 0 = tied)")

    print("\n  By position:")
    print(f"    {'pos':<5}{'n':>5}{'corr':>8}{'MAE':>7}{'mean act':>10}")
    for pos, g in df.groupby("position"):
        if len(g) < 3: continue
        c = g["xpts_forecast"].corr(g["actual"])
        print(f"    {pos:<5}{len(g):>5}{c:>+8.3f}{g['error'].abs().mean():>7.2f}"
              f"{g['actual'].mean():>10.2f}")

    print("\n  By forecast basis:")
    df["basis_kind"] = df["basis"].apply(
        lambda b: "had-form" if str(b).startswith("form") else "prior-only")
    for kind, g in df.groupby("basis_kind"):
        c = g["xpts_forecast"].corr(g["actual"])
        print(f"    {kind:<12}n={len(g):>4}  corr={c:+.3f}  MAE={g['error'].abs().mean():.2f}")

    print(f"\n  Largest over-forecasts (model said high, RND{target} said low):")
    print(f"    {'player':<22}{'pos':<5}{'forecast':>10}{'actual':>8}")
    for _, r in df.sort_values("error").head(8).iterrows():
        print(f"    {str(r['player_name']):<22}{str(r['position']):<5}"
              f"{r['xpts_forecast']:>10.2f}{r['actual']:>8.0f}")

    print(f"\n  Largest under-forecasts (model said low, RND{target} said high):")
    print(f"    {'player':<22}{'pos':<5}{'forecast':>10}{'actual':>8}")
    for _, r in df.sort_values("error", ascending=False).head(8).iterrows():
        print(f"    {str(r['player_name']):<22}{str(r['position']):<5}"
              f"{r['xpts_forecast']:>10.2f}{r['actual']:>8.0f}")


if __name__ == "__main__":
    main()
