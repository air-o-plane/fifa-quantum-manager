r"""
Cross-validate stream-derived points against the hand-entered FIFA points.

Joins stream_points.csv (from the Kafka consumer, FIFA rules applied to the
streamed box scores) against rnd1_points.csv (Oliver's hand-entered actual
FIFA fantasy points) on pool_row, and reports agreement.

If the two AGREE for players in both, the producer->Kafka->consumer->scoring
path is trustworthy, and stream_points can fill in the ~553 players who have
no hand-entered row. If they DIVERGE, the gap tells us what the FIFA scoring
in the consumer is missing (e.g. a points category we didn't encode).

  python3 fantasy_stream_validate.py
"""
from __future__ import annotations
import pandas as pd
from fantasy_name_bridge import _read_csv


def main():
    try:
        stream = _read_csv("stream_points.csv")
        hand = _read_csv("rnd1_points.csv")
    except FileNotFoundError as e:
        print(f"Missing {e.filename}. Run the consumer and the rnd joiner first.")
        return

    s = stream[["pool_row", "player_name", "nation_code", "position",
                "stream_points"]].copy()
    h = hand[["pool_row", "points"]].copy().rename(columns={"points": "hand_points"})
    both = s.merge(h, on="pool_row", how="inner")

    if both.empty:
        print("No players appear in BOTH files — nothing to cross-check.")
        return

    both["diff"] = both["stream_points"] - both["hand_points"]
    both = both.sort_values("diff", key=abs, ascending=False)

    n = len(both)
    exact = (both["diff"].abs() < 0.5).sum()
    close = (both["diff"].abs() <= 2).sum()
    mae = both["diff"].abs().mean()

    print(f"Players in both stream and hand-entered: {n}")
    print(f"  exact match (<0.5 apart) : {exact}/{n} ({100*exact/n:.0f}%)")
    print(f"  within 2 points          : {close}/{n} ({100*close/n:.0f}%)")
    print(f"  mean abs difference      : {mae:.2f} points")

    print(f"\nLargest discrepancies (stream - hand):")
    print(f"  {'player':<22}{'nat':<5}{'pos':<5}{'stream':>7}{'hand':>6}{'diff':>6}")
    for _, r in both.head(15).iterrows():
        print(f"  {str(r['player_name']):<22}{str(r['nation_code']):<5}"
              f"{str(r['position']):<5}{r['stream_points']:>7.1f}"
              f"{r['hand_points']:>6.0f}{r['diff']:>+6.1f}")

    # Players the stream scored but hand-entry missed (potential pickups)
    only_stream = s[~s["pool_row"].isin(h["pool_row"])]
    only_stream = only_stream[only_stream["stream_points"] > 0]
    print(f"\n{len(only_stream)} players have stream points but NO hand-entered row "
          f"(stream fills these gaps).")
    for _, r in only_stream.sort_values("stream_points", ascending=False).head(8).iterrows():
        print(f"  {str(r['player_name']):<22}{str(r['nation_code']):<5}"
              f"{str(r['position']):<5}{r['stream_points']:>6.1f}")


if __name__ == "__main__":
    main()
