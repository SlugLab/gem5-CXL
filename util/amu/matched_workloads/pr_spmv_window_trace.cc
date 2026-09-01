// Copyright (c) 2026
// SPDX-License-Identifier: BSD-3-Clause

// Materialize a bounded, bit-exact pull-PageRank window from frozen CSR files.

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <stdexcept>
#include <string>
#include <vector>

#include <omp.h>

#include "util/amu/matched_workloads/canonical_trace.hh"

using matched_trace::Opcode;
using matched_trace::TraceRecord;

namespace
{

constexpr uint16_t Phase = 0;
constexpr uint64_t InOffsets = UINT64_C(0x800000000000);
constexpr uint64_t InNeighbors = UINT64_C(0x810000000000);
constexpr uint64_t Contributions = UINT64_C(0x840000000000);
constexpr uint64_t Scores = UINT64_C(0x850000000000);

template <typename T>
std::vector<T>
readArray(const std::string &path)
{
    std::ifstream stream(path, std::ios::binary | std::ios::ate);
    if (!stream)
        throw std::runtime_error("cannot open CSR array: " + path);
    const auto bytes = stream.tellg();
    if (bytes < 0 || static_cast<uint64_t>(bytes) % sizeof(T))
        throw std::runtime_error("CSR array size is invalid: " + path);
    std::vector<T> values(static_cast<uint64_t>(bytes) / sizeof(T));
    stream.seekg(0);
    stream.read(reinterpret_cast<char *>(values.data()), bytes);
    if (!stream)
        throw std::runtime_error("cannot read CSR array: " + path);
    return values;
}

uint32_t
bits(float value)
{
    uint32_t result;
    std::memcpy(&result, &value, sizeof(result));
    return result;
}

void
emit(std::FILE *stream, Opcode opcode, uint64_t workItem,
     uint64_t &sequence, uint64_t address = 0, uint64_t operand0 = 0,
     uint64_t operand1 = 0, uint64_t result = 0)
{
    matched_trace::emit(stream, TraceRecord{
        Phase, static_cast<uint16_t>(opcode), 0, workItem, sequence++,
        address, operand0, operand1, result});
}

uint64_t
parseCount(const char *text, const char *label)
{
    std::size_t consumed = 0;
    const auto value = std::stoull(text, &consumed, 10);
    if (text[consumed] != '\0')
        throw std::runtime_error(std::string(label) + " is invalid");
    return value;
}

} // anonymous namespace

int
main(int argc, char **argv)
{
    try {
        if (argc != 8) {
            throw std::runtime_error(
                "usage: pr_spmv_window_trace OFFSETS NEIGHBORS DEGREE "
                "WARMUP_START MEASURE_START MEASURE_STOP OUTPUT");
        }
        const auto offsets = readArray<uint64_t>(argv[1]);
        const auto neighbors = readArray<uint32_t>(argv[2]);
        const auto degree = readArray<uint32_t>(argv[3]);
        if (offsets.size() < 2 || degree.size() + 1 != offsets.size() ||
            offsets.back() != neighbors.size()) {
            throw std::runtime_error("CSR shape differs");
        }
        const uint64_t nodes = degree.size();
        const uint64_t warmupStart = parseCount(argv[4], "warmup start");
        const uint64_t measureStart = parseCount(argv[5], "measure start");
        const uint64_t measureStop = parseCount(argv[6], "measure stop");
        if (!(warmupStart < measureStart && measureStart < measureStop &&
              measureStop <= nodes)) {
            throw std::runtime_error("PageRank window coordinate is invalid");
        }

        std::vector<float> scores(nodes);
        std::vector<float> next(nodes);
        std::vector<float> contribution(nodes);
        const float initial = 1.0f / static_cast<float>(nodes);
        const float damping = 0.85f;
        const float base = (1.0f - damping) / static_cast<float>(nodes);
        std::fill(scores.begin(), scores.end(), initial);

        for (int iteration = 0; iteration < 20; ++iteration) {
#pragma omp parallel for schedule(static)
            for (uint64_t node = 0; node < nodes; ++node)
                contribution[node] = scores[node] /
                    static_cast<float>(degree[node]);
#pragma omp parallel for schedule(static)
            for (uint64_t row = 0; row < nodes; ++row) {
                float incoming = 0.0f;
                for (uint64_t edge = offsets[row]; edge < offsets[row + 1];
                     ++edge) {
                    incoming = incoming + contribution[neighbors[edge]];
                }
                next[row] = base + damping * incoming;
            }
            if (iteration == 19) {
                std::FILE *trace = std::fopen(argv[7], "wb");
                if (trace == nullptr)
                    throw std::runtime_error("cannot create PageRank trace");
                uint64_t sequence = 0;
                for (uint64_t row = warmupStart; row < measureStop; ++row) {
                    const uint64_t item = row - warmupStart;
                    emit(trace, Opcode::LOAD_U64, item, sequence,
                         InOffsets + row * 8, offsets[row], 0, offsets[row]);
                    emit(trace, Opcode::LOAD_U64, item, sequence,
                         InOffsets + (row + 1) * 8, offsets[row + 1], 0,
                         offsets[row + 1]);
                    float incoming = 0.0f;
                    for (uint64_t edge = offsets[row]; edge < offsets[row + 1];
                         ++edge) {
                        const uint32_t neighbor = neighbors[edge];
                        const uint64_t indexSequence = sequence;
                        emit(trace, Opcode::LOAD_U32, item, sequence,
                             InNeighbors + edge * 4, neighbor, 0, neighbor);
                        const uint32_t value = bits(contribution[neighbor]);
                        emit(trace, Opcode::LOAD_F32, item, sequence,
                             Contributions + uint64_t(neighbor) * 4, value,
                             indexSequence + 1, value);
                        const uint32_t before = bits(incoming);
                        incoming = incoming + contribution[neighbor];
                        emit(trace, Opcode::F32_ADD, item, sequence, 0,
                             before, value, bits(incoming));
                    }
                    const uint32_t incomingBits = bits(incoming);
                    const float product = damping * incoming;
                    emit(trace, Opcode::F32_MUL, item, sequence, 0,
                         bits(damping), incomingBits, bits(product));
                    const float result = base + product;
                    emit(trace, Opcode::F32_ADD, item, sequence, 0,
                         bits(base), bits(product), bits(result));
                    if (bits(result) != bits(next[row])) {
                        std::fclose(trace);
                        throw std::runtime_error(
                            "PageRank selected row recomputation differs");
                    }
                    emit(trace, Opcode::STORE_F32, item, sequence,
                         Scores + row * 4, bits(result), 0, bits(result));
                }
                if (std::fclose(trace) != 0)
                    throw std::runtime_error("cannot close PageRank trace");
            }
            scores.swap(next);
        }
        return 0;
    } catch (const std::exception &error) {
        std::fprintf(stderr, "PR_WINDOW_FAILED %s\n", error.what());
        return 1;
    }
}
