r"""
FIFA Men's World Cup 2026 Fantasy — name-mapping bridge.

PURPOSE
-------
API-Football identifies players as e.g. "Julián Álvarez" on team "Argentina".
The priced pool identifies the same player as Player Name "Alvarez", First
"Julián", Last "Álvarez", Nation Short Code "ARG", Nation "Argentina".
This module builds the (api_player_id -> pool row) mapping that every
downstream component (xPts model, optimisers) depends on.

STRATEGY (deterministic first, fuzzy last, manual review for stragglers)
------------------------------------------------------------------------
Team level:   API team name -> pool Nation (full name), via normalisation
              plus a small alias table ("South Korea" ~ "Korea Republic",
              "&" ~ "and", ...). Unmapped teams are REPORTED, never guessed.
Player level, within the matched nation only:
  1. exact normalised match on "First Last"
  2. exact normalised match on display Player Name
  3. exact normalised match on Last Name (only if unique in that nation)
  4. initial form: "K. Mbappé" -> first-initial + last-name match
  5. fuzzy (difflib ratio) against all candidate keys:
       >= 0.90  auto-accept, flagged "fuzzy"
       >= 0.75  provisional, flagged "REVIEW"
       <  0.75  unmatched
  A position cross-check (API Goalkeeper/Defender/Midfielder/Attacker vs
  pool GK/DEF/MID/FWD) flags suspicious matches instead of silently passing.

All names are compared accent-insensitively (NFKD, combining marks stripped).

MODES
-----
  selftest            offline: synthesises realistic API-style name variants
                      from the pool itself and measures the matcher (no
                      network, no quota). Run in-sandbox before live use.
  teams <league_id>   1 request: show API team -> pool nation mapping table.
  live  <league_id>   ~49 requests: teams + every squad; writes
                      name_mapping.csv + unmatched report for manual review.

HONESTY NOTES
-------------
- /players/squads response items are parsed defensively; if the structure
  differs from expectation the raw JSON is dumped so you can see reality.
- The squads endpoint reflects CURRENT squads; if a federation's final list
  changed late, a few pool players may legitimately not appear.
- Expect a handful of unmatched players even when everything works (name
  variants beyond fuzzy reach). The CSV is designed for a quick manual pass.
"""

from __future__ import annotations
import os
import sys
import csv
import json
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

import pandas as pd


def _read_csv(path):
    """Read a CSV tolerant of Excel re-encoding (Mac Roman / Latin-1 / cp1252),
    which silently mangles accented names when you save from Excel. Tries UTF-8
    first, then common fallbacks, and normalises the file back to UTF-8 on disk
    so the problem doesn't recur."""
    for enc in ("utf-8", "utf-8-sig", "cp1252", "mac_roman", "latin-1"):
        try:
            df = pd.read_csv(path, encoding=enc)
            if enc != "utf-8":
                print(f"  (note: {path} was {enc}-encoded — re-saving as UTF-8)")
                df.to_csv(path, index=False, encoding="utf-8")
            return df
        except (UnicodeDecodeError, UnicodeError):
            continue
    # last resort: lossy decode so we at least proceed
    return pd.read_csv(path, encoding="latin-1")

PLAYER_POOL = "FIFA_Men_s_World_Cup_2026_Player_Pool.xlsx"
OUT_CSV     = "name_mapping.csv"

FUZZY_ACCEPT = 0.90
FUZZY_REVIEW = 0.75

# pool Nation (full name) aliases <-> names the API plausibly uses.
# Applied AFTER normalisation. Each key maps to a TUPLE of alternatives,
# tried in order against the pool's nation names. Extend as the teams
# probe reveals gaps.
TEAM_ALIASES: dict[str, tuple[str, ...]] = {
    "south korea": ("korea republic",),
    "korea republic": ("south korea",),
    "ivory coast": ("cote divoire",),
    "cote divoire": ("ivory coast",),
    "dr congo": ("congo dr",),
    "congo dr": ("dr congo",),
    "cape verde": ("cabo verde",),
    "cabo verde": ("cape verde",),
    "cape verde islands": ("cape verde", "cabo verde"),
    "usa": ("united states",),
    "united states": ("usa",),
    "iran": ("ir iran",),
    "ir iran": ("iran",),
    "turkiye": ("turkey",),
    "turkey": ("turkiye",),
    "czechia": ("czech republic",),
    "czech republic": ("czechia",),
}

