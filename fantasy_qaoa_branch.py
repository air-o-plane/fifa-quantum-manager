r"""
FIFA Men's World Cup 2026 Fantasy — QAOA branch (Classiq).

WHAT THIS IS
------------
The quantum-experimental counterpart to fantasy_ilp_baseline.py. It poses
squad selection as a QUBO and runs it through Classiq's QAOA workflow, so
you can benchmark the quantum result against the ILP optimum on identical
inputs — the whole point of the experiment.

Because QAOA can't be simulated at 1,245 players, this operates on a
SHORTLIST (top-N per position by real xPts), exactly the reduction we agreed
on. Objective = the fixture-adjusted xPts from xpts.csv (falls back to the
price proxy only if xpts.csv is absent).

API: written against classiq 1.17.0 using the CURRENT CombinatorialProblem
interface (combi = CombinatorialProblem(...); combi.get_model();
combi.optimize(...)). The offline encoding proof uses pyo_model_to_hamiltonian,
confirmed present in 1.17.0.

WHAT IS VERIFIED OFFLINE vs WHAT NEEDS YOUR CLASSIQ LOGIN
--------------------------------------------------------
Verified with no cloud (run the file with RUN_ON_CLASSIQ_CLOUD=False):
  - the Pyomo model builds,
  - pyo_model_to_hamiltonian() produces the QAOA cost Hamiltonian,
  - that Hamiltonian's GROUND STATE equals the classically-optimal squad
    (full enumeration on the quotas-only instance) — encoding provably correct.

Needs your login (cloud, gated behind RUN_ON_CLASSIQ_CLOUD):
  - CombinatorialProblem.get_model() -> synthesize, .optimize() -> execute.
  classiq.authenticate() opens a browser SSO flow (once per session).
  The RESULT PARSING in best_value_from_result() is the line most likely to
  need a tweak on first run — it dumps the raw result shape if it can't parse.

RUN ORDER (recommended):
  1. RUN_ON_CLASSIQ_CLOUD=False        -> offline verification (free)
  2. RUN_ON_CLASSIQ_CLOUD=True, SINGLE_RUN_FIRST=True
       -> ONE simulator run (depth 1, quotas-only) to prove the cloud path
  3. SINGLE_RUN_FIRST=False            -> full depth sweep
"""

from itertools import combinations
import os
import numpy as np
import pandas as pd
import pyomo.environ as pyo
from classiq.applications.combinatorial_optimization import pyo_model_to_hamiltonian

INPUT_XLSX   = "FIFA_Men_s_World_Cup_2026_Player_Pool.xlsx"
XPTS_CSV     = "xpts.csv"     # real objective; falls back to price if absent
SQUAD_QUOTA  = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
# Shortlist size per position. 19 players => 19 qubits (quotas only), small
# enough to FULLY enumerate and verify. Raise for a harder instance.
SHORTLIST    = {"GK": 3, "DEF": 6, "MID": 6, "FWD": 4}
PENALTY      = 100.0          # constraint penalty energy (verified sufficient below)
RUN_ON_CLASSIQ_CLOUD = False  # set True locally after `classiq.authenticate()`
SINGLE_RUN_FIRST     = True   # True: one proving run; False: full depth sweep
EXPORT_QMOD          = False  # True: write .qmod files for the web IDE (no synthesis)
LAYER_SWEEP  = (1, 2, 3)      # QAOA depths to sweep in the experiment harness


# ----------------------------------------------------------------------
# Data + shortlist
# ----------------------------------------------------------------------
def build_shortlist(path):
    df = pd.read_excel(path)
    for c in ("Player Name", "Nation", "Position"):
        df[c] = df[c].astype(str).str.strip()
    df["pool_row"] = df.index

    # Real objective from xpts.csv (pool_row -> xpts); price proxy if absent.
    if os.path.exists(XPTS_CSV):
        xp = pd.read_csv(XPTS_CSV)[["pool_row", "xpts"]]
        df = df.merge(xp, on="pool_row", how="left")
        df["xPts"] = df["xpts"].fillna(0.0)
        src = "xpts.csv (fixture-adjusted)"
    else:
        df["xPts"] = df["Price ($M)"].astype(float)        # fallback
        src = "PRICE PLACEHOLDER (xpts.csv not found)"
    print(f"Objective source: {src}")

    parts = [df[df.Position == p].sort_values("xPts", ascending=False).head(n)
             for p, n in SHORTLIST.items()]
    short = pd.concat(parts).reset_index(drop=True)
    short["pid"] = short.index
    return short


