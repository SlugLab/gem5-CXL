# M2NDP g20 PageRank Trace Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run a bit-exact semantic trace bridge that compares two-core gem5 all-CXL `pr_spmv` at `1us` against M2NDP FuncSim and NDPSim on the identical g20 graph.

**Architecture:** A GAPBS-linked exporter produces a hashed CSR bundle, and a matched fixed-20 `pr_spmv` binary produces the gem5 reference bits outside the timed ROI. Python generators turn the bundle into native four-kernel M2NDP traces; a pinned upstream patch makes FuncSim strict and sequence-aware, while a fail-closed orchestrator calibrates the CXL link, runs warmup plus measured trials, and emits speedup only after every provenance and correctness gate passes.

**Tech Stack:** Python 3 standard library and `unittest`, C++11/OpenMP GAPBS utilities, gem5 X86 SE m5ops, M2NDP C++/CMake/Conan build scripts, M2NDP extended RISC-V trace assembly, CSV/JSON/binary evidence artifacts.

---

## Scope and File Map

This is one dependent pipeline rather than independent subprojects: graph
export feeds both references, strict FuncSim gates NDPSim, and only the final
orchestrator owns speedup publication.

Create these gem5-side files:

- `scripts/m2ndp_artifacts.py`: hashes, atomic JSON/CSV writes, graph-bundle
  validation, reference-container encoding, and provenance constants.
- `scripts/build_gapbs_m2ndp_pr_spmv.py`: copy the pinned GAPBS source, compile
  the exporter and matched binary, generate the experiment header, and record
  build hashes.
- `scripts/m2ndp_pagerank_trace.py`: stream M2NDP memory maps, generate four
  kernels and launch files, and build one-trial FuncSim and two-trial NDPSim
  sequences.
- `scripts/prepare_m2ndp.py`: validate the external M2NDP commit, apply the
  pinned patch, build FuncSim/NDPSim, and record patched-source evidence.
- `scripts/m2ndp_results.py`: parse strict FuncSim markers, NDPSim trial
  cycles, gem5 evidence, and compute gated speedup.
- `scripts/calibrate_m2ndp_cxl.py`: run the matched latency probes and derive a
  M2NDP link configuration within one link clock of gem5.
- `scripts/run_m2ndp_g20_pr_spmv.py`: resumable end-to-end orchestration and
  final `status.json`/`summary.csv`.
- `util/m2ndp/export_gapbs_graph.cc`: load `.sg` through GAPBS `Reader` and
  emit CSR component files.
- `util/m2ndp/gapbs_pr_spmv_fixed.cc`: fixed-20 synchronous PageRank with
  allocation before ROI, bit dump after ROI, and the normal verifier.
- `util/m2ndp/cxl_latency_probe.c`: one uncached 64-byte gem5 CXL
  request/response ROI.
- `util/m2ndp/patches/0001-funcsim-strict-sequence.patch`: strict bit match,
  sequential multi-kernel execution, raw-bit dump, and nonzero failure exit.
- `tests/pyunit/m2ndp/__init__.py`: test package marker.
- `tests/pyunit/m2ndp/test_m2ndp_artifacts.py`: artifact contract tests.
- `tests/pyunit/m2ndp/test_m2ndp_build.py`: source/build manifest tests.
- `tests/pyunit/m2ndp/test_m2ndp_trace.py`: map/kernel/sequence tests.
- `tests/pyunit/m2ndp/test_prepare_m2ndp.py`: pin/patch evidence tests.
- `tests/pyunit/m2ndp/test_m2ndp_results.py`: log parser and gating tests.
- `tests/pyunit/m2ndp/test_m2ndp_calibration.py`: link derivation tests.
- `tests/pyunit/m2ndp/test_run_m2ndp_g20_pr_spmv.py`: orchestration tests.

Modify:

- `docs/amu-gapbs-benchmark.md`: document the matched M2NDP flow and proof
  boundary.

Generated, ignored artifacts live under:

```text
m5out/m2ndp_g20_pr_spmv/<run-id>/
```

The external M2NDP checkout is never committed.

### Task 1: Define the Artifact and Provenance Contract

**Files:**
- Create: `scripts/m2ndp_artifacts.py`
- Create: `tests/pyunit/m2ndp/__init__.py`
- Create: `tests/pyunit/m2ndp/test_m2ndp_artifacts.py`

- [ ] **Step 1: Write failing graph/reference contract tests**

Create tests that exercise exact graph identity, file-size validation,
reference round trips, malformed headers, and atomic JSON writes:

```python
class M2NDPArtifactTest(unittest.TestCase):
    def test_graph_bundle_rejects_wrong_g20_hash(self):
        meta = artifacts.GraphMeta(
            graph_sha256="0" * 64,
            num_nodes=4,
            num_directed_edges=5,
            directed=True,
        )
        with self.assertRaisesRegex(
            artifacts.EvidenceError, "graph SHA-256"
        ):
            artifacts.validate_publication_graph(meta, smoke_test=False)

    def test_reference_container_preserves_float_bits(self):
        words = [0x3F800000, 0x80000000, 0x7FC00001]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reference.m2pr"
            header = {
                "schema": artifacts.REFERENCE_SCHEMA,
                "graph_sha256": artifacts.EXPECTED_G20_SHA256,
                "num_nodes": len(words),
                "iterations": 20,
                "measured_trial": 1,
                "binary_sha256": "a" * 64,
                "source_sha256": "b" * 64,
            }
            artifacts.write_reference(path, header, words)
            actual_header, actual_words = artifacts.read_reference(path)
        self.assertEqual(actual_header, header)
        self.assertEqual(actual_words, words)

    def test_reference_rejects_truncated_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.m2pr"
            header = {
                "schema": artifacts.REFERENCE_SCHEMA,
                "graph_sha256": artifacts.EXPECTED_G20_SHA256,
                "num_nodes": 2,
                "iterations": 20,
                "measured_trial": 1,
                "binary_sha256": "a" * 64,
                "source_sha256": "b" * 64,
            }
            artifacts.write_reference(path, header, [1, 2])
            path.write_bytes(path.read_bytes()[:-4])
            with self.assertRaisesRegex(
                artifacts.EvidenceError, "word count"
            ):
                artifacts.read_reference(path)

    def test_reference_rejects_trailing_partial_word(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.m2pr"
            header = {
                "schema": artifacts.REFERENCE_SCHEMA,
                "graph_sha256": artifacts.EXPECTED_G20_SHA256,
                "num_nodes": 2,
                "iterations": 20,
                "measured_trial": 1,
                "binary_sha256": "a" * 64,
                "source_sha256": "b" * 64,
            }
            artifacts.write_reference(path, header, [1, 2])
            path.write_bytes(path.read_bytes() + b"\x00")
            with self.assertRaisesRegex(
                artifacts.EvidenceError, "partial word"
            ):
                artifacts.read_reference(path)
```

