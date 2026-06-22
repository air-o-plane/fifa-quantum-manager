r"""
FIFA Men's World Cup 2026 Fantasy — stats/form + news data feed into Confluent.

ROLE IN THE SYSTEM
------------------
This is the data layer that produces the xPts inputs the optimisers need.
It pulls player performance/form and availability (injury/suspension/lineup)
data and publishes it to Kafka topics on Confluent Cloud, keyed per player,
so a downstream xPts model can consume a clean, replayable stream.

DESIGN STANCE — what is real here vs what you must supply
--------------------------------------------------------
REAL & RUNNABLE NOW (verified in-sandbox, no broker/keys needed):
  - the event schemas,
  - the Confluent producer wrapper (confluent-kafka 2.x API),
  - the source interface,
  - a MockStatsSource / MockAvailabilitySource that synthesises events from
    your player pool, so the whole pipeline runs end-to-end in DRY-RUN.

YOU SUPPLY (deliberately not invented):
  - your Confluent Cloud bootstrap server + API key/secret (via env vars —
    never hard-code credentials),
  - the concrete provider adapter. RestStatsSource gives you the HTTP
    plumbing; you implement parse_response() against YOUR chosen provider's
    CURRENT docs. I have not fabricated any provider's endpoint paths or
    JSON field names — those differ per provider and per version.

Run `python fantasy_data_feed.py` with no Kafka env vars set and it executes
in DRY-RUN: mock events are printed exactly as they would be produced.
"""

from __future__ import annotations
import os
import json
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, date
from enum import Enum
from typing import Iterable, Optional

import pandas as pd

PLAYER_POOL = "FIFA_Men_s_World_Cup_2026_Player_Pool.xlsx"

TOPIC_STATS        = "wc26.player.stats"
TOPIC_AVAILABILITY = "wc26.player.availability"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ----------------------------------------------------------------------
# Event schemas
# ----------------------------------------------------------------------
@dataclass
class PlayerStatsEvent:
    """One player's performance in one match (or a rolling-form snapshot).
    Keyed on (nation, player_name) — the unique key established in the pool."""
    player_name: str
    nation: str
    position: str
    source: str
    fetched_at: str
    competition: Optional[str] = None      # e.g. "club:EPL", "nt:friendly"
    opponent: Optional[str] = None
    minutes: int = 0
    goals: int = 0
    assists: int = 0
    shots_on_target: int = 0
    xg: float = 0.0
    xa: float = 0.0
    key_passes: int = 0
    clean_sheet: bool = False              # relevant for GK/DEF
    goals_conceded: int = 0
    saves: int = 0
    yellow_cards: int = 0
    red_cards: int = 0
    rating: Optional[float] = None         # API-Football match rating (e.g. 7.2)
    position_raw: Optional[str] = None     # as the stats endpoint sends it: G/D/M/F

    def key(self) -> str:
        return f"{self.nation}:{self.player_name}"

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


class Availability(str, Enum):
    FIT = "FIT"
    DOUBTFUL = "DOUBTFUL"
    INJURED = "INJURED"
    SUSPENDED = "SUSPENDED"
    UNKNOWN = "UNKNOWN"


@dataclass
class PlayerAvailabilityEvent:
    """A structured news/availability signal — the part of 'latest news' that
    actually moves xPts (injuries, suspensions, expected starting status).
    Free-text news can go to a separate raw topic for later NLP; this captures
    the decision-relevant fields directly."""
    player_name: str
    nation: str
    status: Availability
    source: str
    fetched_at: str
    detail: Optional[str] = None
    expected_return: Optional[str] = None   # ISO date if known
    expected_to_start: Optional[bool] = None

    def key(self) -> str:
        return f"{self.nation}:{self.player_name}"

    def to_json(self) -> str:
        d = asdict(self)
        d["status"] = self.status.value
        return json.dumps(d, ensure_ascii=False)


# ----------------------------------------------------------------------
# Confluent producer wrapper
# ----------------------------------------------------------------------
class KafkaPublisher:
    """Thin wrapper over confluent_kafka.Producer.

    dry_run=True prints events instead of producing — used when no Confluent
    credentials are present, so the pipeline is fully testable offline.

    For a live run, set these environment variables (Confluent Cloud):
        KAFKA_BOOTSTRAP_SERVERS   e.g. pkc-xxxx.<region>.confluent.cloud:9092
        KAFKA_API_KEY
        KAFKA_API_SECRET
    """

    def __init__(self, dry_run: Optional[bool] = None):
        self.bootstrap = os.environ.get("KAFKA_BOOTSTRAP_SERVERS")
        self.api_key   = os.environ.get("KAFKA_API_KEY")
        self.api_secret = os.environ.get("KAFKA_API_SECRET")
        self.dry_run = (dry_run if dry_run is not None
                        else not all((self.bootstrap, self.api_key, self.api_secret)))
        self._producer = None
        self._delivered = 0
        self._failed = 0

        if not self.dry_run:
            from confluent_kafka import Producer
            self._producer = Producer({
                "bootstrap.servers": self.bootstrap,
                "security.protocol": "SASL_SSL",
                "sasl.mechanisms": "PLAIN",
                "sasl.username": self.api_key,
                "sasl.password": self.api_secret,
                "client.id": "wc26-fantasy-feed",
                "linger.ms": 50,
            })

    def _on_delivery(self, err, msg):
        if err is not None:
            self._failed += 1
            print(f"  ! delivery failed: {err}")
        else:
            self._delivered += 1

    def publish(self, topic: str, key: str, value_json: str):
        if self.dry_run:
            print(f"[DRY-RUN] -> {topic}  key={key}\n           {value_json}")
            self._delivered += 1
            return
        # produce() is async; poll() serves delivery callbacks.
        self._producer.produce(topic, key=key.encode("utf-8"),
                               value=value_json.encode("utf-8"),
                               on_delivery=self._on_delivery)
        self._producer.poll(0)

    def flush(self):
        if self._producer is not None:
            self._producer.flush(15)
        return {"delivered": self._delivered, "failed": self._failed,
                "mode": "dry-run" if self.dry_run else "live"}


