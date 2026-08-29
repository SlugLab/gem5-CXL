# Relaxed Six-Workload CXL Latency Spectrum Design

Date: 2026-08-29

## Decision

This document amends the correctness gate in
`2026-08-23-six-workload-cxl-latency-spectrum-paper-design.md`. All other
identity, timing, plotting, raw-data, and paper contracts remain in force.

The publication still requires 96 real timing coordinates:

- four modeled CXL link latencies: 200 ns, 500 ns, 1 us, and 2 us;
- six workloads: PageRank g20, MCF, AMG Gather, LULESH Scatter, NPB CG, and
  NPB MG; and
- four systems: matched Vanilla CXL, AMU, CIRA, and M2NDP.

No coordinate may be copied, interpolated, analytically scaled, or inferred
from another latency, workload, or system.

## Relaxed correctness gate

The previous requirement that every accelerated output be byte-for-byte or
elementwise bit-exact is removed. A coordinate is timing-eligible when all of
the following hold:

1. the simulator or emulator exits successfully and reports no fatal error;
2. the workload completes its full declared ROI and native iteration count;
3. backend requests, completions, launches, barriers, and drains balance;
4. the workload-native verifier passes;
5. integer, index, shape, and cardinality invariants remain exact;
6. floating-point outputs are finite and pass the existing workload-specific
   numerical tolerance when one exists; and
7. the timing evidence binds the expected input, binary, configuration,
   latency, four-core/four-worker execution, and all-CXL placement hashes.

An available raw bit-exact result remains recorded as stronger evidence, but
its absence does not reject a point. A numerical mismatch, missing native
verification, incomplete work, or unbalanced mechanism activity still rejects
the coordinate.

## Timing and statistical gate

The relaxed correctness policy does not relax performance measurement. Each
coordinate executes its actual backend at its declared latency. Fixed setup,
synchronization, completion, and final-commit costs remain inside the reported
end-to-end interval. Paired timing windows retain the existing 95% confidence
interval and uncertainty checks. Missing or inconclusive coordinates block the
formal 96-row publication package.

## Figure and raw-data contract

The primary paper artifact is a 2-by-3 small-multiple line figure. Panels are
ordered PageRank g20, MCF, AMG Gather, LULESH Scatter, NPB CG, and NPB MG.
Every panel uses the same ordered x-axis (200 ns, 500 ns, 1 us, 2 us), the same
shared speedup axis, a visible 1.0x matched-Vanilla reference, and fixed AMU,
CIRA, and M2NDP colors, markers, and line styles. Paired 95% confidence
intervals are shown at every point.

The figure is generated from a canonical 96-row CSV and normalized JSON. The
publication manifest records the source evidence and output hashes. The paper
caption states that points passed workload-native correctness but were not all
required to be bit-exact.

## Paper integration

After the 96-row manifest passes, add the composite PDF/SVG/PNG, raw CSV/JSON,
and manifest to the paper repository. Replace the preliminary one-latency-only
discussion in `WIP_jf_asplos.tex` with the four-latency spectrum and exact
evidence wording. Preserve unrelated Overleaf edits and untracked PDFs.

## Acceptance

The work is complete when:

1. tests prove the relaxed gate accepts native-correct non-bit-exact floating
   output and rejects native-verifier failure or malformed work;
2. all 96 real timing coordinates pass the relaxed correctness, identity, and
   uncertainty gates;
3. an independent validator recomputes speedups and confidence intervals from
   raw evidence;
4. the 2-by-3 figure and raw-data package are hash-bound and visually checked;
5. `WIP_jf_asplos.tex` compiles through the modified evaluation section, with
   any unrelated pre-existing LaTeX failure reported separately; and
6. the code and paper repositories are pushed without staging unrelated user
   files.
