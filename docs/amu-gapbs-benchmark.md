# AMU/CIRA GAPBS Benchmarking

This flow compares matched GAPBS baseline, AMU, and CIRA binaries on the gem5
CXL model. Publication runs use two cores, a serialized scale-20 graph, and
two trials. A checkpoint is saved immediately before trial 0. Every measured
configuration restores that checkpoint into a two-core Timing system whose
entire memory range is behind the selected CXL latency, runs trial 0 as CXL
warmup, resets statistics at trial 1 begin, and measures trial 1.

Always use `--verify`. A timing result is invalid unless `summary.csv` reports
`status=ok`, `verification=pass`, `checkpoint_restores=1`,
`cpu_switches=0`, and a positive `sim_ticks`.

## Build matched checkpoint binaries

Use the same CXLMemUring checkout and enable ROI work markers in all three
builders:

```sh
python3 scripts/build_gapbs_baseline_cxlmemuring.py \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --benchmarks bfs,bc,pr,sssp --roi-work-markers \
  --outdir m5out/gapbs_baseline_bins_checkpoint_g20_20260724

python3 scripts/build_gapbs_amu_cxlmemuring.py \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --benchmarks bfs,bc,pr,sssp --roi-work-markers \
  --amu-batch-size 64 \
  --outdir m5out/gapbs_amu_bins_checkpoint_g20_20260724

python3 scripts/build_gapbs_cira_cxlmemuring.py \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --benchmarks bfs,bc,pr,sssp --roi-work-markers \
  --outdir m5out/gapbs_cira_bins_checkpoint_g20_20260724
```

The generated verifier emits `m5_fail` on a mismatch and `m5_exit` only after
all requested trials and verifications complete. The builder also emits the
verification result through an unbuffered path before either m5op.

## Small bit-exact proof

Generate separate unweighted and weighted scale-4 graphs. SSSP rejects `.sg`
by design and therefore uses `.wsg`.

```sh
mkdir -p m5out/gapbs_graphs
m5out/gapbs_baseline_bins_checkpoint_g20_20260724/src/gapbs/converter \
  -g 4 -b m5out/gapbs_graphs/g4.sg
m5out/gapbs_baseline_bins_checkpoint_g20_20260724/src/gapbs/converter \
  -g 4 -w m5out/gapbs_graphs/g4.wsg \
  -b m5out/gapbs_graphs/g4.wsg
sha256sum m5out/gapbs_graphs/g4.sg m5out/gapbs_graphs/g4.wsg
```

Run BFS, BC, and PR against the unweighted graph:

```sh
python3 scripts/compare_gapbs_cxl_amu_cira.py \
  --baseline-bin-dir m5out/gapbs_baseline_bins_checkpoint_g20_20260724/bin \
  --amu-bin-dir m5out/gapbs_amu_bins_checkpoint_g20_20260724/bin \
  --cira-bin-dir cira_pgo=m5out/gapbs_cira_bins_checkpoint_g20_20260724/bin \
  --benchmarks bfs,bc,pr \
  --graph m5out/gapbs_graphs/g4.sg --graph-scale 4 --smoke-test \
  --iterations 2 --measure-trial 1 --cpu timing --cores 2 \
  --checkpoint-root m5out/gapbs_checkpoints/g4 \
  --cxl-link-delay 1us --roi-work-events --verify \
  --allow-zero-cira \
  --outdir m5out/gapbs_cxl_amu_cira/checkpoint_g4_unweighted_1us
```

Run SSSP against the weighted graph:

```sh
python3 scripts/compare_gapbs_cxl_amu_cira.py \
  --baseline-bin-dir m5out/gapbs_baseline_bins_checkpoint_g20_20260724/bin \
  --amu-bin-dir m5out/gapbs_amu_bins_checkpoint_g20_20260724/bin \
  --cira-bin-dir cira_pgo=m5out/gapbs_cira_bins_checkpoint_g20_20260724/bin \
  --benchmarks sssp \
  --graph m5out/gapbs_graphs/g4.wsg --graph-scale 4 --smoke-test \
  --iterations 2 --measure-trial 1 --cpu timing --cores 2 \
  --checkpoint-root m5out/gapbs_checkpoints/g4-weighted \
  --cxl-link-delay 1us --roi-work-events --verify \
  --allow-zero-cira \
  --outdir m5out/gapbs_cxl_amu_cira/checkpoint_g4_weighted_1us
```

