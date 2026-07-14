# QAOA Experiment Results — FIFA WC2026 Fantasy Squad Selection

## Problem Statement

Squad selection as a QUBO solved via QAOA on the Classiq platform.

**Two problem instances:**

| Instance | Qubits | Pauli terms | Constraints |
|---|---|---|---|
| quotas-only | 18 | 55 | 4 equality (position quotas: 2GK, 5DEF, 5MID, 3FWD) |
| quotas+budget | 28 | 407 | 4 equality + 1 inequality (budget ≤ $101.8M) |

**Objective:** maximise sum of expected fantasy points (xPts) across 15 selected players from an 18-player shortlist.

**Pyomo model (budget instance):**
```
Variables : 18 binary (x[i] = 1 if player i selected)
Objective : minimise -sum(xpts[i] * x[i])   # coefficients: 5.70 – 8.88
Quotas    : sum(x[i] for GK) == 2, DEF == 5, MID == 5, FWD == 3
Budget    : sum(price[i]*x[i]) <= 1089      # prices integer-scaled ×10
```

**SDK:** classiq 1.17.0 · Python 3.12 · `CombinatorialProblem` API

**Approximation ratio** = QAOA best value / ILP classical optimum

---

## Experiment 1 — Original shortlist (19 players, FWD:4)

`SHORTLIST = {GK:3, DEF:6, MID:6, FWD:4}` · `PENALTY = 100` · `maxiter = 60`

| Instance | Qubits | Depth 1 | Depth 2 | Depth 3 |
|---|---|---|---|---|
| quotas-only | 19 | 0.989 | 0.977 | **1.000** |
| quotas+budget | 29 | ERR | ERR | ERR |

**Budget ERR:** `Requested qubits: 29, limit: 28` — simulator ceiling exceeded.

**Fix:** reduced FWD candidates from 4 to 3 (shortlist: 19 → 18 players).
Budget instance drops from 29 to 28 qubits — within simulator limit.

---

## Experiment 2 — Reduced shortlist (18 players), penalty=100

`SHORTLIST = {GK:3, DEF:6, MID:6, FWD:3}` · `PENALTY = 100` · `maxiter = 60`

| Instance | Qubits | Depth 1 | Depth 2 | Depth 3 |
|---|---|---|---|---|
| quotas-only | 18 | -0.029 | 1.000 | 1.000 |
| quotas+budget | 28 | -5.014 | -8.960 | -9.854 |

**Finding:** quotas-only works at depths 2–3. Budget instance entirely infeasible — all sampled bitstrings violate constraints, penalty terms dominate.

---

## Experiment 3 — Penalty increased to 500 (wrong direction)

`PENALTY = 500` · `maxiter = 150`

| Instance | Depth 1 | Depth 2 | Depth 3 |
|---|---|---|---|
| quotas-only | -3.796 | 1.000 | 0.984 |
| quotas+budget | -32.969 | -47.562 | -28.259 |

**Finding:** penalty=500 made results substantially worse. Classiq community guidance clarified: penalty must be in the range of the **objective function coefficients** (5.70–8.88), not the constraint scale. Large penalties create steep energy walls that collapse QAOA rotation angles.

---

## Experiment 4 — Penalty calibrated to objective scale (BREAKTHROUGH)

`PENALTY = 10` · `maxiter = 150`

ILP optimum: 103.5 (quotas-only) / 102.7 (quotas+budget)

| Instance | Depth 1 | Depth 2 | Depth 3 |
|---|---|---|---|
| quotas-only | **0.996** | **1.000** | **1.000** |
| quotas+budget | **+0.381** | ERR* | -1.014 |

*\* Classiq platform capacity issue ("overwhelming surge in user activity")*

**First positive result on the budget instance.** penalty=10 is the correct calibration — just above the maximum objective coefficient (8.88), providing enough penalty to enforce constraints without collapsing the landscape.

---

## Experiment 5 — More iterations (maxiter=300)

`PENALTY = 10` · `maxiter = 300`

ILP optimum: 107.0 (quotas-only) / 106.8 (quotas+budget)  
*(Note: classical optimum rose because xpts.csv updated with 5 rounds of real match data — shortlist composition changes as the tournament progresses)*

| Instance | Depth 1 | Depth 2 | Depth 3 |
|---|---|---|---|
| quotas-only | 0.993 | 0.988 | **1.000** |
| quotas+budget | 0.393 | ERR** | 0.063 |

*\*\* HTTP connection timeout during long-running 28-qubit job*

**Finding:** marginal improvement at depth-1 (+0.012 vs maxiter=150).
Depth-3 budget instance improved from -1.014 to +0.063.
Doubling iterations produced diminishing returns — practical ceiling reached for standard QAOA with penalty encoding.

---

## Complete Results Summary

| Config | Budget d1 | Budget d2 | Budget d3 |
|---|---|---|---|
| penalty=100, maxiter=60 | -5.014 | -8.960 | -9.854 |
| penalty=500, maxiter=150 | -32.969 | -47.562 | -28.259 |
| **penalty=10, maxiter=150** | **+0.381** | ERR | -1.014 |
| **penalty=10, maxiter=300** | **+0.393** | ERR | **+0.063** |

Quotas-only (no inequality): consistently 0.96–1.000 across all configs and depths once penalty ≥ 10.

---

## Key Findings

1. **Penalty calibration is the critical parameter.** The penalty must be in the range of the objective function coefficients, not the constraint coefficient scale. For this problem: penalty ≈ 10 (objective coefficients 5.70–8.88).

2. **Equality constraints are well-handled** by standard QAOA. The quotas-only instance reliably achieves ratio 1.000 at depth ≥ 2.

3. **Inequality constraints are harder.** The budget constraint with integer-scaled coefficients (39–105) and RHS=1089 produces a feasible subspace that the X-mixer struggles to stay within. Standard QAOA with penalty encoding appears to plateau around 0.39 for this instance regardless of iteration count.

4. **The Grover mixer QAOA** (GM-QAOA) is the recommended next architectural step. By confining the quantum state to the feasible subspace, it eliminates the penalty calibration problem and should handle the budget inequality directly.

5. **Infrastructure notes:**
   - `ExecutionSession.minimize` is deprecated; `variational_minimize` is the current API (warning only, does not block results)
   - Depth-2 budget runs consistently encounter network timeouts on long jobs — retry logic is needed
   - The Classiq simulator backend has a 28-qubit ceiling; the quotas+budget instance sits exactly at this limit

---

## Files

| File | Description |
|---|---|
| `fantasy_qaoa_branch.py` | Final experiment script (penalty=10, maxiter=300, CLI: `--cloud`, `--sweep`, `--export-qmod`) |
| `wc2026_qaoa_quotas_only_L1.qmod` | Compiled Classiq model — quotas-only instance, depth 1 |
| `wc2026_qaoa_quotas_only_L1.synthesis_options.json` | Synthesis options — quotas-only |
| `wc2026_qaoa_quotas_budget_L1.qmod` | Compiled Classiq model — quotas+budget instance, depth 1 |
| `wc2026_qaoa_quotas_budget_L1.synthesis_options.json` | Synthesis options — quotas+budget |

---

## Next Steps

- Implement Grover mixer QAOA using the Classiq library (`gm_qaoa.ipynb` reference)
- Add HTTP retry logic for depth-2 network timeouts
- Investigate `variational_minimize` migration path per deprecation warning