- [ ] **Step 2: Run the tests and verify the module is missing**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_m2ndp_artifacts -v
```

Expected: import failure naming `scripts/m2ndp_artifacts.py`.

- [ ] **Step 3: Implement the artifact module**

Define these public values and interfaces:

```python
EXPECTED_G20_SHA256 = (
    "ce900a7147a073835a7450e8f1afedf9f13db6833652bf2f9647819be26bedb3"
)
EXPECTED_M2NDP_COMMIT = (
    "fe418e8c30d7c3821f7c91293c74c5c34939a063"
)
REFERENCE_MAGIC = b"M2PRREF1"
REFERENCE_SCHEMA = 1

class EvidenceError(RuntimeError):
    pass

@dataclasses.dataclass(frozen=True)
class GraphMeta:
    graph_sha256: str
    num_nodes: int
    num_directed_edges: int
    directed: bool

def sha256_file(path: Path) -> str: ...
def atomic_write_json(path: Path, value: dict) -> None: ...
def atomic_write_csv(path: Path, fieldnames: tuple[str, ...],
                     rows: list[dict]) -> None: ...
def validate_publication_graph(meta: GraphMeta,
                               smoke_test: bool) -> None: ...
def write_reference(path: Path, header: dict,
                    words: collections.abc.Sequence[int]) -> None: ...
def read_reference(path: Path) -> tuple[dict, list[int]]: ...
```

Use the binary layout:

```text
8 bytes  magic M2PRREF1
4 bytes  little-endian JSON byte count
N bytes  UTF-8 JSON with sorted keys and compact separators
4*V      little-endian uint32 PageRank words
```

`read_reference` must require schema 1, `num_nodes == len(words)`, no trailing
partial word, and all seven schema/provenance keys used by the test.

- [ ] **Step 4: Run artifact tests**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_m2ndp_artifacts -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit the artifact contract**

```bash
git add scripts/m2ndp_artifacts.py tests/pyunit/m2ndp
git commit -m "m2ndp: define trace bridge artifact contract"
```

### Task 2: Export GAPBS `.sg` as a Validated CSR Bundle

**Files:**
- Create: `util/m2ndp/export_gapbs_graph.cc`
- Modify: `scripts/m2ndp_artifacts.py`
- Create: `tests/pyunit/m2ndp/test_m2ndp_build.py`

- [ ] **Step 1: Write failing bundle validation tests**

The tests create small component files directly and prove exact sizes,
monotonic offsets, terminal edge count, vertex bounds, and degree width:

```python
def write_words(path, fmt, values):
    path.write_bytes(b"".join(struct.pack(fmt, value) for value in values))

class GraphBundleTest(unittest.TestCase):
    def test_valid_bundle_preserves_neighbor_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_words(root / "in_offsets.u64", "<Q", [0, 1, 3, 4])
            write_words(root / "in_neighbors.i32", "<i", [2, 0, 2, 1])
            write_words(root / "out_degree.u32", "<I", [1, 1, 2])
            meta = {
                "schema": 1,
                "graph_sha256": artifacts.EXPECTED_G20_SHA256,
                "num_nodes": 3,
                "num_directed_edges": 4,
                "directed": True,
            }
            artifacts.atomic_write_json(root / "graph.meta.json", meta)
            bundle = artifacts.load_graph_bundle(root)
        self.assertEqual(bundle.in_offsets, (0, 1, 3, 4))
        self.assertEqual(bundle.in_neighbors, (2, 0, 2, 1))

    def test_bundle_rejects_bad_terminal_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_words(root / "in_offsets.u64", "<Q", [0, 1, 3, 5])
            write_words(root / "in_neighbors.i32", "<i", [2, 0, 2, 1])
            write_words(root / "out_degree.u32", "<I", [1, 1, 2])
            artifacts.atomic_write_json(root / "graph.meta.json", {
                "schema": 1,
                "graph_sha256": artifacts.EXPECTED_G20_SHA256,
                "num_nodes": 3,
                "num_directed_edges": 4,
                "directed": True,
            })
            with self.assertRaisesRegex(
                artifacts.EvidenceError, "terminal CSR offset"
            ):
                artifacts.load_graph_bundle(root)
```

- [ ] **Step 2: Run the tests and verify `load_graph_bundle` is missing**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_m2ndp_build.GraphBundleTest -v
```

Expected: failure naming `load_graph_bundle`.

- [ ] **Step 3: Implement the C++ exporter**

Use GAPBS's own serialized reader and preserve incoming-neighbor iteration
order:

```cpp
using Graph = CSRGraph<SGID>;

Graph graph = Reader<SGID>(graph_path).ReadSerializedGraph();
write_u64(out / "in_offsets.u64", 0);
uint64_t offset = 0;
for (SGID u = 0; u < graph.num_nodes(); ++u) {
  for (SGID v : graph.in_neigh(u)) {
    if (v < 0 || v >= graph.num_nodes())
      fail("neighbor outside vertex range");
    write_i32(neighbors, v);
    ++offset;
  }
  write_u64(offsets, offset);
  const int64_t degree = graph.out_degree(u);
  if (degree < 0 || degree > UINT32_MAX)
    fail("out degree exceeds uint32");
  write_u32(degrees, static_cast<uint32_t>(degree));
}
if (offset != static_cast<uint64_t>(graph.num_edges_directed()))
  fail("directed edge count mismatch");
```

CLI:

```text
export_gapbs_graph GRAPH.sg OUTPUT_DIR
```

Write component files through `.tmp` names, flush/close them, then rename.
Print exactly one machine-readable line:

```text
M2NDP_GRAPH_EXPORT nodes=<N> directed_edges=<M> directed=<0|1>
```

- [ ] **Step 4: Implement Python bundle loading**

Add:

```python
@dataclasses.dataclass(frozen=True)
class GraphBundle:
    root: Path
    meta: GraphMeta
    in_offsets: tuple[int, ...]
    in_neighbors: tuple[int, ...]
    out_degree: tuple[int, ...]

def finalize_graph_meta(root: Path, graph: Path,
                        exporter_stdout: str) -> GraphMeta: ...
def load_graph_bundle(root: Path) -> GraphBundle: ...
```

Require exact byte sizes `(V+1)*8`, `M*4`, and `V*4`; use `struct.iter_unpack`
to avoid NumPy. `finalize_graph_meta` computes the source graph SHA-256 rather
than trusting the C++ process.

- [ ] **Step 5: Build and exercise the exporter against the existing g20 file**

Compile against the copied GAPBS headers:

```bash
g++ -std=c++11 -O2 -fopenmp \
  -I /home/victoryang00/gem5-CXL/.worktrees/gapbs-latency-table/m5out/gapbs_baseline_bins_checkpoint_g20_20260724/src/gapbs/src \
  util/m2ndp/export_gapbs_graph.cc \
  -o /tmp/export_gapbs_graph
/tmp/export_gapbs_graph \
  m5out/gapbs_graphs/g20.sg \
  /tmp/m2ndp-g20-export
```

