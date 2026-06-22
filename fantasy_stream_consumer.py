r"""
FIFA WC2026 Fantasy — Confluent consumer (makes the stream load-bearing).

Reads the wc26.player.stats and wc26.player.availability topics that
fantasy_data_feed / fantasy_apifootball_adapter produce, and turns the raw
per-match stats into FANTASY POINTS using FIFA's actual scoring rules — giving
a stats-derived points history for EVERY player who appears in the stream,
including the ~553 who have no row in the spreadsheet's RND<N>_Points tab.

WHY THIS EXISTS
---------------
The xPts model's ground truth is the hand-curated RND<N>_Points tab (best when
available). But that only covers manually-transcribed scorers. This consumer
derives points straight from the streamed box scores, so:
  - players with no tab entry get a real, data-driven points estimate
    (not just the price prior), and
  - the Kafka log becomes replayable: re-run this any time to rebuild history.

OUTPUT
------
  stream_points.csv : pool_row, player_name, nation_code, position,
                      stream_points, matches, source='stream'
  availability.csv  : pool_row, player_name, status, expected_to_start
The xPts model can read stream_points.csv as a fallback where the curated
RND tab has no entry (wired in a follow-up; this file produces the data).

FIFA SCORING (grounded in the rules we verified earlier; edit POINTS to taste)
-----------------------------------------------------------------------------
  appearance (>=1 min)            : +1   ; (>=60 min)               : +2 total
  goal: GK/DEF +6, MID +5, FWD +4 ; assist                         : +3
  clean sheet (GK/DEF, >=60')     : +5
  every 2 saves (GK)              : +1
  goals conceded (GK/DEF)         : -1 per 2 conceded
  yellow card -1 ; red card -3
  (shots/key passes available in the stream but not scored by FIFA directly;
   left out to stay faithful to the official scheme.)

USAGE
-----
  python3 fantasy_stream_consumer.py            # consume new messages, then idle-stop
  python3 fantasy_stream_consumer.py --from-start   # re-read the whole log (replay)
Reads Kafka only if KAFKA_* env vars are set; otherwise explains how.
"""

from __future__ import annotations
import os
import sys
import csv
import json
import time

import pandas as pd

from fantasy_name_bridge import load_pool, _read_csv

TOPIC_STATS        = "wc26.player.stats"
TOPIC_AVAILABILITY = "wc26.player.availability"
GROUP_ID           = "wc26-fantasy-consumer"
IDLE_STOP_SECONDS  = 8          # stop after this many seconds with no new message
PLAYER_MAP_CSV     = "player_map.csv"

# Official FIFA Fantasy scoring (from FIFA_Fantasy_League_Scoring doc).
GOAL_PTS = {"GK": 9, "DEF": 7, "MID": 6, "FWD": 5}


def fifa_points(ev: dict, position: str) -> float:
    """Apply the OFFICIAL FIFA fantasy scoring to one PlayerStatsEvent.
    Appearance +1 (<60) / +1 more (60+); goals by position (GK9/DEF7/MID6/FWD5);
    assist +3; clean sheet GK/DEF +5, MID +1 (60+, 0 conceded); first goal
    conceded +0, each additional -1; GK every 3 saves +1, penalty save +3;
    MID every 3 tackles +1, every 2 chances created +1; FWD every 2 shots on
    target +1; yellow -1, red -2, own goal -2, win pen +2, concede pen -1.
    Categories without reliable stream fields are scored only if present."""
    mins = ev.get("minutes", 0) or 0
    if mins <= 0:
        return 0.0

    pts = 1.0                                              # appearance (<60)
    if mins >= 60:
        pts += 1.0                                         # +1 more for 60+

    pts += GOAL_PTS.get(position, 5) * (ev.get("goals", 0) or 0)
    pts += 3 * (ev.get("assists", 0) or 0)                 # assist +3

    conceded = ev.get("goals_conceded", 0) or 0
    if position in ("GK", "DEF"):
        if mins >= 60 and conceded == 0:
            pts += 5                                       # clean sheet
        pts -= max(conceded - 1, 0)                        # 1st free, then -1 each
    elif position == "MID":
        if mins >= 60 and conceded == 0:
            pts += 1                                       # MID clean sheet +1
        # NOTE: 'tackles' not in the event schema, so the every-3-tackles bonus
        # can't be scored from the stream; key_passes ~ chances created.
        pts += (ev.get("key_passes", 0) or 0) // 2         # every 2 chances +1
    if position == "GK":
        pts += (ev.get("saves", 0) or 0) // 3              # every 3 saves +1
    if position == "FWD":
        pts += (ev.get("shots_on_target", 0) or 0) // 2    # every 2 SoT +1

    pts -= (ev.get("yellow_cards", 0) or 0)                # yellow -1
    pts -= 2 * (ev.get("red_cards", 0) or 0)               # red -2
    # own goals / penalties won/conceded / penalty saves / direct-FK bonus are
    # not in the current event schema — add to the producer to score them.
    return float(pts)


