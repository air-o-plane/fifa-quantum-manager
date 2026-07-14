r"""
FIFA Men's World Cup 2026 Fantasy — API-Football (api-sports.io) adapter.

Implements the concrete StatsSource / AvailabilitySource for fantasy_data_feed.py
against API-Football v3, grounded in the provider's official getting-started
guide (api-football.com, March 2026) — not from memory.

GROUNDED FACTS (from their official docs/guide):
  - Base URL https://v3.football.api-sports.io/ ; GET only.
  - Auth: single header  x-apisports-key: <key>.
  - Every response is wrapped: {get, parameters, errors, results, paging, response}.
    Check `errors` first; `paging.total` > 1 means more pages (players: 20/page).
  - /fixtures?league&season&date|last  -> fixtures; fixture.id is the master key.
  - /fixtures/players?fixture=ID       -> per-player match stats for ALL players
    in that match in ONE call (goals, assists, cards, shots.on, passes.key,
    tackles, rating). Ideal for the 100 req/day free quota.
  - /injuries?league&season            -> unavailable players with type
    ("Injury"/"Suspension") and reason. Updates every 4h.
  - Rate-limit headers: x-ratelimit-requests-remaining (daily),
    X-Ratelimit-Remaining (per-minute).
  - Coverage flags per league/season in /leagues; for a competition that has
    not started, flags are false and flip once underway.

HONESTY NOTES:
  - The WORLD CUP LEAGUE ID is NOT hard-coded: discover_league_id() finds it
    via /leagues?search=... and prints candidates. (It is widely reported as
    league id 1, but verify via discovery or your dashboard.)
  - The exact NESTED JSON of /fixtures/players and /injuries items is parsed
    DEFENSIVELY (dict.get chains) and PROBE MODE dumps raw JSON first, so any
    field-shape mismatch is visible immediately instead of failing silently.
  - This sandbox cannot reach the API (network allowlist), so this file was
    NOT run against the live API. Run probe mode locally first.

USAGE (on your machine):
    export API_FOOTBALL_KEY="<your key>"        # never hard-code or commit it
    python fantasy_apifootball_adapter.py probe          # 2-3 requests
    python fantasy_apifootball_adapter.py feed           # produce to Kafka/dry-run

SECURITY: the key lives ONLY in the environment variable. Since yours was
pasted into a chat, regenerate it in the dashboard (Account -> My Access)
once the project is wired up.
"""

from __future__ import annotations
import os
import sys
import json
import time
from datetime import datetime, timezone
from typing import Iterable, Optional

import requests

from fantasy_data_feed import (
    PlayerStatsEvent, PlayerAvailabilityEvent, Availability,
    StatsSource, AvailabilitySource, KafkaPublisher, run_feed,
    TOPIC_AVAILABILITY,
)

