# Evidence-Gated GAPBS g20 Paper Figure Design

Date: 2026-07-31

## Goal

Add a publication-quality, reproducible figure comparing AMU, CIRA, and M2NDP
without weakening the existing exact-number table or the correctness gates. The
figure must not be published from partial, stale, or mixed-granularity results.

The paper will present two related but explicitly separate views:

1. Formal g20 PageRank end-to-end latency for Vanilla CXL, AMU, CIRA, and
   M2NDP under the matched two-core, all-CXL, 1 us configuration.
2. Scale-4, single-core GAPBS sensitivity showing AMU and CIRA speedup over
   Vanilla CXL at 200 ns, 500 ns, 1 us, and 2 us.

The existing LaTeX table remains the source for exact numeric lookup. The new
figure provides the visual comparison and trend.

## Evidence boundary

The plot publisher consumes only data that have passed the same validation path
as `scripts/generate_gapbs_g20_e2e_table.py`. It must not discover or parse raw
results independently.

Publication is allowed only when all of the following are true:

- The formal g20 Vanilla CXL, AMU, CIRA, and M2NDP rows are complete.
- Both gem5 configurations use two cores, `g20.sg`, 20 synchronous
  double-buffered PageRank iterations, all-CXL placement, and 1 us CXL latency.
- AMU and CIRA summaries pass their configured verifier.
- M2NDP FuncSim passes strict per-element bit-exact comparison using the matched
  floating-point accumulation order.
- M2NDP NDPSim contains every required launch and its final summary passes the
  existing artifact checks.
- Every scale-4 sensitivity point required by the table generator is present and
  marked PASS.

If any check fails, the command exits nonzero and leaves all previously
published CSV, JSON, TeX, PDF, and SVG files unchanged. It does not create a
placeholder or partial chart.

## Chart contract

### Analytical questions

- Under the formal matched g20 experiment, how do absolute end-to-end latencies
  compare across Vanilla CXL, AMU, CIRA, and M2NDP?
- In the separate scale-4 sensitivity suite, how do AMU and CIRA speedups change
  as CXL access latency increases?

### Title and claim discipline

The neutral figure title is `End-to-end PageRank latency and CXL-link
sensitivity`. The code and caption report measured values and experiment scope;
they do not claim a winner or causal explanation. Performance claims in prose
are updated only after final artifacts exist.

### Panel (a): formal g20 end-to-end latency

- Mark: horizontal bars.
- Y categories, in semantic order: Vanilla CXL, AMU, CIRA, M2NDP.
- X measure: end-to-end latency in seconds.
- Scale: logarithmic when the largest value is at least 10 times the smallest;
  otherwise linear. The selected scale is stated in generated metadata.
- Labels: every bar has its exact latency and its speedup relative to Vanilla
  CXL. Vanilla is labeled `1.00x`.
- Sorting: never sort by measured value; preserve the system order above.
- Scope annotation: `g20 PageRank, 2 cores, all CXL, 1 us, 20 iterations`.

### Panel (b): latency sensitivity

- Mark: line plus marker.
- X measure: CXL latency at 200, 500, 1000, and 2000 ns, in physical order.
- Y measure: geometric-mean speedup over the corresponding Vanilla CXL run.
- Series: AMU and CIRA only.
- Reference: a subdued dashed horizontal line at 1.0x.
- Labels: direct labels at the right endpoint; no redundant legend.
- Scope annotation: `scale-4, single-core GAPBS sensitivity`.
- The caption explicitly states that panel (b) is a sensitivity suite and is
  not formal g20 evidence. M2NDP is absent because no matched multi-latency
  sensitivity sweep exists for it.

### Visual encoding

Use a colorblind-safe palette with redundant non-color cues:

- Vanilla CXL: neutral gray, solid fill.
- AMU: blue, solid line and circle marker.
- CIRA: orange, dashed line and square marker.
- M2NDP: muted green, hatched bar.

Use black or near-black text, light gray grid lines only on the quantitative
axis, and no enclosing chart box. Values remain legible in grayscale through
hatching, line style, and marker shape.

### Layout and output

