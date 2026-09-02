#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
#
# Execute a CIRA JIT-generated .vxbin through Vortex's gem5 hosted runtime.
#
# The artifact remains an explicit input: generate it with CIRA's
# cira_vortex_jit_compile path (or cira_jit_steady_state preflight) first.
# This runner then compiles the small simulated-host dispatcher, maps Vortex's
# CP register file and VRAM BAR in gem5, uploads that artifact, launches it,
# waits for its completion event, and prints Vortex performance counters.

set -euo pipefail

usage() {
    cat >&2 <<'EOF'
Usage: run_cira_vortex_hosted_dispatch.sh <artifact.vxbin> <edge-count>

Required environment:
  CIRA_GEM5_HOME           gem5 checkout containing this script
  CIRA_VORTEX_HOME         Vortex SDK checkout
  CIRA_VORTEX_BUILD        configured Vortex build directory
  CIRA_VORTEX_HOSTED_OUT   persistent output directory for executable and gem5 files

The Vortex gem5 device stack must first be built with:
  scripts/build_vortex_gem5.sh
EOF
    exit 2
}

if [ "$#" -ne 2 ]; then
    usage
fi

ARTIFACT="$1"
EDGE_COUNT="$2"
GEM5_HOME="${CIRA_GEM5_HOME:-}"
VORTEX_HOME="${CIRA_VORTEX_HOME:-}"
VORTEX_BUILD="${CIRA_VORTEX_BUILD:-}"
OUTDIR="${CIRA_VORTEX_HOSTED_OUT:-}"

for required in "$ARTIFACT" "$GEM5_HOME" "$VORTEX_HOME" "$VORTEX_BUILD" "$OUTDIR"; do
    if [ -z "$required" ]; then
        usage
    fi
done

if [ ! -f "$ARTIFACT" ]; then
    echo "missing CIRA JIT artifact: $ARTIFACT" >&2
    exit 2
fi
if [ ! -f "$GEM5_HOME/util/cira/cira_vortex_hosted_dispatch.cc" ]; then
    echo "CIRA hosted dispatcher source is missing from $GEM5_HOME" >&2
    exit 2
fi
if [ ! -x "$GEM5_HOME/build/X86/gem5.opt" ]; then
    echo "Vortex-enabled gem5 binary is missing: $GEM5_HOME/build/X86/gem5.opt" >&2
    exit 2
fi

DEVICE_LIBRARY="$VORTEX_BUILD/sim/simx/libvortex-gem5.so"
RUNTIME_DIR="$VORTEX_BUILD/sw/runtime"
HOST_RUNTIME="$RUNTIME_DIR/libvortex.so"
HOST_BACKEND="$RUNTIME_DIR/libvortex-gem5-x86_64.so"
for required in "$DEVICE_LIBRARY" "$HOST_RUNTIME" "$HOST_BACKEND" \
                "$VORTEX_HOME/sw/runtime/include/vortex.h" \
                "$VORTEX_HOME/ci/gem5_run_app.py"; do
    if [ ! -e "$required" ]; then
        echo "required Vortex component is missing: $required" >&2
        exit 2
    fi
done

mkdir -p "$OUTDIR"
HOST_APP="$OUTDIR/cira-vortex-hosted-dispatch"
g++ -std=c++17 -O2 \
    -I"$VORTEX_HOME/sw/runtime/include" \
    "$GEM5_HOME/util/cira/cira_vortex_hosted_dispatch.cc" \
    -L"$RUNTIME_DIR" -Wl,-rpath,"$RUNTIME_DIR" -lvortex \
    -o "$HOST_APP"

RUN_DIR="$OUTDIR/gem5-run"
mkdir -p "$RUN_DIR"
cd "$RUN_DIR"
exec env \
    VORTEX_GEM5_DEV_LIB="$DEVICE_LIBRARY" \
    VORTEX_GEM5_HOST_RT_DIR="$RUNTIME_DIR" \
    VORTEX_TEST_DIR="$OUTDIR" \
    VORTEX_TEST_BIN="$(basename "$HOST_APP")" \
    VORTEX_TEST_ARGS="$ARTIFACT $EDGE_COUNT" \
    VORTEX_DRIVER=gem5-x86_64 \
    "$GEM5_HOME/build/X86/gem5.opt" --outdir=. \
    "$VORTEX_HOME/ci/gem5_run_app.py"