`--allow-zero-cira` is valid only for this correctness smoke: tiny BFS, BC,
or SSSP graphs may not reach a profile-guided descriptor. It must not be used
for scale-20 evidence. Require all 12 rows across the two summaries to pass;
AMU and CIRA issued/completed counters must balance even when both are zero.

For an independent local-memory reference, rebuild with
`--verification-exit` instead of `--roi-work-markers`, run the same serialized
graphs with `--cpu atomic --cxl-link-delay 0ns` and
`--require-m5-verification-exit`, and require both the strict m5-exit marker
and `Verification: PASS` in all 12 logs. For example, the exact baseline PR
reference command and artifact path are:

```sh
python3 scripts/build_gapbs_baseline_cxlmemuring.py \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --benchmarks pr --verification-exit \
  --outdir m5out/gapbs_baseline_bins_reference_g20_20260724

build/X86/gem5.opt \
  --outdir=m5out/gapbs_reference_atomic_g20_20260724/pr/cxl_vanilla \
  configs/example/gem5_library/x86-gapbs-amu-se.py \
  --binary m5out/gapbs_baseline_bins_reference_g20_20260724/bin/pr \
  --arguments "-f $(pwd)/m5out/gapbs_graphs/g20.sg -n 2 -v" \
  --cpu atomic --cores 2 --scale 20 --iterations 2 \
  --cxl-link-delay 0ns --no-asmc --require-m5-verification-exit
```

This reference does not produce publication timing.

## Scale-20 PR gate at CXL 1 us

First verify the canonical graph:

```sh
sha256sum m5out/gapbs_graphs/g20.sg
stat --printf='%s\n' m5out/gapbs_graphs/g20.sg
```

Required values:

```text
ce900a7147a073835a7450e8f1afedf9f13db6833652bf2f9647819be26bedb3
133986161
```

Dry-run the exact foreground command:

```sh
python3 scripts/compare_gapbs_cxl_amu_cira.py \
  --baseline-bin-dir m5out/gapbs_baseline_bins_checkpoint_g20_20260724/bin \
  --cira-bin-dir cira_pgo=m5out/gapbs_cira_bins_checkpoint_g20_20260724/bin \
  --benchmarks pr \
  --graph m5out/gapbs_graphs/g20.sg --graph-scale 20 \
  --iterations 2 --measure-trial 1 --cpu timing --cores 2 \
  --checkpoint-root m5out/gapbs_checkpoints/g20 \
  --cxl-link-delay 1us --roi-work-events --verify \
  --timeout 0 --dry-run \
  --outdir m5out/gapbs_cxl_amu_cira/checkpoint_g20_pr_1us_20260724
```

The checkpoint-save commands must show local Atomic, 0 ns, and `-f g20.sg`.
The restore commands must show two-core Timing, all-CXL, 1 us, and the same
absolute graph path.

## Unlimited background launch

Create the log directory and launch a persistent transient unit. Do not set
`RuntimeMaxSec`; `--timeout 0` also disables the runner's subprocess timeout.

```sh
mkdir -p m5out/background
sudo systemd-run \
  --unit=gapbs-g20-pr-1us-20260724 \
  --property=WorkingDirectory=/home/victoryang00/gem5-CXL/.worktrees/gapbs-latency-table \
  --property=StandardOutput=append:/home/victoryang00/gem5-CXL/.worktrees/gapbs-latency-table/m5out/background/gapbs-g20-pr-1us-20260724.log \
  --property=StandardError=append:/home/victoryang00/gem5-CXL/.worktrees/gapbs-latency-table/m5out/background/gapbs-g20-pr-1us-20260724.log \
  /usr/bin/python3 scripts/compare_gapbs_cxl_amu_cira.py \
  --baseline-bin-dir m5out/gapbs_baseline_bins_checkpoint_g20_20260724/bin \
  --cira-bin-dir cira_pgo=m5out/gapbs_cira_bins_checkpoint_g20_20260724/bin \
  --benchmarks pr \
  --graph m5out/gapbs_graphs/g20.sg --graph-scale 20 \
  --iterations 2 --measure-trial 1 --cpu timing --cores 2 \
  --checkpoint-root m5out/gapbs_checkpoints/g20 \
  --cxl-link-delay 1us --roi-work-events --verify \
  --timeout 0 \
  --outdir m5out/gapbs_cxl_amu_cira/checkpoint_g20_pr_1us_20260724
```

Check live state:

