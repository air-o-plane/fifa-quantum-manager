#!/usr/bin/env bash
# transfer_check.sh — run this BEFORE making any transfer decision.
#
# Two fast steps:
#   1. Refresh injury / availability feed from API-Football (~4hr update cycle)
#   2. Run the recommender against your CURRENT squad (my_squad.csv)
#
# Run cadence: the day before a matchday, AND right before confirming any
# transfer in the FIFA app. Daily during the tournament is fine too — the
# pipeline uses ~3 of your 7500 daily requests.
#
# Usage:  ./transfer_check.sh [free_transfers] [budget] [nation_cap]
#         defaults: free_transfers=2, budget=100, nation_cap=3
#
# CRITICAL: my_squad.csv must reflect your CURRENT 15-player squad
# (player_name, nation_code, position). If you've made transfers in the FIFA
# app since the last run, edit this file first.

set -e
set -o pipefail

FREE="${1:-2}"
BUDGET="${2:-100}"
CAP="${3:-3}"
LEAGUE=1

if [[ -z "${VIRTUAL_ENV:-}" ]] || [[ ! "$VIRTUAL_ENV" == *"venv312"* ]]; then
    echo "⚠  venv312 not active. Run:  source venv312/bin/activate" >&2
    exit 1
fi

if [[ ! -f my_squad.csv ]]; then
    echo "⚠  my_squad.csv not found." >&2
    echo "   Create it with columns: player_name,nation_code,position" >&2
    echo "   and one row per player in your CURRENT 15-player squad." >&2
    exit 1
fi

# Friendly reminder if my_squad.csv hasn't been touched recently — easy to
# forget to update it after FIFA-app transfers.
if [[ "$(uname)" == "Darwin" ]]; then
    AGE_HOURS=$(( ( $(date +%s) - $(stat -f %m my_squad.csv) ) / 3600 ))
else
    AGE_HOURS=$(( ( $(date +%s) - $(stat -c %Y my_squad.csv) ) / 3600 ))
fi
if (( AGE_HOURS > 48 )); then
    echo "ℹ  my_squad.csv was last modified ${AGE_HOURS}h ago — make sure it"
    echo "   still reflects your current squad after any recent transfers."
    echo "   Press ENTER to continue, Ctrl-C to abort and edit first."
    read -r
fi

banner() {
    echo
    echo "═══════════════════════════════════════════════════════════════"
    echo "  $*"
    echo "═══════════════════════════════════════════════════════════════"
}

banner "STEP 1/2 — Refresh injury / availability feed"
python3 fantasy_apifootball_adapter.py injuries "$LEAGUE"

banner "STEP 2/2 — Run transfer recommender ($FREE free, \$${BUDGET}M, cap $CAP/nation)"
python3 fantasy_transfer_recommender.py "$FREE" "$BUDGET" "$CAP"

echo
echo "═══════════════════════════════════════════════════════════════"
echo "  Reminders before you confirm anything in the FIFA app:"
echo "═══════════════════════════════════════════════════════════════"
cat <<'EOF'
  1. The injury feed has known gaps for national-team-only injuries
     (the Schlotterbeck case). Check team news manually for your starters.
  2. The recommender ranks by total-squad xPts — moves that improve a
     BENCH player are near-worthless. Prioritise starting-XI upgrades.
  3. Your captain pick outweighs any single transfer. Pick the premium
     forward with the softest fixture.
  4. The guardrail's "best" line is the optimal stopping point. Going
     beyond it DESTROYS net points. Don't repeat the RND2 -30 lesson.
  5. Use FEWER transfers than the free allotment if no move clears the
     noise — rolling free transfers preserves flexibility for knockouts.
EOF
