/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#include "mcfreg2.hh"

#include <algorithm>
#include <array>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <limits>
#include <set>
#include <sstream>

namespace mcfreg2
{
namespace
{

constexpr char Magic[8] = {'M', 'C', 'F', 'R', 'E', 'G', '2', '\0'};
constexpr uint64_t Uint64Max = std::numeric_limits<uint64_t>::max();

constexpr std::array<uint32_t, 64> ShaConstants = {{
    0x428a2f98U, 0x71374491U, 0xb5c0fbcfU, 0xe9b5dba5U,
    0x3956c25bU, 0x59f111f1U, 0x923f82a4U, 0xab1c5ed5U,
    0xd807aa98U, 0x12835b01U, 0x243185beU, 0x550c7dc3U,
    0x72be5d74U, 0x80deb1feU, 0x9bdc06a7U, 0xc19bf174U,
    0xe49b69c1U, 0xefbe4786U, 0x0fc19dc6U, 0x240ca1ccU,
    0x2de92c6fU, 0x4a7484aaU, 0x5cb0a9dcU, 0x76f988daU,
    0x983e5152U, 0xa831c66dU, 0xb00327c8U, 0xbf597fc7U,
    0xc6e00bf3U, 0xd5a79147U, 0x06ca6351U, 0x14292967U,
    0x27b70a85U, 0x2e1b2138U, 0x4d2c6dfcU, 0x53380d13U,
    0x650a7354U, 0x766a0abbU, 0x81c2c92eU, 0x92722c85U,
    0xa2bfe8a1U, 0xa81a664bU, 0xc24b8b70U, 0xc76c51a3U,
    0xd192e819U, 0xd6990624U, 0xf40e3585U, 0x106aa070U,
    0x19a4c116U, 0x1e376c08U, 0x2748774cU, 0x34b0bcb5U,
    0x391c0cb3U, 0x4ed8aa4aU, 0x5b9cca4fU, 0x682e6ff3U,
    0x748f82eeU, 0x78a5636fU, 0x84c87814U, 0x8cc70208U,
    0x90befffaU, 0xa4506cebU, 0xbef9a3f7U, 0xc67178f2U,
}};

uint32_t
rotateRight(uint32_t value, unsigned count)
{
    return (value >> count) | (value << (32U - count));
}

std::array<uint8_t, 32>
sha256(const uint8_t *data, size_t size)
{
    if (size > (std::numeric_limits<uint64_t>::max() / 8U))
        throw Error("MCFREG2 SHA-256 input is too large");
    std::vector<uint8_t> message(data, data + size);
    const uint64_t bitLength = static_cast<uint64_t>(size) * 8U;
    message.push_back(0x80U);
    while ((message.size() % 64U) != 56U)
        message.push_back(0U);
    for (int shift = 56; shift >= 0; shift -= 8)
        message.push_back(static_cast<uint8_t>(bitLength >> shift));

    std::array<uint32_t, 8> state = {{
        0x6a09e667U, 0xbb67ae85U, 0x3c6ef372U, 0xa54ff53aU,
        0x510e527fU, 0x9b05688cU, 0x1f83d9abU, 0x5be0cd19U,
    }};
    for (size_t block = 0; block < message.size(); block += 64U) {
        std::array<uint32_t, 64> words{};
        for (size_t index = 0; index < 16U; ++index) {
            const size_t offset = block + index * 4U;
            words[index] =
                (static_cast<uint32_t>(message[offset]) << 24U) |
                (static_cast<uint32_t>(message[offset + 1U]) << 16U) |
                (static_cast<uint32_t>(message[offset + 2U]) << 8U) |
                static_cast<uint32_t>(message[offset + 3U]);
        }
        for (size_t index = 16U; index < words.size(); ++index) {
            const uint32_t s0 =
                rotateRight(words[index - 15U], 7U) ^
                rotateRight(words[index - 15U], 18U) ^
                (words[index - 15U] >> 3U);
            const uint32_t s1 =
                rotateRight(words[index - 2U], 17U) ^
                rotateRight(words[index - 2U], 19U) ^
                (words[index - 2U] >> 10U);
            words[index] = words[index - 16U] + s0 +
                           words[index - 7U] + s1;
        }

        uint32_t a = state[0];
        uint32_t b = state[1];
        uint32_t c = state[2];
        uint32_t d = state[3];
        uint32_t e = state[4];
        uint32_t f = state[5];
        uint32_t g = state[6];
        uint32_t h = state[7];
        for (size_t index = 0; index < words.size(); ++index) {
            const uint32_t sigma1 =
                rotateRight(e, 6U) ^ rotateRight(e, 11U) ^
                rotateRight(e, 25U);
            const uint32_t choice = (e & f) ^ ((~e) & g);
            const uint32_t temporary1 =
                h + sigma1 + choice + ShaConstants[index] + words[index];
            const uint32_t sigma0 =
                rotateRight(a, 2U) ^ rotateRight(a, 13U) ^
                rotateRight(a, 22U);
            const uint32_t majority = (a & b) ^ (a & c) ^ (b & c);
            const uint32_t temporary2 = sigma0 + majority;
            h = g;
            g = f;
            f = e;
            e = d + temporary1;
            d = c;
            c = b;
            b = a;
            a = temporary1 + temporary2;
        }
        state[0] += a;
        state[1] += b;
        state[2] += c;
        state[3] += d;
        state[4] += e;
        state[5] += f;
        state[6] += g;
        state[7] += h;
    }

    std::array<uint8_t, 32> digest{};
    for (size_t index = 0; index < state.size(); ++index) {
        digest[index * 4U] = static_cast<uint8_t>(state[index] >> 24U);
        digest[index * 4U + 1U] =
            static_cast<uint8_t>(state[index] >> 16U);
        digest[index * 4U + 2U] =
            static_cast<uint8_t>(state[index] >> 8U);
        digest[index * 4U + 3U] = static_cast<uint8_t>(state[index]);
    }
    return digest;
}

std::string
hexDigest(const uint8_t *data, size_t size)
{
    std::ostringstream output;
    output << std::hex << std::setfill('0');
    for (size_t index = 0; index < size; ++index)
        output << std::setw(2) << static_cast<unsigned>(data[index]);
    return output.str();
}

uint64_t
checkedAdd(uint64_t left, uint64_t right, const char *label)
{
    if (left > Uint64Max - right)
        throw Error(std::string("MCFREG2 ") + label + " overflows u64");
    return left + right;
}

uint64_t
checkedMultiply(uint64_t left, uint64_t right, const char *label)
{
    if (left != 0U && right > Uint64Max / left)
        throw Error(std::string("MCFREG2 ") + label + " overflows u64");
    return left * right;
}

bool
knownSection(uint16_t sectionType)
{
    return sectionType >= MCFREG2_PROVENANCE &&
           sectionType <= MCFREG2_FINAL;
}

uint64_t
streamSize(std::ifstream &stream)
{
    stream.seekg(0, std::ios::end);
    const std::streamoff size = stream.tellg();
    if (size < 0)
        throw Error("cannot determine MCFREG2 file size");
    stream.seekg(0, std::ios::beg);
    return static_cast<uint64_t>(size);
}

template <typename T>
void
readExact(std::ifstream &stream, T &value, const char *label)
{
    stream.read(reinterpret_cast<char *>(&value), sizeof(value));
    if (!stream)
        throw Error(std::string("MCFREG2 ") + label + " is truncated");
}

std::vector<uint8_t>
readBytes(std::ifstream &stream, uint64_t offset, uint64_t count)
{
    if (count > std::numeric_limits<size_t>::max() ||
        count > static_cast<uint64_t>(
                    std::numeric_limits<std::streamsize>::max()))
        throw Error("MCFREG2 section is too large for this host");
    stream.seekg(static_cast<std::streamoff>(offset), std::ios::beg);
    if (!stream)
        throw Error("MCFREG2 section seek failed");
    std::vector<uint8_t> result(static_cast<size_t>(count));
    if (!result.empty())
        stream.read(
            reinterpret_cast<char *>(result.data()),
            static_cast<std::streamsize>(result.size()));
    if (!stream)
        throw Error("MCFREG2 section is truncated");
    return result;
}

} // anonymous namespace

std::string
sha256Hex(std::string_view value)
{
    const auto digest = sha256(
        reinterpret_cast<const uint8_t *>(value.data()), value.size());
    return hexDigest(digest.data(), digest.size());
}

Package
readPackage(const std::string &path)
{
    const uint16_t hostEndian = 1U;
    if (*reinterpret_cast<const uint8_t *>(&hostEndian) != 1U)
        throw Error("MCFREG2 reader requires a little-endian host");
    std::ifstream stream(path, std::ios::binary);
    if (!stream)
        throw Error("cannot open MCFREG2 package: " + path);
    const uint64_t fileSize = streamSize(stream);

    Package package;
    readExact(stream, package.header, "header");
    const auto &header = package.header;
    if (std::memcmp(header.magic, Magic, sizeof(Magic)) != 0)
        throw Error("MCFREG2 header magic differs");
    if (header.schema != MCFREG2_SCHEMA)
        throw Error("MCFREG2 header schema differs");
    if (header.endianTag != MCFREG2_ENDIAN_TAG)
        throw Error("MCFREG2 header endian tag differs");
    if (header.headerBytes != sizeof(McfReg2Header))
        throw Error("MCFREG2 header size differs");
    if (header.flags != 0U)
        throw Error("MCFREG2 header flags are unsupported");
    if (header.reserved != 0U)
        throw Error("MCFREG2 reserved header field is nonzero");
    if (header.directoryOffset != sizeof(McfReg2Header))
        throw Error("MCFREG2 directory offset differs");
    if (header.sectionCount < 10U)
        throw Error("MCFREG2 section count is too small");
    if (header.nodes == 0U || header.activeArcs == 0U)
        throw Error("MCFREG2 network counts must be positive");
    if (header.arenaCapacity < header.activeArcs)
        throw Error("MCFREG2 arena capacity is below active arcs");
    if (header.pricingCalls == 0U || header.priceOutCalls == 0U ||
        header.eventCount == 0U)
        throw Error("MCFREG2 call and event counts must be positive");

    const uint64_t directoryBytes = checkedMultiply(
        header.sectionCount, sizeof(McfReg2DirectoryEntry), "directory size");
    const uint64_t directoryEnd = checkedAdd(
        header.directoryOffset, directoryBytes, "directory end");
    if (directoryEnd > fileSize)
        throw Error("MCFREG2 directory is truncated");
    if (header.sectionCount > std::numeric_limits<size_t>::max())
        throw Error("MCFREG2 section count is too large for this host");

    package.directory.reserve(static_cast<size_t>(header.sectionCount));
    std::set<uint16_t> seen;
    uint16_t priorType = 0U;
    for (uint64_t index = 0; index < header.sectionCount; ++index) {
        McfReg2DirectoryEntry entry{};
        readExact(stream, entry, "directory");
        if (!seen.insert(entry.sectionType).second)
            throw Error(
                "duplicate MCFREG2 section type " +
                std::to_string(entry.sectionType));
        if (entry.sectionType <= priorType)
            throw Error("MCFREG2 directory is not sorted");
        priorType = entry.sectionType;
        if (!knownSection(entry.sectionType) &&
            !(entry.flags & MCFREG2_OPTIONAL_FLAG))
            throw Error(
                "MCFREG2 unknown mandatory section " +
                std::to_string(entry.sectionType));
        if (knownSection(entry.sectionType) && entry.flags != 0U)
            throw Error("MCFREG2 required section flags differ");
        if (entry.schema == 0U)
            throw Error("MCFREG2 section schema is zero");
        if (entry.storedBytes == 0U)
            throw Error("MCFREG2 section is empty");
        if (entry.elementSize != 0U &&
            checkedMultiply(
                entry.elementCount, entry.elementSize,
                "section logical size") != entry.storedBytes)
            throw Error("MCFREG2 section element size differs");
        package.directory.push_back(entry);
    }
    for (uint16_t section = MCFREG2_PROVENANCE;
         section <= MCFREG2_FINAL; ++section) {
        if (!seen.count(section))
            throw Error(
                "missing MCFREG2 section " + std::to_string(section));
    }

    uint64_t cursor = directoryEnd;
    for (const auto &entry : package.directory) {
        if (entry.offset < cursor)
            throw Error("MCFREG2 sections overlap");
        if (entry.offset > cursor)
            throw Error("MCFREG2 section layout has a gap");
        const uint64_t end =
            checkedAdd(entry.offset, entry.storedBytes, "section end");
        if (end > fileSize)
            throw Error("MCFREG2 section is truncated");
        auto data = readBytes(stream, entry.offset, entry.storedBytes);
        const auto digest = sha256(data.data(), data.size());
        if (!std::equal(
                digest.begin(), digest.end(), std::begin(entry.sha256)))
            throw Error("MCFREG2 section SHA-256 differs");
        package.sections.emplace(entry.sectionType, std::move(data));
        cursor = end;
    }
    if (cursor != fileSize)
        throw Error("MCFREG2 file has trailing bytes");
    return package;
}

std::string
directoryJson(const Package &package)
{
    std::ostringstream output;
    output << '[';
    bool first = true;
    for (const auto &entry : package.directory) {
        if (!first)
            output << ',';
        first = false;
        output << "{\"section_type\":" << entry.sectionType
               << ",\"schema\":" << entry.schema
               << ",\"flags\":" << entry.flags
               << ",\"offset\":" << entry.offset
               << ",\"stored_bytes\":" << entry.storedBytes
               << ",\"element_count\":" << entry.elementCount
               << ",\"element_size\":" << entry.elementSize
               << ",\"sha256\":\""
               << hexDigest(entry.sha256, sizeof(entry.sha256)) << "\"}";
    }
    output << ']';
    return output.str();
}

} // namespace mcfreg2