```sh
systemctl is-active gapbs-g20-pr-1us-20260724.service
systemctl show gapbs-g20-pr-1us-20260724.service \
  -p MainPID -p ActiveEnterTimestamp -p RuntimeMaxUSec -p ExecMainStatus
pgrep -af 'gem5.opt.*g20.sg'
tail -n 80 m5out/background/gapbs-g20-pr-1us-20260724.log
```

Do not report a speedup while the unit is active. After it exits, validate:

```sh
python3 scripts/validate_gapbs_amu_latency_sweep.py --pr-gate \
  m5out/gapbs_cxl_amu_cira/checkpoint_g20_pr_1us_20260724
```

Only `PASS: PR@1us scale-20 CIRA discriminator` authorizes a speedup claim.
The validator recomputes graph, binary, gem5, and config hashes; checks the
checkpoint manifest and payload; proves the complete-range CXL topology and
exact link delay; requires exact two-core plus `OMP_NUM_THREADS=2` execution;
binds the restore marker to the manifest checkpoint; validates kind-specific
ASMC/CIRA topology; aggregates both cores' cache/CXL counters; and requires
the strict verifier-triggered m5-exit marker.

## Model limitation

The ASMC path is below the private CPU caches and is not cache coherent. For
bit-exact operation, generated AMU code flushes source cache lines before
`aload` and invalidates each SPM destination before consuming the completion.
These operations are correctness requirements in the current model, not part
of the intended AMU architecture. They dominate tiny graphs and can make AMU
much slower even with broad asynchronous batching. Removing them makes BC,
SSSP, or SPM-backed reads fail verification.

Each result row points to a run directory containing `stats.txt`,
`config.ini`, and `gem5.log`. The runner uses the first stats section as the
explicit trial-1 ROI dump; gem5 may append one later final-exit section.

## G20 AMU/CIRA/M2NDP table publication

The paper table is generated only after the fixed-20 AMU, CIRA, and M2NDP
formal pipelines finish. Run the publisher from the
`m2ndp-g20-pr-spmv` worktree:

```sh
env MPLCONFIGDIR=/tmp/gapbs-mplconfig \
  python3 scripts/generate_gapbs_g20_e2e_table.py \
  --m2ndp-results-root m5out/m2ndp_g20_pr_spmv_e2e \
  --variants-results-root m5out/matched_pr_spmv_g20_e2e \
  --latency-csv \
    /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/gapbs-amu-latency-results.csv \
  --latency-run-root \
    /home/victoryang00/gem5-CXL/.worktrees/gapbs-latency-table \
  --output-dir \
    /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6
```

The command produces:

- `gapbs-vtune-cxl-table.tex`, with a formal g20/1us end-to-end panel and a
  separate scale-4 multi-latency sensitivity panel;
- `gapbs-g20-e2e-results.csv`, with the unrounded Vanilla CXL, AMU, CIRA, and
  M2NDP absolute latencies and speedups; and
- `gapbs-g20-e2e-table-evidence.json`, with the machine-readable contract,
  input hashes, unrounded values, and repository commit;
- `fig/gapbs-g20-e2e.pdf`, the paper-ready two-panel vector figure; and
- `fig/gapbs-g20-e2e.svg`, the editable vector figure.

Both vector files embed the evidence JSON SHA-256. Panel (a) contains only the
formal two-core, all-CXL, 1 us g20 PageRank comparison. Panel (b) contains only
the separate scale-4, single-core sensitivity geomeans and is not g20 evidence.

The publisher is intentionally fail closed. It is expected to fail while any
formal service is running or before its final summary/manifest exists. It
also rejects the result on any verifier, bit-exact, graph, binary, delay,
event-balance, FuncSim, NDPSim, or calibration mismatch. A running or
successfully exited process alone never authorizes a displayed speedup.
All five outputs are validated and staged before replacement. Existing files
are moved to sibling backups, and a failed promotion restores the prior five
files byte-for-byte. No partial table/figure generation is visible to the
paper.

## Formal g4 four-thread latency sweep

This sweep is separate from the g20 services above. It uses the fixed graph

```text
/home/victoryang00/gem5-CXL/.worktrees/gapbs-latency-table/m5out/gapbs_graphs/g4.sg
```

whose required SHA-256 is
`f234532690f6cfc30e993c4d9a1839e65002a618e7da20ea6a4242818b9c6c3d`.
The formal profile fixes four timing cores, `OMP_NUM_THREADS=4`, all-CXL
memory, two trials with trial 1 measured, 20 synchronous double-buffered
PageRank iterations, and link latencies 200 ns, 500 ns, 1 us, and 2 us. Do
not add `--smoke-test` to any command in this section.

