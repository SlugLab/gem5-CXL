# Hardware-Calibrated AMU and CIRA Policy Design

## Goal

Calibrate the gem5 AMU and CIRA performance models from immutable external
evidence before publishing the four-thread, all-memory-CXL PageRank latency
sweep. AMU uses the architecture and evaluation data in the AMU paper
(`3663479.pdf`, DOI 10.1145/3663479). CIRA uses the PGO measurements in
`benchmark_gapbs_workloads_ci_long.csv` selected by the user.

Correctness remains a hard boundary. Calibration may change modeled timing,
queueing, asynchronous coverage, and policy selection, but it may not change
the PageRank algorithm, graph, iteration count, thread count, floating-point
operation order, or result bits. No calibrated performance row is publishable
until it matches the corresponding Vanilla float32 vector bit for bit.

The qualification job already running from the pre-calibration simulator is
not modified in place. Its output is diagnostic evidence only after the model
changes; formal calibrated runs use fresh provenance and checkpoints.

## Immutable calibration sources

The calibration manifest records copies or absolute source paths, hashes,
extraction rules, selected observations, and exclusions.

### AMU source

- Source: `/home/victoryang00/gem5-CXL/3663479.pdf`
- SHA-256: `cba178ece7593b3ede868417a031ded3efddd85d5f7c50672b0a93735187790f`
- Publication: ACM TACO 21(3), Article 55, September 2024,
  DOI 10.1145/3663479.

The following paper facts are direct model inputs:

- Table 2: one 3 GHz, 6-wide OoO RISC-V core, 512-entry ROB, 512 physical
  registers, 192-entry LSQ; private 32 KiB 16-way L1I/L1D with 48 MSHRs and
  four-cycle delay; private 256 KiB 8-way L2 with 48 MSHRs and ten-cycle
  delay; DDR4-2400 memory.
- Section 6.1: CXL is represented by gem5's serial-link packet delay and
  bandwidth model; the AMU evaluation is single-core and uses a 64 KiB SPM.
- Sections 3 and 4: `aload`/`astore` separate issue from completion; `getfin`
  returns completed IDs; `queue_length` bounds outstanding requests; SPM
  holds data and request metadata; ALSU/ASMC communication batches 16-bit IDs
  in 512-bit list-vector registers; large operations split into cache-line
  subrequests.
- Section 6.4: each AMU state machine has a 32-entry pending queue; ASMC has
  two list-vector-length buffers; ALSU has two uncommitted-ID registers.
- Table 3: most pointer/random benchmarks use less than 64-byte accesses;
  STREAM, IS, and HPCG use 512 bytes or more. Most RLP workloads launch 256
  coroutines, while skip-list launches 128.
- Evaluation latency points: 0.1, 0.2, 0.5, 1, 2, and 5 microseconds.

The paper's reported speedups and MLP are validation observations, not knobs.
In particular, the 2.42x geometric-mean speedup at 1 microsecond and GUPS's
26.86x speedup with average MLP above 130 at 5 microseconds must never be
assigned directly to `pr_spmv`. The paper has Graph500 BFS but no PageRank or
GAPBS `pr_spmv`, and its published setup is single-core RISC-V rather than the
formal four-core x86 setup in this repository.

Table 4 provides numeric latency trends for GUPS, HJ, and STREAM. These are
used only where the implementation reproduces the paper's matching CPU,
cache, SPM, granularity, and workload profile. Figure-only observations are
recorded with page and figure provenance and are treated as bounded trend
checks, not fabricated point estimates.

### CIRA source

- Source:
  `/root/ia780i_type2_delay_buffer_new/benchmark_gapbs_workloads_ci_long.csv`
- SHA-256: `4e0297da423cee0a742bc2e10656d022bb27776807f2d2ce4cca43e65c634184`
- Rows: ten trials per configuration with mean, standard deviation, and 95%
  confidence interval fields.

The CSV's labels are normalized in generated artifacts:

- `A` becomes `static` (the CSV calls it CIRA without PGO);
- `B` is the measured `_maa_2K` candidate;
- `C` is the measured `_maa_1K` candidate;
- `ABC` becomes `pgo-selected`, meaning the best measured A/B/C candidate.

