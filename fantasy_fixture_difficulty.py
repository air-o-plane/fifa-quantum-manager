r"""
FIFA WC2026 Fantasy — fixture difficulty (Option 3 for the xPts model).

Produces, per nation, TWO multipliers for the upcoming round:
  - attack_mult : scales attackers'/midfielders' xPts by how weak the next
                  opponent's DEFENCE is (easier defence -> >1).
  - defence_mult: scales defenders'/keepers' xPts by how weak the next
                  opponent's ATTACK is (easier attack -> >1, better clean-sheet
                  odds).

OPPONENT STRENGTH = BLEND (as agreed):
  strength = (1 - w_form) * ranking_strength  +  w_form * tournament_form
  - ranking_strength: from a FIFA-ranking tier table (stable, available now).
    Grounded in the 11 June 2026 FIFA ranking (top tiers verified via search);
    teams not listed default to a mid value.
  - tournament_form : goals for / against SO FAR, from the live API standings.
    After 1 round this is noisy, so w_form starts LOW and grows.
  w_form = rounds_played / (rounds_played + K_FORM), K_FORM=2  (mirrors xPts).

MAGNITUDE IS INTENTIONALLY GENTLE: multipliers are clamped to [1-SPREAD,
1+SPREAD] with SPREAD=0.18 — early fixture info nudges, never dominates.

Run (fetches fixtures + standings from API-Football):
  python3 fantasy_fixture_difficulty.py 1        # league id
  -> writes fixture_multipliers.csv (nation_code, attack_mult, defence_mult,
     next_opponent) and prints the table.

The xPts model imports load_multipliers() to apply them.
"""

from __future__ import annotations
import sys
import csv
from datetime import datetime, timezone

# ---- FIFA ranking tiers (11 June 2026; top tiers search-verified) ----------
# Strength on a 0..1 scale (higher = stronger). Used as the STABLE half.
# Codes are the pool's Nation Short Codes.
RANK_TIER = {
    # elite (top ~6): ARG, ESP, FRA, ENG, POR, BRA
    "ARG": 1.00, "ESP": 0.99, "FRA": 0.98, "ENG": 0.95, "POR": 0.94, "BRA": 0.93,
    # strong (7-15ish): NED, BEL, GER, CRO, MAR, ITA(n/a), COL, URU, USA, MEX, SUI, JPN
    "NED": 0.86, "BEL": 0.85, "GER": 0.84, "CRO": 0.82, "MAR": 0.80,
    "COL": 0.78, "URU": 0.77, "USA": 0.72, "MEX": 0.71, "SUI": 0.70, "JPN": 0.69,
    "SEN": 0.68, "KOR": 0.64, "ECU": 0.63, "AUS": 0.60, "NOR": 0.66, "SWE": 0.62,
    "EGY": 0.61, "CIV": 0.60, "QAT": 0.55, "IRN": 0.58, "PAR": 0.57, "PAN": 0.52,
    "TUN": 0.56, "NZL": 0.45, "SCO": 0.59, "CAN": 0.62, "ALG": 0.60, "GHA": 0.55,
    "RSA": 0.52, "UZB": 0.50, "JOR": 0.48, "IRQ": 0.47, "KSA": 0.49, "BIH": 0.55,
    "AUT": 0.63, "CZE": 0.58, "TUR": 0.62, "CPV": 0.42, "CUW": 0.40, "HAI": 0.43,
    "COD": 0.52, "UZB ": 0.50,
}
DEFAULT_STRENGTH = 0.55

K_FORM = 2.0
SPREAD = 0.18          # max +/- 18% adjustment


def _client():
    from fantasy_apifootball_adapter import ApiFootballClient
    return ApiFootballClient()


def _api_to_pool_code(name: str, lut) -> str | None:
    from fantasy_name_bridge import match_team
    return match_team(name, lut)