# ----------------------------------------------------------------------
# Source interfaces
# ----------------------------------------------------------------------
class StatsSource(ABC):
    @abstractmethod
    def fetch(self) -> Iterable[PlayerStatsEvent]:
        ...


class AvailabilitySource(ABC):
    @abstractmethod
    def fetch(self) -> Iterable[PlayerAvailabilityEvent]:
        ...


# ----------------------------------------------------------------------
# Mock sources (synthesise events from the player pool — for offline testing)
# ----------------------------------------------------------------------
def _load_pool() -> pd.DataFrame:
    df = pd.read_excel(PLAYER_POOL)
    for c in ("Player Name", "Nation", "Position"):
        df[c] = df[c].astype(str).str.strip()
    return df


class MockStatsSource(StatsSource):
    def __init__(self, sample: int = 8, seed: int = 7):
        self.df = _load_pool().sample(sample, random_state=seed)

    def fetch(self) -> Iterable[PlayerStatsEvent]:
        rng = random.Random(11)
        for _, r in self.df.iterrows():
            pos = r["Position"]
            yield PlayerStatsEvent(
                player_name=r["Player Name"], nation=r["Nation"], position=pos,
                source="mock", fetched_at=_now(), competition="club:mock",
                opponent="OPP", minutes=rng.choice([0, 45, 90, 90]),
                goals=rng.choice([0, 0, 0, 1]) if pos in ("MID", "FWD") else 0,
                assists=rng.choice([0, 0, 1]),
                shots_on_target=rng.randint(0, 3), xg=round(rng.random(), 2),
                xa=round(rng.random() * 0.5, 2), key_passes=rng.randint(0, 4),
                clean_sheet=(pos in ("GK", "DEF") and rng.random() < 0.4),
                goals_conceded=rng.randint(0, 2) if pos in ("GK", "DEF") else 0,
                saves=rng.randint(0, 5) if pos == "GK" else 0,
                yellow_cards=rng.choice([0, 0, 0, 1]), red_cards=0,
                rating=round(rng.uniform(6.0, 8.5), 1),
                position_raw={"GK": "G", "DEF": "D", "MID": "M",
                              "FWD": "F"}.get(pos))


class MockAvailabilitySource(AvailabilitySource):
    def __init__(self, sample: int = 5, seed: int = 3):
        self.df = _load_pool().sample(sample, random_state=seed)

    def fetch(self) -> Iterable[PlayerAvailabilityEvent]:
        rng = random.Random(5)
        statuses = [Availability.FIT, Availability.FIT, Availability.DOUBTFUL,
                    Availability.INJURED, Availability.SUSPENDED]
        for _, r in self.df.iterrows():
            st = rng.choice(statuses)
            yield PlayerAvailabilityEvent(
                player_name=r["Player Name"], nation=r["Nation"], status=st,
                source="mock", fetched_at=_now(),
                detail=None if st == Availability.FIT else f"mock {st.value.lower()}",
                expected_to_start=(st == Availability.FIT))


# ----------------------------------------------------------------------
# Real provider adapter — HTTP plumbing only; YOU implement parse_response()
# ----------------------------------------------------------------------
class RestStatsSource(StatsSource):
    """Generic REST adapter. The HTTP machinery is real; the response parsing
    is left abstract ON PURPOSE — every provider (API-Football, Sportmonks,
    SportsDataIO, ...) returns a different JSON shape, and I won't guess at
    field paths. Subclass and implement parse_response() against your chosen
    provider's CURRENT documentation."""

    def __init__(self, base_url: str, headers: dict, params: dict):
        self.base_url, self.headers, self.params = base_url, headers, params

    def parse_response(self, payload: dict) -> Iterable[PlayerStatsEvent]:
        raise NotImplementedError(
            "Map your provider's JSON to PlayerStatsEvent here. See their docs "
            "for the exact response schema — do not assume field names.")

    def fetch(self) -> Iterable[PlayerStatsEvent]:
        import requests                       # real lib; install if needed
        resp = requests.get(self.base_url, headers=self.headers,
                            params=self.params, timeout=30)
        resp.raise_for_status()
        yield from self.parse_response(resp.json())


# ----------------------------------------------------------------------
# Pipeline runner
# ----------------------------------------------------------------------
def run_feed(stats_source: StatsSource,
             avail_source: AvailabilitySource,
             publisher: Optional[KafkaPublisher] = None):
    pub = publisher or KafkaPublisher()
    n_stats = n_avail = 0
    for ev in stats_source.fetch():
        pub.publish(TOPIC_STATS, ev.key(), ev.to_json()); n_stats += 1
    for ev in avail_source.fetch():
        pub.publish(TOPIC_AVAILABILITY, ev.key(), ev.to_json()); n_avail += 1
    summary = pub.flush()
    summary.update(stats_events=n_stats, availability_events=n_avail)
    return summary


if __name__ == "__main__":
    pub = KafkaPublisher()                    # dry-run unless KAFKA_* env vars set
    print(f"Publisher mode: {'DRY-RUN' if pub.dry_run else 'LIVE (Confluent Cloud)'}\n")
    print(f"Topics: {TOPIC_STATS}, {TOPIC_AVAILABILITY}\n")
    summary = run_feed(MockStatsSource(), MockAvailabilitySource(), pub)
    print("\n" + "-" * 60)
    print("Run summary:", summary)
