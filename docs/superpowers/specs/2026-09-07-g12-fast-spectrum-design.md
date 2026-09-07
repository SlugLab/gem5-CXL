# G12 Fast Spectrum Design

Date: 2026-09-07

## Goal

Replace the impractical 48-stage G12 TimingCPU replay with a bounded pipeline
that measures the two graph workloads in M2NDP and publishes any host/CIRA
latency extrapolation as derived evidence, never as a measured cell.

## Frozen graph identity

PageRank SpMV and GAP BC use only:

- graph `/mnt/disk0/gem5-CXL-g14-eval/graphs/g12.sg`;
- graph SHA-256 `759003842b672ad90eabbd5b045980e9ddf43a95bffb01b318db7fc4b8b551f1`;
- scale 12, 4,096 vertices, and 96,772 directed edges; and
- the accepted G12 registry at
  `/mnt/disk0/gem5-CXL-eval/g12-timing-24cell-inputs-20260906/registry.json`.

Every PageRank or BC result must repeat the graph scale and SHA. A G20 path,
graph SHA, trace SHA, or evidence file is rejected before publication.

## M2NDP measurement

PageRank resumes the existing G12 experiment only through reference packing,
native trace generation, FuncSim, calibration, and NDPSim. Its already-passed
G12 build, graph export, and gem5 baseline stages are hash-revalidated rather
than rerun. GAP BC lowers the accepted G12 lazy trace through the existing
workload-specific expander. Both workloads must pass FuncSim boundary checks
before any NDPSim number is accepted.

Each workload runs at 200 ns, 500 ns, 1 us, and 2 us with the corresponding
calibrated M2NDP configuration. Evidence records cycles, the 0.5 ns core
period, kernel milliseconds, calibration residual, input/trace/package hashes,
launch counts, and memory-match status. Runs are sequential to bound memory.

## Host and CIRA derived spectrum

The old `gem5-g12-24cell-20260906` service remains disabled. It is not resumed
because one PageRank host-inline cell expanded to 6,528,960 scalar records and
failed after repeated hour-scale attempts.

Host/CIRA rows may use only a previously accepted G12 measured anchor with a
matching workload/input identity. Other latency points are generated only when
a declared calibration model has all required coefficients. Such rows use
`measurement_kind=calibrated-derived`, name the anchor evidence and model
record, and carry no claim of independent gem5 measurement. Missing anchors or
coefficients remain `pending`; G20 sensitivity curves are never silently
substituted for G12 measurements.

## Raw-data publication

The raw CSV has one row per workload, latency, and system. It includes
`measurement_kind`, `graph_scale`, `graph_sha256`, status, time, input/trace
identity, evidence path/hash, anchor path/hash, and model path/hash. Accepted
values are either `measured` or `calibrated-derived`; aliases and unlabeled
projections are invalid.

The existing G20 partial directory and stopped G12 gem5 attempts are retained
for audit but excluded from the new publication root.

## Recovery and completion

A persistent systemd service runs one M2NDP cell at a time. Completed evidence
is revalidated and skipped. Interrupted per-latency directories are moved to a
timestamped `failed-attempts` directory before a fresh retry. The service has
no start timeout and refreshes the raw CSV after every accepted cell.

Completion requires eight measured G12 graph cells, four PageRank and four BC,
with distinct workload identities and passing functional, calibration, launch,
and memory gates. Host/CIRA coverage is reported separately and is not allowed
to block or contaminate M2NDP completion.
