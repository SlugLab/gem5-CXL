# G12 24-Cell Host and CIRA Timing Design

Date: 2026-09-06

## Goal

Produce a fresh, auditable G12 timing bundle for the same six workloads and
four calibrated CXL latencies as the G14 campaign. Each of the 24 cells must
contain an independently measured host-inline offload-region time and CIRA
device-side runtime. The G12 campaign is a separate evidence identity and must
not consume interrupted G14 attempts as results.

The matrix remains:

- workloads: `pr_spmv`, `gap_bc`, `mcf`, `amg_gather`,
  `lulesh_scatter`, and `npb_cg`;
- CXL latency labels: `200ns`, `500ns`, `1us`, and `2us`;
- execution stages: `host_inline` and `cira_runtime`; and
- four timing CPU cores with all workload memory placed on CXL.

This design intentionally chooses a separate G12 preparation and execution
pipeline. Existing G14 scripts, registries, state, and evidence remain
unchanged.

## Frozen G12 Graph and CSR Identity

PageRank and GAP BC use the same frozen G12 graph:

- graph: `/mnt/disk0/gem5-CXL-g14-eval/graphs/g12.sg`;
- graph SHA-256:
  `759003842b672ad90eabbd5b045980e9ddf43a95bffb01b318db7fc4b8b551f1`;
- manifest: `/mnt/disk0/gem5-CXL-g14-eval/graphs/g12.manifest.json`;
- manifest SHA-256:
  `8abefe654015fe287cb5507e06111abc3b9774d4a690b98373b06b2a4d649217`;
- scale: 12;
- vertices: 4,096; and
- directed edges: 96,772.

The selected CSR is:

`/mnt/disk0/gem5-CXL-eval/pr-scaling-be84a6c362-g12-qualification-v2/scales/g12/m2ndp/csr`

Its files are frozen by the following SHA-256 values:

- `graph.meta.json`:
  `93e3321700687387a329ce47ab45a3a9b4d5c8b8ad331d8025ff75628c94ce13`;
- `in_neighbors.i32`:
  `37466ffb237876aaaf73d43a35b231f4490c77b318b2949cb2bf9b2b85925845`;
- `in_offsets.u64`:
  `fec988ecaa3887e5e8a74e579d0fc13ab226ac219c8689d0c2f3661e922d6bb6`;
- `out_degree.u32`:
  `26c8e7ea51bd71631e07b10cbc87df9850b01ff0e81a54989a46a09462a7b484`.

Preparation fails closed unless the graph manifest, graph bytes, CSR metadata,
array sizes, and all hashes agree on scale 12, 4,096 vertices, and 96,772
directed edges.

## Separate G12 Programs

Three G12-specific entry points are added:

- `scripts/prepare_g12_24cell_registry.py`; and
- `scripts/run_g12_24cell_timing_evidence.py`; and
- `scripts/publish_g12_24cell_timing_evidence.py`.

They may reuse stable trace, timing, hashing, and evidence helper modules, but
must not call a G14 entry point or accept a G14 registry. Their user-facing
messages, validation errors, state identity, and output names say G12.

The existing files below are not behaviorally modified:

- `scripts/prepare_g14_24cell_registry.py`; and
- `scripts/run_24cell_timing_evidence.py`; and
- `scripts/publish_24cell_timing_evidence.py`.

This deliberate duplication prevents a G12 speed experiment from changing the
accepted G14 evidence contract. Any common helper reused by both paths must
retain its current interface and behavior.

The G12 publisher imports only the G12 runner identity and validation helpers.
Its README and manifest label the source as G12, and it rejects a G14 campaign
before copying any evidence into a publication root.

## Registry Preparation

The G12 preparation program regenerates the two graph-derived inputs:

- a 20-iteration PageRank SpMV lazy trace from the frozen G12 CSR; and
- a GAP BC lazy trace rooted at source vertex 0 from the same CSR.

Both source records carry the G12 graph SHA-256 and their independently derived
input SHA-256. MCF, AMG Gather, LULESH Scatter, and NPB CG reuse their current
accepted immutable input and trace records. Reuse does not permit reuse of any
old timing result: host-inline and CIRA are rerun at every latency.

The current broad shared input catalog contains the frozen G12 graph but its
`workloads.pr_spmv` selection still names G20. The G12 preparer therefore also
writes a canonical `inputs.json` inside the fresh registry root. It contains
exactly the six campaign workloads, selects the frozen G12 graph for PageRank
and BC, and copies the four accepted non-graph input records with their source
hashes. The G12 runner validates this manifest against the registry instead of
merely hashing the broad catalog.