API_POS = {"Goalkeeper": "GK", "Defender": "DEF",
           "Midfielder": "MID", "Attacker": "FWD"}


def norm(s: Optional[str]) -> str:
    """Accent-insensitive, casefolded, punctuation-free comparison key."""
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("&", " and ").replace("-", " ").replace("'", "").replace(".", " ")
    return " ".join(s.casefold().split())


# ----------------------------------------------------------------------
# Pool index
# ----------------------------------------------------------------------
@dataclass
class PoolPlayer:
    row: int
    display: str
    first: str
    last: str
    nation_code: str
    nation_name: str
    position: str
    price: float = 0.0

    @property
    def full(self) -> str:
        return f"{self.first} {self.last}".strip()


def load_pool(path: str = PLAYER_POOL) -> list[PoolPlayer]:
    df = pd.read_excel(path)
    for c in ("Player Name", "Nation Short Code", "Nation", "Position",
              "First Name", "Last Name"):
        if c not in df.columns:
            raise RuntimeError(f"Pool is missing expected column: {c!r}")
    out = []
    for i, r in df.iterrows():
        out.append(PoolPlayer(
            row=i,
            display=str(r["Player Name"]).strip(),
            first="" if pd.isna(r["First Name"]) else str(r["First Name"]).strip(),
            last=str(r["Last Name"]).strip(),
            nation_code=str(r["Nation Short Code"]).strip(),
            nation_name=str(r["Nation"]).strip(),
            position=str(r["Position"]).strip(),
            price=float(r["Price ($M)"]),
        ))
    return out


def nation_lookup(pool: list[PoolPlayer]) -> dict[str, str]:
    """normalised pool nation name (+aliases) -> nation_code"""
    lut: dict[str, str] = {}
    for p in pool:
        key = norm(p.nation_name)
        lut.setdefault(key, p.nation_code)
        for alias in TEAM_ALIASES.get(key, ()):
            lut.setdefault(alias, p.nation_code)
    return lut


def match_team(api_team_name: str, lut: dict[str, str]) -> Optional[str]:
    k = norm(api_team_name)
    if k in lut:
        return lut[k]
    for alt in TEAM_ALIASES.get(k, ()):
        if alt in lut:
            return lut[alt]
    # last resort: fuzzy team match, high bar
    best, score = None, 0.0
    for key, code in lut.items():
        r = SequenceMatcher(None, k, key).ratio()
        if r > score:
            best, score = code, r
    return best if score >= 0.92 else None


# ----------------------------------------------------------------------
# Player matching
# ----------------------------------------------------------------------
@dataclass
class Match:
    pool_row: Optional[int]
    pool_display: str
    method: str          # full|display|last|initial|fuzzy|REVIEW|UNMATCHED
    score: float
    position_ok: Optional[bool]


