/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#include "npb_trace_hooks.h"

#include <openssl/evp.h>

#include <algorithm>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <map>
#include <mutex>
#include <tuple>
#include <utility>
#include <vector>

namespace
{

constexpr uint64_t ArrayMagic = 0x4e50424152593032ULL; // NPBARY02
constexpr uint64_t InvocationMagic = 0x4e5042494e563032ULL; // NPBINV02
constexpr uint64_t BoundaryMagic = 0x4e50425348413032ULL; // NPBSHA02
constexpr uint64_t AllocationMagic = 0x4e5042414c4c3031ULL; // NPBALL01

std::FILE *captureStream = nullptr;
std::FILE *allocationStream = nullptr;
std::mutex captureMutex;
std::vector<std::pair<uint16_t, uint64_t>> phaseStack;
uint64_t nextInvocation = 0;

struct ArrayIdentity
{
    uint64_t elementBits;
    uint64_t logicalBase;
    uint64_t count;
    std::vector<unsigned char> digest;
};

std::map<uint64_t, ArrayIdentity> arrays;

[[noreturn]] void
fail(const char *message)
{
    std::fprintf(stderr, "MATCHED_NPB_FAILED error=%s\n", message);
    std::abort();
}

std::FILE *
openFromEnvironment(const char *name)
{
    const char *path = std::getenv(name);
    if (path == nullptr || path[0] == '\0')
        return nullptr;
    std::FILE *stream = std::fopen(path, "ab");
    if (stream == nullptr) {
        std::fprintf(stderr, "MATCHED_NPB_FAILED open=%s error=%s\n",
                     path, std::strerror(errno));
        std::abort();
    }
    return stream;
}

std::FILE *
capture()
{
    if (captureStream == nullptr)
        captureStream = openFromEnvironment("MATCHED_NPB_CAPTURE_FILE");
    return captureStream;
}

void
writeBytes(std::FILE *stream, const void *data, size_t bytes)
{
    if (bytes != 0 && std::fwrite(data, 1, bytes, stream) != bytes)
        fail("capture write failed");
}

void
writeU64(std::FILE *stream, uint64_t value)
{
    writeBytes(stream, &value, sizeof(value));
}

void
flush(std::FILE *stream)
{
    if (std::fflush(stream) != 0)
        fail("capture flush failed");
}

size_t
checkedBytes(int64_t elementBits, int64_t count)
{
    if ((elementBits != 32 && elementBits != 64) || count <= 0)
        fail("array width or count is invalid");
    const uint64_t width = static_cast<uint64_t>(elementBits / 8);
    const uint64_t elements = static_cast<uint64_t>(count);
    if (elements > std::numeric_limits<size_t>::max() / width)
        fail("array byte count overflows size_t");
    return static_cast<size_t>(elements * width);
}

class Sha256
{
  public:
    Sha256() : context(EVP_MD_CTX_new())
    {
        if (context == nullptr || EVP_DigestInit_ex(
                context, EVP_sha256(), nullptr) != 1) {
            fail("SHA-256 initialization failed");
        }
    }

    ~Sha256()
    {
        EVP_MD_CTX_free(context);
    }

    Sha256(const Sha256 &) = delete;
    Sha256 &operator=(const Sha256 &) = delete;

    std::vector<unsigned char> digest(const void *data, size_t bytes)
    {
        if (EVP_DigestUpdate(context, data, bytes) != 1)
            fail("SHA-256 update failed");
        std::vector<unsigned char> result(EVP_MAX_MD_SIZE);
        unsigned int length = 0;
        if (EVP_DigestFinal_ex(context, result.data(), &length) != 1 ||
            length != 32) {
            fail("SHA-256 finalization failed");
        }
        result.resize(length);
        return result;
    }

