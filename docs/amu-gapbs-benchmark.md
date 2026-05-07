# AMU GAPBS Benchmarking

The paper's AMU/AMI evaluation compares a no-architecture-change baseline
against GAPBS-style workloads rewritten to issue asynchronous memory operations.
For a valid timing comparison, the AMU side needs both pieces:

* a timing AMU/ASMC microarchitecture model in gem5, including SPM capacity,
  request queues, completion queue, and L2/cache integration; and
* AMU-aware GAPBS binaries that use `aload`, `astore`, and `getfin`.

The current tree has an X86-callable functional AMU m5op path. That is useful
for software bring-up, but it is not the full timing model from the paper and
stock GAPBS resources do not issue AMU operations.

## Build

Use the system `protoc` when building this tree. The conda `protoc` may be
newer than the system protobuf headers.

```sh
/usr/bin/protoc --cpp_out build/X86/proto --proto_path src/proto \
    src/proto/packet.proto src/proto/inst.proto src/proto/inst_dep_record.proto
scons build/X86/gem5.opt -j4 PROTOC=/usr/bin/protoc
```

## Run

Use `scripts/benchmark_gapbs_amu.py` to run matched baseline and AMU
simulations and write `summary.csv`.

```sh
scripts/benchmark_gapbs_amu.py \
    --benchmark gapbs-bfs-test \
    --baseline-config configs/example/gem5_library/x86-gapbs-benchmarks.py \
    --amu-config configs/example/gem5_library/x86-gapbs-amu-benchmarks.py
```

The reported speedup is:

```text
baseline ROI simTicks / AMU ROI simTicks
```

For a command smoke test only, pass the same config twice with
`--allow-same-config --dry-run`; do not use that as a performance result.
