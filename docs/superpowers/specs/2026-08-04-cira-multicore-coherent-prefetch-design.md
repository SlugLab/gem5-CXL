# CIRA Multicore Coherent Prefetch Repair Design

Date: 2026-08-04

## Goal

Repair the gem5 CIRA prefetch implementation so that a descriptor issued by a
CPU thread fills that thread's private L2, all private L2s participate in
usefulness attribution, and repeated references to the same cache line do not
generate duplicate requests. The repaired path remains a CIRA prefetch sidecar;
it does not move PageRank computation into CIRA.

The formal acceptance workload remains fixed at two cores, `g20.sg`, 20
synchronous double-buffered PageRank iterations, all graph allocations on CXL
memory, and 1 us CXL latency. Correctness remains a hard gate: the CIRA output
must match the baseline output element by element and bit for bit.

## Current Failure

The current board connects CIRA only to `l2-cache-0` and registers demand
probes only on that cache. In a two-core private-L2 hierarchy, descriptors from
core 1 therefore fill core 0's L2 and core-1 demand cannot be attributed as
useful or late.

The CSR walker also expands every indexed 4-byte value into an independent
64-byte soft-prefetch request. It does not coalesce repeated indices that map
to the same line and does not suppress a line already queued, retrying, or
outstanding. The formal run consequently issued 679,339,440 line requests but
credited only 270,597 useful prefetches.

Finally, CSR descriptors enter an unbounded FIFO. Work that is no longer ahead
of demand may remain queued behind older descriptors, while the existing late
counter cannot observe demand for descriptors that have not yet expanded into
line requests.

## Considered Topologies

### Shared L3

CIRA could fill a new shared last-level cache below both private L2s. This would
make prefetched lines accessible to both cores, but it would also change the
cache hierarchy and the Vanilla baseline. That would invalidate direct reuse
of the current two-private-L2 results.

### Broadcast to every private L2

CIRA could send each line to both L2s. This would make every line locally
available but duplicate internal traffic and model an unrealistically powerful
broadcast engine.

### Per-thread private-L2 routing

The selected design retains one system-level CIRA object but exposes one
request port per CPU core. A descriptor captures the issuing
`ThreadContext`'s context ID and routes all of its line requests to that core's
private L2. Existing classic-cache coherence continues to govern interactions
between private L2s through the shared memory-side crossbar. No cache capacity,
latency, associativity, or baseline topology changes are introduced.

## Architecture

### Core-targeted ports

Replace the scalar CIRA memory-side port with an indexed vector of request
ports. The board connects port `i` to private L2 bus `i`. Initialization fails
closed if the number of connected CIRA ports does not equal the configured CPU
core count or if a descriptor resolves to an invalid target.

`issuePrefetch`, indexed descriptors, CSR descriptors, request state, queued
packets, retry state, and completion handling all carry a target core. The
target is derived from the issuing `ThreadContext`, not from the address and
not from queue order. Retries return to the same port that first attempted the
packet.

### Multicore probes and attribution

The board passes all private L2 objects to CIRA as demand-probe targets. CIRA
registers hit, miss, and fill listeners on every target and associates each
listener with its core index.

Usefulness state is keyed by `(target core, physical cache-line address)`.
Core-0 demand cannot consume or classify a core-1 prefetch, and vice versa.
The reported aggregate `usefulPrefetches` and `latePrefetches` remain available
for the result pipeline. New per-core vector statistics expose issued, useful,
late, and completed counts so the formal evidence can prove both cores are
active.

### Cache-line coalescing

Before translation and allocation, CIRA aligns the requested address range to
cache-line boundaries. For each target core, it suppresses a line already in
any tracked CIRA state: CSR expansion, send queue, retry slot, outstanding
request, or completed-prefetch usefulness history. A pending line becomes a
completed line on fill. Matching same-core demand consumes either state as late
or useful and permits a future request. Completed lines never demanded by the
CPU are retained in a finite FIFO history; when that history reaches its
configured capacity, the oldest completed entry is retired. This bounds state
without using a workload-specific time threshold.

Coalescing is per target core because the private L2s have independent
contents. A line requested for core 0 does not suppress a necessary request for
core 1. Contiguous multi-line ranges are decomposed into unique line requests;
no request crosses a cache-line boundary.

The model records `coalescedPrefetches` for line candidates suppressed by an
existing live line. `issuedPrefetches` continues to count requests that
actually enter the timing path.

### Bounded descriptor scheduling

Add a configurable `max_csr_walk_queue` with a conservative finite default.
When the queue is full, a new descriptor is rejected before partial expansion,
increments `rejectedQueueFull`, and increments a descriptor-specific dropped
counter. The m5op returns zero so software can observe non-acceptance.

Scheduling remains FIFO within each core but rotates across cores after a
bounded amount of expansion, preventing one thread's high-degree row from
starving the other. Each scheduling turn expands no more than a configured
line budget before yielding. Descriptor queue depth and high-water mark are
reported.