def fetch_inputs(league_id: int):
    """Returns (next_opponent, standings_form, rounds_played).
    next_opponent: {code: opponent_code} from each team's EARLIEST upcoming
    fixture (resolved by kickoff date, so every team gets its true next game).
    standings_form: {code: (goals_for, goals_against, played)}.
    """
    from fantasy_apifootball_adapter import SEASON
    from fantasy_name_bridge import load_pool, nation_lookup
    client = _client()
    lut = nation_lookup(load_pool())

    # All upcoming fixtures (large window so no team's next game is missed).
    up = client.get("fixtures", league=league_id, season=SEASON, next=60)
    # Collect, per team, ALL upcoming (date, opponent) then keep the earliest.
    per_team: dict[str, list[tuple[str, str]]] = {}
    for f in up.get("response", []):
        fx = f.get("fixture") or {}
        date = fx.get("date") or ""
        t = f.get("teams") or {}
        hc = _api_to_pool_code((t.get("home") or {}).get("name", ""), lut)
        ac = _api_to_pool_code((t.get("away") or {}).get("name", ""), lut)
        if hc and ac:
            per_team.setdefault(hc, []).append((date, ac))
            per_team.setdefault(ac, []).append((date, hc))
    next_opponent = {}
    for code, games in per_team.items():
        games.sort(key=lambda g: g[0])          # earliest kickoff first
        next_opponent[code] = games[0][1]

    # standings -> goals for/against so far
    form = {}
    st = client.get("standings", league=league_id, season=SEASON)
    for blk in st.get("response", []):
        groups = (((blk.get("league") or {}).get("standings")) or [])
        for grp in groups:
            for row in grp:
                code = _api_to_pool_code((row.get("team") or {}).get("name", ""), lut)
                allg = row.get("all") or {}
                goals = allg.get("goals") or {}
                if code:
                    form[code] = (goals.get("for", 0) or 0,
                                  goals.get("against", 0) or 0,
                                  allg.get("played", 0) or 0)
    rounds_played = max((v[2] for v in form.values()), default=0)
    return next_opponent, form, rounds_played, client.daily_remaining


def _form_strength(form: dict) -> dict:
    """Map tournament goals for/against to 0..1 attack & defence strengths."""
    if not form:
        return {}
    # normalise per-game goal rates across teams that have played
    atk, dfn = {}, {}
    rates_f, rates_a = [], []
    for c, (gf, ga, pl) in form.items():
        if pl > 0:
            rates_f.append(gf / pl); rates_a.append(ga / pl)
    if not rates_f:
        return {}
    fmax = max(rates_f) or 1.0
    amax = max(rates_a) or 1.0
    out = {}
    for c, (gf, ga, pl) in form.items():
        if pl > 0:
            attack = (gf / pl) / fmax                 # 0..1, higher = more goals
            defend = 1.0 - (ga / pl) / amax           # 0..1, higher = fewer conceded
            out[c] = (attack, defend)
    return out


