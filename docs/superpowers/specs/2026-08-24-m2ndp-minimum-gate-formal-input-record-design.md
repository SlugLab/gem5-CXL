# M2NDP Minimum-Performance Gate and Formal Input Record Design

## Decision

The measured M2NDP PageRank result of `2.6342721382289415x` is accepted as
valid evidence.  M2NDP is no longer subject to the historical `1.6x` maximum
speedup bound.  Its formal acceptance rule is instead:

1. the shared PageRank output must pass the existing bit-exact verification;
2. M2NDP FuncSim must compare every output element with zero mismatches before
   NDPSim starts; and
3. the independently recomputed speedup must be at least `1.4x`.

AMU and CIRA retain the inclusive `1.4x <= speedup <= 1.6x` performance gate.
This change applies both to the replayed g12 qualification and to the nine
g12/g14/g20 accelerated points in the formal scaling campaign.

## Gate representation

A shared helper in `scripts/pr_offload_contract.py` will own the per-system
policy so qualification, complete-evidence validation, the scaling runner, and
artifact validation cannot silently diverge.  The policies are:

| System | Minimum | Maximum | Correctness prerequisite |
|---|---:|---:|---|
| AMU | 1.4x | 1.6x | existing bit-exact point validation |
| CIRA variants | 1.4x | 1.6x | existing bit-exact point validation |
| M2NDP | 1.4x | none | bit-exact point validation plus ordered FuncSim proof |

Generated gate evidence will state the policy used for each checked point.
For a minimum-only M2NDP row, `maximum` is JSON `null`; it must not contain a
sentinel number or an implied `1.6x` cap.  A speedup above `1.6x` is therefore
accepted only for M2NDP.  Unknown systems fail closed.

The error and hold descriptions will refer to a system-specific performance
policy rather than claiming that every accelerator must lie in the old
`1.4x--1.6x` interval.

## Tests and fresh qualification

Tests will be written before implementation and will prove all of these
boundaries:

- AMU and CIRA accept exactly `1.4x` and `1.6x` and reject values on either
  side;
- M2NDP accepts exactly `1.4x` and values above `1.6x`, including the measured
  `2.6342721382289415x`;
- M2NDP rejects values below `1.4x`;
- no performance result can bypass the existing correctness and FuncSim
  validation;
- g12 qualification and the nine-point scaling gate use the same policy; and
- stored gate evidence and generated artifacts reject a changed or missing
  policy description.

After unit validation, qualification will run in a fresh evidence root.  Old
hold, qualification, point, checkpoint, and timing files will not be reused.
The primary and replay passes must both reproduce bit-exact outputs and native
timing before the new result is called accepted.

## Formal workload input record

The accepted input record remains external evidence at:

`/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/paper-input-record.json`

It is created only when `scripts/freeze_cross_system_inputs.py` validates all
six workloads against live files.  An incomplete template or discovery result
must never be written at that accepted path.

The already located formal PageRank graph candidate is:

- absolute path:
  `/home/victoryang00/gem5-CXL/.worktrees/gapbs-latency-table/m5out/gapbs_graphs/g20.sg`
- allocated file size: `133986161` bytes
- SHA-256:
  `ce900a7147a073835a7450e8f1afedf9f13db6833652bf2f9647819be26bedb3`
- scale: `20`
- vertices: `1048576`
- directed edges: `31399382`

The associated checked manifest is:

`/mnt/disk0/gem5-CXL-eval/pr-scaling-120b389653d8/graphs/g20.manifest.json`

The strict paper record must bind these fields:

| Workload | Required bindings |
|---|---|
| `pr_spmv` | graph path/hash, allocated bytes, scale 20 |
| `mcf` | non-synthetic input path/hash, source path/hash, allocated bytes |
| `amg_gather` | data path/hash, index path/hash, at least 1 GiB allocated |
| `lulesh_scatter` | data path/hash, index path/hash, at least 1 GiB allocated |
| `npb_cg` | clean source root, exact commit, parameter file/hash, class, at least 12.8 GB allocated |
| `npb_mg` | clean source root, exact commit, parameter file/hash, class, at least 12.8 GB allocated |

Discovery may inspect existing MCF and NPB candidates, but a source tree,
binary, or similarly named file is not automatically formal paper input.
AMG/LULESH data and index arrays must be identified explicitly.  Every path
must be resolved and absolute, every file hash must be recomputed, and both NPB
trees must be clean at the recorded commit.

Two non-accepted records will make missing provenance actionable:

1. `paper-input-discovery.json` records candidate paths, observed hashes and
   sizes, and precise rejection or missing-field reasons; and
2. `paper-input-record.template.json` shows the complete required schema with
   unmistakable placeholders.

Neither file may use `status: accepted`, feed a formal campaign, or replace the
validated `paper-input-record.json`.  The existing `failed-input.json` remains
the terminal breadth-campaign result until the accepted record exists.

## Completion boundary

This change is complete only when:

1. all affected gate and artifact tests pass;
2. a fresh g12 primary-and-replay qualification accepts the measured M2NDP
   performance under the minimum-only policy;
3. all outputs remain bit exact and M2NDP FuncSim ordering is preserved;
4. the discovery and template records are available at stable absolute paths;
5. `paper-input-record.json` is created only if all six live inputs validate;
   otherwise the exact missing bindings are reported; and
6. the committed implementation excludes the user's unrelated
   `src/mem/cache/base.cc` modification.