def load_map():
    """Build lookups so a STREAM event — keyed by the API's nation NAME and the
    API's player NAME — resolves to a pool row.

    player_map.csv has columns: api_name, nation_code, pool_row, ...
    The stream's 'nation' field is the API team NAME ("South Korea"), so we also
    need nation-name -> code via the bridge's match_team.
    Returns (pool_by_row, api_lookup, team_lut):
      api_lookup: (nation_code, norm(api_name)) -> pool_row
      team_lut  : pool nation-name lookup for match_team()
    """
    from fantasy_name_bridge import nation_lookup
    pool = {p.row: p for p in load_pool()}
    team_lut = nation_lookup(list(pool.values()))
    api_lookup = {}
    try:
        df = _read_csv(PLAYER_MAP_CSV)
    except FileNotFoundError:
        print(f"Missing {PLAYER_MAP_CSV}; run the name bridge `apply` first.")
        return pool, api_lookup, team_lut, {}
    from fantasy_name_bridge import norm as _norm
    for _, r in df.iterrows():
        pr = int(r["pool_row"])
        code = str(r["nation_code"]).strip()
        api_nm = _norm(str(r["api_name"]))
        api_lookup[(code, api_nm)] = pr
        # also index pool display name as a fallback key
        p = pool.get(pr)
        if p:
            api_lookup.setdefault((code, _norm(p.display)), pr)
    # token-set index: frozenset of name tokens -> pool_row, for ORDER-INDEPENDENT
    # matching (handles Korean etc. where the API flips surname order between
    # endpoints, e.g. "Jae-sung Lee" vs "Lee Jae-sung").
    token_index: dict[tuple, int] = {}
    for (code, nm), pr in list(api_lookup.items()):
        toks = frozenset(t for t in nm.replace("-", " ").split() if t)
        if toks:
            # only set if unambiguous within (code, tokenset)
            key = (code, toks)
            if key in token_index and token_index[key] != pr:
                token_index[key] = -1            # mark ambiguous
            else:
                token_index.setdefault(key, pr)
    # drop ambiguous
    token_index = {k: v for k, v in token_index.items() if v != -1}
    return pool, api_lookup, team_lut, token_index


