# CXL Address-Generation Motivation

This experiment isolates why CPU-side execution over remote CXL memory loses
effective useful-memory throughput when the CPU must fetch address metadata
before it can issue the real data access.

The microbenchmark is in
`tests/test-progs/cxl-addrgen-motivation/addrgen_motivation.c`. It uses
64-byte records and marks only the measured loop with `m5_work_begin/end`.
The runner builds the binary, routes memory traffic through the CXL
`SerialLink`, disables hardware prefetchers, and writes both CSV and LaTeX
summaries.

## Command

```sh
python3 scripts/run_cxl_addrgen_motivation.py \
    --outdir m5out/cxl_addrgen_motivation/full_minor_g1_final_20260512 \
    --nodes 32768 \
    --accesses 4096 \
    --streams 16 \
    --cpu minor \
    --no-flush-workload \
    --timeout 900
```

The `--no-flush-workload` option is used because the current X86 O3 build
aborts at startup with `src/cpu/o3/rename.cc:447:
loadsInProgress[tid] >= 0`. The same assertion is hit by the stock x86
`hello` binary with this config, so this is not specific to the motivation
benchmark. The working set is larger than L2 and uses random cacheline records,
so the measured ROI still produces remote CXL misses.

## Result

Source:
`m5out/cxl_addrgen_motivation/full_minor_g1_final_20260512/summary.csv`

| Mode | Remote address metadata per useful data access | Normalized time | Useful accesses/us | CXL packets/us |
| --- | ---: | ---: | ---: | ---: |
| Known address | 0 | 1.00x | 0.50 | 1.42 |
| Remote index | 1 | 1.91x | 0.26 | 1.43 |
| Two-level index | 2 | 2.94x | 0.17 | 1.44 |
| Pointer chase | serial next | 1.05x | 0.48 | 1.42 |
| Parallel chase | parallel next | 0.97x | 0.52 | 1.42 |

The important observation is that CXL packet rate stays essentially flat
around 1.42-1.44 packets/us, while useful accesses/us drops from 0.50 to 0.17
when the CPU must perform two remote metadata loads before issuing the useful
data load. Relative to the known-address stream, the extra remote address
generation accounts for about 66% of the two-level-index runtime.

This motivates moving address generation and cache-state orchestration to the
CXL-attached device: the host is not primarily blocked by lack of raw CXL packet
bandwidth in this experiment, but by serialized remote metadata round trips
needed before it can even form the useful data addresses.