Build the common fixed-float32 source tree and the aggressive bit-exact AMU
and coherent CIRA binaries:

```sh
python3 scripts/build_gapbs_m2ndp_pr_spmv.py \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --outdir m5out/g4_4thread_latency_sweep_20260809/build/baseline \
  --reference-raw \
    m5out/g4_4thread_latency_sweep_20260809/build/baseline-unused.u32 \
  --m5-library util/m5/build/x86/out/libm5.a

python3 scripts/build_gapbs_matched_pr_spmv_variants.py \
  --baseline-build \
    m5out/g4_4thread_latency_sweep_20260809/build/baseline \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --m5-library util/m5/build/x86/out/libm5.a \
  --outdir m5out/g4_4thread_latency_sweep_20260809/build/variants
```

Run the full 16-entry matrix in the foreground with no wall-clock limit:

```sh
python3 scripts/run_gapbs_g4_4thread_latency_sweep.py \
  --graph \
    /home/victoryang00/gem5-CXL/.worktrees/gapbs-latency-table/m5out/gapbs_graphs/g4.sg \
  --variants-build \
    m5out/g4_4thread_latency_sweep_20260809/build/variants \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --m2ndp-root m5out/m2ndp/source \
  --gem5 build/X86/gem5.opt \
  --outdir m5out/g4_4thread_latency_sweep_20260809 \
  --timeout 0
```

The order within every latency is Vanilla CXL, AMU, CIRA, then M2NDP. The
Vanilla action stops the latency-specific M2NDP runner immediately after its
fresh gem5 baseline; the later M2NDP action resumes the same hashed state.
AMU and CIRA use isolated run directories and four-core checkpoint identities.
No path above overlaps a g20 result or checkpoint directory.

Before starting the persistent matrix, exercise the complete 200 ns row in
an isolated proof root:

```sh
python3 scripts/run_gapbs_g4_4thread_latency_sweep.py \
  --graph \
    /home/victoryang00/gem5-CXL/.worktrees/gapbs-latency-table/m5out/gapbs_graphs/g4.sg \
  --variants-build \
    m5out/g4_4thread_latency_sweep_20260809/build/variants \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --m2ndp-root m5out/m2ndp/source \
  --gem5 build/X86/gem5.opt \
  --outdir m5out/g4_4thread_latency_sweep_20260809-proof \
  --stop-after-latency 200ns \
  --timeout 0
```

The stop boundary is reached only after Vanilla, AMU, CIRA, and M2NDP have
all passed at 200 ns. Its state contract records the stop latency, so it
cannot later be resumed as the formal four-latency sweep.

After an interruption, resume only from the same command contract:

```sh
python3 scripts/run_gapbs_g4_4thread_latency_sweep.py \
  --graph \
    /home/victoryang00/gem5-CXL/.worktrees/gapbs-latency-table/m5out/gapbs_graphs/g4.sg \
  --variants-build \
    m5out/g4_4thread_latency_sweep_20260809/build/variants \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --m2ndp-root m5out/m2ndp/source \
  --gem5 build/X86/gem5.opt \
  --outdir m5out/g4_4thread_latency_sweep_20260809 \
  --timeout 0 --resume
```

Resume accepts a passed entry only when its canonical output hash still
matches. A failed or interrupted entry and all later entries return to
pending. Input path, profile, graph, core/thread, trial, iteration, and
latency-contract drift is rejected before any child process starts.

Only after all 16 entries are passed, collect and atomically publish the
canonical table inputs, then render the vector figure:

```sh
python3 scripts/generate_gapbs_g4_4thread_latency_results.py \
  --sweep-root m5out/g4_4thread_latency_sweep_20260809

python3 scripts/generate_gapbs_g4_4thread_latency_figure.py \
  --csv \
    m5out/g4_4thread_latency_sweep_20260809/published/gapbs-g4-4thread-latency-results.csv \
  --evidence \
    m5out/g4_4thread_latency_sweep_20260809/published/gapbs-g4-4thread-latency-evidence.json \
  --outdir m5out/g4_4thread_latency_sweep_20260809/published
```

The publication directory must then contain the unrounded 16-row CSV, the
machine-readable evidence JSON, `gapbs-g4-4thread-latency-table.tex`, and the
PDF/SVG figure. The collector independently recomputes all host and M2NDP
latencies and all same-latency speedups; checks four active balanced CIRA
ports, AMU issued/completed balance, exact per-latency result-vector hashes,
and all four M2NDP calibrations. Until these gates pass, do not replace the
paper table or describe an intermediate row as a completed comparison.