def consume(from_start: bool):
    bootstrap = os.environ.get("KAFKA_BOOTSTRAP_SERVERS")
    api_key   = os.environ.get("KAFKA_API_KEY")
    api_secret = os.environ.get("KAFKA_API_SECRET")
    if not all((bootstrap, api_key, api_secret)):
        print("No KAFKA_* env vars set — set KAFKA_BOOTSTRAP_SERVERS / KAFKA_API_KEY"
              " / KAFKA_API_SECRET (the same ones the producer uses) and re-run.")
        return

    from confluent_kafka import Consumer, KafkaError
    import uuid
    replay_group = f"{GROUP_ID}-replay-{uuid.uuid4().hex[:8]}"
    conf = {
        "bootstrap.servers": bootstrap,
        "security.protocol": "SASL_SSL",
        "sasl.mechanisms": "PLAIN",
        "sasl.username": api_key,
        "sasl.password": api_secret,
        # replay: a fresh throwaway group every time + earliest + no commit,
        # so re-reading the whole log is always possible. Live tailing uses the
        # stable group so it only sees genuinely new messages.
        "group.id": replay_group if from_start else GROUP_ID,
        "auto.offset.reset": "earliest" if from_start else "latest",
        "enable.auto.commit": not from_start,
    }
    consumer = Consumer(conf)
    consumer.subscribe([TOPIC_STATS, TOPIC_AVAILABILITY])
    print(f"Subscribed to {TOPIC_STATS}, {TOPIC_AVAILABILITY} "
          f"({'replay from start' if from_start else 'new messages'}).")

    pool, api_lookup, team_lut, token_index = load_map()
    from fantasy_name_bridge import match_team, match_player, norm as _norm
    # candidates per (nation_code, position) for the full-matcher fallback
    by_np: dict[tuple, list] = {}
    for p in pool.values():
        by_np.setdefault((p.nation_code, p.position), []).append(p)
    # also by nation only (stream position letter may not be reliable)
    by_nation: dict[str, list] = {}
    for p in pool.values():
        by_nation.setdefault(p.nation_code, []).append(p)

    points: dict[int, list[float]] = {}
    stat_buffer: list = []         # (pool_row, event) buffered for 2-pass scoring
    unmapped: dict[str, int] = {}
    avail_rows = []
    mock_skipped = 0

    POS_FROM_LETTER = {"G": "GK", "D": "DEF", "M": "MID", "F": "FWD"}

    def resolve(ev) -> int | None:
        code = match_team(str(ev.get("nation", "")), team_lut)
        if code is None:
            return None
        nm = _norm(str(ev.get("player_name", "")))
        pr = api_lookup.get((code, nm))
        if pr is not None:
            return pr
        # order-independent token-set fallback
        toks = frozenset(t for t in nm.replace("-", " ").split() if t)
        pr = token_index.get((code, toks))
        if pr is not None:
            return pr
        # FINAL fallback: the bridge's accent-aware fuzzy matcher against the
        # pool directly — resolves pooled players even if absent from
        # player_map.csv (e.g. CZE, whose accents the pool half-stripped).
        cands = by_nation.get(code, [])
        if not cands:
            return None
        m = match_player(str(ev.get("player_name", "")),
                         None, cands)            # position=None: match on name
        if m.pool_row is not None and m.method not in ("REVIEW", "UNMATCHED"):
            return m.pool_row
        return None

    last_msg = time.time()
    n_stats = n_avail = 0
    while True:
        msg = consumer.poll(1.0)
        if msg is None:
            if time.time() - last_msg > IDLE_STOP_SECONDS:
                break
            continue
        if msg.error():
            if msg.error().code() == KafkaError._PARTITION_EOF:
                continue
            print(f"  consumer error: {msg.error()}"); continue
        last_msg = time.time()
        try:
            ev = json.loads(msg.value().decode("utf-8"))
        except Exception:
            continue
        topic = msg.topic()

        if topic == TOPIC_STATS:
            if ev.get("source") == "mock":      # ignore the smoke-test events
                mock_skipped += 1; continue
            n_stats += 1
            pr = resolve(ev)
            if pr is None:
                k = f"{ev.get('nation')}:{ev.get('player_name')}"
                unmapped[k] = unmapped.get(k, 0) + 1
                continue
            stat_buffer.append((pr, ev))         # score in pass 2
        elif topic == TOPIC_AVAILABILITY:
            if ev.get("source") == "mock":
                continue
            n_avail += 1
            pr = resolve(ev)
            avail_rows.append({
                "pool_row": pr if pr is not None else "",
                "player_name": ev.get("player_name"),
                "nation_code": ev.get("nation"),
                "status": ev.get("status"),
                "expected_to_start": ev.get("expected_to_start"),
                "detail": ev.get("detail"),
            })

    consumer.close()

    # PASS 2 — derive each (nation, fixture)'s ACTUAL goals conceded from the
    # goalkeeper's event (the only reliable 'conceded' value; outfield players
    # come through as 0 from the API, which falsely triggered clean sheets).
    # Then score every buffered event with the team's true conceded count.
    team_conceded: dict[tuple, int] = {}
    for pr, ev in stat_buffer:
        if pool[pr].position == "GK":
            key = (ev.get("nation"), ev.get("competition"))
            team_conceded[key] = max(team_conceded.get(key, 0),
                                     ev.get("goals_conceded", 0) or 0)
    no_keeper = set()
    for pr, ev in stat_buffer:
        pos = pool[pr].position
        key = (ev.get("nation"), ev.get("competition"))
        if pos in ("GK", "DEF", "MID"):
            if key in team_conceded:
                ev = {**ev, "goals_conceded": team_conceded[key]}
            else:
                # no keeper event for this team/fixture — can't trust a clean
                # sheet; assume NOT clean (conservative) and flag it.
                no_keeper.add(key)
                ev = {**ev, "goals_conceded": 1}
        points.setdefault(pr, []).append(fifa_points(ev, pos))
    if no_keeper:
        print(f"  note: {len(no_keeper)} team/fixture(s) had no GK event; "
              f"clean sheets suppressed for them (conservative).")

    # write stream_points.csv (sum + per-match list collapsed to total & count)
    out = []
    for pr, vals in points.items():
        p = pool[pr]
        out.append({"pool_row": pr, "player_name": p.display,
                    "nation_code": p.nation_code, "position": p.position,
                    "stream_points": round(sum(vals), 1), "matches": len(vals),
                    "source": "stream"})
    out.sort(key=lambda r: -r["stream_points"])
    with open("stream_points.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["pool_row", "player_name", "nation_code",
                          "position", "stream_points", "matches", "source"])
        w.writeheader(); w.writerows(out)

    if avail_rows:
        with open("availability.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["pool_row", "player_name",
                              "nation_code", "status", "expected_to_start", "detail"])
            w.writeheader(); w.writerows(avail_rows)

    print(f"\nConsumed {n_stats} stat events ({mock_skipped} mock skipped), "
          f"{n_avail} availability events.")
    print(f"Derived points for {len(out)} players -> stream_points.csv")
    if avail_rows:
        print(f"Wrote {len(avail_rows)} availability records -> availability.csv")
    if unmapped:
        print(f"\n{len(unmapped)} stream keys did not map to the pool "
              f"(non-squad or key mismatch), e.g.:")
        for k, c in list(unmapped.items())[:8]:
            print(f"  {k}  ({c} events)")
    if out:
        print(f"\nTop 10 by stream-derived points:")
        for r in out[:10]:
            print(f"  {r['player_name']:<22}{r['nation_code']:<5}{r['position']:<5}"
                  f"{r['stream_points']:>6} ({r['matches']}m)")


if __name__ == "__main__":
    consume(from_start=("--from-start" in sys.argv))
