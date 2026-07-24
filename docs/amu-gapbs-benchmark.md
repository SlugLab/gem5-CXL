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
graphs with `--cpu atomic --cxl-link-delay 0ns
--require-m5-verification-exit`, and require `Verification: PASS` in all 12
logs. This reference does not produce publication timing.

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
  --property=StandardError=inherit \
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
exact link delay; aggregates both cores' cache/CXL counters; and rechecks
verification and ROI evidence.

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