The registry contains exactly 24 cells. It has schema 1, status `verified`, and
a graph record with scale 12. Validation rejects scale 4, 14, or 20; a changed
graph or CSR; a graph/trace mismatch; any missing workload or latency; and any
invalid fixed-trace binding.

Registry creation is fresh-root and atomic. An existing output root is never
overwritten.

## Timing and Correctness Contract

The G12 runner preserves the accepted G14 measurement semantics:

- `cira-inline` executes the offloadable region on the host without issuing an
  offload request;
- CIRA device time is the global span from the first accepted issue to the last
  completion;
- global and per-core issue/completion counts must balance;
- all dynamic per-core spans must be valid and reset-outstanding must be zero;
- PageRank descriptor compute/stall metrics are exported separately; and
- non-PageRank workloads mark PageRank descriptor metrics not applicable and
  keep them zero.

Every gem5 command uses four timing cores, all-CXL memory, the latency-specific
calibration, the same warmup and measured-window rules, and the required m5
verification exit. Missing verification, incomplete spans, partial fixed
control state, or an identity mismatch fails the stage.

No speedup value is used as a correctness or acceptance gate. Speedup is
computed only after the host and CIRA evidence for a cell both pass.

## Campaign Identity and Resume

The G12 state records the repository commit and hashes of the G12 runner and
preparer, input manifest, prepared registry, replay binary, gem5 binary, m5
library, gem5 configuration, and all four calibration records. A G14 prepared
registry cannot have the same identity and is rejected before execution.

Each stage writes to a monotonically numbered fresh attempt directory. Evidence
is committed atomically only after its validation passes. Resume skips a
complete stage only after revalidating its evidence and all referenced hashes.
An interrupted `running` attempt is retained for audit and rerun in the next
attempt directory.

There is no live gem5 checkpoint. Recovery occurs at the host-inline or CIRA
stage boundary.

## Qualification and Full Execution

Execution proceeds in two gates:

1. prepare and independently validate the fresh G12 registry;
2. run `pr_spmv:200ns` and `gap_bc:200ns`, both host-inline and CIRA;
3. require all four stages to pass the full timing and correctness contract;
4. start a new full G12 root containing all 24 cells and both stages; and
5. publish final CSV/data only after all 48 stages pass.

The full runner uses a bounded two-worker queue. It runs as a persistent systemd
service outside a login-session scope. The service bootstrap checks only for
the campaign `state.json`: when it is absent, it invokes a fresh run; when it is
present, it invokes the same G12 runner with `--resume`. Consequently an
abnormal exit or reboot resumes the existing identity without making a second
fresh root. Completed stage evidence is retained; an interrupted active stage
restarts from its beginning.

The service does not mutate repository files or reuse qualification timing as a
formal cell.

## Output Contract

G12 roots contain `g12` in their names and never overwrite G14 roots. The final
CSV contains exactly 24 rows and includes:

- workload, latency, graph scale, and input SHA-256;
- host cumulative ticks, nanoseconds, and entry count;
- CIRA global and per-core busy ticks and nanoseconds;
- exact host-ticks divided by CIRA-busy-ticks speedup after both stages pass;
- global and per-core issued/completed counts;
- PageRank descriptor compute/stall aggregate and per-core fields;
- max-over-cores PageRank compute-plus-stall ticks;
- calibration and campaign identity; and
- evidence paths and SHA-256 values.

Progress output labels missing cells as pending or failed. It never fills a
missing G12 value with a G14 result.

## Validation

Tests cover:

- exact frozen G12 graph and CSR identity;
- rejection of G4, G14, G20, and mismatched graph identities;
- exactly six workloads, four latencies, and two stages;
- graph-derived PageRank and BC trace binding;
- accepted reuse of the four non-graph input records;
- independent host/CIRA stage completion and resume;
- fresh attempt creation after an interrupted stage;
- host zero-offload and entry-count enforcement;
- CIRA global/per-core span and issue/completion consistency;
- PageRank descriptor and generic-workload metric rules;
- exact tick-to-time conversion; and
- exactly 24 complete final rows with valid evidence hashes.

The final handoff includes the registry, state, raw per-attempt evidence,
progress CSV, final CSV, service status/log, and their hashes. Paper figures are
not updated until this G12 bundle passes independent validation.