def match_player(api_name: str, api_position: Optional[str],
                 candidates: list[PoolPlayer]) -> Match:
    n = norm(api_name)
    api_pos = API_POS.get(api_position or "", None)

    def pos_ok(p: PoolPlayer) -> Optional[bool]:
        return None if api_pos is None else (p.position == api_pos)

    def ambiguous(hits: list[PoolPlayer], method: str) -> Match:
        # Multiple equally-good candidates: NEVER silently pick one.
        return Match(None, " | ".join(h.display for h in hits[:4]),
                     "REVIEW", 0.5, None)

    # 1-3) exact keys evaluated TOGETHER: a variant may be one player's
    # surname and another player's full/display name (e.g. 'Hassan' is both
    # a display name and Trezeguet's legal surname). Accept only if every
    # exact interpretation points at the SAME player.
    full_hits    = [p for p in candidates if p.full and norm(p.full) == n]
    display_hits = [p for p in candidates if norm(p.display) == n]
    last_hits    = [p for p in candidates if norm(p.last) == n]
    union = {p.row: p for p in (*full_hits, *display_hits, *last_hits)}
    if len(union) == 1:
        p = next(iter(union.values()))
        method = ("full" if full_hits else "display" if display_hits else "last")
        return Match(p.row, p.display, method, 1.0, pos_ok(p))
    if len(union) > 1:
        return ambiguous(list(union.values()), "exact")
    # 4) initial form: "k mbappe" (from "K. Mbappé")
    parts = n.split()
    if len(parts) >= 2 and len(parts[0]) == 1:
        initial, rest = parts[0], " ".join(parts[1:])
        hits = [p for p in candidates
                if norm(p.last) == rest
                and (not p.first or norm(p.first)[:1] == initial)]
        if len(hits) == 1:
            return Match(hits[0].row, hits[0].display, "initial", 0.99,
                         pos_ok(hits[0]))
    # 5) fuzzy across all candidate keys — tie-aware: if a SECOND player
    #    scores within 0.03 of the best, the match is ambiguous -> REVIEW.
    best_p, best_r, second_r = None, 0.0, 0.0
    for p in candidates:
        p_best = 0.0
        for key in (p.full, p.display, p.last):
            if not key:
                continue
            r = SequenceMatcher(None, n, norm(key)).ratio()
            p_best = max(p_best, r)
        if p_best > best_r:
            best_p, second_r, best_r = p, best_r, p_best
        elif p_best > second_r:
            second_r = p_best
    if best_p is not None and best_r >= FUZZY_ACCEPT:
        if best_r - second_r < 0.03:
            return Match(None, best_p.display, "REVIEW", round(best_r, 3), None)
        return Match(best_p.row, best_p.display, "fuzzy", round(best_r, 3),
                     pos_ok(best_p))
    if best_p is not None and best_r >= FUZZY_REVIEW:
        return Match(best_p.row, best_p.display, "REVIEW", round(best_r, 3),
                     pos_ok(best_p))
    return Match(None, "", "UNMATCHED", round(best_r, 3), None)


# ----------------------------------------------------------------------
# Offline selftest — no network, no quota
# ----------------------------------------------------------------------
def selftest():
    """Synthesise API-style variants of every pool player's name and check
    the matcher recovers the right row. Variants mirror real API habits:
    full name, accent differences, 'K. Mbappé' initial form, surname only."""
    pool = load_pool()
    by_nation: dict[str, list[PoolPlayer]] = {}
    for p in pool:
        by_nation.setdefault(p.nation_code, []).append(p)

    def strip_accents(s):
        s = unicodedata.normalize("NFKD", s)
        return "".join(c for c in s if not unicodedata.combining(c))

    total = correct = wrong = review = unmatched = 0
    wrong_examples = []
    for p in pool:
        variants = []
        if p.first:
            variants.append(p.full)                       # "Kylian Mbappé"
            variants.append(strip_accents(p.full))        # "Kylian Mbappe"
            variants.append(f"{p.first[0]}. {p.last}")    # "K. Mbappé"
        variants.append(p.last)                           # "Mbappé"
        variants.append(strip_accents(p.display))         # display, no accents
        cands = by_nation[p.nation_code]
        for v in variants:
            total += 1
            m = match_player(v, None, cands)
            if m.pool_row == p.row:
                correct += 1
            elif m.method == "REVIEW":
                review += 1
            elif m.method == "UNMATCHED":
                unmatched += 1
            else:
                # matched, but to a different row: only a true error if that
                # row isn't an identically-named player
                wrong += 1
                if len(wrong_examples) < 8:
                    wrong_examples.append((v, p.nation_code, p.display,
                                           m.pool_display, m.method))
    print(f"selftest: {total} synthesized lookups over {len(pool)} players")
    print(f"  correct   : {correct}  ({100*correct/total:.1f}%)")
    print(f"  review    : {review}   (sent to manual review — acceptable)")
    print(f"  unmatched : {unmatched}")
    print(f"  WRONG row : {wrong}   (matched a different player — must be ~0)")
    for w in wrong_examples:
        print(f"    variant {w[0]!r} ({w[1]}) true={w[2]!r} got={w[3]!r} via {w[4]}")


