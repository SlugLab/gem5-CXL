# Periodic Live Checkpoint Rotation Design

## Goal

Continuously protect the running scale-20 AMU/gem5 and M2NDP/NDPSim
evaluations with a new recoverable CRIU checkpoint every 60 minutes. A reboot
must restore the newest completely validated generation, while a failed or
partial capture must leave both workloads running and retain the previously
recoverable generation.

This extends the one-time checkpoint design in
`2026-07-29-live-simulator-checkpoint-design.md`. It does not replace the
benchmark's final bit-exact or end-to-end result gates.

## Selected approach

Each periodic capture uses `criu dump --leave-running` on the complete AMU and
M2NDP process trees. CRIU briefly freezes one tree while it captures a
consistent host-process image, then resumes that same tree. The two workloads
are independent, so they may be captured sequentially within one generation;
they do not need a shared simulation timestamp.

Two other approaches are rejected:

- A destructive dump followed by immediate restore would prove the restore
  path every hour, but it would introduce avoidable downtime and recovery risk.
- Application-level gem5 and M2NDP checkpoints cannot preserve the current
  NDPSim launch state and therefore do not meet the latest-progress contract.

## Component boundaries

The existing `scripts/live_simulator_checkpoint.py` remains responsible for
one process tree:

- building and validating one workload manifest;
- constructing CRIU dump and restore commands;
- hashing inputs and CRIU images;
- reconciling explicitly allowlisted append-open files before restore; and
- verifying an exact restored PID and command tree.

A new `scripts/periodic_live_checkpoint.py` owns generation-level behavior:

- exclusive capture locking;
- disk and live-process preflight;
- generation naming and staging;
- sequential online capture of both workloads;
- transaction validation;
- atomic `latest` and `previous` pointer publication;
- two-generation retention; and
- resolving the latest manifest for boot restore.

This separation keeps individual CRIU mechanics independent from rotation and
retention policy.

## Filesystem layout

Periodic generations live separately from benchmark results and the original
manual recovery image:

```text
m5out/live-periodic-checkpoints/
  capture.lock
  latest -> generations/20260729T230000Z-eeda9abc-000001
  previous -> generations/20260729T220000Z-eeda9abc-000000
  generations/
    20260729T230000Z-eeda9abc-000001/
      amu/
        images/
        work/
        manifest.json
      m2ndp/
        images/
        work/
        manifest.json
      transaction.json
  failures/
    20260729T225900Z-eeda9abc-000001.json
  recovery-evidence/
    20260729T230000Z-eeda9abc-000001/
      eeda9abc-6661-4d6d-82b0-972d94ac86af/
```

An unpublished generation is created as
`generations/.staging-<generation-id>`. It is renamed to its final generation
name only after both manifests and the transaction pass validation.

The existing `m5out/live-reboot-checkpoint-20260729/` tree remains untouched
as a manual disaster-recovery fallback and is not counted against the
two-generation retention policy.

## Generation identity and transaction

A generation ID contains:

- UTC start time;
- the first eight characters of the current boot ID; and
- a monotonically increasing six-digit sequence derived from existing
  generation directories.

`transaction.json` records:

- schema version and generation ID;
- capture start and completion timestamps;
- host, boot ID, kernel, and CRIU version;
- absolute AMU and M2NDP manifest paths;
- PID and command trees observed before and after each dump;
- progress evidence observed before and after each dump;
- free space before capture;
- validation results; and
- publication state.

Only a transaction in state `validated` may become `latest`. Its state changes
atomically from `validated` to `published` before pointer switching.

## Seed metadata and live discovery

The first periodic generation uses the validated manifests in
`m5out/live-reboot-checkpoint-20260729/` as seed metadata. Later generations
use the current `latest` manifests.

The seed supplies immutable provenance:

- workload name and source unit;
- executable, graph, config, trace, and runner input records;
- expected PID and command tree; and
- the explicit restore-file policy allowlist.

The capture process discovers live data directly:

- the root PID must exist and match the seed command line;
- the complete live tree must match the seed PID and command tree;
- input paths, sizes, and SHA-256 hashes must still validate;
- progress is sampled immediately before and after CRIU capture; and
- CRIU image records and append-file offsets come from the new image set.

No process is restarted merely to create a new generation.

## Append-open file handling

M2NDP's `ndpsim.log` is the only currently approved mutable restore file. The
allowlist remains explicit; the periodic capturer must not infer that every
writable regular file is safe to truncate.

For each allowlisted path, the capturer decodes the generation's `files.img`
with `crit`, requires exactly one matching regular-file record, and stores its
captured size as `checkpoint_size` in the new manifest. This replaces the old
fixed 53,680,423-byte offset with the offset belonging to each generation.

On restore:

- a file at the recorded size is accepted;
- a larger file has only its post-checkpoint tail preserved with hashes before
  exact truncation;
- a missing or shorter file fails closed; and
- a size mismatch for a non-allowlisted regular file remains a CRIU restore
  failure.

Preserved tails are written under
`recovery-evidence/<generation-id>/<boot-id>/`, outside generation retention.

## Capture transaction

The hourly service performs these steps under an exclusive non-blocking lock:

1. Resolve the source manifests from `latest`, or from the original manual
   checkpoint during bootstrap.
