#!/usr/bin/env bash
# matchday_refresh.sh — full pipeline refresh after a round of matches finishes.
#
# Run this AFTER a matchday's games have all been played, to:
#   1. Stream new match stats from API-Football into Confluent Cloud
#   2. Derive per-player points from the stream (FIFA scoring rules)
#   3. Wait for you to hand-curate the round's official points in the spreadsheet
#   4. Validate and rebuild the xPts model with the new data
#   5. Refresh fixture difficulty (next opponent shifts each round)
#   6. Optionally backtest model performance
#
# DOES NOT automatically transfer players or commit any irreversible action.
# The transfer recommender is a SEPARATE script — see `transfer_check.sh`.
#
# Usage:  ./matchday_refresh.sh <round_number>   e.g. ./matchday_refresh.sh 3
#
# Exits immediately on any error so you can see what went wrong.
# Assumes the venv312 environment is active (the prompt should show "(venv312)").

set -e                              # stop on any non-zero exit
set -o pipefail

ROUND="${1:-}"
if [[ -z "$ROUND" || ! "$ROUND" =~ ^[0-9]+$ ]]; then
    echo "Usage: $0 <round_number>" >&2
    echo "  e.g.  $0 3" >&2
    exit 1
fi

# Confirm we're in venv312 — wrong env means missing classiq / wrong Python
if [[ -z "${VIRTUAL_ENV:-}" ]] || [[ ! "$VIRTUAL_ENV" == *"venv312"* ]]; then
    echo "⚠  venv312 not active. Run:  source venv312/bin/activate" >&2
    echo "   (current VIRTUAL_ENV: ${VIRTUAL_ENV:-none})" >&2
    exit 1
fi

LEAGUE=1                            # WC2026

banner() {
    echo
    echo "════════════════════════════════════════════════════════════════"
    echo "  $*"
    echo "════════════════════════════════════════════════════════════════"
}

banner "STEP 1/6 — Stream new match stats into Confluent (league $LEAGUE)"
python3 fantasy_apifootball_adapter.py feed "$LEAGUE"

banner "STEP 2/6 — Consume stream → derive per-player points (FIFA scoring)"
python3 fantasy_stream_consumer.py --from-start

banner "STEP 3/6 — MANUAL: hand-curate RND${ROUND}_Points in the spreadsheet"
cat <<EOF
This is the human-in-the-loop step. Open the player pool spreadsheet and:
  1. Add or update the RND${ROUND}_Points tab with the official FIFA points
     per scoring player. Format matches RND1/RND2 tabs.
  2. Save as CSV UTF-8 if you save outside Excel.
This step is the GOLD ground truth that validates everything downstream.
Stream-derived points (just written to stream_points.csv) can be your
starting reference — agreement to within ~0.5 MAE.

When the spreadsheet is updated, press ENTER to continue.
Press Ctrl-C to abort and resume later (no state is lost).
EOF
read -r

banner "STEP 4/6 — Join + validate the round's points"
python3 fantasy_rnd_points.py "$ROUND"
python3 fantasy_rnd_validate.py "$ROUND"

banner "STEP 5/6 — Refresh fixture multipliers (next opponent shifts each round)"
python3 fantasy_fixture_difficulty.py "$LEAGUE"

banner "STEP 6/6 — Rebuild xPts with new round + fresh fixtures"
python3 fantasy_xpts_model.py

# Optional backtest — only meaningful from round 2 onwards
if (( ROUND >= 2 )); then
    banner "OPTIONAL — Backtest: did the previous round's model predict this round?"
    python3 fantasy_xpts_backtest.py
fi

banner "DONE — matchday refresh complete for round $ROUND"
cat <<EOF
What's been updated:
  - Kafka topic wc26.player.stats           (fed)
  - stream_points.csv                       (consumed)
  - rnd${ROUND}_points.csv                  (curated + validated)
  - fixture_multipliers.csv                 (refreshed)
  - xpts.csv                                (rebuilt with $ROUND round(s))

What HAS NOT been changed:
  - Your actual FIFA fantasy squad — nothing was transferred.
  - my_squad.csv — update it manually to reflect your current team
    BEFORE running transfer_check.sh.

Next step when you're ready to think about transfers:
  ./transfer_check.sh <free_transfers>
EOF