# ----------------------------------------------------------------------
# Live modes (API-Football)
# ----------------------------------------------------------------------
def _pos_from_api_letter_or_word(val: Optional[str]) -> Optional[str]:
    """API uses 'Goalkeeper'/'Defender'/... in squads and 'G'/'D'/'M'/'F'
    in fixture stats. Normalise either to GK/DEF/MID/FWD."""
    if not val:
        return None
    if val in API_POS:
        return API_POS[val]
    return {"G": "GK", "D": "DEF", "M": "MID", "F": "FWD"}.get(val.strip()[:1].upper())


def _sug_row(r) -> dict:
    return {"api_name": r["api_name"], "api_team": r["api_team"],
            "nation_code": r["nation_code"]}


def resolve():
    """Offline pass over the two CSVs from `live`. For each UNMATCHED API
    player, propose the gap (pool-unmatched) player of the SAME nation, using
    name similarity with position context. Writes name_mapping_suggestions.csv
    with a confidence tag, so you approve rather than hunt. No network; run
    AFTER `live`."""
    main_csv = OUT_CSV
    gap_csv  = OUT_CSV.replace(".csv", "_pool_unmatched.csv")
    sug_csv  = OUT_CSV.replace(".csv", "_suggestions.csv")
    try:
        main = _read_csv(main_csv)
        gap  = _read_csv(gap_csv)
    except FileNotFoundError as e:
        print(f"Missing input: {e.filename}. Run `live <league_id>` first.")
        return

    unmatched = main[main["method"] == "UNMATCHED"].copy()

    gap_by_nation: dict[str, list[dict]] = {}
    for _, g in gap.iterrows():
        gap_by_nation.setdefault(g["nation_code"], []).append({
            "pool_row": int(g["pool_row"]), "position": str(g["position"]),
            "name": str(g["player_name"]), "price": g["price"], "used": False,
        })

    suggestions = []
    for _, r in unmatched.iterrows():
        code = r["nation_code"]
        cands = [g for g in gap_by_nation.get(code, []) if not g["used"]]
        if not cands:
            suggestions.append({**_sug_row(r), "suggested_pool_player": "",
                                "suggested_pool_row": "", "match_position": "",
                                "confidence": "NO_GAP_CANDIDATE"})
            continue
        best, best_score = None, -1.0
        for g in cands:
            name_sim = SequenceMatcher(None, norm(r["api_name"]),
                                       norm(g["name"])).ratio()
            last_sim = SequenceMatcher(None, norm(r["api_name"]).split()[-1],
                                       norm(g["name"]).split()[-1]).ratio()
            sim = max(name_sim, last_sim)
            if sim > best_score:
                best, best_score = g, sim
        n_same = len(cands)
        if best_score >= 0.6 and n_same == 1:
            conf = "HIGH (only gap player for nation)"
        elif best_score >= 0.6:
            conf = "MEDIUM (name-similar, multiple candidates)"
        elif n_same == 1:
            conf = "LOW (sole nation candidate, weak name match)"
        else:
            conf = "REVIEW (weak match, multiple candidates)"
        if best_score >= 0.6:
            best["used"] = True              # consume confident pairings
        suggestions.append({**_sug_row(r),
                            "suggested_pool_player": best["name"],
                            "suggested_pool_row": best["pool_row"],
                            "match_position": best["position"],
                            "confidence": conf})

    with open(sug_csv, "w", newline="", encoding="utf-8") as f:
        cols = ["api_name", "api_team", "nation_code", "suggested_pool_player",
                "suggested_pool_row", "match_position", "confidence", "approved"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for s in suggestions:
            # Pre-approve HIGH/MEDIUM; leave the rest blank for your decision.
            # Edit this column (yes / no / a pool_row number) before `apply`.
            s["approved"] = ("yes" if s["confidence"].startswith(("HIGH", "MEDIUM"))
                             else "")
            w.writerow(s)

    by_conf: dict[str, int] = {}
    for s in suggestions:
        key = s["confidence"].split(" ")[0]
        by_conf[key] = by_conf.get(key, 0) + 1
    print(f"Resolved {len(suggestions)} UNMATCHED API players -> {sug_csv}")
    print(f"  confidence breakdown: {by_conf}")
    print("\nHIGH/MEDIUM suggestions (approve these first):")
    for s in suggestions:
        if s["confidence"].startswith(("HIGH", "MEDIUM")):
            print(f"  {s['nation_code']:<4} {s['api_name']:<24} -> "
                  f"{s['suggested_pool_player']:<28} "
                  f"[{s['match_position']}] {s['confidence']}")
    leftover = [s for s in suggestions
                if not s["confidence"].startswith(("HIGH", "MEDIUM"))]
    if leftover:
        print(f"\n{len(leftover)} need your eye (LOW / REVIEW / NO_GAP_CANDIDATE):")
        for s in leftover[:15]:
            print(f"  {s['nation_code']:<4} {s['api_name']:<24} -> "
                  f"{s['suggested_pool_player'] or '(no pool candidate)':<28} "
                  f"{s['confidence']}")
    print(f"\nNext: review {sug_csv} — the 'approved' column is pre-filled 'yes'")
    print("for HIGH/MEDIUM. Set 'yes'/'no' (or a specific pool_row) on the rest,")
    print("then run:  python3 fantasy_name_bridge.py apply")


def apply():
    """Merge the auto-matches (name_mapping.csv) with your approved
    suggestions (name_mapping_suggestions.csv, 'approved' column) into one
    authoritative player_map.csv: api_player_id -> pool_row. No network.

    'approved' values: 'yes' (use the suggested_pool_row), 'no'/blank (skip,
    player stays unmapped), or a specific integer pool_row to override the
    suggestion. The merge refuses to map two API players to the same pool_row
    and reports any such conflicts instead of silently picking one."""
    main_csv = OUT_CSV
    sug_csv  = OUT_CSV.replace(".csv", "_suggestions.csv")
    map_csv  = "player_map.csv"
    excl_file = "exclusions.txt"
    try:
        main = _read_csv(main_csv)
    except FileNotFoundError:
        print(f"Missing {main_csv}. Run `live <league_id>` first.")
        return
    try:
        sug = _read_csv(sug_csv)
    except FileNotFoundError:
        sug = None

    # Exclusions: api players to DROP from the map regardless of source.
    # One per line in exclusions.txt — either an api_player_id (digits) or an
    # exact api_name. '#' starts a comment. Use this to resolve auto-match
    # conflicts (the losing sibling/duplicate) or to drop non-pool call-ups.
    excl_ids: set[str] = set()
    excl_names: set[str] = set()
    try:
        with open(excl_file, encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                (excl_ids if line.isdigit() else excl_names).add(
                    line if line.isdigit() else norm(line))
        print(f"Loaded {len(excl_ids) + len(excl_names)} exclusion(s) from "
              f"{excl_file}.")
    except FileNotFoundError:
        pass   # optional file

    pool = {p.row: p for p in load_pool()}
    rows: list[dict] = []
    seen_pool: dict[int, str] = {}     # pool_row -> api_name (conflict guard)
    conflicts: list[str] = []
    excluded_hits = 0

    def add(api_id, api_name, nation, pool_row, method):
        nonlocal excluded_hits
        if (str(api_id) in excl_ids) or (norm(str(api_name)) in excl_names):
            excluded_hits += 1
            return
        if pool_row is None or (isinstance(pool_row, float) and pd.isna(pool_row)):
            return
        pool_row = int(pool_row)
        p = pool.get(pool_row)
        if p is None:
            conflicts.append(f"{api_name}: pool_row {pool_row} not in pool")
            return
        if pool_row in seen_pool:
            conflicts.append(f"pool_row {pool_row} ({p.display}) claimed by both "
                             f"'{seen_pool[pool_row]}' and '{api_name}'")
            return
        seen_pool[pool_row] = api_name
        rows.append({"api_player_id": api_id, "api_name": api_name,
                     "nation_code": nation, "pool_row": pool_row,
                     "pool_player_name": p.display, "pool_position": p.position,
                     "price": p.price, "source": method})

    # 1) all confident auto-matches from the main run
    auto = main[~main["method"].isin(["REVIEW", "UNMATCHED"])]
    for _, r in auto.iterrows():
        add(r["api_player_id"], r["api_name"], r["nation_code"],
            r["pool_row"], r["method"])

    # 2) your approved suggestions (need api_player_id back from main_csv by name+nation)
    approved_count = 0
    if sug is not None:
        # index api ids from the main csv by (api_name, nation_code)
        id_lookup = {}
        for _, r in main.iterrows():
            id_lookup[(str(r["api_name"]), str(r["nation_code"]))] = r["api_player_id"]
        for _, s in sug.iterrows():
            val = str(s.get("approved", "")).strip().lower()
            if val in ("", "no", "n", "false", "0", "nan"):
                continue
            if val in ("yes", "y", "true", "1"):
                pool_row = s["suggested_pool_row"]
            else:
                try:
                    pool_row = int(float(val))   # explicit pool_row override
                except ValueError:
                    conflicts.append(f"{s['api_name']}: bad 'approved' value {val!r}")
                    continue
            api_id = id_lookup.get((str(s["api_name"]), str(s["nation_code"])))
            add(api_id, s["api_name"], s["nation_code"], pool_row, "approved")
            approved_count += 1

    with open(map_csv, "w", newline="", encoding="utf-8") as f:
        cols = ["api_player_id", "api_name", "nation_code", "pool_row",
                "pool_player_name", "pool_position", "price", "source"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r["nation_code"],
                                                r["pool_player_name"])))

    mapped_pool = {r["pool_row"] for r in rows}
    unmapped_pool = [p for p in pool.values() if p.row not in mapped_pool]
    print(f"Wrote {map_csv}: {len(rows)} api->pool mappings "
          f"({len(auto)} auto + {approved_count} approved"
          f"{f', {excluded_hits} excluded' if excluded_hits else ''}).")
    print(f"Pool players covered: {len(mapped_pool)}/{len(pool)}  "
          f"({len(unmapped_pool)} still unmapped — fine if they're non-squad).")
    if conflicts:
        print(f"\n!! {len(conflicts)} CONFLICTS to fix (not written to the map):")
        for c in conflicts[:20]:
            print(f"   {c}")
        print("\n   To resolve: add the LOSING api player of each pair to "
              f"exclusions.txt\n   (one api_name per line), then re-run apply. "
              "For an auto-matched\n   conflict the loser isn't in the suggestions "
              "file — exclusions.txt is\n   the place to drop it. Suggestion-side "
              "losers can instead be set to\n   'no' in the suggestions CSV.")