## G14 four-thread real-CXL formal runner

The publication replacement uses deterministic g12 qualification and g14
formal graphs on external storage. The g4 flow above remains a correctness and
runner regression test; it is not real-CXL performance evidence. Before any
graph generation or formal latency, require at least 100 GiB free and create
the stable link exactly once:

```sh
mkdir -p /mnt/disk0/gem5-CXL-g14-eval
ln -s /mnt/disk0/gem5-CXL-g14-eval \
  /home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv/m5out/g14-real-cxl-eval
df -B1 /mnt/disk0
readlink -f m5out/g14-real-cxl-eval
```

Prepare and freeze both graph manifests, then run qualification. The
qualification uses four timing cores/threads, two trials, 20 synchronous
PageRank iterations, a checkpoint immediately before trial 0, raw bit-exact
comparison after every mechanism, and activity-only CIRA lead selection:

```sh
python3 scripts/prepare_gapbs_pr_graph.py \
  --scale 12 --root /mnt/disk0/gem5-CXL-g14-eval/graphs
python3 scripts/prepare_gapbs_pr_graph.py \
  --scale 14 --root /mnt/disk0/gem5-CXL-g14-eval/graphs
python3 scripts/run_gapbs_g12_qualification.py \
  --root /mnt/disk0/gem5-CXL-g14-eval --resume
```

`qualification/qualification.json` must identify exactly one of
`g12_real_cxl` and `g12_cache_resident` as true. The exclusively created
`policy/cira-lead.json` freezes the smallest passing 1 us lead and its result
hashes. A cache-resident g12 graph routes qualification to g14/1 us; it never
waives the positive memory-controller traffic gate.

Run the latency-major 16-entry formal matrix in a low-priority user service:

```sh
systemd-run --user --unit=gem5-g14-real-cxl \
  --property=Nice=15 --property=IOSchedulingClass=idle \
  --collect /usr/bin/python3 \
  /home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv/scripts/run_gapbs_g14_4thread_latency_sweep.py \
  --root /mnt/disk0/gem5-CXL-g14-eval --resume
```

For the staged 1 us proof, use `--only-latency 1us --stop-after cira`; resume
with the same root to continue through M2NDP. The immutable experiment contract
does not include these operational stop filters, so a later full `--resume`
continues the remaining matrix. The runner never falls back to another
filesystem and never deletes a formal output directory. A passed action is
reusable only when its command plus graph, binary, config, checkpoint, policy,
raw-vector, and summary hashes still match. Every AMU/CIRA raw vector must be
bit-identical to the same-latency Vanilla vector.

## Current background recovery policy

Periodic and live CRIU checkpointing are disabled. The old
`gapbs-amu-criu-restore.service` and `m2ndp-criu-restore.service` units must
remain disabled; they are not part of the current run or publication path.

The formal jobs use their application-level recovery mechanisms instead:

- `gapbs-matched-pr-spmv-amu-g20-resume.service` restores the validated gem5
  trial-0 checkpoint and re-executes the warmup/measurement path.
- `m2ndp-g20-pr-spmv-resume.service` uses the orchestrator's completed-stage
  evidence and `--resume`; an interrupted NDPSim stage restarts that stage.
- `gapbs-g20-table-publisher.timer` checks every five minutes and publishes only
  after all final summaries and bit-exact gates are present.

This policy intentionally provides no periodic live-process snapshots. A
service restart can repeat simulator work, but it cannot authorize or preserve
a partial paper result.

## Formal four-scale CIRA/AMU/M2NDP PR scaling

The formal scaling profile is `pr-scaling-4thread-1us`: g4, g12, g14, and g20;
Vanilla, AMU, CIRA, and M2NDP; four host timing cores/threads; all memory behind
the modeled CXL link; exactly 1 us link delay; two trials; and 20 synchronous
double-buffered PageRank iterations. Trial 0 is the complete CXL warmup and
trial 1 is measured through final mechanism drain. A point is reusable only
after the full rank vector is bit-exact and all mechanism/configuration gates
pass.

Scaling input identity is independent of the six-workload breadth record. This
does not relax breadth: `freeze_cross_system_inputs.py` must continue to emit
`failed_input` until the real MCF, AMG, LULESH, NPB CG, and NPB MG paper inputs
are bound. The eventual combined publisher permits different scoped input
hashes but requires identical calibration and g20 graph SHA-256 values.