- Matplotlib static renderer with a deterministic double-column size of 7.0 by
  3.2 inches.
- Two horizontal panels with aligned visual weight and sufficient label space.
- Primary publication output: vector PDF.
- Secondary output: SVG for inspection and reuse.
- A PNG may be rendered only as a local QA artifact and is not committed or
  published by the results pipeline.
- Fonts and sizes follow the paper's existing visual style and remain readable
  at final placement size.

## Data flow and implementation surface

Add a focused plotting module,
`scripts/generate_gapbs_g20_e2e_figure.py`. Its rendering entry point accepts
the already validated formal rows, sensitivity results, and evidence digest. It
does not accept paths to raw simulator logs.

`scripts/generate_gapbs_g20_e2e_table.py` remains the evidence authority and
will:

1. Load and validate all source artifacts.
2. Construct the formal rows and sensitivity values once.
3. Render the CSV, evidence JSON, and LaTeX table.
4. Pass the validated in-memory data to the plotting module.
5. Stage all five publication artifacts before replacing any destination.

The complete publication set is:

- `gapbs-g20-e2e-results.csv`
- `gapbs-g20-e2e-table-evidence.json`
- `gapbs-vtune-cxl-table.tex`
- `fig/gapbs-g20-e2e.pdf`
- `fig/gapbs-g20-e2e.svg`

Atomic publication covers the entire set. The implementation stages files in
the destination filesystem, validates that every staged file is nonempty and
parseable, and then promotes the set. If promotion fails, it restores the prior
generation so that the paper never observes a mixed generation.

The evidence JSON SHA-256 digest is embedded in PDF metadata and an SVG metadata
element. This links the visual artifact to the exact validated table evidence.

## Paper integration

The final evidence-gated publisher updates the paper checkout only after all
validation succeeds.

- Keep `\input{gapbs-vtune-cxl-table}` for exact formal and sensitivity values.
- Replace the stale hard-coded comparison table currently labeled
  `tab:gem5_amu_comparison` with the new PDF figure.
- Use label `fig:gem5_amu_m2ndp_e2e` and update nearby references.
- The caption names both experiment grains and states that panel (b) is the
  separate scale-4, single-core sensitivity suite.
- Update numeric prose only from the final generated evidence. Do not preserve
  older manually entered speedups if they disagree with final artifacts.

The intended LaTeX placement is a normal double-column figure using
`fig/gapbs-g20-e2e.pdf`. The exact table and figure coexist because they serve
different reading tasks.

## Tests

Add unit coverage under `tests/pyunit/m2ndp/` for:

- Exact formal-system ordering.
- Exact latency ordering and AMU/CIRA-only sensitivity series.
- Correct baseline-relative speedup computation and geometric means.
- Linear versus logarithmic panel-(a) threshold behavior.
- Required scope annotations and scale-4/g20 separation.
- Evidence digest presence in PDF and SVG metadata.
- Deterministic output dimensions and nonempty vector files.
- No publication when a formal result, sensitivity point, verifier result, or
  M2NDP bit-exact result is absent or failing.
- Preservation of the prior complete generation on staging or promotion error.

Existing table-generator tests continue to verify exact CSV, evidence, and TeX
content. A small synthetic validated dataset is used for plot unit tests; formal
performance numbers are never hard-coded into test expectations.

## Visual and paper verification

Before completion:

1. Run the focused Python tests and the existing table-generator tests.
2. Run the publisher against the complete formal result set.
3. Inspect PDF metadata and dimensions with `pdfinfo`.
4. Rasterize the PDF with `pdftocairo` and visually inspect labels, clipping,
   grayscale distinction, and panel balance.
5. Build the paper and inspect the page containing the figure and exact table.
6. Confirm that the paper's printed numbers match the generated CSV and that
   the evidence digest matches the figure metadata.

No implementation is complete until the tests pass, the figure is visually
inspected in the compiled paper, and all bit-exact/verifier gates remain green.

## Operational scope

Periodic CRIU/live checkpointing is outside this design and remains disabled as
requested. The formal runs may continue under their ordinary systemd restart
policy, but the chart pipeline does not manage, restore, or rotate checkpoints.
