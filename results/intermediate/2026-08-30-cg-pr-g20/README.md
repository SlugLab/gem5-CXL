# CG and PageRank intermediate results (2026-08-30)

This directory records the publishable intermediate boundary captured at
`2026-08-30T04:37:20.965807+00:00`. It is intentionally not a final
AMU/CIRA/M2NDP comparison.

Completed evidence:

- NPB CG Class D native run: 4 threads, 1,500,000 elements, 100 iterations,
  2305.92 seconds, official verification pass.
- NPB CG M2NDP at 1 us: 102,531,389 cycles at a 0.5 ns core period
  (0.051265695 seconds), one of one launches complete, FuncSim operation-level
  bit-exact verification pass over 2,395,647 operations, and NDPSim memory
  match pass.
- GAP PageRank `pr_spmv`, scale 20, 4 threads, all memory on CXL: the Vanilla
  baseline completed at 200 ns, 500 ns, 1 us, and 2 us. The source summary rows
  are preserved in `pagerank-vanilla-raw.csv`.

Still running at capture time:

- Eight PageRank AMU/CIRA jobs covering 200 ns, 500 ns, 1 us, and 2 us.
- One PageRank CIRA PGO job at 1 us.

Those nine jobs had not emitted timing statistics, so no AMU/CIRA/PGO
speedups are reported here. Existing gem5 checkpoints are valid trial-0-entry
restart points, but restarting from them repeats the entire timing ROI and does
not retain current mid-ROI progress. A live CRIU checkpoint was not attempted:
the checkpoint tool requires at least 32 GiB free, while 25.672 GiB was
available on `/mnt/disk0`.

`manifest.json` contains source paths, content hashes, active service identity,
checkpoint identity, and the limitations of this intermediate record. Absolute
paths are retained as machine-local provenance; the small evidence files in
this directory are the portable copies committed to Git.