def build(league_id: int):
    next_opp, form, rounds_played, remaining = fetch_inputs(league_id)
    w_form = rounds_played / (rounds_played + K_FORM)
    fstr = _form_strength(form)

    def strength(code, side):
        """side 'attack' or 'defence' overall strength of `code` in 0..1."""
        base = RANK_TIER.get(code, DEFAULT_STRENGTH)
        if code in fstr:
            atk, dfn = fstr[code]
            f = atk if side == "attack" else dfn
            return (1 - w_form) * base + w_form * f
        return base

    rows = []
    for code, opp in next_opp.items():
        # attacker multiplier: weaker opponent DEFENCE -> boost.
        opp_def = strength(opp, "defence")
        opp_atk = strength(opp, "attack")
        attack_mult = 1.0 + SPREAD * (1 - 2 * opp_def)   # opp_def low -> >1
        defence_mult = 1.0 + SPREAD * (1 - 2 * opp_atk)  # opp_atk low -> >1
        attack_mult = max(1 - SPREAD, min(1 + SPREAD, attack_mult))
        defence_mult = max(1 - SPREAD, min(1 + SPREAD, defence_mult))
        rows.append({"nation_code": code,
                     "attack_mult": round(attack_mult, 3),
                     "defence_mult": round(defence_mult, 3),
                     "next_opponent": opp})

    rows.sort(key=lambda r: -r["attack_mult"])
    with open("fixture_multipliers.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["nation_code", "attack_mult",
                                          "defence_mult", "next_opponent"])
        w.writeheader(); w.writerows(rows)

    print(f"Wrote fixture_multipliers.csv for {len(rows)} nations "
          f"(rounds_played={rounds_played}, form weight w={w_form:.2f}, "
          f"spread +/-{int(SPREAD*100)}%).")
    print(f"\n  {'nation':<6}{'opp':<6}{'attack':>8}{'defence':>9}")
    for r in rows:
        print(f"  {r['nation_code']:<6}{r['next_opponent']:<6}"
              f"{r['attack_mult']:>8}{r['defence_mult']:>9}")
    print(f"\n[daily requests remaining: {remaining}]")


# ---- consumed by the xPts model -------------------------------------------
def load_multipliers(path="fixture_multipliers.csv"):
    """-> {code: (attack_mult, defence_mult)}; empty dict if file absent."""
    try:
        import pandas as pd
        df = pd.read_csv(path)
        return {r["nation_code"]: (float(r["attack_mult"]), float(r["defence_mult"]))
                for _, r in df.iterrows()}
    except FileNotFoundError:
        return {}


def diagnose(league_id: int, codes: str = "CZE,RSA,KOR,MEX"):
    """Dump what the API actually returns for given teams' upcoming fixtures,
    so we debug against reality, not assumptions. codes = comma-separated
    pool Nation Short Codes."""
    from fantasy_apifootball_adapter import SEASON
    from fantasy_name_bridge import load_pool, nation_lookup
    client = _client()
    lut = nation_lookup(load_pool())
    want = {c.strip() for c in codes.split(",")}

    up = client.get("fixtures", league=league_id, season=SEASON, next=60)
    resp = up.get("response", [])
    print(f"API returned {len(resp)} upcoming fixtures (next=60).\n")
    print("Fixtures touching the teams of interest:")
    seen_codes = set()
    for f in resp:
        fx = f.get("fixture") or {}
        t = f.get("teams") or {}
        hn = (t.get("home") or {}).get("name", "")
        an = (t.get("away") or {}).get("name", "")
        hc = _api_to_pool_code(hn, lut)
        ac = _api_to_pool_code(an, lut)
        seen_codes.update(c for c in (hc, ac) if c)
        if (hc in want) or (ac in want):
            print(f"  {fx.get('date','?')[:16]}  {hn} ({hc}) v {an} ({ac})  "
                  f"round={(f.get('league') or {}).get('round','?')}")
    print(f"\nTeams of interest present in the fixture window: "
          f"{sorted(want & seen_codes)}")
    print(f"Teams of interest MISSING from the window: "
          f"{sorted(want - seen_codes)}")
    # also show any team names that failed to map (could hide CZE etc.)
    unmapped = set()
    for f in resp:
        t = f.get("teams") or {}
        for side in ("home", "away"):
            nm = (t.get(side) or {}).get("name", "")
            if nm and _api_to_pool_code(nm, lut) is None:
                unmapped.add(nm)
    if unmapped:
        print(f"\nUnmapped team names in fixtures (would be silently skipped): "
              f"{sorted(unmapped)}")
    print(f"\n[daily requests remaining: {client.daily_remaining}]")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "diagnose":
        diagnose(int(sys.argv[2]) if len(sys.argv) > 2 else 1)
    else:
        build(int(sys.argv[1]) if len(sys.argv) > 1 else 1)