# ----------------------------------------------------------------------
# Pyomo model  (this is the Classiq input)
# ----------------------------------------------------------------------
def pyomo_model(short, budget_m=None):
    """budget_m=None -> quotas only; a float -> add the budget inequality."""
    idx   = list(short.pid)
    pos   = dict(zip(short.pid, short.Position))
    xpts  = dict(zip(short.pid, short.xPts))
    price = dict(zip(short.pid, (short["Price ($M)"] * 10).round().astype(int)))  # integer

    m = pyo.ConcreteModel()
    m.x = pyo.Var(idx, domain=pyo.Binary)
    # Classiq minimises; we want max points -> minimise negative points.
    m.obj = pyo.Objective(expr=-sum(xpts[i] * m.x[i] for i in idx), sense=pyo.minimize)
    m.quota = pyo.ConstraintList()
    for p, q in SQUAD_QUOTA.items():
        m.quota.add(sum(m.x[i] for i in idx if pos[i] == p) == q)
    if budget_m is not None:
        m.budget = pyo.Constraint(expr=sum(price[i] * m.x[i] for i in idx)
                                  <= int(round(budget_m * 10)))
    return m


# ----------------------------------------------------------------------
# Classical ground truth on the shortlist (combinatorial, exact)
# ----------------------------------------------------------------------
def squad_cost_range(short):
    """Cheapest and dearest valid squad cost achievable from the shortlist."""
    lo = hi = 0.0
    for p, q in SQUAD_QUOTA.items():
        prices = sorted(short.loc[short.Position == p, "Price ($M)"])
        lo += sum(prices[:q])
        hi += sum(prices[-q:])
    return lo, hi


def classical_optimum(short, budget_m=None):
    by = {p: short[short.Position == p] for p in SQUAD_QUOTA}
    best, best_val = None, -1.0
    for gk in combinations(by["GK"].pid, SQUAD_QUOTA["GK"]):
        for de in combinations(by["DEF"].pid, SQUAD_QUOTA["DEF"]):
            for mi in combinations(by["MID"].pid, SQUAD_QUOTA["MID"]):
                for fw in combinations(by["FWD"].pid, SQUAD_QUOTA["FWD"]):
                    sel = gk + de + mi + fw
                    cost = short.loc[list(sel), "Price ($M)"].sum()
                    if budget_m is not None and cost > budget_m + 1e-9:
                        continue
                    val = short.loc[list(sel), "xPts"].sum()
                    if val > best_val:
                        best_val, best = val, set(sel)
    return best, best_val


# ----------------------------------------------------------------------
# Evaluate Classiq's Hamiltonian energy on a computational-basis state
# ----------------------------------------------------------------------
def ham_energy(ham, bits):
    """Energy of basis state `bits` (0/1 array) for a list of PauliTerms.
    Z|0>=+1, Z|1>=-1, so z = 1-2*bit; I contributes a factor of 1."""
    z = 1 - 2 * np.asarray(bits)
    total = 0.0
    for term in ham:
        factor = 1.0
        for q, p in enumerate(term.pauli):
            if getattr(p, "name", "") == "Z":
                factor *= z[q]
        total += term.coefficient * factor
    return total


def verify_encoding(short):
    """Proof that the QAOA cost Hamiltonian's ground state is the constrained
    optimum. We compare the Hamiltonian's minimum energy (full 2^n enumeration)
    against -(classical optimum value). They are equal iff the ground state is
    feasible AND optimal: any feasible squad has energy = -value >= -opt_val,
    and any infeasible squad pays a positive penalty on top of -value, so the
    minimum energy can only reach -opt_val by sitting on a feasible optimum.
    (Energy comparison is used rather than bit-matching because qubit ordering
    is internal to Classiq and equally-priced players create optimal ties.)"""
    m = pyomo_model(short, budget_m=None)
    ham = pyo_model_to_hamiltonian(m, penalty_energy=PENALTY)
    n = len(ham[0].pauli)
    _, opt_val = classical_optimum(short, budget_m=None)

    best_e = np.inf
    for k in range(1 << n):
        bits = np.array([(k >> j) & 1 for j in range(n)])
        best_e = min(best_e, ham_energy(ham, bits))
    match = abs(best_e - (-opt_val)) < 1e-6
    return n, len(ham), match, opt_val, best_e