`ABC` is an offline, post-hoc selector. It is not called a JIT, online
few-shot execution, or a real GPU offload in the gem5 paper results. Any CSV
fallback is retained explicitly. Rows whose `Verification` is not `PASS` are
excluded, including `pr`. The matched `pr_spmv` observation is the primary
PageRank calibration target: static speedup 0.992813, PGO-selected speedup
0.996912, and PGO/static ratio 1.004128673. The other verified workloads are
holdout observations. Their seven-workload geometric means are 0.884214397
for static and 0.892296283 for PGO-selected, a ratio of 1.009140189.

The CSV is accepted as the requested PGO evidence, with its original labels,
fallbacks, confidence intervals, and verification status preserved. The
manifest does not upgrade it into evidence of counter-backed FPGA CIRA
execution.

## Selected calibration approach

Calibration is split into a paper-reproduction profile and the formal matched
comparison profile.

### AMU paper-reproduction profile

This profile reproduces every configuration parameter exposed by the paper
and the repository model. It uses one core, 64 KiB SPM, the Table 2 CPU/cache
limits, the paper latency sweep, and workload-specific access granularity and
software concurrency. It calibrates only implementation costs absent from the
direct paper configuration: AMI issue/decode, ID refill/writeback batching,
SPM metadata access/cache behavior, completion publication, `getfin` polling,
and finite pending queues.

The fitting objective is hierarchical:

1. preserve request counts and attainable outstanding-request bounds;
2. match reported MLP observations where numeric data exist;
3. match normalized latency trends and Table 4 values;
4. use aggregate speedup only as a final external validation.

At least one latency point and one benchmark family are held out. A parameter
set that improves the fit set but violates the held-out direction or produces
an impossible queue/MLP state is rejected. The manifest records the objective,
weights, search space, selected parameters, residuals, and held-out error.

### AMU formal matched profile

The formal profile retains the repository's common x86 CPU hierarchy, four
threads, fixed g12/g14 graph, 20 synchronous double-buffered PageRank
iterations, and identical all-CXL placement across Vanilla, AMU, and CIRA. It
imports only AMU-internal parameters validated by the reproduction profile;
it does not copy a paper speedup or silently replace the common CPU/cache
baseline with Table 2.

AMU admission and useful asynchronous coverage are modeled as:

```text
N_effective = min(N_software_window,
                  queue_length,
                  max_outstanding,
                  floor(SPM_available / bytes_per_request),
                  pending_queue_capacity,
                  downstream_capacity)

T_request = T_aload_issue + T_id_batch_amortized + T_metadata
          + T_queue + T_CXL_and_memory + T_coherent_SPM_writeback
          + T_completion_publish

benefit = covered_demand_stall
        - issue_and_scheduler_cost
        - getfin_wait_and_poll_cost
        - metadata_and_writeback_traffic_cost
```

`load_value()` may wait only when its consumer reaches an incomplete request,
when the finite asynchronous window is full, or at the final drain. A hidden
per-request synchronous wait is a calibration failure. Statistics distinguish
issued operations, useful overlap, consumer waits, window-full stalls,
`getfin` polls, ID-batch refills/writebacks, metadata accesses, queue high-water
marks, and coherent SPM writebacks.

The direct-paper profile uses 64 KiB SPM, workload-specific paper access
granularities, and a 32-entry per-state-machine pending boundary. The formal
`pr_spmv` profile begins with the repository's 8-byte AMU operation and the
paper-calibrated 64 KiB SPM. It also reports a clearly labeled sensitivity
point using the repository's prior 256 KiB SPM so that the source of any
improvement is visible rather than folded into the calibrated result.

### CIRA modes and analytical hoist gate

The gem5 study exposes three distinct CIRA modes:

1. `static`: the compiler fixes a legal row block and lead distance before
   execution.
2. `pgo-selected`: an offline profile selects among the frozen A/2K/1K policy
   family. Selection and provenance are fixed before the measured run.
3. `few-shot-online`: warmup executes candidate policies, charges their
   profiling and reconfiguration cost, selects once, and freezes before the
   measured steady state. It is not allowed to use future measured samples.

A candidate hoist is legal only when address and guard operands dominate the
hoist point, alias and invalidation analysis preserves coherent visibility,
and the prefetched object's lifetime covers issue through consumption. A
legal candidate is profitable only when:

```text
available_slack >= issue + index_walk + queue_wait
                 + CXL_and_memory + cache_install

expected_saved_stall > descriptor_formation + runtime_guards
                     + selection_cost + extra_traffic
                     + cache_pollution + late_request_cost
```

