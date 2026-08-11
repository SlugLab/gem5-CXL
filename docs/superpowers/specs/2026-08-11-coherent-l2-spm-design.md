# Coherent Per-Core L2-SPM Design

## Status and scope

This design repairs the AMU paper-reproduction profile after the completed
36-run collection showed only 2.31 average outstanding requests for GUPS at
5 us. The paper requires average single-core MLP above 130 at that point.
The threshold, full-ROI definition, workload size, and bit-exact gate remain
unchanged.

The repair applies to the calibrated AMU path. It does not alter Vanilla CXL,
CIRA, M2NDP, CXL link latency, or graph placement. All benchmark graph and
table data remain behind the selected CXL link.

## Root cause

ASMC currently sends both far-memory packets and `spmChunks` through one
memory-side port. Under `--cxl-memory`, this makes the paper's L2-resident SPM
writeback traverse the CXL link. The CPU then consumes the destination slot
with another ordinary access to all-CXL memory. The measured 5 us run therefore
executes 65,536 far reads and 131,072 writes through the ASMC I/O path, commits
about 86 million instructions at 0.031 IPC, and averages only 2.31 outstanding
requests despite reaching a peak of 256.

A single-fence batching experiment preserved bit equality but produced 2.53
average MLP and essentially unchanged ROI ticks. It was rejected and reverted.

## Port architecture

ASMC retains one scalar `mem_side_port` for far-memory request data. It gains a
`VectorRequestPort` named `spm_side_ports`, with exactly one connection per
timing core. Construction and `getPort()` follow CIRA's existing per-core port
pattern.

Each request records `targetCore`, derived from the issuing
`ThreadContext::contextId()`. The mapping is checked against the connected SPM
port count and fails closed on an invalid context.

Packet routing is phase-specific:

- `memoryChunks` for both `aload` and `astore` use only `mem_side_port` and
  therefore traverse the selected CXL link.
- `spmChunks` for load destinations use only
  `spm_side_ports[targetCore]`, connected to that core's private-L2 bus.
- An `astore` first enters an explicit `SpmRead` phase and obtains its source
  bytes coherently through the same target-core SPM port. Only after that
  response may it issue the far-memory write. It must not use stale bytes from
  the internal `spmData` map or a functional guest-memory read.
- Completion becomes visible only after the local SPM phase finishes.

Far and SPM ports have independent send queues, retry state, reservations, and
packet counters. Backpressure on either path is modeled and cannot drop or
reorder a request. SPM queue capacity is explicit and hash-bound by the
experiment command/configuration.

The configuration connects every `spm_side_ports[i]` to
`cache_hierarchy.l2buses[i].cpu_side_ports`. It rejects calibrated AMU when the
number of L2 buses, processor cores, and connected SPM ports differ. The
existing ASMC I/O cache remains exclusively on the far-memory path.

The paper repurposes, rather than adds to, the 256 KiB L2. Each private 8-way
L2 therefore reserves two ways (64 KiB) for SPM and leaves six ways for normal
cache allocation. SPM packets carry a dedicated request flag. A partition
manager maps that flag to the two reserved ways and maps ordinary CPU traffic
to the other six ways. Tag lookup remains address-based, so CPU loads/stores
can coherently hit an SPM line even though their ordinary allocations cannot
evict one. Far-memory and CIRA packets must never receive the SPM flag.

## Software scheduling

The paper proxy uses a persistent, aligned SPM arena sized to the declared
64 KiB capacity. Before the measured ROI it primes every used arena line with
completed AMU load operations, causing ASMC's flagged local write phase to
allocate those lines in the reserved L2 ways. Priming is fully drained before
the ROI/stat reset and cannot contribute to reported MLP or latency.

GUPS uses 256 persistent coroutine slots. Each slot advances through load
issue, load completion, update computation, store issue, and store completion,
then accepts its next update. The scheduler immediately refills a completed
slot until all updates are issued, so it does not drain the complete load
window before starting stores.

Request ID lookup is O(1): an ID-indexed table stores the owning slot and
expected phase. Every returned ID is range-, ownership-, and phase-checked.
Linear `std::find()` completion scans are forbidden.

HJ and STREAM preserve their published access granularities and deterministic
operation order while using the same persistent SPM and completion machinery.
Verification checksums are computed after `m5_work_end`; they remain mandatory
but are not part of the paper kernel's measured execution time.

## Correctness and evidence gates

The implementation is accepted only if all of these hold:

1. Static tests prove that far-memory and per-core SPM packets use distinct
   ports, that calibrated configurations have one SPM port per core, and that
   the 6-way CPU/2-way SPM partition covers each L2 way exactly once.
2. ASMC statistics separately report far reads/writes and local SPM
   reads/writes, including per-core SPM activity and retry/backpressure.
3. GUPS 5 us reports `avgOutstanding > 130` using the unchanged full-ROI
   `outstandingIntegral / occupancyTicks` formula and reaches no more than the
   configured 256-request limit.
4. GUPS baseline and AMU checksums are byte-identical. The same rule applies to
   every HJ and STREAM calibration point.
5. No graph/table demand packet is observed on an SPM port, and no SPM packet
   is observed on the far CXL port.
6. The full 36-run collection is regenerated with one binary hash. Old and new
   measurement rows may not be mixed.
7. Fit, holdout, g12, and g14 remain fail-closed. Paper artifacts are installed
   only after independent validation passes.

## Verification order

Implementation follows test-driven development:

1. Add failing port-routing, queue, configuration, scheduler, and ROI-boundary
   tests.
2. Implement per-core coherent SPM ports and separate queue state.
3. Implement the persistent coroutine scheduler and O(1) ID mapping.
4. Rebuild gem5 and `libm5`, then run focused ASMC tests.
5. Run only GUPS 5 us baseline/AMU first. Reject the implementation unless it
   is bit-exact and average MLP exceeds 130.
6. On success, discard the old measurements and rerun all 36 calibration
   simulations, followed by fit, fresh g12, g14, publication validation, paper
   build, and the authorized branch pushes.

No MLP relabeling, peak-for-average substitution, threshold reduction, copied
paper speedup, or cached pre-fix evidence is permitted.