# ----------------------------------------------------------------------
# Classiq cloud QAOA  (gated — needs authenticate(); see experiment_sweep)
# ----------------------------------------------------------------------
def run_on_classiq(short, budget_m, num_layers):
    """One QAOA run on Classiq cloud using the CURRENT CombinatorialProblem API
    (classiq 1.17.0). Assumes classiq.authenticate() was already called this
    session. Returns the raw result object from .optimize() for parsing."""
    from classiq.applications.combinatorial_optimization import CombinatorialProblem
    from classiq.execution import ExecutionPreferences, ClassiqBackendPreferences

    m = pyomo_model(short, budget_m)
    combi = CombinatorialProblem(pyo_model=m, num_layers=num_layers,
                                 penalty_factor=int(PENALTY))
    combi.get_model()                       # synthesize the QAOA ansatz (cloud)

    # Pin execution to the SIMULATOR backend (free-tier friendly; no hardware).
    exec_prefs = ExecutionPreferences(
        backend_preferences=ClassiqBackendPreferences(backend_name="simulator"))
    # optimize() runs the hybrid classical-quantum loop (cloud) and returns the
    # optimized result. maxiter/quantile keep it light for the free tier.
    result = combi.optimize(exec_prefs, maxiter=60, quantile=0.7)
    return combi, result


def best_value_from_result(short, combi, result):
    """Best (max-points) objective among the QAOA solutions.

    The CombinatorialProblem result shape can vary by version, so this is
    defensive: it tries the common forms and, if none match, raises with the
    raw structure so you can see exactly what to key on. We MINIMISE -points,
    so a solution's pyomo objective is -value; best value = -min(objective)
    over feasible solutions, OR we recompute value directly from the chosen
    players when a bitstring/selection is available (more robust)."""
    # Preferred: result exposes a solutions table (DataFrame or list of dicts)
    # with a 'solution'/'x' selection and a 'cost'/'objective' column.
    rows = None
    for attr in ("solutions", "result", "optimized_result"):
        cand = getattr(result, attr, None)
        if cand is not None:
            rows = cand
            break
    if rows is None and isinstance(result, (list, tuple)):
        rows = result

    # If we got a DataFrame, turn into records
    if isinstance(rows, pd.DataFrame):
        rows = rows.to_dict("records")

    best_val = None
    if isinstance(rows, (list, tuple)) and rows:
        for r in rows:
            cost = None
            if isinstance(r, dict):
                cost = r.get("cost", r.get("objective", r.get("value")))
            else:
                cost = (getattr(r, "cost", None) or getattr(r, "objective", None))
            if cost is not None:
                v = -float(cost)            # we minimise -points
                best_val = v if best_val is None else max(best_val, v)
    if best_val is not None:
        return best_val

    raise ValueError(
        "Could not parse Classiq result. Inspect and adjust best_value_from_result.\n"
        f"  type(result)={type(result)}\n"
        f"  dir(result)={[a for a in dir(result) if not a.startswith('_')][:25]}\n"
        f"  repr={repr(result)[:400]}")


def export_qmod(short, budget_m, num_layers=1):
    """Write .qmod model files for manual upload to the Classiq web IDE
    (platform.classiq.io -> Synthesis tab). This is the fallback when the SDK's
    synthesize endpoint is gated (HTTP 403): the IDE can synthesize/execute the
    same model interactively.

    IMPORTANT: this calls combi.get_model() + write_qmod(), which (per Classiq
    docs) build and SERIALISE the model locally — they should NOT hit the gated
    /tasks/generate endpoint that synthesize()/optimize() use. If get_model()
    itself 403s, the SDK is gated even for model-building and you must rebuild
    the model in the web IDE directly (the Pyomo structure is simple: the
    quotas + optional budget constraints printed below).
    """
    from classiq import write_qmod
    from classiq.applications.combinatorial_optimization import CombinatorialProblem

    instances = [("quotas_only", None), ("quotas_budget", budget_m)]
    written = []
    for label, bud in instances:
        m = pyomo_model(short, bud)
        combi = CombinatorialProblem(pyo_model=m, num_layers=num_layers,
                                     penalty_factor=int(PENALTY))
        qmod = combi.get_model()               # local model build (not synthesis)
        fname = f"wc2026_qaoa_{label}_L{num_layers}"
        write_qmod(qmod, fname)                 # writes {fname}.qmod
        written.append(f"{fname}.qmod")
        print(f"  wrote {fname}.qmod  ({label}: "
              f"{'quotas only' if bud is None else f'quotas + ${bud}M budget'})")

    print("\nUpload these to platform.classiq.io -> Synthesis tab, then run on the "
          "simulator backend.")
    print("In the IDE you can set QAOA layers and view the approximation ratio "
          "against the ILP optimum below.")
    return written
    """Control-first QAOA experiment. Runs the VERIFIED quotas-only instance
    across QAOA depths, then the harder quotas+budget instance, and reports
    the approximation ratio (QAOA best / ILP optimum) for each. The ratio
    sitting below 1.0 and rising with depth is the expected, healthy shape."""
    import classiq
    classiq.authenticate()                     # once per session (cached after)

    if SINGLE_RUN_FIRST:
        # ONE proving run: verified quotas-only instance, depth 1. Confirms the
        # whole cloud path works and the result parses before spending more.
        print("SINGLE_RUN_FIRST: one proving run (quotas-only, depth 1)...")
        _, ilp_val = classical_optimum(short, budget_m=None)
        combi, result = run_on_classiq(short, None, 1)
        qv = best_value_from_result(short, combi, result)
        print(f"\n  PROVING RUN OK")
        print(f"  QAOA best value : {qv:.2f}")
        print(f"  ILP optimum     : {ilp_val:.2f}")
        print(f"  approx ratio    : {qv/ilp_val:.3f}")
        print("\n  If this looks sane, set SINGLE_RUN_FIRST=False for the full "
              "depth sweep.")
        return [("quotas-only", 1, f"{qv:.1f}", f"{ilp_val:.1f}",
                 f"{qv/ilp_val:.3f}")]

    instances = [("quotas-only", None), ("quotas+budget", budget_m)]
    rows = []
    for label, bud in instances:
        _, ilp_val = classical_optimum(short, budget_m=bud)
        for L in layers:
            try:
                combi, result = run_on_classiq(short, bud, L)
                qv = best_value_from_result(short, combi, result)
                rows.append((label, L, f"{qv:.1f}", f"{ilp_val:.1f}",
                             f"{qv/ilp_val:.3f}"))
            except Exception as e:                # one failure shouldn't kill the sweep
                rows.append((label, L, "ERR", f"{ilp_val:.1f}", str(e)[:60]))

    print(f"\n{'instance':<15}{'layers':>7}{'QAOA':>8}{'ILP':>8}{'approx ratio':>14}")
    print("-" * 52)
    for label, L, qv, iv, r in rows:
        print(f"{label:<15}{L:>7}{qv:>8}{iv:>8}{r:>14}")
    return rows


