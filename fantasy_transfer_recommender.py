r"""
FIFA WC2026 Fantasy — transfer recommender.

Given your CURRENT 15-player squad and the model's xpts.csv, finds the
transfers that most improve expected points for the upcoming round — judged
against FIFA's real transfer economy, not from scratch.

FIFA WC2026 transfer rules encoded (verified June 2026):
  - 2 free transfers before MD2 and before MD3.
  - 1 unused transfer rolls over within the group stage (so MD3 can have 3).
  - Each transfer beyond your free allotment costs 3 points.
  - Unlimited transfers before MD1 and before the Round of 32.
  - Squad: 2 GK / 5 DEF / 5 MID / 3 FWD, $100M group-stage budget, max 3/nation.

So a transfer is only worth making if its xPts gain clears the bar:
  - within free transfers: any positive gain helps (but tiny gains may not be
    worth burning a transfer you could roll over — flagged).
  - beyond free transfers: gain must exceed 3.0 xPts to beat the hit.

The recommender is CONSERVATIVE by design: with little data, w is low, so it
won't suggest churn. It reports each candidate's gain so YOU decide.

INPUT  my_squad.csv : columns player_name, nation_code, position
                      (exactly your 15; the script maps them to pool rows).
USAGE
  python3 fantasy_transfer_recommender.py [free_transfers] [budget] [nation_cap]
  e.g. python3 fantasy_transfer_recommender.py 2 100 3
"""

from __future__ import annotations
import sys
import csv
from difflib import SequenceMatcher

import pandas as pd

from fantasy_name_bridge import load_pool, norm, _read_csv

HIT_COST = 3.0          # points per extra transfer
SQUAD_QUOTA = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}


def map_squad(path="my_squad.csv"):
    pool = load_pool()
    by_np: dict[tuple, list] = {}
    for p in pool:
        by_np.setdefault((p.nation_code, p.position), []).append(p)
    df = _read_csv(path)
    mapped, problems = [], []
    for _, r in df.iterrows():
        nm, nat, pos = str(r["player_name"]), str(r["nation_code"]), str(r["position"])
        cands = by_np.get((nat.strip(), pos.strip()), [])
        n = norm(nm)
        union = {p.row: p for p in cands
                 if norm(p.display) == n or (p.full and norm(p.full) == n)
                 or norm(p.last) == n}
        if len(union) == 1:
            mapped.append(next(iter(union.values())))
        else:
            best, br = None, 0.0
            for p in cands:
                rr = max(SequenceMatcher(None, n, norm(k)).ratio()
                         for k in (p.display, p.full, p.last) if k)
                if rr > br:
                    best, br = p, rr
            if best and br >= 0.85 and len(union) == 0:
                mapped.append(best)
            else:
                problems.append((nm, nat, pos, best.display if best else "?", round(br, 2)))
    return pool, mapped, problems