Run from the worktree with the existing ignored g20 data mapping. Expected:
one `M2NDP_GRAPH_EXPORT` line and three nonempty component files.

- [ ] **Step 6: Run bundle tests**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_m2ndp_build.GraphBundleTest -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit the exporter**

```bash
git add util/m2ndp/export_gapbs_graph.cc \
  scripts/m2ndp_artifacts.py \
  tests/pyunit/m2ndp/test_m2ndp_build.py
git commit -m "m2ndp: export validated GAPBS CSR bundles"
```

### Task 3: Build a Matched Fixed-20 gem5 `pr_spmv`

**Files:**
- Create: `util/m2ndp/gapbs_pr_spmv_fixed.cc`
- Create: `scripts/build_gapbs_m2ndp_pr_spmv.py`
- Modify: `tests/pyunit/m2ndp/test_m2ndp_build.py`

- [ ] **Step 1: Write failing source and manifest contract tests**

Assert that the dedicated source:

- allocates and zero-touches `scores`/`outgoing_contrib` before
  `m5_work_begin`;
- initializes them after `m5_work_begin`;
- contains exactly 20 fixed iterations;
- has no convergence reduction or early break;
- uses separate multiply then add;
- writes raw bits only after `m5_work_end`; and
- invokes the unchanged tolerance verifier.

```python
class MatchedPageRankSourceTest(unittest.TestCase):
    def test_fixed_source_has_matched_roi_contract(self):
        source = (REPO / "util/m2ndp/gapbs_pr_spmv_fixed.cc").read_text()
        self.assertIn("constexpr int kPageRankIterations = 20;", source)
        self.assertLess(source.index("scores(g.num_nodes())"),
                        source.index("m5_work_begin"))
        self.assertLess(source.index("m5_work_end"),
                        source.index("WriteScoreBits"))
        self.assertNotIn("reduction(+ : error)", source)
        self.assertNotIn("if (error <", source)
        self.assertIn("PRVerifier", source)
```

Mock `subprocess.run` in the builder and require the manifest fields:

```python
"page_rank_iterations": 20
"fixed_iterations": true
"convergence_reduction": false
"fp_contract": false
"reference_raw_path": "<absolute path>"
```

- [ ] **Step 2: Run tests and verify the files are absent**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_m2ndp_build.MatchedPageRankSourceTest -v
```

Expected: file-not-found failure for `gapbs_pr_spmv_fixed.cc`.

- [ ] **Step 3: Implement the matched C++ benchmark**

Use `CLPageRank`, `Builder`, `Graph`, and the original `PRVerifier` formula.
The timed function must be structurally equivalent to:

```cpp
void PageRankPullFixed(
    const Graph &g,
    pvector<float> &scores,
    pvector<float> &outgoing_contrib) {
  const float init_score = 1.0f / g.num_nodes();
  const float base_score = (1.0f - 0.85f) / g.num_nodes();
  #pragma omp parallel for
  for (NodeID n = 0; n < g.num_nodes(); ++n)
    scores[n] = init_score;
  for (int iter = 0; iter < kPageRankIterations; ++iter) {
    #pragma omp parallel for
    for (NodeID n = 0; n < g.num_nodes(); ++n)
      outgoing_contrib[n] = scores[n] / g.out_degree(n);
    #pragma omp parallel for schedule(dynamic, 16384)
    for (NodeID u = 0; u < g.num_nodes(); ++u) {
      float incoming_total = 0.0f;
      for (NodeID v : g.in_neigh(u))
        incoming_total = incoming_total + outgoing_contrib[v];
      const float product = 0.85f * incoming_total;
      scores[u] = base_score + product;
    }
  }
}
```

The trial wrapper must:

```cpp
for (int trial = 0; trial < cli.num_trials(); ++trial) {
  pvector<float> scores(g.num_nodes());
  pvector<float> outgoing(g.num_nodes());
  std::fill(scores.begin(), scores.end(), 0.0f);
  std::fill(outgoing.begin(), outgoing.end(), 0.0f);
  m5_work_begin(trial, 0);
  PageRankPullFixed(g, scores, outgoing);
  m5_work_end(trial, 0);
  if (trial == cli.num_trials() - 1) {
    const bool verification_passed =
        PRVerifier(g, scores, cli.tolerance());
    const char *marker = verification_passed ? "Verification: PASS\n" :
                                                "Verification: FAIL\n";
    write(2, marker, std::strlen(marker));
    if (!verification_passed)
      m5_fail(0, 1);
    WriteScoreBits(M2NDP_REFERENCE_RAW_PATH, scores);
  }
}
m5_exit(0);
```

`WriteScoreBits` writes little-endian `uint32_t` using `memcpy`, flushes,
calls `fsync`, and atomically renames `<path>.tmp` to the configured path.

- [ ] **Step 4: Implement the builder**

CLI:

```text
build_gapbs_m2ndp_pr_spmv.py
  --cxlmemuring PATH
  --outdir PATH
  --reference-raw PATH
  [--cxx g++]
```

The builder copies GAPBS source, writes
`generated/m2ndp_experiment_config.h`, and compiles:

```bash
g++ -std=c++11 -O3 -Wall -fopenmp -static -no-pie \
  -ffp-contract=off -fno-fast-math \
  -I <copied-gapbs>/src -I include -I <outdir>/generated \
  util/m2ndp/gapbs_pr_spmv_fixed.cc \
  util/m5/build/x86/out/libm5.a \
  -o <outdir>/bin/pr_spmv
```

Compile the exporter from Task 2 in the same build. Record hashes for every
copied `.h`/`.cc`, the generated header, all three binaries, the builder, m5
library, compiler version, and exact flags. Also build the copied GAPBS
`converter` target and copy it to `<outdir>/bin/converter`; record its hash.

- [ ] **Step 5: Run build tests**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_m2ndp_build -v
```

Expected: all tests pass.

- [ ] **Step 6: Perform a real local build**

```bash
python3 scripts/build_gapbs_m2ndp_pr_spmv.py \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --outdir m5out/m2ndp_pr_spmv_build_smoke \
  --reference-raw \
    m5out/m2ndp_pr_spmv_build_smoke/reference/scores.u32
```

Expected: `bin/pr_spmv`, `bin/export_gapbs_graph`, `bin/converter`, and
`manifest.json` exist;
`readelf -s bin/pr_spmv` shows `m5_work_begin`, `m5_work_end`, and `m5_exit`.

- [ ] **Step 7: Commit the matched baseline**

