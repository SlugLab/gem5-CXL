/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#include "canonical_trace.hh"

#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace
{

using matched_trace::Opcode;
using matched_trace::TraceRecord;

constexpr uint16_t PricingPhase = 1;
constexpr uint16_t PriceOutPhase = 2;
constexpr uint64_t ArcBase = 0x100000000ULL;
constexpr uint64_t PotentialBase = 0x200000000ULL;
constexpr uint64_t TreeBase = 0x300000000ULL;
constexpr uint64_t PredecessorBase = 0x310000000ULL;
constexpr uint64_t DepthBase = 0x320000000ULL;
constexpr uint64_t OrientationBase = 0x330000000ULL;
constexpr uint64_t ObjectiveBase = 0x400000000ULL;
constexpr char Magic[8] = {'M', 'C', 'F', 'R', 'E', 'G', '1', '\0'};

#pragma pack(push, 1)
struct Header
{
    char magic[8];
    uint64_t nodes;
    uint64_t arcs;
    uint64_t pricingCalls;
    uint64_t pricingItems;
    uint64_t priceOutCalls;
};

struct Arc
{
    uint64_t tail;
    uint64_t head;
    int64_t cost;
    int64_t flow;
};
#pragma pack(pop)

static_assert(sizeof(Header) == 48, "MCF fixture header drift");
static_assert(sizeof(Arc) == 32, "MCF arc layout drift");

struct State
{
    std::vector<Arc> arcs;
    std::vector<int64_t> potential;
    std::vector<int64_t> predecessor;
    std::vector<int64_t> depth;
    std::vector<int64_t> orientation;
    std::vector<int64_t> tree;
    std::vector<uint64_t> pricingOffsets;
    std::vector<uint64_t> pricingIndex;
    std::vector<uint64_t> priceOutIndex;
};

struct Options
{
    std::string input;
    std::string outputRoot;
    std::string trace;
};

std::string
argument(int argc, char **argv, int &position, const char *name)
{
    if (++position >= argc)
        throw std::runtime_error(std::string("missing value for ") + name);
    return argv[position];
}

Options
parseOptions(int argc, char **argv)
{
    Options options;
    for (int position = 1; position < argc; ++position) {
        const std::string option = argv[position];
        if (option == "--input")
            options.input = argument(argc, argv, position, "--input");
        else if (option == "--output-root")
            options.outputRoot = argument(
                argc, argv, position, "--output-root");
        else if (option == "--trace")
            options.trace = argument(argc, argv, position, "--trace");
        else
            throw std::runtime_error("unknown option: " + option);
    }
    if (options.input.empty() || options.outputRoot.empty() ||
        options.trace.empty())
        throw std::runtime_error("input, output-root, and trace are required");
    return options;
}

template <typename T>
void
readExact(std::ifstream &stream, std::vector<T> &values, const char *label)
{
    if (!values.empty())
        stream.read(
            reinterpret_cast<char *>(values.data()),
            static_cast<std::streamsize>(values.size() * sizeof(T)));
    if (!stream)
        throw std::runtime_error(std::string("short MCF ") + label + " read");
}

State
loadState(const std::string &path, Header &header)
{
    std::ifstream stream(path, std::ios::binary);
    if (!stream)
        throw std::runtime_error("cannot open MCF input: " + path);
    stream.read(reinterpret_cast<char *>(&header), sizeof(header));
    if (!stream || std::memcmp(header.magic, Magic, sizeof(Magic)) != 0)
        throw std::runtime_error("MCF input magic differs");
    if (header.nodes == 0 || header.arcs == 0 || header.pricingCalls == 0 ||
        header.pricingItems == 0 || header.priceOutCalls == 0)
        throw std::runtime_error("MCF input counts must be positive");
    State state{
        std::vector<Arc>(header.arcs),
        std::vector<int64_t>(header.nodes),
        std::vector<int64_t>(header.nodes),
        std::vector<int64_t>(header.nodes),
        std::vector<int64_t>(header.nodes),
        std::vector<int64_t>(header.nodes),
        std::vector<uint64_t>(header.pricingCalls + 1),
        std::vector<uint64_t>(header.pricingItems),
        std::vector<uint64_t>(header.priceOutCalls),
    };
    readExact(stream, state.arcs, "arcs");
    readExact(stream, state.potential, "potential");
    readExact(stream, state.predecessor, "predecessor");
    readExact(stream, state.depth, "depth");
    readExact(stream, state.orientation, "orientation");
    readExact(stream, state.tree, "tree");
    readExact(stream, state.pricingOffsets, "pricing offsets");
    readExact(stream, state.pricingIndex, "pricing index");
    readExact(stream, state.priceOutIndex, "price-out index");
    char extra;
    if (stream.read(&extra, 1))
        throw std::runtime_error("MCF input has trailing bytes");
    if (state.pricingOffsets.front() != 0 ||
        state.pricingOffsets.back() != header.pricingItems)
        throw std::runtime_error("MCF pricing offsets differ from item count");
    for (uint64_t i = 1; i < state.pricingOffsets.size(); ++i)
        if (state.pricingOffsets[i] < state.pricingOffsets[i - 1])
            throw std::runtime_error("MCF pricing offsets are not monotonic");
    for (const Arc &arc : state.arcs)
        if (arc.tail >= header.nodes || arc.head >= header.nodes)
            throw std::runtime_error("MCF arc endpoint is out of bounds");
    for (const uint64_t index : state.pricingIndex)
        if (index >= header.arcs)
            throw std::runtime_error("MCF pricing index is out of bounds");
    for (const uint64_t index : state.priceOutIndex)
        if (index >= header.arcs)
            throw std::runtime_error("MCF price-out index is out of bounds");
    return state;
}

void
emit(
    std::FILE *trace, uint16_t phase, Opcode opcode, uint64_t workItem,
    uint64_t &sequence, uint64_t address, uint64_t operand0,
    uint64_t operand1, uint64_t result)
{
    matched_trace::emit(trace, TraceRecord{
        phase, static_cast<uint16_t>(opcode), 0, workItem, sequence++,
        address, operand0, operand1, result});
}

uint64_t
bits(int64_t value)
{
    return static_cast<uint64_t>(value);
}

int64_t
reducedCost(const State &state, const Arc &arc)
{
    return arc.cost + state.potential[arc.tail] - state.potential[arc.head];
}

void
runPricing(const Header &header, const State &state, std::FILE *trace,
           uint64_t &sequence)
{
    for (uint64_t invocation = 0; invocation < header.pricingCalls;
         ++invocation) {
        int64_t best = std::numeric_limits<int64_t>::max();
        uint64_t bestIndex = std::numeric_limits<uint64_t>::max();
        for (uint64_t item = state.pricingOffsets[invocation];
             item < state.pricingOffsets[invocation + 1]; ++item) {
            const uint64_t arcIndex = state.pricingIndex[item];
            const Arc &arc = state.arcs[arcIndex];
            const int64_t reduced = reducedCost(state, arc);
            emit(trace, PricingPhase, Opcode::LOAD_U64, item, sequence,
                 ArcBase + arcIndex * sizeof(Arc), arc.tail, 0, arc.tail);
            const int64_t partial = arc.cost + state.potential[arc.tail];
            emit(trace, PricingPhase, Opcode::I64_ADD, item, sequence,
                 PotentialBase + arc.tail * sizeof(int64_t), bits(arc.cost),
                 bits(state.potential[arc.tail]), bits(partial));
            emit(trace, PricingPhase, Opcode::I64_ADD, item, sequence,
                 PotentialBase + arc.head * sizeof(int64_t), bits(partial),
                 bits(-state.potential[arc.head]), bits(reduced));
            const int64_t priorBest = best;
            if (reduced < best) {
                best = reduced;
                bestIndex = arcIndex;
            }
            emit(trace, PricingPhase, Opcode::I64_MIN, item, sequence,
                 0, bits(priorBest), bits(reduced), bits(best));
        }
        emit(trace, PricingPhase, Opcode::COMMIT, invocation, sequence,
             0, bestIndex, bits(best), bestIndex);
    }
}

template <typename T>
void
appendSnapshot(std::vector<uint64_t> &output, const std::vector<T> &values)
{
    for (const T value : values)
        output.push_back(static_cast<uint64_t>(value));
}

void
writeWords(const std::string &path, const std::vector<uint64_t> &values)
{
    std::ofstream stream(path, std::ios::binary | std::ios::trunc);
    if (!stream)
        throw std::runtime_error("cannot open MCF output: " + path);
    if (!values.empty())
        stream.write(
            reinterpret_cast<const char *>(values.data()),
            static_cast<std::streamsize>(values.size() * sizeof(uint64_t)));
    stream.flush();
    if (!stream)
        throw std::runtime_error("MCF output write failed: " + path);
}

void
runPriceOut(
    const Header &header, State &state, std::FILE *trace, uint64_t &sequence,
    const std::string &outputRoot)
{
    int64_t objective = 0;
    std::vector<uint64_t> objectives;
    std::vector<uint64_t> flow;
    std::vector<uint64_t> cost;
    std::vector<uint64_t> potential;
    std::vector<uint64_t> predecessor;
    std::vector<uint64_t> depth;
    std::vector<uint64_t> orientation;
    std::vector<uint64_t> tree;
    for (uint64_t invocation = 0; invocation < header.priceOutCalls;
         ++invocation) {
        const uint64_t arcIndex = state.priceOutIndex[invocation];
        Arc &arc = state.arcs[arcIndex];
        const int64_t reduced = reducedCost(state, arc);
        emit(trace, PriceOutPhase, Opcode::LOAD_U64, invocation, sequence,
             ArcBase + arcIndex * sizeof(Arc), arc.tail, 0, arc.tail);
        if (reduced < 0) {
            ++arc.flow;
            arc.cost = reduced;
            state.potential[arc.head] += reduced;
            state.predecessor[arc.head] = static_cast<int64_t>(arc.tail);
            state.depth[arc.head] = state.depth[arc.tail] + 1;
            state.orientation[arc.head] = 1;
            state.tree[arc.head] = static_cast<int64_t>(arcIndex);
            objective += reduced;
            emit(trace, PriceOutPhase, Opcode::STORE_U64, invocation, sequence,
                 ArcBase + arcIndex * sizeof(Arc) + 24,
                 bits(arc.flow), 0, bits(arc.flow));
            emit(trace, PriceOutPhase, Opcode::STORE_U64, invocation, sequence,
                 ArcBase + arcIndex * sizeof(Arc) + 16,
                 bits(arc.cost), 0, bits(arc.cost));
            emit(trace, PriceOutPhase, Opcode::STORE_U64, invocation, sequence,
                 PotentialBase + arc.head * sizeof(int64_t),
                 bits(state.potential[arc.head]), 0,
                 bits(state.potential[arc.head]));
            emit(trace, PriceOutPhase, Opcode::STORE_U64, invocation, sequence,
                 PredecessorBase + arc.head * sizeof(int64_t),
                 bits(state.predecessor[arc.head]), 0,
                 bits(state.predecessor[arc.head]));
            emit(trace, PriceOutPhase, Opcode::STORE_U64, invocation, sequence,
                 DepthBase + arc.head * sizeof(int64_t),
                 bits(state.depth[arc.head]), 0, bits(state.depth[arc.head]));
            emit(trace, PriceOutPhase, Opcode::STORE_U64, invocation, sequence,
                 OrientationBase + arc.head * sizeof(int64_t),
                 bits(state.orientation[arc.head]), 0,
                 bits(state.orientation[arc.head]));
            emit(trace, PriceOutPhase, Opcode::STORE_U64, invocation, sequence,
                 TreeBase + arc.head * sizeof(int64_t),
                 bits(state.tree[arc.head]), 0, bits(state.tree[arc.head]));
            emit(trace, PriceOutPhase, Opcode::STORE_U64, invocation, sequence,
                 ObjectiveBase, bits(objective), 0, bits(objective));
        }
        emit(trace, PriceOutPhase, Opcode::COMMIT, invocation, sequence,
             TreeBase + arc.head * sizeof(int64_t), bits(reduced),
             bits(arc.flow), bits(objective));
        objectives.push_back(bits(objective));
        for (const Arc &snapshotArc : state.arcs) {
            flow.push_back(bits(snapshotArc.flow));
            cost.push_back(bits(snapshotArc.cost));
        }
        appendSnapshot(potential, state.potential);
        appendSnapshot(predecessor, state.predecessor);
        appendSnapshot(depth, state.depth);
        appendSnapshot(orientation, state.orientation);
        appendSnapshot(tree, state.tree);
    }
    writeWords(outputRoot + "/objective.u64", objectives);
    writeWords(outputRoot + "/flow.u64", flow);
    writeWords(outputRoot + "/cost.u64", cost);
    writeWords(outputRoot + "/potential.u64", potential);
    writeWords(outputRoot + "/predecessor.u64", predecessor);
    writeWords(outputRoot + "/depth.u64", depth);
    writeWords(outputRoot + "/orientation.u64", orientation);
    writeWords(outputRoot + "/tree.u64", tree);
}

} // anonymous namespace

