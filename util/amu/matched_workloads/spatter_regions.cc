/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#include "canonical_trace.hh"

#include <algorithm>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>

namespace
{

using matched_trace::Opcode;
using matched_trace::TraceRecord;

constexpr uint16_t GatherPhase = 3;
constexpr uint16_t ScatterPhase = 4;
constexpr uint64_t IndexBase = 0x100000000ULL;
constexpr uint64_t ValueBase = 0x200000000ULL;
constexpr uint64_t DestinationBase = 0x300000000ULL;

struct Options
{
    std::string kind;
    std::string values;
    std::string index;
    std::string destination;
    std::string trace;
    bool reverseDuplicateStores = false;
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
        if (option == "--kind")
            options.kind = argument(argc, argv, position, "--kind");
        else if (option == "--values")
            options.values = argument(argc, argv, position, "--values");
        else if (option == "--index")
            options.index = argument(argc, argv, position, "--index");
        else if (option == "--destination")
            options.destination = argument(
                argc, argv, position, "--destination");
        else if (option == "--trace")
            options.trace = argument(argc, argv, position, "--trace");
        else if (option == "--reverse-duplicate-stores")
            options.reverseDuplicateStores = true;
        else
            throw std::runtime_error("unknown option: " + option);
    }
    if (options.kind != "gather" && options.kind != "scatter")
        throw std::runtime_error("--kind must be gather or scatter");
    if (options.values.empty() || options.index.empty() ||
        options.destination.empty() || options.trace.empty())
        throw std::runtime_error("values, index, destination, and trace are required");
#ifndef MATCHED_FIXTURE
    if (options.reverseDuplicateStores)
        throw std::runtime_error("fault injection is unavailable in formal builds");
#endif
    return options;
}

template <typename T>
std::vector<T>
readArray(const std::string &path)
{
    std::ifstream stream(path, std::ios::binary | std::ios::ate);
    if (!stream)
        throw std::runtime_error("cannot open input: " + path);
    const auto end = stream.tellg();
    if (end < 0 || static_cast<uint64_t>(end) % sizeof(T) != 0)
        throw std::runtime_error("input size has the wrong element width: " + path);
    std::vector<T> result(static_cast<uint64_t>(end) / sizeof(T));
    stream.seekg(0);
    if (!result.empty() && !stream.read(
            reinterpret_cast<char *>(result.data()), end))
        throw std::runtime_error("short input read: " + path);
    return result;
}

template <typename T>
void
writeArray(const std::string &path, const std::vector<T> &values)
{
    std::ofstream stream(path, std::ios::binary | std::ios::trunc);
    if (!stream)
        throw std::runtime_error("cannot open output: " + path);
    if (!values.empty())
        stream.write(
            reinterpret_cast<const char *>(values.data()),
            static_cast<std::streamsize>(values.size() * sizeof(T)));
    stream.flush();
    if (!stream)
        throw std::runtime_error("output write failed: " + path);
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

std::vector<uint64_t>
canonicalOrder(
    const std::vector<uint64_t> &index, bool reverseDuplicateStores)
{
    std::vector<uint64_t> order(index.size());
    for (uint64_t i = 0; i < order.size(); ++i)
        order[i] = i;
    if (!reverseDuplicateStores)
        return order;
    std::unordered_map<uint64_t, uint64_t> first;
    for (uint64_t i = 0; i < index.size(); ++i) {
        const auto inserted = first.emplace(index[i], i);
        if (!inserted.second) {
            std::swap(order[inserted.first->second], order[i]);
            return order;
        }
    }
    throw std::runtime_error("fault injection requires a duplicate index");
}

void
runGather(
    const Options &options, const std::vector<float> &values,
    const std::vector<uint64_t> &index, std::FILE *trace)
{
    std::vector<float> destination(index.size());
    uint64_t sequence = 0;
    for (uint64_t i = 0; i < index.size(); ++i) {
        const uint64_t source = index[i];
        if (source >= values.size())
            throw std::runtime_error("gather index is out of bounds");
        const uint32_t bits = matched_trace::raw_bits(values[source]);
        const uint64_t indexSequence = sequence;
        emit(trace, GatherPhase, Opcode::LOAD_U64, i, sequence,
             IndexBase + i * sizeof(uint64_t), source, 0, source);
        emit(trace, GatherPhase, Opcode::LOAD_F32, i, sequence,
             ValueBase + source * sizeof(float), bits, indexSequence + 1,
             bits);
        destination[i] = values[source];
        emit(trace, GatherPhase, Opcode::STORE_F32, i, sequence,
             DestinationBase + i * sizeof(float), bits, 0, bits);
    }
    writeArray(options.destination, destination);
    std::cout << "MATCHED_PHASE_WORK=amg_gather:" << index.size() << '\n'
              << "MATCHED_PHASE_INVOCATIONS=amg_gather:1\n";
}

void
runScatter(
    const Options &options, const std::vector<float> &values,
    const std::vector<uint64_t> &index, std::FILE *trace)
{
    if (values.size() != index.size())
        throw std::runtime_error("scatter values/index length mismatch");
    const uint64_t destinationCount = index.empty() ? 0 :
        *std::max_element(index.begin(), index.end()) + 1;
    std::vector<float> destination(destinationCount, 0.0f);
    uint64_t sequence = 0;
    for (const uint64_t i : canonicalOrder(index, options.reverseDuplicateStores)) {
        const uint64_t target = index[i];
        const uint32_t bits = matched_trace::raw_bits(values[i]);
        emit(trace, ScatterPhase, Opcode::LOAD_U64, i, sequence,
             IndexBase + i * sizeof(uint64_t), target, 0, target);
        emit(trace, ScatterPhase, Opcode::LOAD_F32, i, sequence,
             ValueBase + i * sizeof(float), bits, 0, bits);
        destination[target] = values[i];
        emit(trace, ScatterPhase, Opcode::STORE_F32, i, sequence,
             DestinationBase + target * sizeof(float), bits, 0, bits);
    }
    writeArray(options.destination, destination);
    std::cout << "MATCHED_PHASE_WORK=lulesh_scatter:" << index.size() << '\n'
              << "MATCHED_PHASE_INVOCATIONS=lulesh_scatter:1\n"
              << "MATCHED_DUPLICATE_POLICY=canonical_program_order\n";
}

} // anonymous namespace

int
main(int argc, char **argv)
{
    try {
        const Options options = parseOptions(argc, argv);
        const auto values = readArray<float>(options.values);
        const auto index = readArray<uint64_t>(options.index);
        std::FILE *trace = std::fopen(options.trace.c_str(), "wb");
        if (trace == nullptr)
            throw std::runtime_error(
                "cannot open trace: " + std::string(std::strerror(errno)));
        try {
            if (options.kind == "gather")
                runGather(options, values, index, trace);
            else
                runScatter(options, values, index, trace);
            if (std::fclose(trace) != 0)
                throw std::runtime_error("trace close failed");
        } catch (...) {
            std::fclose(trace);
            throw;
        }
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "MATCHED_SPATTER_FAILED error=" << error.what() << '\n';
        return 1;
    }
}