```bash
git add util/m2ndp/gapbs_pr_spmv_fixed.cc \
  scripts/build_gapbs_m2ndp_pr_spmv.py \
  tests/pyunit/m2ndp/test_m2ndp_build.py
git commit -m "m2ndp: build matched fixed-20 pr_spmv baseline"
```

### Task 4: Generate Native Four-Stage M2NDP Traces

**Files:**
- Create: `scripts/m2ndp_pagerank_trace.py`
- Create: `tests/pyunit/m2ndp/test_m2ndp_trace.py`

- [ ] **Step 1: Write failing map and kernel tests**

Use a three-node bundle and exact reference words. Require:

```python
class PageRankTraceTest(unittest.TestCase):
    def test_kernel_contract_and_launch_counts(self):
        result = trace.generate_trace(
            bundle=self.bundle,
            reference=self.reference,
            outdir=self.root / "trace",
            trials=2,
            iterations=20,
        )
        self.assertEqual(result.unique_kernels,
                         ("K0_INIT", "K1_META",
                          "K2_CONTRIB", "K3_PULL_DAMP"))
        self.assertEqual(result.funcsim_launches, 42)
        self.assertEqual(result.ndpsim_launches, 84)
        self.assertEqual(result.measure_marker, "K0_INIT_TRIAL1")

    def test_strict_kernel_forbids_reduction_and_fma(self):
        text = (self.root / "trace/0/K3_PULL_DAMP.traceg").read_text()
        self.assertIn("fadd", text)
        self.assertIn("fmul", text)
        self.assertNotIn("vfred", text)
        self.assertNotIn("vfmacc", text)

    def test_memory_map_round_trips_all_float_words(self):
        parsed = trace.parse_float32_map(
            self.root / "trace/0/K3_PULL_DAMP_output.data",
            trace.SCORES_ADDR,
            self.bundle.meta.num_nodes,
        )
        self.assertEqual(parsed, self.reference_words)
```

- [ ] **Step 2: Run tests and verify the generator is missing**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_m2ndp_trace -v
```

Expected: import failure naming `m2ndp_pagerank_trace.py`.

- [ ] **Step 3: Implement streaming memory-map output**

Use fixed addresses:

```python
IN_OFFSETS_ADDR = 0x8000_0000_0000
IN_NEIGHBORS_ADDR = 0x8100_0000_0000
OUT_DEGREE_ADDR = 0x8200_0000_0000
SCORES_ADDR = 0x8300_0000_0000
CONTRIB_ADDR = 0x8400_0000_0000
```

Write one `_META_`, type, `_DATA_` section per array. Emit eight 32-bit words
or four 64-bit words per 32-byte packet. For float values use
`format(value, ".9g")` and immediately parse them back with `float`; compare
`struct.pack("<f", parsed)` against the source word before writing.

Do not build a g20 map as one Python string. Stream to `.tmp`, `flush`,
`os.fsync`, then `os.replace`.

- [ ] **Step 4: Implement four scalar kernels**

Generate:

```text
K0_INIT.traceg       kernel id 0, initialize scores
K1_META.traceg       kernel id 1, validate/install integer metadata
K2_CONTRIB.traceg    kernel id 2, scalar fdiv score/out_degree
K3_PULL_DAMP.traceg  kernel id 3, ordered scalar fadd then fmul then fadd
```

`K3_PULL_DAMP` must use row start/end from `in_offsets.u64`, iterate neighbor
indices in increasing CSR offset, load one contribution at a time, and never
use a vector reduction. Its final arithmetic is:

```text
flw f1, contribution
fadd f0, f0, f1
...
fmul f0, f2, f0
fadd f0, f3, f0
fsw f0, score
```

Compute `init_score`, `base_score`, and damping through separate IEEE
single-precision operations matching the C++ source. Record their resulting
hex words in `trace.meta.json`.

- [ ] **Step 5: Implement launch sequences**

One functional trial contains:

```text
K0_INIT K1_META (K2_CONTRIB K3_PULL_DAMP) repeated 20 times
```

The timing `kernelslist.g` contains that sequence twice, using the alias
`K0_INIT_TRIAL1` for the second trial's first launch. Copy the K0 trace and
launch record under that alias while keeping kernel id 0.

Set derived `max_kernel_launch=128`, because two trials require 84 launches.
Generate:

- `funcsim.sequence`;
- `0/kernelslist.g`;
- one launch file per unique kernel/alias;
- `K0_INIT_input.data`;
- `K3_PULL_DAMP_output.data`; and
- `trace.meta.json` with hashes and launch counts.

- [ ] **Step 6: Run trace tests**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_m2ndp_trace -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit the generator**

```bash
git add scripts/m2ndp_pagerank_trace.py \
  tests/pyunit/m2ndp/test_m2ndp_trace.py
git commit -m "m2ndp: generate strict PageRank trace packages"
```

### Task 5: Patch and Prepare the Pinned M2NDP Checkout

**Files:**
- Create: `util/m2ndp/patches/0001-funcsim-strict-sequence.patch`
- Create: `scripts/prepare_m2ndp.py`
- Create: `tests/pyunit/m2ndp/test_prepare_m2ndp.py`

- [ ] **Step 1: Write failing pin and patch-state tests**

Mock git commands and require fail-closed behavior:

```python
class PrepareM2NDPTest(unittest.TestCase):
    def test_rejects_wrong_commit(self):
        with mock.patch.object(
            prepare, "git_output", return_value="deadbeef"
        ):
            with self.assertRaisesRegex(
                prepare.PrepareError, "expected M2NDP commit"
            ):
                prepare.validate_upstream(Path("/checkout"))

    def test_state_records_commit_and_patch_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            patch = root / "strict.patch"
            patch.write_bytes(b"patch")
            funcsim = root / "FuncSim"
            ndpsim = root / "NDPSim"
            cxl_probe = root / "M2NDPCXLProbe"
            for executable in (funcsim, ndpsim, cxl_probe):
                executable.write_bytes(b"binary")
                executable.chmod(0o755)
            state = prepare.build_state(
                root=root,
                commit=artifacts.EXPECTED_M2NDP_COMMIT,
                patch=patch,
                funcsim=funcsim,
                ndpsim=ndpsim,
                cxl_probe=cxl_probe,
            )
        self.assertEqual(
            state["upstream_commit"], artifacts.EXPECTED_M2NDP_COMMIT
        )
        self.assertEqual(len(state["patch_sha256"]), 64)
```

- [ ] **Step 2: Run tests and verify preparation code is missing**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_prepare_m2ndp -v
```

Expected: import failure naming `prepare_m2ndp.py`.

- [ ] **Step 3: Create the strict FuncSim patch**

Against upstream
`fe418e8c30d7c3821f7c91293c74c5c34939a063`, modify:

- `src/memory_map.h`;
- `src/memory_map.cc`;
- `functional_runner/main.cc`;
- `perf_runner/cxl_probe_main.cc`;
- `perf_runner/synthetic_traffic.h`;
- `perf_runner/synthetic_traffic.cc`; and
- `CMakeLists.txt`.

