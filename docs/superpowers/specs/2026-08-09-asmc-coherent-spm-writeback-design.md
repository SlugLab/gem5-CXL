# ASMC Coherent SPM Writeback Design

## Goal

Make batched AMU loads correct on four coherent gem5 timing cores. An AMU
load must become visible at its destination SPM virtual address through the
same coherent memory hierarchy used by the CPUs, and its request ID must not
become observable through `amu_getfin()` until that destination write is
complete.

This repair is a correctness prerequisite for the formal scale-4,
four-thread PageRank latency sweep. It does not change the PageRank update
scheme, floating-point accumulation order, AMU batch size, CXL latency, or
the existing g20 process state.

## Observed failure

The 200 ns proof produced a valid four-core Vanilla result, then failed in
AMU before a result row could be published. The bounds gate reported:

```text
AMU_INVALID_NODE node=998803593 num_nodes=16
```

`998803593` is `0x3b888889`, the float32 bit pattern of a PageRank outgoing
contribution for this graph. The node SPM slot therefore contained data from
the preceding score phase rather than the completed node load.

The current ASMC reads the source with timing packets, but
`ASMC::completeRequest()` writes the load result to the destination through
`SETranslatingPortProxy::tryWriteBlob()`. That functional write bypasses the
timing CPU cache-coherence path. ASMC then enqueues the completion ID, so the
CPU can observe completion and read a stale destination cache line.

## Selected architecture

Each ASMC load has two ordered timing phases:

1. **Source read.** Translate the source virtual range for read access and
   issue the existing timing `ReadReq` packet or packets.
2. **Destination writeback.** After every source response has populated the
   request data buffer, translate the SPM destination virtual range for write
   access and issue timing `WriteReq` packet or packets through the existing
   ASMC coherent memory port.
3. **Completion.** Only after every destination write response returns may
   ASMC update its internal SPM image, release modeled SPM capacity, update
   completion statistics, and enqueue the ID in the issuing
   `ThreadContext`'s finished queue.

The request state records the translated destination chunks and its current
phase. Packet sender state distinguishes source reads from destination
writes. Response handling transitions once from source-read to
destination-writeback and rejects phase or response-count inconsistencies.
Admission precomputes the packet counts of both phases and reserves space for
the later destination packets, including cache-line or page-boundary splits.

The destination timing write uses the same requestor and coherent port that
ASMC already uses for source traffic. In the all-CXL configuration this port
continues through the cache hierarchy and CXL link, so both source data and
destination SPM traffic obey the selected link latency and coherence model.

The classic cache hierarchy requires a coherent I/O cache between a
non-caching requestor and the point-of-coherence xbar. A raw device
`WriteReq` attached directly beside the private L2 caches is not a legal
snoop command when an L2 has a matching MSHR. The X86 GAPBS configuration
therefore places one shared 1 KiB, one-cycle ASMC I/O cache between the ASMC
port and the membus. It has 256 MSHRs and write buffers to preserve the
accepted asynchronous concurrency, and it converts partial device writes to
the hierarchy's normal `ReadEx`/upgrade/invalidate protocol. This is a
coherence adapter, not a per-core ASMC or a software-visible result cache.
Topology validation and directional CXL accounting require the resulting
`board.asmc_io_cache.mem_side` cell.

## Completion and queue rules

- A load with a source or destination translation fault is rejected before
  any packet is admitted.
- The request ID remains in `outstanding` across both phases.
- `pendingPackets` counts only the active phase and must reach zero exactly
  once per phase.
- Destination packets reuse the request-owned data buffer; their storage
  remains valid until the final write response.
- `finished[tc]` is updated only by final completion, preserving per-thread
  `amu_getfin()` semantics.
- `completedLoads` counts complete AMU operations, not source-read phases.
- Timing write packets and bytes are included in ASMC write traffic stats.
- Queue admission remains fail-closed. Source packets plus reserved future
  destination packets must fit the configured send capacity. Destination
  enqueue consumes that reservation; reset and final completion leave no
  reservation behind. An overflow or invalid phase is a simulator panic
  rather than silent loss.

## Removed behavior

The load completion path no longer calls `writeGuest()` for the SPM
destination; the helper is removed if it has no remaining callers. Existing
functional SPM reads used to source an AMU store are unchanged. The internal
`spmData` image may still be updated at final load completion for existing
ASMC store semantics, but it is not the CPU visibility mechanism.

No software-side post-completion flush or synchronous reload is added. Such
a workaround would add host memory accesses to every offloaded load, distort
the performance result, and still leave the simulator's completion contract
incorrect.

## Validation

Implementation uses test-driven development with these gates:

1. Source-level unit tests require explicit load phases, destination write
   translation, coherent timing write packets, final-only completion, and the
   absence of functional SPM `writeGuest()` in load completion.
2. The incremental `build/X86/gem5.opt` build must succeed.
3. Existing AMU, CIRA, M2NDP, checkpoint, and result-publisher Python tests
   must remain green.
4. The isolated 200 ns proof resumes from the already passed Vanilla row but
   creates a fresh AMU checkpoint because the simulator and binary hashes
   change.
5. AMU passes only if all four host threads complete, issued loads equal
   completed loads, no ASMC translation/queue/SPM rejection occurs, the
   bounds gate observes no invalid node, verification passes, and the raw
   float32 vector hash equals the matched Vanilla hash exactly.
6. Only after AMU passes may the proof proceed to coherent CIRA, FuncSim, and
   NDPSim. The formal four-latency background sweep remains blocked until all
   four proof mechanisms pass.

## Alternatives rejected

Post-completion software flush/reload was rejected because it would insert a
synchronous CPU memory operation into every AMU load and contaminate the
measured architecture. Creating four independent ASMC devices was rejected
because the stale-data failure is caused by non-coherent destination
writeback, not by sharing the request scheduler; per-core devices would be a
larger and different architectural experiment.

## Acceptance criteria

The repair is complete when the incremental gem5 build and all relevant unit
tests pass, and the 200 ns four-thread AMU proof produces a positive timing
ROI with balanced ASMC activity and an element-for-element, bit-for-bit match
to the already validated Vanilla result. Process survival, compilation, or a
tolerance-based PageRank check alone is not completion evidence.