The first repair does not infer staleness from wall-clock time or guess that a
future row has already been consumed. A bounded, fair queue plus end-to-end
late attribution provides deterministic behavior without inventing a
workload-specific deadline. Deadline-based dropping may be added later only
with an explicit software epoch contract.

## Data Flow

1. A CPU thread issues a CIRA CSR descriptor for future PageRank rows.
2. CIRA validates the descriptor, resolves its target core, and atomically
   accepts or rejects it based on descriptor capacity.
3. The fair CSR scheduler reads indices in original CSR order, aligns value
   addresses to cache lines, and discards duplicates for that target core.
4. Unique packets enter the target core's port-specific send/retry path.
5. The target private L2 handles the soft-prefetch through the existing classic
   coherent hierarchy.
6. Target-tagged probes update useful or late state only for demand from the
   same private L2.
7. Completion changes the per-core line from pending to completed. It remains
   deduplicated until matching demand consumes its usefulness record or the
   bounded completed-line history retires it.

## Correctness and Failure Handling

- CIRA changes cache timing and placement only; the host executes the same
  PageRank instructions, neighbor order, floating-point additions, and score
  writes as the baseline.
- No generated GAPBS computation or floating-point expression is changed by
  this repair.
- Invalid target cores, partially connected port vectors, impossible retry
  ownership, and duplicate completion ownership are fatal configuration/model
  errors rather than silent rerouting to core 0.
- Descriptor admission is atomic: an accepted descriptor is fully represented
  in one core's queue; a rejected descriptor produces no partial line traffic.
- Reset and checkpoint restore clear or reconstruct all per-core queue,
  retry, deduplication, and usefulness state consistently.
- Existing scalar CIRA operation remains supported for a one-core hierarchy
  through a one-element port/probe vector; there is no special core-0 fallback
  in the multicore path.

## Tests

Implementation follows red-green-refactor. Production changes begin only after
focused tests fail for the current single-L2 behavior.

### Static configuration regressions

- A two-core configuration exposes two CIRA request ports and two probe
  targets.
- Port `i` connects to private L2 bus `i`; no hard-coded `l2-cache-0` path
  remains.
- A one-core configuration produces exactly one connection.
- Port-count and probe-count mismatches fail closed.

### C++ model unit tests

- Repeated value indices mapping to one 64-byte line issue one packet for one
  target core.
- The same line requested for two target cores issues one packet per core.
- A duplicate is suppressed while queued, retrying, and outstanding.
- Completion and later lifecycle retirement permit a valid future request.
- Descriptor queue capacity rejects atomically and records the correct stats.
- Fair scheduling makes progress for both cores when one core submits a
  high-degree row.
- Probe events affect only matching `(core, line)` usefulness state.

### gem5 integration

Run a deterministic two-thread micro-workload whose cores issue disjoint and
shared cache-line patterns. Require nonzero per-core issued and useful counts,
balanced completion accounting, and a substantial reduction in actual issued
packets relative to raw indexed candidates.

Build matched Vanilla and CIRA `pr_spmv` binaries and run a small deterministic
graph under the same two-core all-CXL configuration. Require successful GAPBS
verification and exact equality of the dumped output-bit hashes.

## Formal Acceptance

After focused and small-graph tests pass, rerun the existing formal checkpoint
path for both CIRA and its matched Vanilla baseline with:

- `g20.sg`;
- two timing cores;
- all graph memory on CXL;
- 1 us CXL link delay;
- 20 synchronous double-buffered iterations;
- the same measured trial and checkpoint boundary;
- hardware prefetchers and cache parameters unchanged.

Acceptance requires:

1. CIRA verification is PASS and its output bits match the baseline exactly.
2. Both CIRA ports issue and complete requests; no core is silently inactive.
3. Issued and completed line-request accounting balances after ROI drain.
4. Cache-line coalescing is nonzero and the useful ratio is reported from both
   private L2s.
5. No descriptor or line request is silently dropped; any configured-capacity
   rejection makes the formal row invalid.
6. CIRA end-to-end ROI latency is strictly lower than the freshly rerun matched
   Vanilla latency before the paper table or figure is updated.

If correctness passes but speedup does not exceed 1.0, the run is reported as
a failed performance acceptance, not tuned away by changing cache sizes,
MSHRs, graph placement, core count, or CXL latency. Prefetch distance may be
retuned only after the repaired implementation exposes trustworthy per-core
useful, late, coalesced, queue-depth, and rejection statistics.

## Out of Scope

- Full PageRank computation offload to CIRA.
- Adding or resizing a shared LLC.
- Broadcasting every prefetch to every private L2.
- Changing PageRank iteration count, accumulation order, graph, core count, or
  memory placement.
- Using the existing timing-only device-offload shortcut as correctness or
  performance evidence.
- Updating paper numbers before the formal acceptance gates pass.