  private:
    EVP_MD_CTX *context;
};

std::vector<unsigned char>
sha256(const void *data, size_t bytes)
{
    Sha256 hash;
    return hash.digest(data, bytes);
}

void
requireLittleEndian()
{
    const uint16_t one = 1;
    if (*reinterpret_cast<const unsigned char *>(&one) != 1)
        fail("capture host is not little endian");
}

void
writeInvocationRecord(uint64_t ordinal, uint64_t phase, uint64_t kernel,
                      uint64_t iteration, uint64_t workItems,
                      const int64_t *parameters, uint64_t parameterCount)
{
    std::FILE *stream = capture();
    if (stream == nullptr)
        return;
    writeU64(stream, InvocationMagic);
    writeU64(stream, ordinal);
    writeU64(stream, phase);
    writeU64(stream, kernel);
    writeU64(stream, iteration);
    writeU64(stream, workItems);
    writeU64(stream, parameterCount);
    writeBytes(stream, parameters,
               static_cast<size_t>(parameterCount) * sizeof(int64_t));
    flush(stream);
}

} // anonymous namespace

extern "C" void
matched_phase_begin_(const int64_t *phase, const int64_t *iteration,
                     const int64_t *workItems)
{
    if (*phase < 0 || *phase > UINT16_MAX || *iteration < 0 || *workItems < 0)
        fail("phase-begin argument is outside canonical range");
    std::lock_guard<std::mutex> lock(captureMutex);
    phaseStack.emplace_back(static_cast<uint16_t>(*phase),
                            static_cast<uint64_t>(*iteration));
    if (!arrays.empty()) {
        const uint64_t ordinal = nextInvocation++;
        writeInvocationRecord(
            ordinal, static_cast<uint64_t>(*phase),
            static_cast<uint64_t>(*phase), static_cast<uint64_t>(*iteration),
            static_cast<uint64_t>(*workItems), nullptr, 0);
    }
}

extern "C" void
matched_phase_end_(const int64_t *phase, const int64_t *iteration)
{
    if (*phase < 0 || *phase > UINT16_MAX || *iteration < 0)
        fail("phase-end argument is outside canonical range");
    std::lock_guard<std::mutex> lock(captureMutex);
    if (phaseStack.empty() || phaseStack.back() != std::make_pair(
            static_cast<uint16_t>(*phase), static_cast<uint64_t>(*iteration))) {
        fail("phase-end does not match the active phase stack");
    }
    phaseStack.pop_back();
}

extern "C" void
matched_array_image_(const int64_t *arrayId, const int64_t *elementBits,
                     const int64_t *logicalBase, const void *data,
                     const int64_t *count)
{
    if (*arrayId <= 0 || *logicalBase <= 0 || data == nullptr)
        fail("array image argument is invalid");
    requireLittleEndian();
    const size_t bytes = checkedBytes(*elementBits, *count);
    const auto digest = sha256(data, bytes);
    const uint64_t id = static_cast<uint64_t>(*arrayId);
    const ArrayIdentity identity{
        static_cast<uint64_t>(*elementBits),
        static_cast<uint64_t>(*logicalBase),
        static_cast<uint64_t>(*count), digest,
    };
    std::lock_guard<std::mutex> lock(captureMutex);
    const auto found = arrays.find(id);
    if (found != arrays.end()) {
        const auto &old = found->second;
        if (std::tie(old.elementBits, old.logicalBase, old.count, old.digest) !=
            std::tie(identity.elementBits, identity.logicalBase,
                     identity.count, identity.digest)) {
            fail("repeated array id has different metadata or content");
        }
        return;
    }
    arrays.emplace(id, identity);
    std::FILE *stream = capture();
    if (stream == nullptr)
        return;
    writeU64(stream, ArrayMagic);
    writeU64(stream, id);
    writeU64(stream, identity.elementBits);
    writeU64(stream, identity.logicalBase);
    writeU64(stream, identity.count);
    writeBytes(stream, digest.data(), digest.size());
    writeBytes(stream, data, bytes);
    flush(stream);
}

extern "C" void
matched_array_image_u32_(const int64_t *arrayId, const int64_t *elementBits,
                         const int64_t *logicalBase, const int32_t *data,
                         const int64_t *count)
{
    if (*elementBits != 32)
        fail("u32 array wrapper received a non-32-bit image");
    matched_array_image_(arrayId, elementBits, logicalBase, data, count);
}