2. Require at least 48 GiB free on the checkpoint filesystem.
3. Require both root process trees to match their source manifests exactly.
4. Validate all immutable input hashes and the current source generation.
5. Create a staging generation and record initial process/progress evidence.
6. Capture AMU with `--leave-running`; verify that its original process tree
   still exists and advances afterward.
7. Capture M2NDP with `--leave-running`; perform the same continuity check.
8. Decode and validate `inventory.img`, `pstree.img`, and `files.img` for both
   workloads.
9. Build both new manifests, including the new M2NDP log offset.
10. Hash every CRIU image and validate both manifests.
11. Write and validate the generation transaction.
12. Rename the staging directory to its final generation name and atomically
    change its transaction state to `published`; its contents are immutable
    after that state change.
13. Publish `previous` and then `latest` using temporary symlinks plus
    `os.replace`.
14. Delete only completed generation directories that are targets of neither
    `latest` nor `previous`.

Publishing `previous` before `latest` preserves a valid fallback across a
power loss between pointer updates. Boot restore reads only `latest`.

## Failure behavior

The periodic checkpoint is fail-closed and non-destructive to live workloads:

- An existing lock causes the invocation to exit successfully as skipped.
- Insufficient space, changed input, PID mismatch, CRIU error, decoding error,
  image hash mismatch, or missing progress leaves `latest` unchanged.
- A failure after one workload capture is safe because `--leave-running` left
  that workload active.
- The staging directory is removed only after its failure metadata and CRIU
  log locations are written under `failures/`.
- Retention never runs before a new `latest` pointer is published.
- The service does not invoke reboot, stop either workload, or restart either
  original application service.

If either workload has exited, no new generation is published. The last valid
generation remains recoverable until the final benchmark publisher confirms
the normal completion and correctness artifacts.

## Boot restore

The two restore services keep their existing ordering:

1. `gapbs-amu-criu-restore.service`
2. `m2ndp-criu-restore.service`

Their commands change from a dated manifest path to:

```text
periodic_live_checkpoint.py restore-latest --root ... --job amu
periodic_live_checkpoint.py restore-latest --root ... --job m2ndp
```

`restore-latest` resolves the `latest` symlink once, rejects symlink targets
outside `generations/`, validates the complete published transaction, and then
delegates to the existing per-manifest restore implementation. The service
still verifies the exact restored PID and command tree after CRIU returns.

The installed restore units are changed only after the first periodic
generation has been captured and validated. Updating and reloading an active
oneshot unit must not stop or restart the processes currently living in its
cgroup. No restore command is run on the current boot after unit installation.

## Timer and resource policy

`live-simulator-checkpoint.timer` uses:

```ini
OnBootSec=60min
OnUnitActiveSec=60min
AccuracySec=1min
Persistent=false
```

The associated oneshot service has an infinite start timeout because a
multi-gigabyte memory image may take longer than the timer interval under
storage pressure. The application-level file lock prevents overlap. The
service uses low CPU and best-effort low I/O priority so benchmark progress
remains favored.

The timer is enabled only after a successful manual bootstrap capture and
installed-unit validation. A manual invocation immediately creates the first
periodic generation; the first timer-triggered capture occurs 60 minutes
later.

## Retention and disk safety

The system retains at most two completely published periodic generations:
`latest` and, after the second successful capture, `previous`. During capture,
one additional staging generation may exist. With the observed image sizes
this requires approximately 38 GiB, so the preflight threshold is 48 GiB.

Deletion targets are explicit resolved generation directories. The rotation
code refuses to delete:

- a path outside the `generations/` directory;
- either symlink target;
- a staging directory;
- a generation without a valid transaction; or
- the original manual checkpoint tree.

Failure records and recovery evidence are outside `generations/` and are not
removed by generation retention.

## Verification strategy

Unit tests cover:

- deterministic generation IDs;
- exclusive-lock skip behavior;
- 48 GiB free-space rejection;
- exact live process-tree requirements;
- `--leave-running` on periodic dumps;
- partial two-workload capture failure without pointer movement;
- CRIU image and input hash rejection;
- `files.img` extraction of the generation-specific M2NDP log size;
- atomic `latest`/`previous` switching;
- two-generation retention and deletion boundaries;
- rejection of unsafe symlink targets;
- latest-manifest resolution and job-name matching; and
- systemd timer and unit ordering.

A disposable parent/child process integration test proves that an online
snapshot leaves both PIDs running, restores after the disposable tree is
terminated, and resumes an append-open log from the captured offset.

Live activation requires:

1. all focused and neighboring Python tests to pass;
2. a manual periodic capture of both current workloads;
3. unchanged AMU and M2NDP PID/command trees after capture;
4. continued CPU time and progress after capture;
5. two validated manifests and a published transaction;
6. `latest` resolving to the new generation;
7. installed restore services resolving that same generation; and
8. the timer enabled and scheduled 60 minutes later.

No additional real reboot is part of implementation verification without
separate user approval.

## Correctness boundary

CRIU image validation proves recoverable host-process continuity, not PageRank
numerical correctness. The AMU, CIRA, and M2NDP latency comparison remains
blocked until the existing final summaries, calibration checks, event-balance
checks, FuncSim validation, and element-by-element bit-exact verifier all pass.
Periodic checkpointing must not weaken, replace, or pre-authorize those gates.
