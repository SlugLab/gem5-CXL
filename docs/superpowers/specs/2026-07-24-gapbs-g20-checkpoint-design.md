# GAPBS Scale-20 Checkpointed CXL Measurement Design

## Goal

Make the scale-20 GAPBS experiment finish without changing the measured
architecture: gem5 must load the serialized `g20.sg` graph, restore all
physical memory behind the CXL `SerialLink`, use the requested link latency
(initially 1 us), execute the measured ROI on the Timing CPU, and accept a row
only after GAPBS bit-exact verification passes.

The previous direct run used an Atomic CPU while the complete address space
was already behind the 1 us link. It ran for nearly two hours without reaching
the first work-begin event and was then lost in a host reboot. KVM is not a
usable fast-forward replacement on this host: both GAPBS and gem5's own x86
hello binary terminate at startup with Linux `KVM_EXIT_SHUTDOWN` (exit reason
8). The experiment therefore needs a gem5-native setup accelerator.

## Selected Architecture

Use one reusable SE-mode checkpoint per exact binary, graph, arguments, core
count, and setup configuration:

1. **Checkpoint creation:** run the exact benchmark binary with an Atomic CPU,
   no cache, and local simple memory. Load the serialized graph with
   `-f <absolute-g20.sg> -n 2 -v`. At trial 0's work-begin event, before the
   OpenMP kernel creates or wakes worker threads, save a checkpoint and
   terminate successfully without executing trial 0.
2. **Measured restore:** start a separate gem5 process with a Timing CPU,
   private L1/L2 caches, and the complete physical address range reachable
   only through `board.cxl_mem_link0`, a CXL `SerialLink`. Restore the
   checkpoint, execute trial 0 as an unmeasured two-core cache warmup, reset
   statistics at trial 1 work-begin, execute trial 1, dump exactly one ROI
   statistics section at work-end, then continue only far enough to obtain
   GAPBS verification.

The serialized graph is generated at scale 20 and its manifest records that
provenance. The measured guest command loads that graph through `-f`; it does
not regenerate a graph with `-g` inside gem5. The runner records
`graph_scale=20`, graph size, SHA-256, and absolute path independently of the
guest argument spelling.

gem5 checkpoints contain architectural and physical-memory state but do not
preserve cache contents. The restore therefore places the checkpoint's graph
and kernel arrays in the measured board's CXL-backed memory while beginning
with cold L1/L2 caches. Checkpoint creation time and local-memory traffic are
not part of the measured ROI.

## Experiment Contract

- Graph: the existing deterministic `g20.sg`, with 1,048,576 vertices,
  31,399,382 directed entries, and SHA-256
  `ce900a7147a073835a7450e8f1afedf9f13db6833652bf2f9647819be26bedb3`.
- Guest arguments: `-f <absolute-g20.sg> -n 2 -v`.
- Threads and cores: the configured value must match checkpoint creation and
  restore. The first resumed run uses two cores and `OMP_NUM_THREADS=2`.
- Measured CPU: Timing.
- Measured cache hierarchy: the existing private L1/private L2 hierarchy.
- Measured memory placement: one physical memory range, entirely behind
  `board.cxl_mem_link0`; no host-memory range or direct controller path.
- Initial measured latency: 1 us. The later table may reuse a compatible
  checkpoint at 200 ns, 500 ns, and 2 us because cache and link state are not
  checkpointed; each restore must still pass the full configuration audit.
- ROI: trial 1 only. Restored trial 0 warms the measured CXL/cache system.
  Statistics reset at trial 1 work-begin before its first kernel instruction,
  then dump at trial 1 work-end.
- Correctness: verification must be present and equal to `PASS`. Missing or
  failed verification invalidates the row.
- Two-core restored SE processes do not reliably complete OpenMP/libc thread
  teardown. The three ROI-instrumented binary builders therefore emit
  `m5_exit(0)` only after the complete trial loop and all requested
  verifications finish. A failed verifier still emits `m5_fail(0, 1)` first.
  The config treats only that post-verification `m5_exit` as success; it never
  infers success from buffered guest stdout.
- Performance: speedup is matched baseline trial-1 `simTicks` divided by the
  candidate's trial-1 `simTicks`. Checkpoint creation time is never included.

## Components and Interfaces

### gem5 GAPBS configuration

Extend `configs/example/gem5_library/x86-gapbs-amu-se.py` with mutually
exclusive checkpoint modes:

