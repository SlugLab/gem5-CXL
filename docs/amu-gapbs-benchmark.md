# AMU GAPBS Benchmarking

This flow compares matched GAPBS baseline and AMU binaries on the local gem5
CXL model. Always use `--verify`: the runner keeps simulation alive after the
kernel ROI, runs the GAPBS verifier, and records its result in `summary.csv`.
A timing result without `verification=pass` is invalid.

## Build matched binaries

Use the same CXLMemUring checkout for both builds. The AMU builder generates an
asynchronous load window, preserves each kernel's ordered commit semantics, and
adds the verifier-to-`m5_fail` handshake.

```sh
python3 scripts/build_gapbs_baseline_cxlmemuring.py \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --benchmarks bfs,bc,pr,sssp --roi-work-markers \
  --outdir m5out/gapbs_baseline_bins

python3 scripts/build_gapbs_amu_cxlmemuring.py \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --benchmarks bfs,bc,pr,sssp --roi-work-markers \
  --amu-batch-size 64 --outdir m5out/gapbs_amu_bins
```

## Run at CXL 1 us

```sh
python3 scripts/compare_gapbs_cxl_amu_cira.py \
  --gem5 build/X86/gem5.opt \
  --baseline-bin-dir m5out/gapbs_baseline_bins/bin \
  --amu-bin-dir m5out/gapbs_amu_bins/bin \
  --benchmarks bfs,bc,pr,sssp \
  --scale 4 --iterations 1 --cpu timing --cores 1 \
  --cxl-link-delay 1us --roi-work-events --verify --timeout 600 \
  --outdir m5out/gapbs_cxl_amu_cira/window_g4_1us
```

The speedup column is `baseline ROI simTicks / AMU ROI simTicks`; only the
first stats section is used, so verifier execution is outside the timed ROI.
Require eight rows with `status=ok` and `verification=pass` before comparing
ticks.

## Current model limitation

The ASMC path is below the private CPU caches and is not cache coherent. For
bit-exact operation, generated AMU code flushes source cache lines before
`aload` and invalidates each SPM destination before consuming the completion.
These operations are correctness requirements in the current model, not part
of the intended AMU architecture. They can dominate small graphs and may make
AMU slower even with broad asynchronous batching. Treat that result as a model
integration limit; removing the flushes makes BC/SSSP or SPM-backed reads fail
verification.

The result artifact is `OUTDIR/summary.csv`; each row also points to the exact
run directory containing `stats.txt`, `config.ini`, and `gem5.log`.