Freeze the selected endpoint graphs without regenerating them, then bind the
four ordered manifests:

```sh
SCALING_SHA=$(git rev-parse --short=12 HEAD)
SCALING_ROOT=/mnt/disk0/gem5-CXL-eval/pr-scaling-${SCALING_SHA}
CONVERTER=/home/victoryang00/gem5-CXL/.worktrees/gapbs-latency-table/m5out/gapbs_baseline_bins_latency_g20/src/gapbs/converter
mkdir -p "${SCALING_ROOT}/graphs"
python3 scripts/prepare_gapbs_pr_graph.py --scale 4 \
  --existing-graph /home/victoryang00/gem5-CXL/.worktrees/gapbs-latency-table/m5out/gapbs_graphs/g4.sg \
  --generator "${CONVERTER}" \
  --output "${SCALING_ROOT}/graphs/g4.manifest.json"
python3 scripts/prepare_gapbs_pr_graph.py --scale 20 \
  --existing-graph /home/victoryang00/gem5-CXL/.worktrees/gapbs-latency-table/m5out/gapbs_graphs/g20.sg \
  --generator "${CONVERTER}" \
  --output "${SCALING_ROOT}/graphs/g20.manifest.json"
python3 scripts/freeze_pr_scaling_inputs.py \
  --graph-manifest "${SCALING_ROOT}/graphs/g4.manifest.json" \
  --graph-manifest /mnt/disk0/gem5-CXL-g14-eval/graphs/g12.manifest.json \
  --graph-manifest /mnt/disk0/gem5-CXL-g14-eval/graphs/g14.manifest.json \
  --graph-manifest "${SCALING_ROOT}/graphs/g20.manifest.json" \
  --output "${SCALING_ROOT}/inputs.json"
```

The immutable graph hashes are
`f234532690f6cfc30e993c4d9a1839e65002a618e7da20ea6a4242818b9c6c3d`
for g4,
`759003842b672ad90eabbd5b045980e9ddf43a95bffb01b318db7fc4b8b551f1`
for g12,
`72fb08147f63112b4ea3fcff8a14b1713fdf8b097b2cf459a1ecdc217baf6524`
for g14, and
`ce900a7147a073835a7450e8f1afedf9f13db6833652bf2f9647819be26bedb3`
for g20. The accepted AMU calibration is
`/mnt/disk0/gem5-CXL-g14-eval/amu-paper-full-2c07da6b73.calibration.json`
with SHA-256
`e62f01b90dc6377e5c05e5e5358c40486cee351b78ab82001201ca55f24ae4ab`.

Start the matrix as an unlimited transient service. There is no periodic live
checkpoint; `state.json` advances only after a complete passed point:

```sh
systemd-run --unit=cira-amu-m2ndp-pr-scaling-formal --collect \
  --description='Formal four-thread all-CXL 1us PR scaling' \
  --property=WorkingDirectory=/home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv \
  /usr/bin/python3 scripts/run_cira_amu_m2ndp_scaling.py \
  --inputs "${SCALING_ROOT}/inputs.json" \
  --calibration /mnt/disk0/gem5-CXL-g14-eval/amu-paper-full-2c07da6b73.calibration.json \
  --root "${SCALING_ROOT}/run" \
  --gem5 /mnt/disk0/gem5-CXL-g14-eval/amu-paper-full-2c07da6b73/inputs/gem5 \
  --config configs/example/gem5_library/x86-gapbs-amu-se.py \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --m2ndp-root /mnt/disk0/M2NDP-public \
  --variants-build-root "${SCALING_ROOT}/builds" \
  --timeout 0
```

Only `run/complete.json` with exactly 16 passed points is publishable:

```sh
python3 scripts/generate_pr_scaling_artifacts.py \
  --scaling "${SCALING_ROOT}/run/complete.json" \
  --output-root "${SCALING_ROOT}/publication"
```

The publication root contains lossless `pr-scaling-raw.json`, rectangular
`pr-scaling-raw.csv`, `pr-scaling-table.tex`, and hash-bound
`pr-scaling-evidence.json`. The `fig/` directory contains PDF and SVG versions
of speedup scaling, absolute end-to-end log latency, grouped normalized bars,
and the system-by-scale speedup heatmap. Each raw row includes exact latency,
the native gem5 tick or NDPSim cycle count, recomputed speedup, rank/summary
hashes, mechanism evidence, and graph/input/calibration/code/config identities.