Add:

```cpp
struct ExactMatchResult {
  bool matched;
  uint64_t compared;
  uint64_t mismatched;
};

ExactMatchResult MatchFloat32Bits(
    MemoryMap &actual, uint64_t base, uint64_t count, FILE *report);
void DumpFloat32Bits(
    uint64_t base, uint64_t count, const std::string &path);
```

Compare each float with:

```cpp
uint32_t expected_bits;
uint32_t actual_bits;
std::memcpy(&expected_bits, &expected_value, sizeof(expected_bits));
std::memcpy(&actual_bits, &actual_value, sizeof(actual_bits));
const bool equal = expected_bits == actual_bits;
```

Add FuncSim options:

```text
--sequence_file PATH
--strict_float32_base HEX
--strict_float32_count INTEGER
--dump_float32_bits PATH
```

`--sequence_file` lines contain:

```text
TRACEG_PATH<TAB>LAUNCH_RECORD
```

Reuse one `HashMemoryMap` and execute every listed kernel in order. On success
print:

```text
M2NDP_STRICT_MODE=1
M2NDP_STRICT_COMPARED=<V>
M2NDP_STRICT_MISMATCHED=0
M2NDP_STRICT_MATCH=PASS
```

On any mismatch print `M2NDP_STRICT_MATCH=FAIL` and return exit status 2.
Missing strict arguments return exit status 64.

Expose the already-shipped `SyntheticTrafficRunner` through a dedicated
timing executable. `perf_runner/cxl_probe_main.cc` constructs exactly one
`SyntheticTrafficRunner`, and `CMakeLists.txt` adds
`M2NDPCXLProbe` only in `PERFORMANCE_BUILD=1`, linking it to `NDPSim_lib`.
Extend the runner with an explicit request-size option and accept this exact
CLI:

```text
M2NDPCXLProbe --config PATH --num_reqs 1 --request_bytes 64
```

Require `request_bytes` to be a positive power of two no larger than
`MAX_MEMORY_ACCESS_SIZE`, pass it as the `mem_fetch` payload size, and print
`M2NDP_CXL_PROBE request_bytes=64 requests=1` before the existing
`Memory request latency: <cycles>` marker. This keeps the probe payload
explicit regardless of which official packet-size variant is built.

- [ ] **Step 4: Implement the preparation script**

CLI:

```text
prepare_m2ndp.py
  --m2ndp-root PATH
  --tools-dir PATH
  --state PATH
  [--build]
```

Behavior:

1. require exact upstream commit;
2. permit only either a clean checkout or the exact already-applied patch;
3. run `git apply --check` then `git apply`;
4. when `--build` is set, run:

```bash
./scripts/build_functional.sh
install -m 0755 build/bin/FuncSim <tools-dir>/bin/FuncSim
./scripts/build_timing.sh
install -m 0755 build/bin/NDPSim <tools-dir>/bin/NDPSim
install -m 0755 build/bin/M2NDPCXLProbe \
  <tools-dir>/bin/M2NDPCXLProbe
```

The immediate copy after each build is mandatory because both official build
scripts reuse `build/`; the timing build replaces the functional build.

5. require all three executables in `<tools-dir>/bin`;
6. atomically write commit, patch hash, executable hashes, compiler versions,
   and build command evidence.

- [ ] **Step 5: Run preparation unit tests**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_prepare_m2ndp -v
```

Expected: all tests pass.

- [ ] **Step 6: Apply and build in a disposable pinned checkout**

```bash
git clone https://github.com/PSAL-POSTECH/M2NDP-public \
  /tmp/M2NDP-public-strict
git -C /tmp/M2NDP-public-strict checkout \
  fe418e8c30d7c3821f7c91293c74c5c34939a063
python3 scripts/prepare_m2ndp.py \
  --m2ndp-root /tmp/M2NDP-public-strict \
  --tools-dir /tmp/M2NDP-public-strict-tools \
  --state /tmp/M2NDP-public-strict-state.json \
  --build
```

Expected: both simulator binaries exist and the state file contains the exact
commit and patch hash; the state file also names the independently copied
CXL probe.

- [ ] **Step 7: Commit the upstream integration**

```bash
git add util/m2ndp/patches/0001-funcsim-strict-sequence.patch \
  scripts/prepare_m2ndp.py \
  tests/pyunit/m2ndp/test_prepare_m2ndp.py
git commit -m "m2ndp: enforce strict sequential FuncSim validation"
```

### Task 6: Parse FuncSim and NDPSim Evidence Fail-Closed

**Files:**
- Create: `scripts/m2ndp_results.py`
- Create: `tests/pyunit/m2ndp/test_m2ndp_results.py`

- [ ] **Step 1: Write failing parser and speedup tests**

Cover pass, missing marker, duplicate marker, reordered trial marker, nonzero
exit, wrong compared count, and a one-bit reference mismatch:

```python
class M2NDPResultTest(unittest.TestCase):
    def test_parse_strict_funcsim_pass(self):
        log = "\n".join([
            "M2NDP_STRICT_MODE=1",
            "M2NDP_STRICT_COMPARED=3",
            "M2NDP_STRICT_MISMATCHED=0",
            "M2NDP_STRICT_MATCH=PASS",
        ])
        evidence = results.parse_funcsim(log, returncode=0,
                                         expected_count=3)
        self.assertTrue(evidence.passed)

    def test_parse_ndpsim_uses_trial_one_only(self):
        log = "\n".join([
            "Launching NDP kernel: /t/K0_INIT at cycle 10",
            "Launching NDP kernel: /t/K0_INIT_TRIAL1 at cycle 110",
            "EXPR FINISHED 350",
        ])
        evidence = results.parse_ndpsim(log)
        self.assertEqual(evidence.start_cycle, 110)
        self.assertEqual(evidence.end_cycle, 350)
        self.assertEqual(evidence.measured_cycles, 240)

    def test_speedup_is_suppressed_when_funcsim_fails(self):
        with self.assertRaisesRegex(
            artifacts.EvidenceError, "FuncSim"
        ):
            results.build_summary(
                gem5=self.gem5_pass,
                funcsim=self.funcsim_fail,
                ndpsim=self.ndpsim_pass,
                calibration=self.calibration_pass,
            )
```

- [ ] **Step 2: Run tests and verify result code is missing**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_m2ndp_results -v
```

Expected: import failure naming `m2ndp_results.py`.

- [ ] **Step 3: Implement strict parsers**

Define immutable evidence types:

```python
@dataclasses.dataclass(frozen=True)
class FuncSimEvidence:
    passed: bool
    compared: int
    mismatched: int
    dump_sha256: str

@dataclasses.dataclass(frozen=True)
class NDPSimEvidence:
    start_cycle: int
    end_cycle: int
    measured_cycles: int
    core_period_seconds: decimal.Decimal
```