int
main(int argc, char **argv)
{
    try {
        const Options options = parseOptions(argc, argv);
        Header header{};
        State state = loadState(options.input, header);
        std::FILE *trace = std::fopen(options.trace.c_str(), "wb");
        if (trace == nullptr)
            throw std::runtime_error(
                "cannot open trace: " + std::string(std::strerror(errno)));
        try {
            uint64_t sequence = 0;
            runPricing(header, state, trace, sequence);
            runPriceOut(header, state, trace, sequence, options.outputRoot);
            if (std::fclose(trace) != 0)
                throw std::runtime_error("trace close failed");
        } catch (...) {
            std::fclose(trace);
            throw;
        }
        std::cout << "MATCHED_PHASE_WORK=pricing_kernel:"
                  << header.pricingItems << '\n'
                  << "MATCHED_PHASE_WORK=price_out_impl:"
                  << header.priceOutCalls << '\n'
                  << "MATCHED_PHASE_INVOCATIONS=pricing_kernel:"
                  << header.pricingCalls << '\n'
                  << "MATCHED_PHASE_INVOCATIONS=price_out_impl:"
                  << header.priceOutCalls << '\n'
                  << "MATCHED_STATE_SHAPE=nodes:" << header.nodes
                  << ",arcs:" << header.arcs
                  << ",price_out_boundaries:" << header.priceOutCalls
                  << '\n';
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "MATCHED_MCF_FAILED error=" << error.what() << '\n';
        return 1;
    }
}