extern "C" void
matched_array_image_f64_(const int64_t *arrayId, const int64_t *elementBits,
                         const int64_t *logicalBase, const double *data,
                         const int64_t *count)
{
    if (*elementBits != 64)
        fail("f64 array wrapper received a non-64-bit image");
    matched_array_image_(arrayId, elementBits, logicalBase, data, count);
}

extern "C" void
matched_invocation_(const int64_t *ordinal, const int64_t *phase,
                    const int64_t *kernel, const int64_t *iteration,
                    const int64_t *workItems, const int64_t *parameters,
                    const int64_t *parameterCount)
{
    if (*ordinal < 0 || *phase < 0 || *phase > UINT16_MAX || *kernel <= 0 ||
        *iteration < 0 || *workItems < 0 || *parameterCount < 0 ||
        (*parameterCount != 0 && parameters == nullptr)) {
        fail("invocation argument is outside canonical range");
    }
    std::lock_guard<std::mutex> lock(captureMutex);
    if (static_cast<uint64_t>(*ordinal) != nextInvocation)
        fail("invocation ordinal is duplicate or non-contiguous");
    ++nextInvocation;
    writeInvocationRecord(
        static_cast<uint64_t>(*ordinal), static_cast<uint64_t>(*phase),
        static_cast<uint64_t>(*kernel), static_cast<uint64_t>(*iteration),
        static_cast<uint64_t>(*workItems), parameters,
        static_cast<uint64_t>(*parameterCount));
}

extern "C" void
matched_boundary_sha256_(const int64_t *boundary, const int64_t *iteration,
                         const void *data, const int64_t *elementBits,
                         const int64_t *count)
{
    if (*boundary < 0 || *iteration < 0 || data == nullptr)
        fail("boundary argument is invalid");
    std::lock_guard<std::mutex> lock(captureMutex);
    if (arrays.empty())
        return;
    requireLittleEndian();
    const size_t bytes = checkedBytes(*elementBits, *count);
    const auto digest = sha256(data, bytes);
    std::FILE *stream = capture();
    if (stream == nullptr)
        return;
    writeU64(stream, BoundaryMagic);
    writeU64(stream, static_cast<uint64_t>(*boundary));
    writeU64(stream, static_cast<uint64_t>(*iteration));
    writeU64(stream, static_cast<uint64_t>(*elementBits));
    writeU64(stream, static_cast<uint64_t>(*count));
    writeBytes(stream, digest.data(), digest.size());
    flush(stream);
}

extern "C" double
matched_reduce_sum4_(const double lanes[4])
{
    const double left = lanes[0] + lanes[1];
    const double right = lanes[2] + lanes[3];
    return left + right;
}

extern "C" double
matched_reduce_max4_(const double lanes[4])
{
    const double left = std::max(lanes[0], lanes[1]);
    const double right = std::max(lanes[2], lanes[3]);
    return std::max(left, right);
}

extern "C" void
matched_require_four_threads_(const int64_t *threads)
{
    if (*threads != 4) {
        std::fprintf(stderr, "MATCHED_NPB_FAILED threads=%lld expected=4\n",
                     static_cast<long long>(*threads));
        std::abort();
    }
}

extern "C" void
matched_allocation_probe_(const int64_t *workload,
                          const int64_t *allocatedBytes)
{
    if (*workload <= 0 || *allocatedBytes <= 0)
        fail("allocation probe argument is invalid");
    std::lock_guard<std::mutex> lock(captureMutex);
    if (allocationStream == nullptr) {
        allocationStream = openFromEnvironment(
            "MATCHED_NPB_ALLOCATION_FILE");
    }
    if (allocationStream == nullptr)
        return;
    writeU64(allocationStream, AllocationMagic);
    writeU64(allocationStream, static_cast<uint64_t>(*workload));
    writeU64(allocationStream, static_cast<uint64_t>(*allocatedBytes));
    flush(allocationStream);
}