`parse_funcsim` requires each marker exactly once, return code zero, expected
count, zero mismatches, and PASS.

`parse_ndpsim` requires exactly one
`K0_INIT_TRIAL1 ... cycle <N>` launch, exactly one `EXPR FINISHED <N>`,
positive `end-start`, and a core period parsed from `output.out` or the exact
derived configuration. Never use a hardcoded `0.5ns`.

- [ ] **Step 4: Implement gem5/reference and summary gates**

Parse the one-row baseline `summary.csv` and require:

```python
benchmark == "pr_spmv"
kind == "baseline"
status == "ok"
verification == "pass"
roi_cpu == "timing"
cores == 2
cxl_link_delay == "1us"
all_memory_cxl == "True"
graph_sha256 == EXPECTED_G20_SHA256
iterations == 2
measured_trial == 1
checkpoint_restores == 1
```

Use:

```python
gem5_seconds = Decimal(sim_ticks) / Decimal(10**12)
m2ndp_seconds = Decimal(measured_cycles) * core_period_seconds
speedup = gem5_seconds / m2ndp_seconds
```

Only return a summary row after graph, binary, trace, patch, config,
calibration, FuncSim, reference, and NDPSim evidence all validate.

- [ ] **Step 5: Run result tests**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_m2ndp_results -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit result validation**

```bash
git add scripts/m2ndp_results.py \
  tests/pyunit/m2ndp/test_m2ndp_results.py
git commit -m "m2ndp: gate timing results on strict evidence"
```

### Task 7: Calibrate M2NDP to gem5's `1us` CXL Condition

**Files:**
- Create: `util/m2ndp/cxl_latency_probe.c`
- Create: `scripts/calibrate_m2ndp_cxl.py`
- Create: `tests/pyunit/m2ndp/test_m2ndp_calibration.py`

- [ ] **Step 1: Write failing calibration tests**

Use a fake monotonic simulator callback:

```python
class CalibrationTest(unittest.TestCase):
    def test_selects_closest_link_latency_within_one_clock(self):
        def simulate(link_latency):
            return Decimal("100.0") + Decimal(link_latency) * Decimal("0.125")
        result = calibration.search_link_latency(
            target_ns=Decimal("1100.0"),
            link_period_ns=Decimal("0.125"),
            simulate=simulate,
            low=0,
            high=10000,
        )
        self.assertEqual(result.link_latency, 8000)
        self.assertLessEqual(
            abs(result.measured_ns - result.target_ns),
            result.link_period_ns,
        )

    def test_rejects_default_35ns_as_1us(self):
        with self.assertRaisesRegex(
            calibration.CalibrationError, "outside one link clock"
        ):
            calibration.require_residual(
                target_ns=Decimal("1000"),
                measured_ns=Decimal("35"),
                link_period_ns=Decimal("0.125"),
            )
```

- [ ] **Step 2: Run tests and verify calibration code is missing**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_m2ndp_calibration -v
```

Expected: import failure naming `calibrate_m2ndp_cxl.py`.

- [ ] **Step 3: Implement the gem5 probe**

Allocate and align 64 bytes, touch it before the ROI, flush the cache line,
issue memory fences, then time one volatile load:

```c
_Alignas(64) static volatile uint64_t line[8];

int main(void) {
    line[0] = 0x123456789abcdef0ULL;
    _mm_clflush((const void *)&line[0]);
    _mm_mfence();
    m5_work_begin(0, 0);
    volatile uint64_t value = line[0];
    _mm_mfence();
    m5_work_end(0, 0);
    return value == 0x123456789abcdef0ULL ? 0 : 1;
}
```

Build statically with libm5. Run it through
`x86-gapbs-amu-se.py` with one Timing core, all CXL memory, `1us`, and
ROI work events. Record `simTicks` and the directional 64-byte CXL request.

- [ ] **Step 4: Implement M2NDP configuration derivation**

Parse `freq=` and `link_latency =` without regex replacement across unrelated
lines. Copy the official `config/performance/M2NDP` directory into the run's
calibration directory and modify only:

- `cxl_link.icnt: link_latency`;
- `m2ndp.config: max_kernel_launch=128`; and
- relative config paths needed for the copied directory.

Invoke the pinned timing probe for each candidate:

```bash
<tools-dir>/bin/M2NDPCXLProbe \
  --config <calibration-dir>/config/m2ndp.config \
  --num_reqs 1 \
  --request_bytes 64
```

Require exactly one `M2NDP_CXL_PROBE request_bytes=64 requests=1` marker and
one `Memory request latency: <cycles>` marker. Convert its
M2NDP core cycles with the core frequency parsed from the first
`freq=` component in the copied `m2ndp.config`; convert `link_latency` with
the fourth component, the 8 GHz link clock. Reject a zero request count,
duplicate marker, nonpositive latency, or missing frequency component.

Use integer binary search over `link_latency`, write every sample to
`samples.csv`, and require residual error within one 8GHz link period.

- [ ] **Step 5: Run calibration unit tests**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_m2ndp_calibration -v
```

Expected: all tests pass.

- [ ] **Step 6: Run a real smoke calibration**

```bash
python3 scripts/calibrate_m2ndp_cxl.py \
  --gem5 build/X86/gem5.opt \
  --m2ndp-root /tmp/M2NDP-public-strict \
  --m2ndp-tools /tmp/M2NDP-public-strict-tools \
  --outdir m5out/m2ndp_calibration_smoke \
  --cxl-delay 1us
```

Expected: `calibration.json` reports `passed: true`, residual no larger than
one link clock, and selected latency differs from the official default 274.

- [ ] **Step 7: Commit calibration tooling**

```bash
git add util/m2ndp/cxl_latency_probe.c \
  scripts/calibrate_m2ndp_cxl.py \
  tests/pyunit/m2ndp/test_m2ndp_calibration.py
git commit -m "m2ndp: calibrate host link to gem5 1us CXL"
```

### Task 8: Build the Resumable End-to-End Orchestrator

**Files:**
- Create: `scripts/run_m2ndp_g20_pr_spmv.py`
- Create: `tests/pyunit/m2ndp/test_run_m2ndp_g20_pr_spmv.py`

- [ ] **Step 1: Write failing stage and publication tests**

Mock subprocess calls and verify exact command contracts:

```python
class OrchestratorTest(unittest.TestCase):
    def test_publication_command_is_two_core_all_cxl_trial_one(self):
        command = runner.gem5_command(self.options, self.paths)
        self.assertIn("--benchmarks", command)
        self.assertIn("pr_spmv", command)
        self.assertIn("--cores", command)
        self.assertIn("2", command)
        self.assertIn("--cxl-link-delay", command)
        self.assertIn("1us", command)
        self.assertIn("--iterations", command)
        self.assertIn("--measure-trial", command)
        self.assertIn("--checkpoint-root", command)

    def test_failed_funcsim_blocks_ndpsim_and_summary(self):
        state = self.state_with("funcsim", status="failed")
        with self.assertRaisesRegex(
            artifacts.EvidenceError, "FuncSim"
        ):
            runner.next_stage(state)
        self.assertFalse((self.outdir / "summary.csv").exists())

    def test_resume_does_not_repeat_hashed_passed_stage(self):
        state = self.state_with(
            "graph_export",
            status="passed",
            outputs={"graph.meta.json": self.meta_hash},
        )
        self.assertFalse(runner.should_run("graph_export", state,
                                           self.outdir))
```

- [ ] **Step 2: Run tests and verify the runner is missing**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_run_m2ndp_g20_pr_spmv -v
```

Expected: import failure naming `run_m2ndp_g20_pr_spmv.py`.

- [ ] **Step 3: Implement the CLI and immutable run contract**

CLI:

```text
run_m2ndp_g20_pr_spmv.py
  --graph PATH
  --graph-scale 20
  --cxlmemuring PATH
  --m2ndp-root PATH
  --gem5 PATH
  --outdir PATH
  [--smoke-test]
  [--resume]
  [--timeout 0]
  [--stop-after STAGE]
```

Hardcode publication values: benchmark `pr_spmv`, PageRank iterations 20,
trials 2, measured trial 1, Timing CPU, cores 2, all-CXL, and `1us`.
Reject CLI attempts to weaken these values outside `--smoke-test`.

- [ ] **Step 4: Implement atomic stage state**

Stages:

```python
STAGES = (
    "prepare_m2ndp",
    "build_gapbs",
    "graph_export",
    "gem5_baseline",
    "reference_pack",
    "trace_generate",
    "funcsim",
    "calibration",
    "ndpsim",
    "publish",
)
```

Each stage records:

```json
{
  "status": "pending|running|passed|failed",
  "started_at": "UTC ISO-8601",
  "finished_at": "UTC ISO-8601",
  "command": ["argv"],
  "returncode": 0,
  "inputs": {"path": "sha256"},
  "outputs": {"path": "sha256"},
  "log": "relative/path.log"
}
```

Before resume, rehash all inputs/outputs. A mismatch invalidates that stage and
every downstream stage.

The `prepare_m2ndp` stage invokes `prepare_m2ndp.py` with
`--tools-dir <run>/tools`; every later stage must use only those hashed copies,
never whichever executable happens to remain under the upstream checkout's
shared `build/` directory.

- [ ] **Step 5: Implement gem5/reference stages**

Invoke the matched builder with a run-specific raw output path. Invoke the
existing checkpoint runner:

```bash
python3 scripts/compare_gapbs_cxl_amu_cira.py \
  --baseline-bin-dir <run>/build/bin \
  --benchmarks pr_spmv \
  --graph <g20.sg> --graph-scale 20 \
  --iterations 2 --measure-trial 1 \
  --cpu timing --cores 2 \
  --checkpoint-root <run>/gem5/checkpoints \
  --cxl-link-delay 1us \
  --roi-work-events --verify --timeout 0 \
  --outdir <run>/gem5/run
```

After validating the gem5 row and raw word count, create the `.m2pr` reference
container with graph, source, binary, trial, and iteration evidence.

- [ ] **Step 6: Implement FuncSim and NDPSim stages**

FuncSim (using the immutable copies produced by `prepare_m2ndp.py`):

```bash
<run>/tools/bin/FuncSim \
  --sequence_file <run>/trace/funcsim.sequence \
  --memory_map <run>/trace/0/K0_INIT_input.data \
  --target_map <run>/trace/0/K3_PULL_DAMP_output.data \
  --config <run>/trace/functional.config \
  --strict_float32_base 0x830000000000 \
  --strict_float32_count <V> \
  --dump_float32_bits <run>/funcsim/scores.u32
```

Validate strict markers and independently compare the raw dump with the gem5
reference before starting NDPSim.

NDPSim:

```bash
<run>/tools/bin/NDPSim \
  --trace <run>/trace/0 \
  --num_hosts 1 --num_m2ndps 1 \
  --config <run>/calibration/config/m2ndp.config \
  --synthetic_memory false --serial_launch true
```

Use no timeout when `--timeout 0`. Parse trial-1 cycles only.

- [ ] **Step 7: Implement final publication**

Call `m2ndp_results.build_summary`, atomically write one `summary.csv` row,
and write `manifest.json` containing hashes for:

- gem5 repository and binary;
- copied GAPBS source and matched source;
- graph and CSR bundle;
- M2NDP upstream, patch, binaries, traces, and configs;
- reference and strict dump;
- calibration;
- gem5/FuncSim/NDPSim logs; and
- final summary.

On failure, preserve logs and write `status.json`, but ensure
`summary.csv` does not exist.

- [ ] **Step 8: Run orchestrator tests**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_run_m2ndp_g20_pr_spmv -v
```

Expected: all tests pass.

- [ ] **Step 9: Commit the orchestrator**

```bash
git add scripts/run_m2ndp_g20_pr_spmv.py \
  tests/pyunit/m2ndp/test_run_m2ndp_g20_pr_spmv.py
git commit -m "m2ndp: orchestrate bit-exact g20 speedup runs"
```

### Task 9: Prove Small-Graph Bit Exactness and Fault Rejection

**Files:**
- Modify: `tests/pyunit/m2ndp/test_run_m2ndp_g20_pr_spmv.py`
- Generated only: `m5out/m2ndp_pr_smoke/`

- [ ] **Step 1: Create a deterministic directed graph**

```bash
mkdir -p m5out/m2ndp_pr_smoke/input
printf '%s\n' \
  '0 1' '1 2' '2 0' '2 3' '3 1' \
  > m5out/m2ndp_pr_smoke/input/tiny.el
m5out/m2ndp_pr_spmv_build_smoke/src/gapbs/converter \
  -f m5out/m2ndp_pr_smoke/input/tiny.el \
  -b m5out/m2ndp_pr_smoke/input/tiny.sg
```

Expected: `tiny.sg` is nonempty.

- [ ] **Step 2: Run the full smoke pipeline**

```bash
python3 scripts/run_m2ndp_g20_pr_spmv.py \
  --graph m5out/m2ndp_pr_smoke/input/tiny.sg \
  --graph-scale 2 \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --m2ndp-root /tmp/M2NDP-public-strict \
  --gem5 build/X86/gem5.opt \
  --outdir m5out/m2ndp_pr_smoke/run \
  --smoke-test --timeout 0
```

Expected:

```text
GAPBS verification: pass
M2NDP_STRICT_MATCH=PASS
M2NDP strict mismatches: 0
NDPSim measured cycles: positive
```

and one `summary.csv` row.