def recommend(free_transfers=2, budget=100.0, nation_cap=3):
    pool, squad, problems = map_squad()
    if problems:
        print("Could not confidently map these — fix my_squad.csv first:")
        for p in problems:
            print(f"  {p[0]} ({p[1]} {p[2]}) closest={p[3]} [{p[4]}]")
        return
    xp = {int(r["pool_row"]): float(r["xpts"])
          for _, r in _read_csv("xpts.csv").iterrows()}

    squad_rows = {p.row for p in squad}
    cur_value = sum(p.price for p in squad)
    cur_xpts = sum(xp.get(p.row, 0.0) for p in squad)
    bank = budget - cur_value
    nat_count: dict[str, int] = {}
    for p in squad:
        nat_count[p.nation_code] = nat_count.get(p.nation_code, 0) + 1

    print(f"Current squad: ${cur_value:.1f}M used, ${bank:.1f}M in bank, "
          f"sum xPts {cur_xpts:.2f} (all 15).")
    print(f"Free transfers: {free_transfers}; extra cost {HIT_COST:.0f} pts each.")

    # ---- AVAILABILITY FLAGS: surface injured/suspended squad members ----
    # Reads availability_snapshot.csv if present (produced by
    # fantasy_apifootball_adapter.py injuries <league>). These are FORCED-OUT
    # candidates that the model cannot see — they'll score 0 every round
    # they're out, so transferring them is high-value regardless of xPts gain.
    try:
        from fantasy_name_bridge import nation_lookup, match_team, norm as _norm
        avail = _read_csv("availability_snapshot.csv")
        team_lut = nation_lookup(pool)
        squad_by_key = {(p.nation_code, _norm(p.display)): p for p in squad}
        # also key on last name as a fallback
        for p in squad:
            squad_by_key.setdefault((p.nation_code, _norm(p.last)), p)
        forced_out = []
        for _, r in avail.iterrows():
            code = match_team(str(r.get("nation", "")), team_lut)
            if not code:
                continue
            key = (code, _norm(str(r.get("player_name", ""))))
            p = squad_by_key.get(key)
            if p is None:
                # try last-name match
                last_key = (code, _norm(str(r.get("player_name", "")).split()[-1])
                            if str(r.get("player_name", "")).strip() else (code, ""))
                p = squad_by_key.get(last_key)
            if p is not None:
                forced_out.append((p, str(r.get("status", "")),
                                   str(r.get("detail", ""))))
        if forced_out:
            print("\n⚠ FORCED OUT — your squad has unavailable players (injury/suspension):")
            for p, status, detail in forced_out:
                print(f"   {p.position} {p.display} ({p.nation_code})  "
                      f"{status} — {detail or '(no detail)'}")
            print("  These will score 0 while unavailable. Transferring them out "
                  "is high-value regardless of xPts gain.")
    except FileNotFoundError:
        pass        # no availability data yet; silent skip
    except Exception as e:
        print(f"  (availability check skipped: {e})")

    # ---- ELIMINATION FLAGS: surface squad members from knocked-out nations ----
    # Reads qualified_nations.csv (or r32_nations.csv). Any squad player from a
    # nation NOT in that file will score 0 every remaining round — they must be
    # transferred out regardless of xPts ranking. The injury feed cannot catch
    # this (it tracks player-level unavailability, not team elimination).
    try:
        import os
        _qf_path = None
        for _p in ("qualified_nations.csv", "r32_nations.csv"):
            if os.path.exists(_p):
                _qf_path = _p; break
        if _qf_path:
            from fantasy_name_bridge import nation_lookup, match_team
            _qdf = _read_csv(_qf_path)
            _qcol = "nation" if "nation" in _qdf.columns else _qdf.columns[0]
            _qual_names = set(_qdf[_qcol].astype(str).str.strip())
            # map full names → codes using pool
            _team_lut = nation_lookup(pool)
            _qual_codes: set[str] = set()
            for _nm in _qual_names:
                _code = match_team(_nm, _team_lut)
                if _code:
                    _qual_codes.add(_code)
                else:
                    _qual_codes.add(_nm)   # keep as-is if bridge misses
            _eliminated = [p for p in squad if p.nation_code not in _qual_codes]
            if _eliminated:
                print("\n⚠ ELIMINATED — your squad contains players from knocked-out nations"
                      " (they will score 0 every remaining round):")
                for p in _eliminated:
                    print(f"   {p.position} {p.display} ({p.nation_code})  "
                          f"— nation eliminated, MUST transfer out")
                print(f"  {len(_eliminated)} forced transfer(s) needed before optional upgrades.")
    except Exception as e:
        print(f"  (elimination check skipped: {e})")
    print()

    # All candidate single transfers: out (in squad) -> in (same position, not in squad,
    # KNOCKOUT FILTER: restrict candidate "IN" players to qualified nations only.
    # Mirrors the filter in fantasy_build_squad.py. Without this, the recommender
    # suggests players from eliminated nations (Iran, Curaçao, Uruguay etc.) who
    # will score 0 every remaining round. Activates when qualified_nations.csv or
    # r32_nations.csv is present; silent no-op in group stage when neither exists.
    import os
    qualified_nations: set[str] | None = None
    for path in ("qualified_nations.csv", "r32_nations.csv"):
        if os.path.exists(path):
            qdf = _read_csv(path)
            col = "nation" if "nation" in qdf.columns else qdf.columns[0]
            # convert full names → nation codes via the pool
            full_to_code = {p.display_nation: p.nation_code
                            for p in pool if hasattr(p, "display_nation")}
            # fallback: match on nation_code directly if already codes
            qual_raw = set(qdf[col].astype(str).str.strip())
            # check if they look like codes (≤3 chars) or full names
            if all(len(x) <= 3 for x in qual_raw if x):
                qualified_nations = qual_raw
            else:
                # map full names to codes via pool
                code_map = {}
                for p in pool:
                    code_map[p.nation_code] = p.nation_code   # identity
                # use match_team from bridge for full-name → code
                from fantasy_name_bridge import nation_lookup, match_team
                team_lut_q = nation_lookup(pool)
                qualified_nations = set()
                for name in qual_raw:
                    code = match_team(name, team_lut_q)
                    if code:
                        qualified_nations.add(code)
                    else:
                        qualified_nations.add(name)  # keep as-is, bridge miss
            print(f"Transfer filter: candidates restricted to {len(qualified_nations)} "
                  f"qualified nations (from {path}).")
            break

    # affordable, nation cap ok). Score = xpts(in) - xpts(out).
    pool_by_pos: dict[str, list] = {}
    for p in pool:
        pool_by_pos.setdefault(p.position, []).append(p)

    singles = []
    for out_p in squad:
        for in_p in pool_by_pos[out_p.position]:
            if in_p.row in squad_rows:
                continue
            # reject candidates from eliminated nations
            if qualified_nations and in_p.nation_code not in qualified_nations:
                continue
            if xp.get(in_p.row, 0.0) <= xp.get(out_p.row, 0.0):
                continue
            new_bank = bank + out_p.price - in_p.price
            if new_bank < -1e-9:
                continue
            nc = dict(nat_count)
            nc[out_p.nation_code] -= 1
            nc[in_p.nation_code] = nc.get(in_p.nation_code, 0) + 1
            if nc[in_p.nation_code] > nation_cap:
                continue
            gain = xp[in_p.row] - xp.get(out_p.row, 0.0)
            singles.append((gain, out_p, in_p, new_bank))

    singles.sort(key=lambda x: -x[0])
    if not singles:
        print("No positive-gain single transfer available. Hold your transfers.")
        return

    print("Top single-transfer upgrades (gain = xPts in - xPts out):")
    print(f"  {'OUT':<22}{'xPts':>5}   {'IN':<22}{'xPts':>5}{'gain':>7}{'bank':>7}")
    for gain, o, i, nb in singles[:10]:
        print(f"  {o.display:<22}{xp.get(o.row,0):>5.2f} - {i.display:<22}"
              f"{xp[i.row]:>5.2f}{gain:>7.2f}{nb:>7.1f}")

    # ---- GUARDRAIL: evaluate transfers CUMULATIVELY, net of the points hit ----
    # Greedily assemble the best non-conflicting transfers in gain order, then
    # show the running net = (sum of gains) - max(0, n_transfers - free)*HIT_COST.
    # The optimal stopping point is where net peaks; going further is flagged.
    seq, used_out, used_in = [], set(), set()
    bank_now = bank
    nc = dict(nat_count)
    for gain, o, i, _ in singles:
        if o.row in used_out or i.row in used_in:
            continue
        new_bank = bank_now + o.price - i.price
        if new_bank < -1e-9:
            continue
        nc2 = dict(nc); nc2[o.nation_code] -= 1
        nc2[i.nation_code] = nc2.get(i.nation_code, 0) + 1
        if nc2[i.nation_code] > nation_cap:
            continue
        seq.append((gain, o, i)); used_out.add(o.row); used_in.add(i.row)
        bank_now = new_bank; nc = nc2

    # running cumulative net of hits
    best_n, best_net, run = 0, 0.0, 0.0
    rows_tbl = []
    for k, (gain, o, i) in enumerate(seq, start=1):
        run += gain
        hits = max(0, k - free_transfers) * HIT_COST
        net = run - hits
        rows_tbl.append((k, o, i, gain, run, hits, net))
        if net > best_net + 1e-9:
            best_net, best_n = net, k

    print("\n" + "=" * 70)
    print("  TRANSFER GUARDRAIL — cumulative value net of the points hit")
    print("=" * 70)
    print(f"  free transfers: {free_transfers}   |   each extra transfer: "
          f"-{HIT_COST:.0f} pts")
    print(f"\n  {'#':>2} {'OUT -> IN':<42}{'gain':>6}{'Σgain':>7}{'hit':>6}{'NET':>7}")
    for k, o, i, gain, run, hits, net in rows_tbl[:12]:
        marker = "  <= best" if k == best_n else ("  (paid)" if k > free_transfers else "")
        label = f"{o.display} -> {i.display}"
        print(f"  {k:>2} {label:<42}{gain:>6.2f}{run:>7.2f}{hits:>6.0f}{net:>7.2f}{marker}")

    # Verdict
    print("\n  VERDICT:")
    if best_n == 0:
        print("    No transfer has positive net value. HOLD — make zero transfers.")
    elif best_n <= free_transfers:
        print(f"    Make at most {best_n} transfer(s) — all within your free "
              f"allotment, net +{best_net:.2f} xPts, NO points hit.")
        if best_n < free_transfers:
            print(f"    You have {free_transfers - best_n} free transfer(s) left over: "
                  f"bank/roll it rather than forcing a move.")
    else:
        paid = best_n - free_transfers
        print(f"    Peak net is at {best_n} transfers ({paid} paid, "
              f"-{paid*HIT_COST:.0f} pts), net +{best_net:.2f}. Only worth it if "
              f"you're confident in the gains.")

    # Loud warning about over-trading past the optimum
    if len(rows_tbl) > best_n:
        nxt = rows_tbl[best_n] if best_n < len(rows_tbl) else None
        if nxt:
            _, _, _, _, _, _, net_after = nxt
            loss = best_net - net_after
            print(f"\n  ⚠ WARNING: making transfer #{best_n+1} drops net value by "
                  f"{loss:.2f} (it's a paid move that gains less than {HIT_COST:.0f}).")
            print(f"    Every transfer beyond #{best_n} DESTROYS points. The "
                  f"from-scratch 'optimal squad' is NOT reachable for free — do "
                  f"not chase it with hits.")

    # Roll-over nudge (group stage)
    if best_n <= 1 and free_transfers >= 2:
        print("\n  TIP: at most one move helps — use 1 transfer and roll the other "
              "into the next matchday (group-stage rollover).")
    print("\n  Reminder: this ranks by squad xPts. A move improving a BENCH player "
          "is near-worthless;\n  prioritise starting-XI upgrades, and remember your "
          "captain pick outweighs any single transfer.")


if __name__ == "__main__":
    ft = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    bg = float(sys.argv[2]) if len(sys.argv) > 2 else 100.0
    ncap = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    recommend(ft, bg, ncap)