- `--checkpoint-save PATH` creates a checkpoint at trial 0 work-begin and
  exits with an explicit success marker.
- `--checkpoint-restore PATH` restores a checkpoint through a
  `CheckpointResource`, requires a non-Atomic measured CPU and
  `--roi-work-events`, resumes inside trial 0, and accepts only the exact
  sequence trial-0 work-end, trial-1 work-begin/reset, and trial-1 work-end.

Save mode rejects CXL memory but retains the target's baseline, AMU, or CIRA
model and exact model parameters. No AMU/CIRA kernel operation executes before
the checkpoint boundary. Trial 0 executes only after restore, so worker/futex
state is created in the measured process instead of being serialized. The
checkpoint state machine and the normal Atomic-to-Timing state machine remain
separate so an event cannot be misclassified as both a switch and a
checkpoint.

### Comparison runner

Extend `scripts/compare_gapbs_cxl_amu_cira.py` with:

- a serialized graph path;
- a checkpoint root;
- checkpoint creation/reuse controls; and
- checkpoint provenance fields in `summary.csv`.

The runner derives a checkpoint identity from the binary SHA-256, graph
SHA-256, guest arguments, core/thread count, memory size, gem5 binary SHA-256,
and configuration-script SHA-256. It reuses a checkpoint only when its
manifest matches every identity field. A mismatch creates a new checkpoint;
it is never silently reused.

Baseline, AMU, and CIRA binaries receive separate checkpoints because their
text and pre-ROI behavior may differ. Multiple link latencies may reuse an
otherwise identical checkpoint. Each measured run remains an independent
gem5 restore process.

### Evidence validator

Extend the existing fail-closed validator to require:

- checkpoint identity and manifest hashes;
- exactly one checkpoint-save marker in the creation log;
- exactly one checkpoint-restore marker in the measurement log;
- no CPU-switch marker in a restore run;
- `graph_scale=20`, the expected graph hash, `-f`, two trials, and measured
  trial 1;
- the complete memory range behind `board.cxl_mem_link0`;
- the requested CXL link delay;
- one measured stats section with positive `simTicks`; and
- bit-exact verification `PASS`.

Rows that fail any condition have no publishable speedup.

## Failure Handling

- An incomplete checkpoint directory or missing manifest is invalid and is
  rebuilt.
- A checkpoint whose identity does not match the requested run is invalid and
  is not restored.
- Save mode reaching program exit, verification failure, or the wrong ROI
  event before saving is an error.
- Restore mode observing a work-begin before the expected work-end, producing
  multiple stats sections, missing verification, or exiting abnormally is an
  error.
- A host reboot may interrupt a save or restore process. Checkpoints are first
  written to a run-specific temporary directory and become reusable only
  after the manifest and success marker are complete. Measurement output is
  accepted only after the existing evidence validator passes.
- Background units have no runtime limit, but their PID, start time, command,
  log path, checkpoint identity, current stats size, and last log activity are
  recorded so progress can be checked without guessing.

## Validation Sequence

1. Add unit tests for CLI exclusion rules, checkpoint identity, graph
   provenance, save/restore ROI event ordering, and fail-closed summary rows.
2. Observe those tests fail before production changes.
3. Implement the minimum checkpoint behavior and make the unit suite pass.
4. Run gem5's existing x86 SE checkpoint smoke test.
5. Generate and restore a small serialized graph checkpoint for baseline,
   AMU, and CIRA. Require identical verified answers between a direct
   non-checkpoint reference and each restored run.
6. Audit each small restore's `config.ini` to prove all memory is behind the
   CXL link and the measured link delay is 1 us.
7. Create the three scale-20 checkpoints from `g20.sg`.
8. Start the 1 us scale-20 baseline/CIRA discriminator as persistent
   background units with no runtime limit.
9. Accept results only if bit-exact verification, checkpoint provenance,
   topology, counters, and the existing PR@1us discriminator all pass.
10. Run the remaining workload/latency matrix and regenerate the table only
    after the discriminator succeeds.

## Out of Scope

- Repairing host KVM or changing gem5's KVM implementation.
- Measuring graph loading or checkpoint creation time.
- Adding a host-memory tier to the measured system.
- Dynamically changing `SerialLink` latency after gem5 instantiation.
- Publishing partial, unverified, scale-4, or topology-mismatched results.