def _client():
    from fantasy_apifootball_adapter import ApiFootballClient
    return ApiFootballClient()


def teams(league_id: int):
    from fantasy_apifootball_adapter import SEASON
    client = _client()
    pool = load_pool()
    lut = nation_lookup(pool)
    data = client.get("teams", league=league_id, season=SEASON)
    rows = data.get("response", [])
    if not rows:
        print("(no teams returned — check league/season)")
        return
    print(f"{'API team':<28}{'API id':>8}   -> pool nation code")
    for item in rows:
        t = item.get("team") or {}
        code = match_team(t.get("name", ""), lut)
        flag = "" if code else "   !! UNMAPPED — add to TEAM_ALIASES"
        print(f"{t.get('name',''):<28}{t.get('id',''):>8}   -> {code or '??'}{flag}")
    print(f"\n[daily requests remaining: {client.daily_remaining}]")


def live(league_id: int, accept_threshold: float = 1.01):
    """accept_threshold: REVIEW rows with score >= this AND a concrete pool_row
    are auto-promoted to 'auto-accept' (default 1.01 = off). 0.83 is a sensible
    value: it confirms 'L. Vuskovic'->Vuskovic-type rows while leaving genuine
    ambiguities (the 0.5 tie rows, which have no single pool_row) for you."""
    from fantasy_apifootball_adapter import SEASON
    client = _client()
    pool = load_pool()
    lut = nation_lookup(pool)
    by_nation: dict[str, list[PoolPlayer]] = {}
    for p in pool:
        by_nation.setdefault(p.nation_code, []).append(p)

    data = client.get("teams", league=league_id, season=SEASON)
    team_rows = data.get("response", [])
    print(f"{len(team_rows)} teams returned; fetching squads...")

    out_rows, unmatched_rows, unmapped_teams = [], [], []
    matched_pool_rows: set[int] = set()
    auto_promoted = 0
    for item in team_rows:
        t = item.get("team") or {}
        code = match_team(t.get("name", ""), lut)
        if code is None:
            unmapped_teams.append(t.get("name"))
            continue
        sq = client.get("players/squads", team=t.get("id"))
        resp = sq.get("response", [])
        players = (resp[0].get("players") if resp else None) or []
        if not players and resp:
            print("Unexpected squads shape — raw first item:")
            print(json.dumps(resp[0], indent=2, ensure_ascii=False)[:800])
        for ap in players:
            m = match_player(ap.get("name", ""), ap.get("position"),
                             by_nation.get(code, []))
            method = m.method
            # auto-promote unambiguous REVIEW rows above the threshold
            if (method == "REVIEW" and m.pool_row is not None
                    and m.score >= accept_threshold):
                method = "auto-accept"
                auto_promoted += 1
            if m.pool_row is not None and method != "REVIEW":
                matched_pool_rows.add(m.pool_row)
            rec = {
                "api_player_id": ap.get("id"),
                "api_name": ap.get("name"),
                "api_team": t.get("name"),
                "nation_code": code,
                "pool_player_name": m.pool_display,
                "pool_row": m.pool_row,
                "method": method,
                "score": m.score,
                "position_consistent": m.position_ok,
            }
            out_rows.append(rec)
            if method in ("REVIEW", "UNMATCHED"):
                unmatched_rows.append(rec)

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)

    # Reverse gap: priced pool players that NO API squad member matched.
    # These are players you can't actually field — the list that affects picks.
    gap = [p for p in pool if p.row not in matched_pool_rows]
    gap_csv = OUT_CSV.replace(".csv", "_pool_unmatched.csv")
    with open(gap_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["pool_row", "player_name", "nation_code", "position", "price"])
        for p in sorted(gap, key=lambda x: (x.nation_code, x.position)):
            w.writerow([p.row, p.display, p.nation_code, p.position, p.price])

    n_ok = sum(1 for r in out_rows if r["method"] not in ("REVIEW", "UNMATCHED"))
    print(f"\nMapped {n_ok}/{len(out_rows)} squad players automatically "
          f"({100*n_ok/max(len(out_rows),1):.1f}%).")
    if accept_threshold <= 1.0:
        print(f"  (incl. {auto_promoted} REVIEW rows auto-accepted at "
              f">= {accept_threshold})")
    print(f"Wrote {OUT_CSV}.")
    if unmapped_teams:
        print(f"UNMAPPED TEAMS (extend TEAM_ALIASES): {unmapped_teams}")
    if unmatched_rows:
        print(f"\n{len(unmatched_rows)} rows still need manual review "
              f"(method REVIEW/UNMATCHED), e.g.:")
        for r in unmatched_rows[:12]:
            print(f"  {r['api_team']:<14} {r['api_name']:<26} -> "
                  f"{r['pool_player_name'] or '???':<22} "
                  f"[{r['method']} {r['score']}]")

    print(f"\nReverse gap: {len(gap)} priced pool players matched NO API squad "
          f"member.\n  -> wrote {gap_csv} (players you likely cannot field).")
    for p in sorted(gap, key=lambda x: x.nation_code)[:12]:
        print(f"  {p.nation_code:<4} {p.display:<24} {p.position:<4} "
              f"${p.price}")
    if len(gap) > 12:
        print(f"  ... and {len(gap)-12} more (see CSV)")
    print(f"\n[daily requests remaining: {client.daily_remaining}]")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "selftest"
    if mode == "selftest":
        selftest()
    elif mode == "teams" and len(sys.argv) > 2:
        teams(int(sys.argv[2]))
    elif mode == "live" and len(sys.argv) > 2:
        thr = float(sys.argv[3]) if len(sys.argv) > 3 else 1.01
        live(int(sys.argv[2]), accept_threshold=thr)
    elif mode == "resolve":
        resolve()
    elif mode == "apply":
        apply()
    else:
        print("usage: fantasy_name_bridge.py selftest | teams <league_id> "
              "| live <league_id> [accept_threshold] | resolve | apply")