Capacity constraints include descriptor queues, CSR index-walk queues,
outstanding reads, per-core destination ports, cache MSHRs, and the allowed
lead/window. A failed condition leaves the demand synchronous; it never emits
an unsafe or known-unprofitable prefetch.

The PGO calibration matches relative policy behavior, not absolute speedup.
For `pr_spmv`, PGO-selected should improve over static by approximately
1.00413x while remaining statistically consistent with the broad source
confidence intervals. The remaining verified workloads test whether the
selector reproduces the source's direction and approximate geometric-mean
ratio. The model is rejected if it obtains the target by adding uncharged
oracle knowledge or runtime work.

## Provenance and generated artifacts

One machine-readable calibration manifest contains:

- source paths, SHA-256 hashes, publication/table/figure references, and CSV
  row identities;
- direct inputs versus fit observations versus held-out observations;
- simulator, configuration, workload, graph, and binary hashes;
- parameter search space, objective, selected point, residuals, and rejection
  reasons;
- AMU reproduction and formal-profile parameters;
- CIRA static, PGO-selected, and few-shot policy definitions and charged
  selection costs;
- explicit source limitations and excluded rows.

Generated CSV and JSON files carry the manifest hash. A formal result whose
simulator or policy hash differs is stale and fails closed. Existing g12/g14
checkpoints made with the old timing model cannot be used for a calibrated
ROI unless their checkpoint boundary precedes all affected model state and
the manifest validator explicitly proves compatibility; otherwise fresh
checkpoints are required.

## Validation and publication gates

Implementation follows test-driven development and passes these gates:

1. Unit tests parse both sources, validate their exact SHA-256 values, reject
   failed CIRA verification rows, preserve fallbacks and confidence intervals,
   and distinguish direct inputs from validation observations.
2. AMU source tests prove finite SPM/pending/ID capacity, charged metadata and
   completion paths, consumer-only waits, and no per-request synchronous
   `load_value()` drain.
3. CIRA tests prove the analytical legality/profitability gate, offline PGO
   selection, charged causal few-shot selection, and freeze-before-measure.
4. The incremental gem5 build and existing AMU, CIRA, M2NDP, checkpoint,
   qualification, and publisher tests pass.
5. The AMU paper-reproduction profile reports fit and held-out residuals and
   passes the configured tolerance without violating request/MLP bounds.
6. Static and PGO-selected CIRA reproduce the selected CSV comparison within
   a predeclared tolerance; the `pr` FAIL rows never enter fitting or plots.
7. At every formal latency, Vanilla, calibrated AMU, and each calibrated CIRA
   mode use the same four-core all-CXL configuration and raw graph hash.
8. Every accelerated output matches Vanilla element for element and bit for
   bit. AMU issued/completed counts balance with no rejection; all four CIRA
   ports are active with no drops or queue rejection.
9. M2NDP retains its independent FuncSim bit-exact and NDPSim calibration
   gates. It is not retuned from AMU or CIRA targets.
10. Only validated rows enter the canonical CSV, LaTeX table, and paper
    figure. Labels distinguish AMU paper-calibrated, CIRA static,
    CIRA PGO-selected, CIRA few-shot-online, and M2NDP.

## Alternatives rejected

Directly scaling `pr_spmv` to the AMU paper's 2.42x average was rejected
because the paper does not evaluate PageRank and uses a different ISA, core
count, CPU, cache, and dataset. Treating CIRA `ABC` as a zero-cost JIT was
rejected because it is a post-hoc best-of-three row. Changing the common
four-core baseline to the AMU paper's single-core RISC-V system was rejected
because it would destroy the end-to-end comparability with CIRA and M2NDP.
Leaving the current idealized 1 ns/0 ns AMU control path and 256 KiB SPM
unqualified was rejected because it omits paper-described resource and
scheduling costs while granting four times the evaluated SPM capacity.

## Acceptance criteria

The design is complete when one hash-bound calibration manifest can reproduce
the accepted AMU paper profile and CIRA PGO/static relationship with declared
fit and holdout errors; the four-thread all-CXL g12 qualification then passes
for Vanilla, calibrated AMU, and calibrated CIRA with bit-exact outputs and
balanced activity; the frozen parameters carry unchanged into g14 and the
200 ns, 500 ns, 1 microsecond, and 2 microsecond sweep; and only those verified
end-to-end latencies are installed in the paper and pushed to the appropriate
branches.
