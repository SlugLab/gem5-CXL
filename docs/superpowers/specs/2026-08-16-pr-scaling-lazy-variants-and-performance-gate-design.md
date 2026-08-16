# PR Scaling Lazy Variants and Performance Gate Design

## Problem

The formal four-scale runner completed the g4 Vanilla point and then failed
before g4 AMU because it treated `--variants-build-root` as a populated input.
The documented launch instead supplied a fresh empty directory, so
`run_gapbs_matched_pr_spmv_variants.py` could not load
`builds/g4/manifest.json`. This is an orchestration defect: each scale-specific
AMU/CIRA build depends on the exact baseline GAPBS source and manifest produced
by that scale's Vanilla/M2NDP preparation stage.

The formal result also needs an explicit performance acceptance rule. For each
of AMU, CIRA, and M2NDP at g4, g12, g14, and g20, the measured end-to-end
speedup relative to the matched Vanilla point must be between 1.4x and 1.6x,
inclusive. This is a publication gate, not a license to adjust measurements.

## Selected architecture

The top-level scaling runner owns a lazy, scale-local variant-build stage.
After the Vanilla point for a scale passes, and before AMU is launched, the
runner calls `build_gapbs_matched_pr_spmv_variants.py` using:

- the exact `run/scales/g<scale>/m2ndp/build` baseline build;
- the frozen `libm5.a` already bound into scaling identity;
- the accepted AMU/CIRA calibration manifest;
- the same CXLMemUring source tree;
- `pgo-selected` CIRA policy at 1000 ns and row batch 64; and
- `builds/g<scale>` as the isolated output.

The builder writes to a temporary sibling directory, validates the complete
result, and atomically renames it to `builds/g<scale>`. It runs at most once
for an unchanged identity. If a manifest already exists, the runner loads it
and validates its baseline-manifest SHA-256,
calibration SHA-256, fixed-20 floating-point contract, CIRA mode, effective
latency, and both variant binaries before reuse. A partial or mismatched build
is rejected; it is never silently overwritten or reused.

This follows the existing g12/g14 lazy-builder pattern. It is preferable to a
manual build-and-resume loop because one service invocation remains complete
and restartable. It is preferable to freezing variants before execution
because the variants depend on the scale-local baseline build produced during
the run.

## State and resume semantics

Each scale gains a variant-build record with status, command, input hashes,
output hashes, timestamps, and error. The top-level evidence identity continues
to bind code, graph set, calibration, gem5, `libm5.a`, and configuration.

On resume:

1. already passed matrix points are revalidated byte-for-byte;
2. the passed g4 Vanilla point is retained only through a one-time, explicit
   migration from the exact pre-fix code SHA-256; the migration requires that
   g4 Vanilla is the sole passed point, revalidates all its outputs, and records
   both old and new code hashes in `resume_lineage`;
3. a failed or absent g4 variant-build record is retried from its first
   incomplete stage;
4. a passed variant build is reusable only when its live hashes and semantic
   contract still match; and
5. AMU/CIRA cannot start until the matching scale's variant build passes.

The current root may resume only through that narrowly scoped migration because
it contains one passed g4 Vanilla point and no AMU/CIRA timing. Any other code
drift or state shape requires a fresh root. No simulator timing is inferred
from a build result. A stale `failed.json` is removed only after resume identity
and passed-point revalidation succeed.

## Correctness and performance gates

Bit-exact correctness remains the first hard gate. Every accelerated point must
pass its existing mechanism checks and full rank-vector comparison before its
latency or speedup is accepted.

After all four systems for a scale pass correctness, the runner recomputes each
accelerated speedup from exact absolute latency:

`speedup = matched Vanilla latency / accelerated latency`.

All twelve accelerated points must satisfy `1.4 <= speedup <= 1.6`. The runner
does not tune simulator parameters based on observed speedup, clamp values,
replace data with an analytical estimate, or omit an out-of-range point.

If correctness passes but any performance point is outside the interval, the
runner preserves the complete raw point records and writes a terminal
`performance-hold.json` naming every offending scale/system/value. It does not
write the publishable `complete.json`, and the plot/table publisher remains
blocked. Those real measurements become input to a separate implementation or
model diagnosis.

## Failure handling

The run fails closed on a missing baseline build, changed baseline manifest,
missing or malformed variant manifest, calibration mismatch, wrong CIRA mode,
wrong effective latency, absent binary, binary hash drift, builder failure,
bit mismatch, or speedup outside the accepted interval.

Build failure records point to the exact builder log. Correctness failures and
performance holds remain distinct so an optimization problem cannot be
mistaken for a bit-exact failure. Neither terminal condition produces paper
figures.

## Tests

Tests are written before implementation and prove:

- AMU/CIRA command generation lazily creates the missing scale-local build;
- the builder receives the exact Vanilla baseline, frozen `libm5.a`,
  calibration, PGO-selected mode, 1000 ns policy latency, and row batch 64;
- an existing manifest is reused only after all identity checks pass;
- baseline, calibration, mode, latency, binary, or hash drift is rejected;
- resume retains a passed Vanilla point and retries only the missing build;
- every accelerated speedup is recomputed from absolute times;
- 1.4x and 1.6x are accepted boundaries;
- a value below 1.4x or above 1.6x creates `performance-hold.json` and blocks
  `complete.json`; and
- all existing bit-exact, 16-point, raw-data, and publication tests remain
  green.

After focused red-green tests, the complete `amu`, `m2ndp`, and `cross_system`
Python suites and static compilation gates run before commit and push. The
background service is resumed only from the pushed revision.

## Out of scope

This repair does not change AMU, CIRA, or M2NDP mechanism parameters in
response to measured speedup; weaken bit-exact checks; synthesize missing
points; alter the four frozen graphs; change the 1 us all-CXL configuration;
or publish breadth results.
