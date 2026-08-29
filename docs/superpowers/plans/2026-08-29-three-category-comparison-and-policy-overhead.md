# Three-Category Comparison and Policy-Overhead Implementation Plan

**Goal:** Publish matched CIRA, AMU, and M2NDP latency spectra for the fixed
six-workload matrix and a measured CIRA PGO-versus-Few-shot overhead breakdown.

**Design:** Follow
`docs/superpowers/specs/2026-08-29-three-category-comparison-and-policy-overhead-design.md`.
Extend the canonical workload trace pipeline for GAP BC, reuse the accepted
Spatter/MCF paths, finish the indexed NPB CG path, and make one fail-closed
aggregate publisher the only input to the paper generator.

## Task 1: Freeze the revised aggregate contract

- Add failing tests for the exact six-workload order, four latencies, systems,
  matched-baseline rule, four threads, all-CXL placement, and unique identities.
- Implement the schema and validation changes that replace NPB MG with GAP BC
  in the primary 2-by-3 matrix while keeping MG as optional diagnostic data.
- Verify old or mixed six-workload manifests are rejected.

## Task 2: Implement canonical GAP BC trace generation

- Add small-graph reference fixtures that expose discovery, dependency, and
  reverse-propagation phases.
- Add failing tests for dynamic operation order, barriers, output boundaries,
  source/range partition safety, and deterministic hashes.
- Implement the BC trace generator and bounded-window interface.
- Verify the trace against native GAP BC for the fixtures before scaling.

## Task 3: Implement and qualify M2NDP BC

- Add failing lowering/package tests for all BC opcodes and phase launches.
- Lower BC through the existing canonical M2NDP ISA without reassociation.
- Add FuncSim output-boundary and NDPSim launch/memory-match gates.
- Run a small functional qualification, then freeze the g20 package identity.

## Task 4: Finish formal NPB CG-D preparation

- Generate a fresh Class D descriptor containing exact matrix cardinality and
  seed metadata; reject the legacy descriptor that lacks `nonzeros`.
- Build indexed timing windows using only proven CG cut points.
- Run native-reference, Vanilla, AMU, CIRA, and FuncSim qualification.
- Freeze input, trace, binary, window, and output hashes.

## Task 5: Normalize Spatter and MCF evidence

- Revalidate accepted AMG Gather, LULESH Scatter, and MCF identities against the
  revised aggregate schema.
- Reuse read-only formal artifacts only when every bound hash matches.
- Create a new campaign root for any changed runner, trace, or simulator.

## Task 6: Run the four-latency campaign

- Launch 200 ns, 500 ns, 1 us, and 2 us campaigns with four host timing cores,
  four workers, all-CXL placement, and no timeout limit.
- Resume only from identity-matched semantic checkpoints.
- Validate correctness, mechanism, placement, timing, and confidence gates for
  every coordinate before creating the aggregate complete manifest.

## Task 7: Build the PGO/Few-shot overhead dataset

- Add tests that require the six additive phases, exact phase sum, matched
  identity, and true zeros for offline-selected PGO runtime phases.
- Extract PR g12/g14/g20 1-us PGO and Few-shot ledgers from formal runs.
- Record offline PGO collection/build duration when an authoritative log exists;
  otherwise emit `not-recorded` and suppress break-even calculations.
- Generate canonical overhead CSV/JSON and a paired stacked-bar figure.

## Task 8: Publish figures, raw data, and paper

- Extend the publisher tests for the revised 2-by-3 order, grouped 1-us chart,
  per-workload plots, overhead figure, and exact output set.
- Generate PDF, SVG, PNG, CSV, JSON, and LaTeX fragments atomically.
- Update `WIP_jf_asplos.tex`, replace stale claims, build the paper, and inspect
  logs/layout for missing figures, references, and new LaTeX errors.
- Commit and push code and paper changes separately with evidence manifests.