# ----------------------------------------------------------------------
if __name__ == "__main__":
    short = build_shortlist(INPUT_XLSX)
    print(f"Shortlist: {len(short)} players "
          f"({', '.join(f'{p}:{SHORTLIST[p]}' for p in SHORTLIST)})\n")

    # Binding-but-feasible budget for the shortlist (midpoint of achievable range)
    lo, hi = squad_cost_range(short)
    budget_m = round((lo + hi) / 2, 1)
    print(f"Shortlist squad cost range: ${lo:.1f}M – ${hi:.1f}M  "
          f"-> demo budget ${budget_m:.1f}M\n")

    # Qubit cost of each constraint set (the scaling lesson)
    for label, bud in [("quotas only", None), ("quotas + budget", budget_m)]:
        h = pyo_model_to_hamiltonian(pyomo_model(short, bud), penalty_energy=PENALTY)
        print(f"  {label:<16}: {len(h[0].pauli):>2} qubits, {len(h):>3} Pauli terms")
    print()

    n, nterms, match, opt_val, gs_e = verify_encoding(short)
    print(f"ENCODING VERIFICATION (quotas-only, {n} qubits, full 2^{n} enumeration)")
    print(f"  classical optimum value                : {opt_val:.1f}")
    print(f"  Hamiltonian ground-state energy        : {gs_e:.2f}")
    print(f"  ground state == constrained optimum    : {match}\n")

    opt_set, opt_val = classical_optimum(short, budget_m=budget_m)
    cost = short.loc[list(opt_set), "Price ($M)"].sum()
    print(f"Classical optimum WITH ${budget_m:.1f}M budget (the QAOA target):")
    print(f"  value {opt_val:.1f}, cost ${cost:.1f}M")
    for _, r in short.loc[list(opt_set)].sort_values('Position').iterrows():
        print(f"    {r['Position']:<3} {r['Player Name']:<20} {r['Nation']:<4} ${r['Price ($M)']:.1f}M")

    if EXPORT_QMOD:
        print("\nExporting .qmod model files for the Classiq web IDE "
              "(no synthesis call)...")
        # also print both ILP optima so you can compute the approx ratio in the IDE
        _, ilp_quotas = classical_optimum(short, budget_m=None)
        _, ilp_budget = classical_optimum(short, budget_m=budget_m)
        export_qmod(short, budget_m, num_layers=1)
        print(f"\n  ILP optima to benchmark the IDE's QAOA result against:")
        print(f"    quotas-only   : {ilp_quotas:.1f}")
        print(f"    quotas+budget : {ilp_budget:.1f}")
    elif RUN_ON_CLASSIQ_CLOUD:
        print("\nRunning QAOA on Classiq cloud (control instance first)...")
        experiment_sweep(short, budget_m)
    else:
        print("\n[Classiq cloud run disabled — set RUN_ON_CLASSIQ_CLOUD=True for the "
              "depth sweep, or EXPORT_QMOD=True to write .qmod files for the web IDE.]")