- [ ] **Step 3: Inject a one-bit target error**

Copy the passed run, flip bit zero in the first target PageRank word using the
artifact module's test helper, and resume from FuncSim:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_run_m2ndp_g20_pr_spmv.\
OrchestratorIntegrationTest.test_one_bit_fault_blocks_speedup -v
```

Expected: FuncSim exits 2, reports one mismatch, NDPSim is not invoked, and
`summary.csv` is absent.

- [ ] **Step 4: Run all M2NDP and existing AMU tests**

```bash
python3 -m unittest discover \
  -s tests/pyunit/m2ndp -p 'test_*.py'
python3 -m unittest discover \
  -s tests/pyunit/amu -p 'test_*.py'
```

Expected: all M2NDP tests pass and the existing 115 AMU tests pass.

- [ ] **Step 5: Commit the integration proof**

```bash
git add tests/pyunit/m2ndp/test_run_m2ndp_g20_pr_spmv.py
git commit -m "tests: prove M2NDP PageRank bit-exact gate"
```

### Task 10: Document the Reproducible Flow

**Files:**
- Modify: `docs/amu-gapbs-benchmark.md`

- [ ] **Step 1: Add the exact M2NDP commands and proof boundary**

Document:

- pinned M2NDP commit and patch behavior;
- matched fixed-20 `pr_spmv`;
- graph hash;
- trial-0 warmup/trial-1 timing;
- link calibration;
- strict FuncSim markers;
- full runner command;
- resume command; and
- why raw packet traces are not M2NDP application traces.

Include:

```bash
python3 scripts/run_m2ndp_g20_pr_spmv.py \
  --graph m5out/gapbs_graphs/g20.sg \
  --graph-scale 20 \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --m2ndp-root m5out/m2ndp/source \
  --gem5 build/X86/gem5.opt \
  --outdir m5out/m2ndp_g20_pr_spmv/g20-1us \
  --timeout 0
```

State that speedup is invalid unless `verification=pass`,
`funcsim_strict=pass`, `funcsim_mismatches=0`, and
`calibration_status=pass`.

- [ ] **Step 2: Validate documentation commands through dry-run**

```bash
python3 scripts/run_m2ndp_g20_pr_spmv.py \
  --graph m5out/gapbs_graphs/g20.sg \
  --graph-scale 20 \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --m2ndp-root /tmp/M2NDP-public-strict \
  --gem5 build/X86/gem5.opt \
  --outdir /tmp/m2ndp-g20-dry-run \
  --stop-after graph_export --timeout 0
```

Expected: preparation/build/export pass, graph hash matches, and no timing
summary is emitted.

- [ ] **Step 3: Commit documentation**

```bash
git add docs/amu-gapbs-benchmark.md
git commit -m "docs: describe M2NDP g20 PageRank evaluation"
```

### Task 11: Run and Validate Full g20 in the Background

**Files:**
- Generated only: `m5out/m2ndp_g20_pr_spmv/g20-1us/`

- [ ] **Step 1: Confirm the existing gem5 g20 service remains untouched**

```bash
systemctl show gapbs-g20-pr-1us-20260724.service \
  -p ActiveState -p SubState -p MainPID -p InvocationID
```

Record the result in the new run's launch note. Do not stop or restart that
service.

- [ ] **Step 2: Prepare the pinned external checkout**

```bash
git clone https://github.com/PSAL-POSTECH/M2NDP-public \
  m5out/m2ndp/source
git -C m5out/m2ndp/source checkout \
  fe418e8c30d7c3821f7c91293c74c5c34939a063
python3 scripts/prepare_m2ndp.py \
  --m2ndp-root m5out/m2ndp/source \
  --tools-dir m5out/m2ndp_g20_pr_spmv/g20-1us/tools \
  --state m5out/m2ndp_g20_pr_spmv/g20-1us/prepare/state.json \
  --build
```

Expected: strict FuncSim, NDPSim, and M2NDPCXLProbe binaries with recorded
hashes.

- [ ] **Step 3: Launch the no-timeout pipeline as a service**

```bash
systemd-run \
  --unit=m2ndp-g20-pr-spmv-1us-20260724 \
  --property=WorkingDirectory=$PWD \
  --property=RuntimeMaxSec=infinity \
  /usr/bin/python3 scripts/run_m2ndp_g20_pr_spmv.py \
    --graph m5out/gapbs_graphs/g20.sg \
    --graph-scale 20 \
    --cxlmemuring /home/victoryang00/CXLMemUring \
    --m2ndp-root m5out/m2ndp/source \
    --gem5 build/X86/gem5.opt \
    --outdir m5out/m2ndp_g20_pr_spmv/g20-1us \
    --timeout 0 --resume
```

Expected: unit enters `active/running`; no `RuntimeMaxUSec` limit is set.

- [ ] **Step 4: Monitor stage evidence**

```bash
systemctl show m2ndp-g20-pr-spmv-1us-20260724.service \
  -p ActiveState -p SubState -p MainPID -p ExecMainStatus -p InvocationID
journalctl -u m2ndp-g20-pr-spmv-1us-20260724.service -n 80 --no-pager
python3 -m json.tool \
  m5out/m2ndp_g20_pr_spmv/g20-1us/status.json
```

Expected: stages move monotonically from running to passed; unchanged state
during a long FuncSim/NDPSim run is not treated as failure.

- [ ] **Step 5: Validate the final evidence**

After the unit exits successfully:

```bash
python3 scripts/run_m2ndp_g20_pr_spmv.py \
  --graph m5out/gapbs_graphs/g20.sg \
  --graph-scale 20 \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --m2ndp-root m5out/m2ndp/source \
  --gem5 build/X86/gem5.opt \
  --outdir m5out/m2ndp_g20_pr_spmv/g20-1us \
  --timeout 0 --resume
```

Expected validation-only resume confirms:

```text
graph_sha256=ce900a7147a073835a7450e8f1afedf9f13db6833652bf2f9647819be26bedb3
verification=pass
funcsim_strict=pass
funcsim_compared=<g20 vertex count>
funcsim_mismatches=0
calibration_status=pass
ndpsim_measured_cycles>0
speedup>0
```

- [ ] **Step 6: Run final repository verification**

```bash
python3 -m unittest discover \
  -s tests/pyunit/m2ndp -p 'test_*.py'
python3 -m unittest discover \
  -s tests/pyunit/amu -p 'test_*.py'
git diff --check
git status --short
```

Expected: all tests pass, `git diff --check` is silent, and only ignored
`m5out` artifacts are untracked.

- [ ] **Step 7: Request code review before local merge**

Use the `superpowers:requesting-code-review` skill against the full branch
diff. Resolve every Critical or Important finding, rerun Task 11 Step 6, and
only then use `superpowers:finishing-a-development-branch` to offer the local
merge into `codex-gem5-cira-amu-eval`.