BASE_URL = "https://v3.football.api-sports.io"
# Season is the starting year per the docs. Default 2026 (the live WC), but
# overridable for free-tier testing: the free plan only allows 2022-2024,
# and season 2022 = the Qatar World Cup (league id 1) — same JSON shapes.
#   export APIFOOTBALL_SEASON=2022   # validate the adapter at zero cost
SEASON   = int(os.environ.get("APIFOOTBALL_SEASON", "2026"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ApiFootballClient:
    """Minimal client honouring the documented wrapper + rate limits."""

    def __init__(self, api_key: Optional[str] = None, min_interval_s: float = 1.5):
        self.api_key = api_key or os.environ.get("API_FOOTBALL_KEY")
        if not self.api_key:
            raise RuntimeError("Set API_FOOTBALL_KEY environment variable "
                               "(never hard-code the key).")
        self.min_interval_s = min_interval_s   # gentle pacing; per-minute cap exists
        self._last_call = 0.0
        self.daily_remaining: Optional[str] = None

    def get(self, path: str, **params) -> dict:
        # pace requests to respect the per-minute cap
        wait = self.min_interval_s - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        resp = requests.get(f"{BASE_URL}/{path.lstrip('/')}",
                            headers={"x-apisports-key": self.api_key},
                            params=params, timeout=30)
        self._last_call = time.time()
        self.daily_remaining = resp.headers.get("x-ratelimit-requests-remaining")
        resp.raise_for_status()
        data = resp.json()
        # Per the docs: check `errors` first — 200 can still carry an error.
        if data.get("errors"):
            raise RuntimeError(f"API error on /{path}: {data['errors']}")
        return data

    def get_all_pages(self, path: str, max_pages: int = 30, **params) -> list:
        """Follow paging.total (players paginate at 20/page per the docs)."""
        out, page = [], 1
        while page <= max_pages:
            data = self.get(path, page=page, **params)
            out.extend(data.get("response", []))
            total = (data.get("paging") or {}).get("total", 1)
            if page >= total:
                break
            page += 1
        return out


def discover_league_id(client: ApiFootballClient,
                       search: str = "World Cup") -> list[dict]:
    """Find candidate league IDs by name instead of hard-coding one.
    Returns [{id, name, country, seasons-with-coverage}] for inspection."""
    data = client.get("leagues", search=search)
    cands = []
    for item in data.get("response", []):
        lg = item.get("league") or {}
        seasons = item.get("seasons") or []
        season_entry = next((s for s in seasons if s.get("year") == SEASON), None)
        cands.append({
            "id": lg.get("id"), "name": lg.get("name"),
            "type": lg.get("type"),
            "country": (item.get("country") or {}).get("name"),
            "has_2026_season": season_entry is not None,
            "coverage_2026": (season_entry or {}).get("coverage"),
        })
    return cands


# ----------------------------------------------------------------------
# Stats source: recent fixtures -> /fixtures/players (one call per match)
# ----------------------------------------------------------------------
class ApiFootballStatsSource(StatsSource):
    def __init__(self, client: ApiFootballClient, league_id: int,
                 last_n_fixtures: int = 8):
        self.client, self.league_id, self.last_n = client, league_id, last_n_fixtures

    def _recent_fixture_ids(self) -> list[int]:
        data = self.client.get("fixtures", league=self.league_id,
                               season=SEASON, last=self.last_n)
        ids = []
        for fx in data.get("response", []):
            fid = ((fx.get("fixture") or {}).get("id"))
            if fid is not None:
                ids.append(fid)
        return ids

    def fetch(self) -> Iterable[PlayerStatsEvent]:
        for fid in self._recent_fixture_ids():
            data = self.client.get("fixtures/players", fixture=fid)
            for team_block in data.get("response", []):
                team_name = (team_block.get("team") or {}).get("name", "")
                for p in team_block.get("players", []):
                    yield self._to_event(p, team_name, fid)

    @staticmethod
    def _to_event(p: dict, team_name: str, fixture_id: int) -> PlayerStatsEvent:
        """Defensive mapping. Stat paths follow the official guide's field list
        (goals.total, goals.assists, cards.yellow/red, shots.on, passes.key,
        tackles.total, rating under a per-match statistics block). If probe
        mode shows a different nesting, adjust HERE and only here."""
        player = p.get("player") or {}
        stats_list = p.get("statistics") or [{}]
        s = stats_list[0] if stats_list else {}
        games  = s.get("games")  or {}
        shots  = s.get("shots")  or {}
        goals  = s.get("goals")  or {}
        passes = s.get("passes") or {}
        cards  = s.get("cards")  or {}

        def num(x, cast=int, default=0):
            try:
                return cast(x) if x is not None else default
            except (TypeError, ValueError):
                return default

        def fnum(x):
            try:
                return float(x) if x is not None else None
            except (TypeError, ValueError):
                return None

        return PlayerStatsEvent(
            player_name=player.get("name", "UNKNOWN"),
            nation=team_name,                # national team == nation at the WC
            position=str(games.get("position") or ""),
            source="api-football",
            fetched_at=_now(),
            competition=f"wc2026:fixture:{fixture_id}",
            opponent=None,
            minutes=num(games.get("minutes")),
            goals=num(goals.get("total")),
            assists=num(goals.get("assists")),
            shots_on_target=num(shots.get("on")),
            xg=0.0,                          # xG not in this endpoint's documented list
            xa=0.0,
            key_passes=num(passes.get("key")),
            clean_sheet=False,               # derive downstream from goals conceded
            goals_conceded=num(goals.get("conceded")),
            saves=num(goals.get("saves")),
            yellow_cards=num(cards.get("yellow")),
            red_cards=num(cards.get("red")),
            rating=fnum(games.get("rating")),
            position_raw=games.get("position"),
        )


# ----------------------------------------------------------------------
# Availability source: /injuries?league&season
# ----------------------------------------------------------------------
class ApiFootballAvailabilitySource(AvailabilitySource):
    def __init__(self, client: ApiFootballClient, league_id: int):
        self.client, self.league_id = client, league_id

    def fetch(self) -> Iterable[PlayerAvailabilityEvent]:
        data = self.client.get("injuries", league=self.league_id, season=SEASON)
        for item in data.get("response", []):
            player = item.get("player") or {}
            team   = item.get("team") or {}
            # API actually returns type='Missing Fixture' for most records, with
            # the real cause in `reason` (e.g. 'Calf Injury', 'Suspended 1 match').
            # Classify on whichever field gives a signal; reason is the reliable one.
            itype  = (player.get("type") or item.get("type") or "").lower()
            reason = player.get("reason") or item.get("reason") or ""
            sig = f"{itype} {reason}".lower()
            status = (Availability.SUSPENDED if "susp" in sig
                      else Availability.INJURED if any(k in sig for k in
                          ("injur", "strain", "knock", "fracture", "sprain",
                           "tear", "illness", "concussion", "missing fixture"))
                      else Availability.UNKNOWN)
            yield PlayerAvailabilityEvent(
                player_name=player.get("name", "UNKNOWN"),
                nation=team.get("name", ""),
                status=status,
                source="api-football",
                fetched_at=_now(),
                detail=reason,
                expected_to_start=False,
            )


# ----------------------------------------------------------------------
# Probe mode: spend 2-3 requests to SEE the real shapes before trusting them
# ----------------------------------------------------------------------
def probe():
    client = ApiFootballClient()
    print("== /leagues search: candidate World Cup league IDs ==")
    for c in discover_league_id(client):
        print(json.dumps(c, indent=2)[:600])
    print(f"\n[daily requests remaining: {client.daily_remaining}]")
    print("\nPick the correct league id from above (FIFA World Cup, type Cup,")
    print("has_2026_season true), then re-run:  probe2 <league_id>")


def probe2(league_id: int):
    client = ApiFootballClient()

    # 0) Coverage check: does this league+season exist, and what's enabled?
    print(f"== /leagues id={league_id}: season {SEASON} coverage ==")
    data = client.get("leagues", id=league_id)
    for item in data.get("response", []):
        lg = item.get("league") or {}
        season_entry = next((s for s in (item.get("seasons") or [])
                             if s.get("year") == SEASON), None)
        print(f"league: {lg.get('id')} {lg.get('name')}")
        if season_entry is None:
            print(f"  !! NO season {SEASON} listed for this league — wrong "
                  f"league id, or season not yet registered.")
        else:
            print(f"  season {SEASON}: start={season_entry.get('start')} "
                  f"end={season_entry.get('end')} current={season_entry.get('current')}")
            print(f"  coverage: {json.dumps(season_entry.get('coverage'), indent=2)}")

    # 1) Finished fixtures (empty is NORMAL before any match has completed)
    print(f"\n== last 2 FINISHED fixtures, league {league_id} season {SEASON} ==")
    data = client.get("fixtures", league=league_id, season=SEASON, last=2)
    print(json.dumps(data.get("response", [])[:1], indent=2, ensure_ascii=False)[:1200]
          or "(none finished yet)")
    fids = [((f.get("fixture") or {}).get("id")) for f in data.get("response", [])]

    # 2) Upcoming fixtures — proves the season's fixtures are loaded
    print(f"\n== next 3 UPCOMING fixtures ==")
    d_up = client.get("fixtures", league=league_id, season=SEASON, next=3)
    for f in d_up.get("response", []):
        fx, teams = f.get("fixture") or {}, f.get("teams") or {}
        print(f"  {fx.get('date')}  "
              f"{(teams.get('home') or {}).get('name')} vs "
              f"{(teams.get('away') or {}).get('name')}  (fixture id {fx.get('id')})")
    if not d_up.get("response"):
        print("  (none — if coverage above also looks wrong, the league id or "
              "season is the problem)")

    # 3) Player stats for the most recent finished fixture, if any
    if fids:
        print(f"\n== /fixtures/players for fixture {fids[0]} (first player raw) ==")
        d2 = client.get("fixtures/players", fixture=fids[0])
        resp = d2.get("response", [])
        if resp and resp[0].get("players"):
            print(json.dumps(resp[0]["players"][0], indent=2, ensure_ascii=False)[:2000])
        else:
            print("(no player data yet — can lag shortly after full-time)")

    # 4) Injuries (empty can be normal at tournament start)
    print(f"\n== /injuries league={league_id} season={SEASON} (first 2 raw) ==")
    d3 = client.get("injuries", league=league_id, season=SEASON)
    print(json.dumps(d3.get("response", [])[:2], indent=2, ensure_ascii=False)[:2000]
          or "(none recorded yet)")
    print(f"\n[daily requests remaining: {client.daily_remaining}]")


def probe_injuries(league_id: int):
    """Diagnostic — call /injuries directly and show what API-Football returns
    for this league/season. The /leagues coverage.injuries flag must be true
    for data to exist (API-Football policy). Updates ~every 4 hours.

    Use BEFORE running 'injuries' to confirm there is data to publish.
    """
    client = ApiFootballClient()
    # 1) coverage check
    print(f"== /leagues coverage for league {league_id}, season {SEASON} ==")
    lg = client.get("leagues", id=league_id, season=SEASON)
    cov = None
    for blk in lg.get("response", []):
        for s in blk.get("seasons", []):
            if s.get("year") == SEASON:
                cov = (s.get("coverage") or {})
                break
    if cov is None:
        print("  could not locate coverage block for this season.")
    else:
        inj = cov.get("injuries")
        print(f"  coverage.injuries = {inj!r}  "
              f"({'OK' if inj else 'API does not track injuries for this league'})")

    # 2) injuries call
    print(f"\n== /injuries for league {league_id}, season {SEASON} ==")
    data = client.get("injuries", league=league_id, season=SEASON)
    resp = data.get("response", [])
    print(f"  records returned: {len(resp)}")
    if resp:
        print("\nFirst record raw JSON (shape we'll parse):")
        print(json.dumps(resp[0], indent=2, ensure_ascii=False)[:1500])
        # quick summary
        types = {}
        for r in resp:
            p = r.get("player") or {}
            t = (p.get("type") or r.get("type") or "unknown")
            types[t] = types.get(t, 0) + 1
        print(f"\nType breakdown: {types}")
    print(f"\n[daily requests remaining: {client.daily_remaining}]")


def injuries(league_id: int):
    """Fetch injuries/suspensions and publish to wc26.player.availability.
    Dry-run unless KAFKA_* env vars are set (same behaviour as feed).
    Writes a local CSV (availability_snapshot.csv) regardless, so the
    transfer recommender can read it without needing the stream consumer.

    The API returns one record per upcoming missed fixture, so the SAME player
    appears multiple times (e.g. Neymar 3x = will miss 3 upcoming fixtures).
    We dedupe to one row per (nation, player) for the snapshot CSV so the
    recommender doesn't list them multiple times. Each event is still published
    to Kafka individually (the stream is allowed to carry per-fixture detail).
    """
    import csv
    client = ApiFootballClient()
    src = ApiFootballAvailabilitySource(client, league_id)
    pub = KafkaPublisher()
    mode_msg = ("LIVE (Confluent Cloud)" if not pub.dry_run else
                "DRY-RUN (KAFKA_* env vars not set — messages will NOT reach Confluent)")
    print(f"Publisher mode: {mode_msg}\n")
    events = list(src.fetch())
    seen: dict[tuple, dict] = {}
    for ev in events:
        # publish each per-fixture event to Kafka (downstream may want the detail)
        pub.publish(TOPIC_AVAILABILITY, ev.key(), ev.to_json())
        # dedupe per (nation, player) for the CSV snapshot
        key = (ev.nation, ev.player_name)
        if key not in seen:
            seen[key] = {
                "player_name": ev.player_name,
                "nation": ev.nation,
                "status": ev.status.value if hasattr(ev.status, "value") else str(ev.status),
                "detail": ev.detail or "",
                "fetched_at": ev.fetched_at,
                "n_fixtures_affected": 1,
            }
        else:
            seen[key]["n_fixtures_affected"] += 1

    # CRITICAL: flush the producer so buffered messages actually reach Confluent
    # before the process exits. Without this, "publish" calls are merely queued.
    summary = pub.flush()

    rows = list(seen.values())
    with open("availability_snapshot.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["player_name", "nation", "status",
                                          "detail", "fetched_at",
                                          "n_fixtures_affected"])
        w.writeheader(); w.writerows(rows)

    print(f"\nFetched {len(events)} availability records.")
    print(f"Publisher summary: {summary}")
    print(f"Deduped to {len(rows)} unique players -> availability_snapshot.csv.")
    if rows:
        print("\nUnique unavailable players:")
        for r in rows:
            tag = f" (misses {r['n_fixtures_affected']} fixtures)" if r['n_fixtures_affected'] > 1 else ""
            print(f"  {r['nation']:<20} {r['player_name']:<24} "
                  f"{r['status']:<10} {r['detail']}{tag}")
    print(f"\n[daily requests remaining: {client.daily_remaining}]")


def feed(league_id: int):
    client = ApiFootballClient()
    pub = KafkaPublisher()   # dry-run unless KAFKA_* env vars are set
    summary = run_feed(ApiFootballStatsSource(client, league_id),
                       ApiFootballAvailabilitySource(client, league_id), pub)
    print("\nRun summary:", summary,
          f"| daily requests remaining: {client.daily_remaining}")


def probe3(fixture_id: int):
    """Check player stats for ONE specific fixture (run after a match ends).
    e.g. fixture 1489369 = Mexico vs South Africa, the 2026 opener."""
    client = ApiFootballClient()
    print(f"== /fixtures/players for fixture {fixture_id} ==")
    d = client.get("fixtures/players", fixture=fixture_id)
    resp = d.get("response", [])
    if not resp:
        print("(empty — player stats not yet available for this fixture)")
    else:
        for team_block in resp:
            t = (team_block.get("team") or {}).get("name")
            n = len(team_block.get("players", []))
            print(f"team: {t}  players with stats: {n}")
        print("\nFirst player raw JSON:")
        print(json.dumps(resp[0]["players"][0], indent=2, ensure_ascii=False)[:2200])
    print(f"\n[daily requests remaining: {client.daily_remaining}]")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "probe"
    if mode == "probe":
        probe()
    elif mode == "probe2" and len(sys.argv) > 2:
        probe2(int(sys.argv[2]))
    elif mode == "probe3" and len(sys.argv) > 2:
        probe3(int(sys.argv[2]))
    elif mode == "probe_injuries" and len(sys.argv) > 2:
        probe_injuries(int(sys.argv[2]))
    elif mode == "injuries" and len(sys.argv) > 2:
        injuries(int(sys.argv[2]))
    elif mode == "feed" and len(sys.argv) > 2:
        feed(int(sys.argv[2]))
    else:
        print("usage: fantasy_apifootball_adapter.py probe | probe2 <league_id> "
              "| probe3 <fixture_id> | feed <league_id>")


if __name__ == "__main__":
    main()
