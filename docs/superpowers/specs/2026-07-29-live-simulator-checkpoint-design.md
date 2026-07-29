# Live Simulator Checkpoint and Reboot Design

## Goal

Preserve the exact current host-process state of the running AMU/gem5 and
M2NDP/NDPSim evaluations, reboot the host, and restore both process trees
without falling back to their earlier application-level restart boundaries.

The operation is fail-closed: reboot is permitted only after both checkpoint
sets pass structural and provenance validation. A failed probe or dump must
leave the corresponding workload running, or restore it immediately before
the operation stops.

## Current boundaries

The two simulators do not offer the same native checkpoint contract:

- gem5 can create an architectural checkpoint at an event boundary, but its
  signal handlers only dump or reset statistics. The current AMU/CXL objects
  have not been proven to serialize every in-flight request, and the existing
  benchmark runner accepts only its trial-0 checkpoint provenance.
- NDPSim has no serialization or resume interface. Its runner can resume
  completed top-level stages, but an interrupted `ndpsim` stage starts again
  from its first launch.
- CRIU and DMTCP were not installed at discovery time.

Consequently, a native gem5 checkpoint plus an NDPSim stage restart does not
meet the requested exact-position requirement. The selected mechanism is a
host-process checkpoint of each complete service process tree with CRIU.

## Alternatives considered

### Native simulator checkpoints

Trigger gem5's debugger checkpoint event and add launch-boundary serialization
to NDPSim. This is the best long-term simulator architecture, but it cannot
preserve the already-running NDPSim process because the necessary serializer
is not present in that binary.

### Reuse existing restart boundaries

Allow systemd to restart AMU from trial 0 and M2NDP from the beginning of the
NDPSim stage. This is already supported and safest, but discards roughly a day
of AMU work and all current NDPSim-stage progress.

### CRIU process-tree checkpoint

Checkpoint the Python runner and simulator child together for each service.
This is the only available route that can preserve the existing processes at
their current host instruction and memory state. It is selected despite being
more operationally sensitive.

## Snapshot layout and provenance

Snapshots live outside generated benchmark evidence so they cannot be mistaken
for publication results:

```text
m5out/live-reboot-checkpoint-20260729/
  amu/
    images/
    work/
    dump.log
    manifest.json
  m2ndp/
    images/
    work/
    dump.log
    manifest.json
  transaction.json
```

Each manifest records:

- workload name and source systemd unit;
- root PID and complete PID/command tree at capture;
- boot ID, kernel release, CRIU version, host name, and capture timestamp;
- executable, config, graph, trace, and runner hashes;
- checkpoint image filenames, sizes, and SHA-256 hashes;
- open regular-file paths and offsets before capture;
- the latest simulator progress marker;
- dump return code and CRIU log path.

`transaction.json` reaches `ready_for_reboot` only when both workload manifests
validate against the current host, all recorded image hashes match, CRIU's
inventory and process-tree images decode, and the restore units are installed
and enabled.

## Preflight and compatibility probe

Before touching either process:

1. Confirm both source units are active and resolve their live root PIDs.
2. Confirm at least 32 GiB of free space on the snapshot filesystem.
3. Install CRIU from the host's package repository and run `criu check`.
4. Run a CRIU dump/restore smoke test on a disposable process that owns an open
   append-only log and a child process.
5. Inventory unsupported descriptors, deleted files, namespaces, cgroups,
   mounts, sessions, and external resources for both real process trees.
6. Run `criu dump --leave-running` into temporary probe directories.

A probe failure is non-destructive: delete only its temporary directory, leave
the workload running, record the exact CRIU error, and do not reboot.

## Final capture transaction

The original systemd units use `Restart=on-abnormal`. Before final capture,
install runtime drop-ins setting `Restart=no`, reload systemd, and verify that
the live MainPIDs did not change.

Final capture is sequential:

1. Dump AMU without `--leave-running`. A successful dump freezes and removes
   the original process tree at the captured state.
2. Validate AMU images and manifest.
3. Dump M2NDP without `--leave-running`.
4. Validate M2NDP images and manifest.
5. Stop and disable the original resume units without sending signals to
   already-dumped processes.

If the second dump fails, restore the first snapshot immediately on the
current boot, verify its process tree and progress marker, and abort the reboot.
No snapshot directory is published atomically until its dump succeeds.

## Boot restore

Install two oneshot system units ordered after `local-fs.target` and before the
publisher:

- `gapbs-amu-criu-restore.service`
- `m2ndp-criu-restore.service`

Each unit invokes the repository restore command with an exact manifest path.
The restore command rejects a changed kernel, boot-independent input hash,
executable, graph, trace, or checkpoint image. It then calls CRIU detached
restore and verifies:

- the expected process tree exists;
- gem5 or NDPSim is consuming CPU;
- the restored command lines match the manifest;
- logs advance from the recorded file position without truncation;
- the simulator progress marker is at or after the captured marker.

The original restart services remain disabled until their restored process
finishes or an operator explicitly abandons the CRIU transaction.

## Reboot gate

The reboot command is issued only when all of these are true:

- both final dumps returned zero;
- neither original process tree remains;
- both manifests and all image hashes validate;
- both restore units are enabled;
- both original resume units are disabled;
- `transaction.json` says `ready_for_reboot`;
- `systemctl --dry-run reboot` succeeds.

After the host returns, validation must show both restore units succeeded and
both simulator process trees are active. A restore failure is reported as a
hard blocker; the workflow must not silently start a workload from an older
application-level checkpoint.

## Correctness and evidence boundary

CRIU preservation does not weaken the existing result gates. AMU, CIRA, and
M2NDP publication remains blocked until the normal end-to-end summaries,
bit-exact checks, calibration evidence, and table validator pass. The CRIU
manifests prove only continuity across the reboot, never benchmark correctness
or performance.

