#!/bin/bash
set -euo pipefail

repo=/home/victoryang00/gem5-CXL/.worktrees/evidence-24cell-timing-contract
runner="$repo/scripts/run_g12_m2ndp_graph_spectrum.py"
output=/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/shared/g12-graph-m2ndp-r1
failed="$output/failed-attempts"

export PYTHONPATH="$repo"
export OPENBLAS_NUM_THREADS=1
export OMP_NUM_THREADS=1

mkdir -p "$failed"
python3 "$runner" --check

for workload in pr_spmv gap_bc; do
    for latency in 200ns 500ns 1us 2us; do
        cell="$output/$workload/$latency"
        if [ -f "$cell/ndpsim-evidence.json" ]; then
            if python3 - "$output" "$workload" "$latency" <<'PY'
import sys
from pathlib import Path
from scripts import run_g12_m2ndp_graph_spectrum as runner
runner.read_cell(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
PY
            then
                echo "G12_GRAPH_SPECTRUM_CELL_ALREADY_PASS workload=$workload latency=$latency"
                continue
            fi
        fi
        if [ -d "$cell" ]; then
            stamp=$(date -u +%Y%m%dT%H%M%SZ)
            mv "$cell" "$failed/${workload}-${latency}-${stamp}"
        fi
        python3 "$runner" \
            --output "$output" \
            --workload "$workload" \
            --latency "$latency"
    done
done

python3 "$repo/scripts/publish_g12_fast_spectrum.py" \
    --source "$output" \
    --output "$output/publication"
python3 "$repo/scripts/generate_g12_m2ndp_latency_spectrum.py" \
    --input "$output/publication/g12-m2ndp-measured-raw.csv" \
    --output "$output/publication/g12-m2ndp-latency-spectrum"
echo G12_GRAPH_M2NDP_ALL_PASS
