# G12 M²NDP latency spectrum

This snapshot contains the eight accepted, directly measured M²NDP cells for
PageRank SpMV and GAP BC on the frozen G12 graph at 200 ns, 500 ns, 1 us, and
2 us CXL round-trip latency.

The accepted raw table is
`publication/g12-m2ndp-measured-raw.csv`.  Each row names its original evidence
path and SHA-256; repository copies of those eight evidence records are under
`evidence/`.  The PDF, SVG, and PNG are generated from the raw table by:

```sh
PYTHONPATH=. python3 scripts/generate_g12_m2ndp_latency_spectrum.py \
  --input results/intermediate/2026-09-07-g12-m2ndp-latency-spectrum/publication/g12-m2ndp-measured-raw.csv \
  --output results/intermediate/2026-09-07-g12-m2ndp-latency-spectrum/publication/g12-m2ndp-latency-spectrum
```

The enclosing 24-cell host-inline/CIRA/M²NDP publication remains partial:
only these eight M²NDP cells have accepted evidence.  The snapshot does not
substitute values for the missing host-inline or CIRA cells.
