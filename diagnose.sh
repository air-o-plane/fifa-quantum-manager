#!/usr/bin/env bash
# diagnose.sh — quick pipeline health check.
#
# Run when something feels off, or before a major matchday-refresh, to
# verify all the data files and external services are in the state you
# expect. Touches no state; spends ~3 API calls.
#
# Usage:  ./diagnose.sh

set -e

if [[ -z "${VIRTUAL_ENV:-}" ]] || [[ ! "$VIRTUAL_ENV" == *"venv312"* ]]; then
    echo "⚠  venv312 not active. Run:  source venv312/bin/activate" >&2
    exit 1
fi

LEAGUE=1

banner() {
    echo
    echo "═══════════════════════════════════════════════════════════════"
    echo "  $*"
    echo "═══════════════════════════════════════════════════════════════"
}

banner "Files present?"
for f in \
    FIFA_Men_s_World_Cup_2026_Player_Pool.xlsx \
    player_map.csv \
    rnd1_points.csv \
    rnd2_points.csv \
    xpts.csv \
    fixture_multipliers.csv \
    stream_points.csv \
    availability_snapshot.csv \
    my_squad.csv \
; do
    if [[ -f "$f" ]]; then
        if [[ "$(uname)" == "Darwin" ]]; then
            AGE_H=$(( ( $(date +%s) - $(stat -f %m "$f") ) / 3600 ))
        else
            AGE_H=$(( ( $(date +%s) - $(stat -c %Y "$f") ) / 3600 ))
        fi
        printf "  ✓ %-50s (%dh old)\n" "$f" "$AGE_H"
    else
        printf "  ✗ %-50s MISSING\n" "$f"
    fi
done

banner "Kafka env vars set?"
for v in KAFKA_BOOTSTRAP_SERVERS KAFKA_API_KEY KAFKA_API_SECRET; do
    if [[ -n "${!v:-}" ]]; then
        printf "  ✓ %s set (length %d)\n" "$v" "${#v}"
    else
        printf "  ✗ %s NOT SET — producer will run in dry-run mode\n" "$v"
    fi
done

banner "API-Football reachable + coverage check"
python3 fantasy_apifootball_adapter.py probe_injuries "$LEAGUE" | head -10

banner "Diagnose done"
echo "If anything looks unexpected, address it before running the matchday"
echo "refresh or transfer check."
