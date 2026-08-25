/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#include "mcfreg2.hh"
#include "canonical_trace.hh"

#include <algorithm>
#include <array>
#include <cctype>
#include <charconv>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <limits>
#include <set>
#include <sstream>
#include <utility>
#include <zlib.h>

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

class Sha256
{
  public:
    void update(const void *rawData, size_t size)
    {
        if (finalized)
            throw Error("MCFREG2 SHA-256 update follows finalization");
        if (size > std::numeric_limits<uint64_t>::max() - totalBytes)
            throw Error("MCFREG2 SHA-256 input is too large");
        totalBytes += size;
        const auto *data = static_cast<const uint8_t *>(rawData);
        while (size != 0U) {
            const size_t bytes = std::min(size, buffer.size() - buffered);
            std::memcpy(buffer.data() + buffered, data, bytes);
            buffered += bytes;
            data += bytes;
            size -= bytes;
            if (buffered == buffer.size()) {
                process(buffer.data());
                buffered = 0;
            }
        }
    }

    std::array<uint8_t, 32> finish()
    {
        if (finalized)
            throw Error("MCFREG2 SHA-256 is finalized twice");
        if (totalBytes > std::numeric_limits<uint64_t>::max() / 8U)
            throw Error("MCFREG2 SHA-256 input is too large");
        const uint64_t bitLength = totalBytes * 8U;
        buffer[buffered++] = 0x80U;
        if (buffered > 56U) {
            std::fill(buffer.begin() + buffered, buffer.end(), 0U);
            process(buffer.data());
            buffered = 0;
        }
        std::fill(buffer.begin() + buffered, buffer.begin() + 56U, 0U);
        for (unsigned index = 0; index < 8U; ++index)
            buffer[56U + index] = static_cast<uint8_t>(
                bitLength >> ((7U - index) * 8U));
        process(buffer.data());
        finalized = true;
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

  private:
    void process(const uint8_t *block)
    {
        std::array<uint32_t, 64> words{};
        for (size_t index = 0; index < 16U; ++index) {
            const size_t offset = index * 4U;
            words[index] =
                (static_cast<uint32_t>(block[offset]) << 24U) |
                (static_cast<uint32_t>(block[offset + 1U]) << 16U) |
                (static_cast<uint32_t>(block[offset + 2U]) << 8U) |
                static_cast<uint32_t>(block[offset + 3U]);
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

    std::array<uint32_t, 8> state = {{
        0x6a09e667U, 0xbb67ae85U, 0x3c6ef372U, 0xa54ff53aU,
        0x510e527fU, 0x9b05688cU, 0x1f83d9abU, 0x5be0cd19U,
    }};
    std::array<uint8_t, 64> buffer{};
    size_t buffered = 0;
    uint64_t totalBytes = 0;
    bool finalized = false;
};

std::array<uint8_t, 32>
sha256(const uint8_t *data, size_t size)
{
    Sha256 digest;
    digest.update(data, size);
    return digest.finish();
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

namespace
{

struct Json
{
    enum class Kind { Null, Boolean, Number, String, Array, Object };
    Kind kind = Kind::Null;
    bool boolean = false;
    int64_t number = 0;
    std::string string;
    std::vector<Json> array;
    std::map<std::string, Json> object;
};

class JsonParser
{
  public:
    explicit JsonParser(std::string_view input) : input(input) {}

    Json parse()
    {
        Json result = value();
        whitespace();
        if (position != input.size())
            throw Error("MCFREG2 JSON has trailing bytes");
        return result;
    }

  private:
    std::string_view input;
    size_t position = 0;

    void whitespace()
    {
        while (position < input.size() &&
               std::isspace(static_cast<unsigned char>(input[position])))
            ++position;
    }

    char take()
    {
        if (position == input.size())
            throw Error("MCFREG2 JSON is truncated");
        return input[position++];
    }

    void expect(char expected)
    {
        if (take() != expected)
            throw Error("MCFREG2 JSON token differs");
    }

    bool consume(std::string_view token)
    {
        if (input.substr(position, token.size()) != token)
            return false;
        position += token.size();
        return true;
    }

    static unsigned hex(char value)
    {
        if (value >= '0' && value <= '9')
            return static_cast<unsigned>(value - '0');
        if (value >= 'a' && value <= 'f')
            return static_cast<unsigned>(value - 'a' + 10);
        if (value >= 'A' && value <= 'F')
            return static_cast<unsigned>(value - 'A' + 10);
        throw Error("MCFREG2 JSON unicode escape is invalid");
    }

    std::string parseString()
    {
        expect('"');
        std::string result;
        while (true) {
            const char value = take();
            if (value == '"')
                return result;
            if (static_cast<unsigned char>(value) < 0x20U)
                throw Error("MCFREG2 JSON string has a control byte");
            if (static_cast<unsigned char>(value) >= 0x80U)
                throw Error("MCFREG2 JSON must be canonical ASCII");
            if (value != '\\') {
                result.push_back(value);
                continue;
            }
            const char escape = take();
            switch (escape) {
              case '"': result.push_back('"'); break;
              case '\\': result.push_back('\\'); break;
              case '/': result.push_back('/'); break;
              case 'b': result.push_back('\b'); break;
              case 'f': result.push_back('\f'); break;
              case 'n': result.push_back('\n'); break;
              case 'r': result.push_back('\r'); break;
              case 't': result.push_back('\t'); break;
              case 'u': {
                unsigned code = 0;
                for (unsigned index = 0; index < 4; ++index)
                    code = code * 16U + hex(take());
                if (code > 0x7fU)
                    throw Error("MCFREG2 JSON must be canonical ASCII");
                result.push_back(static_cast<char>(code));
                break;
              }
              default:
                throw Error("MCFREG2 JSON escape is invalid");
            }
        }
    }

    Json number()
    {
        const size_t begin = position;
        if (input[position] == '-')
            ++position;
        const size_t digits = position;
        while (position < input.size() &&
               std::isdigit(static_cast<unsigned char>(input[position])))
            ++position;
        if (digits == position || (position - digits > 1 && input[digits] == '0'))
            throw Error("MCFREG2 JSON number is invalid");
        int64_t result = 0;
        const auto converted = std::from_chars(
            input.data() + begin, input.data() + position, result);
        if (converted.ec != std::errc{} ||
            converted.ptr != input.data() + position)
            throw Error("MCFREG2 JSON number is out of range");
        Json value;
        value.kind = Json::Kind::Number;
        value.number = result;
        return value;
    }

    Json array()
    {
        Json result;
        result.kind = Json::Kind::Array;
        expect('[');
        whitespace();
        if (position < input.size() && input[position] == ']') {
            ++position;
            return result;
        }
        while (true) {
            result.array.push_back(value());
            whitespace();
            const char separator = take();
            if (separator == ']')
                return result;
            if (separator != ',')
                throw Error("MCFREG2 JSON array separator differs");
        }
    }

    Json object()
    {
        Json result;
        result.kind = Json::Kind::Object;
        expect('{');
        whitespace();
        if (position < input.size() && input[position] == '}') {
            ++position;
            return result;
        }
        while (true) {
            whitespace();
            if (position == input.size() || input[position] != '"')
                throw Error("MCFREG2 JSON object key is invalid");
            std::string key = parseString();
            whitespace();
            expect(':');
            if (!result.object.emplace(std::move(key), value()).second)
                throw Error("MCFREG2 JSON object key is duplicated");
            whitespace();
            const char separator = take();
            if (separator == '}')
                return result;
            if (separator != ',')
                throw Error("MCFREG2 JSON object separator differs");
        }
    }

    Json value()
    {
        whitespace();
        if (position == input.size())
            throw Error("MCFREG2 JSON is truncated");
        if (input[position] == '{')
            return object();
        if (input[position] == '[')
            return array();
        if (input[position] == '"') {
            Json result;
            result.kind = Json::Kind::String;
            result.string = parseString();
            return result;
        }
        if (input[position] == '-' ||
            std::isdigit(static_cast<unsigned char>(input[position])))
            return number();
        Json result;
        if (consume("true")) {
            result.kind = Json::Kind::Boolean;
            result.boolean = true;
            return result;
        }
        if (consume("false")) {
            result.kind = Json::Kind::Boolean;
            return result;
        }
        if (consume("null"))
            return result;
        throw Error("MCFREG2 JSON value is invalid");
    }
};

const Json &
field(const Json &object, const char *name)
{
    if (object.kind != Json::Kind::Object)
        throw Error("MCFREG2 event is not an object");
    const auto found = object.object.find(name);
    if (found == object.object.end())
        throw Error(std::string("MCFREG2 event field is missing: ") + name);
    return found->second;
}

int64_t
integer(const Json &object, const char *name)
{
    const Json &value = field(object, name);
    if (value.kind != Json::Kind::Number)
        throw Error(std::string("MCFREG2 event field is not integer: ") + name);
    return value.number;
}

uint64_t
nonnegative(const Json &object, const char *name)
{
    const int64_t value = integer(object, name);
    if (value < 0)
        throw Error(std::string("MCFREG2 event field is negative: ") + name);
    return static_cast<uint64_t>(value);
}

std::string
stringField(const Json &object, const char *name)
{
    const Json &value = field(object, name);
    if (value.kind != Json::Kind::String)
        throw Error(std::string("MCFREG2 event field is not string: ") + name);
    return value.string;
}

bool
boolField(const Json &object, const char *name)
{
    const Json &value = field(object, name);
    if (value.kind != Json::Kind::Boolean)
        throw Error(std::string("MCFREG2 event field is not boolean: ") + name);
    return value.boolean;
}

std::string
escapeJson(const std::string &input)
{
    std::ostringstream output;
    output << '"';
    for (const unsigned char value : input) {
        switch (value) {
          case '"': output << "\\\""; break;
          case '\\': output << "\\\\"; break;
          case '\b': output << "\\b"; break;
          case '\f': output << "\\f"; break;
          case '\n': output << "\\n"; break;
          case '\r': output << "\\r"; break;
          case '\t': output << "\\t"; break;
          default:
            if (value < 0x20U)
                output << "\\u00" << std::hex << std::setfill('0')
                       << std::setw(2) << static_cast<unsigned>(value)
                       << std::dec;
            else if (value < 0x80U)
                output << static_cast<char>(value);
            else
                throw Error("MCFREG2 JSON must be canonical ASCII");
        }
    }
    output << '"';
    return output.str();
}

std::string
canonicalJson(const Json &value)
{
    switch (value.kind) {
      case Json::Kind::Null: return "null";
      case Json::Kind::Boolean: return value.boolean ? "true" : "false";
      case Json::Kind::Number: return std::to_string(value.number);
      case Json::Kind::String: return escapeJson(value.string);
      case Json::Kind::Array: {
        std::string result = "[";
        for (size_t index = 0; index < value.array.size(); ++index) {
            if (index)
                result += ',';
            result += canonicalJson(value.array[index]);
        }
        return result + ']';
      }
      case Json::Kind::Object: {
        std::string result = "{";
        bool first = true;
        for (const auto &[key, element] : value.object) {
            if (!first)
                result += ',';
            first = false;
            result += escapeJson(key) + ':' + canonicalJson(element);
        }
        return result + '}';
      }
    }
    throw Error("MCFREG2 JSON kind is invalid");
}

std::string
sectionText(const Package &package, uint16_t type)
{
    const auto found = package.sections.find(type);
    if (found == package.sections.end())
        throw Error("MCFREG2 replay section is missing");
    return std::string(found->second.begin(), found->second.end());
}

class EventReader
{
  public:
    explicit EventReader(const Package &package)
        : data(sectionData(package)), schema(sectionSchema(package))
    {
        if (schema == 1U) {
            pending.assign(data.begin(), data.end());
            finished = true;
        } else if (schema == 2U) {
            std::memset(&stream, 0, sizeof(stream));
            if (inflateInit2(&stream, 16 + MAX_WBITS) != Z_OK)
                throw Error("MCFREG2 gzip event initialization failed");
            initialized = true;
        } else {
            throw Error("MCFREG2 event section schema is unsupported");
        }
    }

    ~EventReader()
    {
        if (initialized)
            inflateEnd(&stream);
    }

    bool next(Json &result)
    {
        while (true) {
            const size_t newline = pending.find('\n');
            if (newline != std::string::npos) {
                const std::string line = pending.substr(0, newline);
                pending.erase(0, newline + 1U);
                if (line.empty())
                    continue;
                result = JsonParser(line).parse();
                ++rows;
                return true;
            }
            if (finished) {
                if (pending.empty())
                    return false;
                const std::string line = std::move(pending);
                pending.clear();
                result = JsonParser(line).parse();
                ++rows;
                return true;
            }
            decompress();
        }
    }

    uint64_t count() const { return rows; }

  private:
    static const std::vector<uint8_t> &
    sectionData(const Package &package)
    {
        const auto found = package.sections.find(MCFREG2_EVENTS);
        if (found == package.sections.end())
            throw Error("MCFREG2 event section is missing");
        return found->second;
    }

    static uint16_t
    sectionSchema(const Package &package)
    {
        const auto found = std::find_if(
            package.directory.begin(), package.directory.end(),
            [](const auto &entry) {
                return entry.sectionType == MCFREG2_EVENTS;
            });
        if (found == package.directory.end())
            throw Error("MCFREG2 event directory entry is missing");
        return found->schema;
    }

    void decompress()
    {
        if (stream.avail_in == 0U && inputOffset < data.size()) {
            const size_t bytes = std::min(
                data.size() - inputOffset,
                static_cast<size_t>(std::numeric_limits<uInt>::max()));
            stream.next_in = const_cast<Bytef *>(
                reinterpret_cast<const Bytef *>(data.data() + inputOffset));
            stream.avail_in = static_cast<uInt>(bytes);
            inputOffset += bytes;
        }
        std::array<char, 1024 * 1024> output{};
        stream.next_out = reinterpret_cast<Bytef *>(output.data());
        stream.avail_out = static_cast<uInt>(output.size());
        const int status = inflate(&stream, Z_NO_FLUSH);
        const size_t produced = output.size() - stream.avail_out;
        pending.append(output.data(), produced);
        if (status == Z_STREAM_END) {
            if (stream.avail_in != 0U || inputOffset != data.size())
                throw Error("MCFREG2 gzip event stream has trailing bytes");
            finished = true;
        } else if (status != Z_OK) {
            throw Error("MCFREG2 gzip event stream is invalid");
        } else if (produced == 0U && stream.avail_in == 0U &&
                   inputOffset == data.size()) {
            throw Error("MCFREG2 gzip event stream is truncated");
        }
    }

    const std::vector<uint8_t> &data;
    uint16_t schema;
    z_stream stream{};
    bool initialized = false;
    bool finished = false;
    size_t inputOffset = 0;
    uint64_t rows = 0;
    std::string pending;
};

const Json &
reference(const Json &event)
{
    const Json &value = field(event, "reference");
    if (value.kind != Json::Kind::Object)
        throw Error("MCFREG2 stable reference is not an object");
    return value;
}

uint64_t
referenceIndex(
    const Json &event, uint64_t generation, uint64_t capacity,
    bool required)
{
    const Json &value = reference(event);
    if (value.object.empty()) {
        if (required)
            throw Error("MCFREG2 stable reference is absent");
        return Uint64Max;
    }
    if (stringField(value, "kind") != "arc" ||
        nonnegative(value, "generation") != generation)
        throw Error("MCFREG2 stable reference generation differs");
    const uint64_t index = nonnegative(value, "index");
    if (index >= capacity)
        throw Error("MCFREG2 stable reference index is out of range");
    return index;
}

int64_t
reducedCost(const Json &event)
{
    const __int128 result = static_cast<__int128>(integer(event, "arc_cost")) -
                            integer(event, "tail_potential") +
                            integer(event, "head_potential");
    if (result < std::numeric_limits<int64_t>::min() ||
        result > std::numeric_limits<int64_t>::max())
        throw Error("MCFREG2 reduced cost overflows i64");
    return static_cast<int64_t>(result);
}

int64_t
checkedSubtract(int64_t left, int64_t right)
{
    const __int128 result =
        static_cast<__int128>(left) - static_cast<__int128>(right);
    if (result < std::numeric_limits<int64_t>::min() ||
        result > std::numeric_limits<int64_t>::max())
        throw Error("MCFREG2 reduced-cost partial overflows i64");
    return static_cast<int64_t>(result);
}

uint64_t
bits(int64_t value)
{
    return static_cast<uint64_t>(value);
}

class TraceSink
{
  public:
    explicit TraceSink(std::FILE *stream) : stream(stream) {}

    void write(const matched_trace::TraceRecord &record)
    {
        if (stream != nullptr &&
            std::fwrite(&record, sizeof(record), 1, stream) != 1)
            throw Error("canonical trace write failed");
        digest.update(&record, sizeof(record));
    }

    std::string finish()
    {
        const auto value = digest.finish();
        return hexDigest(value.data(), value.size());
    }

  private:
    std::FILE *stream;
    Sha256 digest;
};

uint64_t
emitTrace(
    TraceSink &trace, uint16_t phase, matched_trace::Opcode opcode,
    uint64_t workItem, uint64_t &sequence, uint64_t address,
    uint64_t operand0, uint64_t operand1, uint64_t result)
{
    const uint64_t current = sequence++;
    trace.write(matched_trace::TraceRecord{
        phase, static_cast<uint16_t>(opcode), 0, workItem, current,
        address, operand0, operand1, result,
    });
    return current;
}

struct BasketRow
{
    uint64_t slot;
    uint64_t arc;
    int64_t cost;
    int64_t absolute;
};

Json
jsonArray(const std::vector<Json> &values)
{
    Json result;
    result.kind = Json::Kind::Array;
    result.array = values;
    return result;
}

std::map<uint64_t, std::pair<std::string, std::string>>
boundaryDigests(const Package &package)
{
    const Json root = JsonParser(sectionText(package, MCFREG2_BOUNDARIES)).parse();
    const Json &rows = field(root, "rows");
    if (rows.kind != Json::Kind::Array)
        throw Error("MCFREG2 boundaries rows are not an array");
    std::map<uint64_t, std::pair<std::string, std::string>> result;
    for (const Json &row : rows.array) {
        const uint64_t order = nonnegative(row, "order");
        if (!result.emplace(
                order,
                std::make_pair(
                    stringField(row, "pre_sha256"),
                    stringField(row, "post_sha256"))).second)
            throw Error("MCFREG2 boundary order is duplicated");
    }
    return result;
}

std::vector<Json>
sectionRows(const Package &package, uint16_t section)
{
    const Json root = JsonParser(sectionText(package, section)).parse();
    const Json &rows = field(root, "rows");
    if (rows.kind != Json::Kind::Array)
        throw Error("MCFREG2 section rows are not an array");
    return rows.array;
}

const McfReg2DirectoryEntry &
directoryEntry(const Package &package, uint16_t section)
{
    const auto found = std::find_if(
        package.directory.begin(), package.directory.end(),
        [section](const auto &entry) { return entry.sectionType == section; });
    if (found == package.directory.end())
        throw Error("MCFREG2 replay directory entry is missing");
    return *found;
}

void
validateStableRef(
    const McfStableRef &reference, const Package &package,
    bool nodeOnly)
{
    if (reference.kind == MCFREG2_OBJECT_NULL) {
        if (reference.generation != 0U || reference.objectId != Uint64Max)
            throw Error("MCFREG2 null stable reference is malformed");
        return;
    }
    if (reference.kind == MCFREG2_OBJECT_NODE) {
        if (!nodeOnly || reference.generation != 0U ||
            reference.objectId >= package.header.nodes)
            throw Error("MCFREG2 node stable reference is out of range");
        return;
    }
    if (nodeOnly)
        throw Error("MCFREG2 node relationship kind differs");
    if (reference.kind == MCFREG2_OBJECT_ARC) {
        if (reference.generation != 0U ||
            reference.objectId >= package.header.activeArcs)
            throw Error("MCFREG2 arc stable reference is out of range");
        return;
    }
    if (reference.kind == MCFREG2_OBJECT_DUMMY_ARC) {
        if (reference.generation != 0U ||
            reference.objectId >= package.header.dummyArcs)
            throw Error("MCFREG2 dummy-arc reference is out of range");
        return;
    }
    throw Error("MCFREG2 stable reference kind is unknown");
}

McfStableRef
stableRefAt(const std::vector<uint8_t> &data, size_t offset)
{
    if (offset > data.size() || data.size() - offset < sizeof(McfStableRef))
        throw Error("MCFREG2 stable reference is truncated");
    McfStableRef result{};
    std::memcpy(&result, data.data() + offset, sizeof(result));
    return result;
}

void
validateInitialState(const Package &package)
{
    constexpr uint64_t NetworkWords = 22U;
    constexpr uint64_t NodeBytes = 176U;
    constexpr uint64_t ArcBytes = 96U;
    const auto &networkEntry = directoryEntry(package, MCFREG2_NETWORK);
    const auto &nodeEntry = directoryEntry(package, MCFREG2_NODES);
    const auto &arcEntry = directoryEntry(package, MCFREG2_ARCS);
    if (networkEntry.elementCount != NetworkWords ||
        networkEntry.elementSize != 8U ||
        nodeEntry.elementCount != package.header.nodes ||
        nodeEntry.elementSize != NodeBytes ||
        arcEntry.elementCount !=
            package.header.activeArcs + package.header.dummyArcs ||
        arcEntry.elementSize != ArcBytes)
        throw Error("MCFREG2 normalized state layout differs");
    const auto &nodes = package.sections.at(MCFREG2_NODES);
    const auto &arcs = package.sections.at(MCFREG2_ARCS);
    for (uint64_t node = 0; node < package.header.nodes; ++node) {
        const size_t base = static_cast<size_t>(node * NodeBytes);
        for (size_t index = 0; index < 4; ++index)
            validateStableRef(
                stableRefAt(nodes, base + 16U + index * 16U),
                package, true);
        for (size_t index = 0; index < 4; ++index)
            validateStableRef(
                stableRefAt(nodes, base + 80U + index * 16U),
                package, false);
    }
    const uint64_t arcCount =
        package.header.activeArcs + package.header.dummyArcs;
    for (uint64_t arc = 0; arc < arcCount; ++arc) {
        const size_t base = static_cast<size_t>(arc * ArcBytes);
        const McfStableRef tail = stableRefAt(arcs, base + 8U);
        const McfStableRef head = stableRefAt(arcs, base + 24U);
        validateStableRef(tail, package, true);
        validateStableRef(head, package, true);
        if (tail.kind == MCFREG2_OBJECT_NULL ||
            head.kind == MCFREG2_OBJECT_NULL)
            throw Error("MCFREG2 arc endpoint is null");
        validateStableRef(stableRefAt(arcs, base + 48U), package, false);
        validateStableRef(stableRefAt(arcs, base + 64U), package, false);
    }
}

} // anonymous namespace

ReplaySummary
replay(
    const Package &package, std::FILE *canonicalTrace,
    const std::string &outputRoot)
{
    constexpr uint64_t ArcAddress = UINT64_C(0x800000000);
    constexpr uint64_t PotentialAddress = UINT64_C(0x900000000);
    constexpr uint64_t DeltaAddress = UINT64_C(0xa00000000);
    TraceSink trace(canonicalTrace);
    validateInitialState(package);
    EventReader events(package);
    const auto boundaries = boundaryDigests(package);
    const std::vector<Json> callRows =
        sectionRows(package, MCFREG2_CALL_INDEX);
    std::map<uint64_t, Json> callIndex;
    for (const Json &row : callRows) {
        if (!callIndex.emplace(nonnegative(row, "order"), row).second)
            throw Error("MCFREG2 call index order is duplicated");
    }
    const std::vector<Json> recordedBasket =
        sectionRows(package, MCFREG2_BASKET);
    const std::vector<Json> recordedDeltas =
        sectionRows(package, MCFREG2_DELTAS);
    std::vector<Json> expectedBasket;
    std::vector<Json> expectedDeltas;
    ReplaySummary summary;
    uint64_t sequence = 0;
    uint64_t expectedOrder = 0;
    uint64_t generation = 0;
    uint64_t capacity = package.header.arenaCapacity;
    bool active = false;
    bool candidatePending = false;
    std::string activePhase;
    uint64_t activeOrdinal = 0;
    uint64_t pricingM = 0;
    uint64_t pricingGroups = 0;
    uint64_t pricingStartGroup = 0;
    uint64_t priceOutLiveIn = 0;
    uint64_t scans = 0;
    uint64_t arcStates = 0;
    uint64_t candidateId = 0;
    uint64_t frameStart = 0;
    int64_t candidateReduced = 0;
    std::vector<uint64_t> scannedArcs;
    std::vector<BasketRow> liveOutBasket;
    std::vector<Json> frameRows;

    Json event;
    while (events.next(event)) {
        const uint64_t eventIndex = events.count() - 1U;
        const std::string kind = stringField(event, "kind");
        const std::string phase = stringField(event, "phase_name");
        if (kind == "BEGIN") {
            if (active || nonnegative(event, "order") != expectedOrder)
                throw Error("MCFREG2 call order differs");
            active = true;
            frameStart = eventIndex;
            activePhase = phase;
            activeOrdinal = nonnegative(event, "ordinal");
            if (nonnegative(event, "call") != activeOrdinal)
                throw Error("MCFREG2 call ordinal differs");
            scans = 0;
            arcStates = 0;
            candidatePending = false;
            scannedArcs.clear();
            liveOutBasket.clear();
            frameRows.clear();
            if (phase == "pricing") {
                pricingM = nonnegative(event, "m");
                pricingGroups = nonnegative(event, "nr_group");
                pricingStartGroup = nonnegative(event, "group_pos");
                if (pricingGroups == 0 || pricingStartGroup >= pricingGroups)
                    throw Error("MCFREG2 pricing group state differs");
                (void)boolField(event, "initialize");
            } else if (phase == "price_out") {
                priceOutLiveIn = nonnegative(event, "live_in_m");
                if (nonnegative(event, "generation") != generation ||
                    nonnegative(event, "capacity") != capacity)
                    throw Error("MCFREG2 price-out arena state differs");
            } else {
                throw Error("MCFREG2 call phase is unknown");
            }
            frameRows.push_back(event);
            continue;
        }
        if (!active || phase != activePhase ||
            nonnegative(event, "ordinal") != activeOrdinal)
            throw Error("MCFREG2 event is outside its call frame");
        if (kind == "END" || kind == "ARC_STATE" ||
            kind == "ARENA_REMAP" || kind == "ADJACENCY" ||
            (kind == "BASKET" &&
             (stringField(event, "phase") == "live_in" ||
              stringField(event, "phase") == "live_out")))
            frameRows.push_back(event);
        if (kind == "BASKET")
            expectedBasket.push_back(event);
        if (kind == "ARC_STATE" || kind == "ARENA_REMAP" ||
            kind == "ADJACENCY")
            expectedDeltas.push_back(event);
        if (phase == "pricing" && kind == "SCAN") {
            const int64_t computed = reducedCost(event);
            if (computed != integer(event, "reduced_cost"))
                throw Error("MCFREG2 pricing reduced cost differs");
            const int64_t ident = integer(event, "ident");
            const bool expectedCandidate =
                (computed < 0 && ident == 1) ||
                (computed > 0 && ident == 2);
            if (boolField(event, "candidate") != expectedCandidate)
                throw Error("MCFREG2 pricing candidate differs");
            const uint64_t arc = nonnegative(event, "arc_id");
            if (arc >= pricingM ||
                nonnegative(event, "group_pos") != arc % pricingGroups)
                throw Error("MCFREG2 pricing scan group differs");
            scannedArcs.push_back(arc);
            ++scans;
            emitTrace(
                trace, 1, matched_trace::Opcode::LOAD_U64,
                eventIndex, sequence, ArcAddress + arc * 96U,
                bits(integer(event, "arc_cost")), 0,
                bits(integer(event, "arc_cost")));
            emitTrace(
                trace, 1, matched_trace::Opcode::LOAD_U64,
                eventIndex, sequence,
                PotentialAddress + nonnegative(event, "tail_id") * 8U,
                bits(integer(event, "tail_potential")), 0,
                bits(integer(event, "tail_potential")));
            emitTrace(
                trace, 1, matched_trace::Opcode::LOAD_U64,
                eventIndex, sequence,
                PotentialAddress + nonnegative(event, "head_id") * 8U,
                bits(integer(event, "head_potential")), 0,
                bits(integer(event, "head_potential")));
            const int64_t tailPotential = integer(event, "tail_potential");
            const int64_t partial = checkedSubtract(
                integer(event, "arc_cost"), tailPotential);
            emitTrace(
                trace, 1, matched_trace::Opcode::I64_ADD,
                eventIndex, sequence, 0, bits(integer(event, "arc_cost")),
                UINT64_C(0) - bits(tailPotential), bits(partial));
            emitTrace(
                trace, 1, matched_trace::Opcode::I64_ADD,
                eventIndex, sequence, 0, bits(partial),
                bits(integer(event, "head_potential")), bits(computed));
        } else if (phase == "pricing" && kind == "BASKET") {
            if (stringField(event, "phase") == "live_out")
                liveOutBasket.push_back(BasketRow{
                    nonnegative(event, "slot"), nonnegative(event, "arc_id"),
                    integer(event, "cost"), integer(event, "abs_cost"),
                });
        } else if (phase == "price_out" && kind == "CANDIDATE") {
            if (candidatePending)
                throw Error("MCFREG2 price-out candidate overlaps");
            candidateId = nonnegative(event, "candidate");
            candidateReduced = reducedCost(event);
            if (candidateReduced != integer(event, "reduced_cost"))
                throw Error("MCFREG2 price-out reduced cost differs");
            candidatePending = true;
            arcStates = 0;
            emitTrace(
                trace, 2, matched_trace::Opcode::I64_ADD,
                eventIndex, sequence, 0, bits(integer(event, "arc_cost")),
                UINT64_C(0) - bits(integer(event, "tail_potential")),
                bits(checkedSubtract(
                    integer(event, "arc_cost"),
                    integer(event, "tail_potential"))));
            emitTrace(
                trace, 2, matched_trace::Opcode::I64_ADD,
                eventIndex, sequence, 0,
                bits(checkedSubtract(
                    integer(event, "arc_cost"),
                    integer(event, "tail_potential"))),
                bits(integer(event, "head_potential")),
                bits(candidateReduced));
        } else if (phase == "price_out" && kind == "ARC_STATE") {
            if (!candidatePending ||
                nonnegative(event, "candidate") != candidateId)
                throw Error("MCFREG2 arc delta has no candidate");
            const uint64_t index =
                referenceIndex(event, generation, capacity, true);
            ++arcStates;
            for (unsigned fieldIndex = 0; fieldIndex < 4; ++fieldIndex) {
                const char *names[] = {"cost", "org_cost", "flow", "ident"};
                emitTrace(
                    trace, 2, matched_trace::Opcode::STORE_U64,
                    eventIndex, sequence,
                    DeltaAddress + index * 96U + fieldIndex * 8U,
                    bits(integer(event, names[fieldIndex])), 0,
                    bits(integer(event, names[fieldIndex])));
            }
        } else if (phase == "price_out" && kind == "DECISION") {
            if (!candidatePending ||
                nonnegative(event, "candidate") != candidateId)
                throw Error("MCFREG2 price-out decision has no candidate");
            const std::string decision = stringField(event, "decision");
            if (decision == "NO_CHANGE") {
                referenceIndex(event, generation, capacity, false);
            } else if (decision == "INSERT" || decision == "REPLACE") {
                if (candidateReduced >= 0 || arcStates == 0)
                    throw Error("MCFREG2 price-out mutation differs");
                referenceIndex(event, generation, capacity, true);
            } else {
                throw Error("MCFREG2 price-out decision is unknown");
            }
            candidatePending = false;
        } else if (phase == "price_out" && kind == "ARENA_REMAP") {
            if (candidatePending ||
                nonnegative(event, "old_generation") != generation ||
                nonnegative(event, "new_generation") != generation + 1 ||
                nonnegative(event, "old_capacity") != capacity ||
                nonnegative(event, "new_capacity") <= capacity ||
                nonnegative(event, "mapped_elements") > capacity)
                throw Error("MCFREG2 arena remap differs");
            ++generation;
            capacity = nonnegative(event, "new_capacity");
            emitTrace(
                trace, 2, matched_trace::Opcode::BARRIER,
                eventIndex, sequence, 0, generation - 1, generation,
                capacity);
        } else if (phase == "price_out" && kind == "ADJACENCY") {
            if (nonnegative(event, "node_id") >= package.header.nodes ||
                nonnegative(event, "generation") != generation)
                throw Error("MCFREG2 adjacency reference differs");
        } else if (kind != "END") {
            throw Error("MCFREG2 semantic event kind is unknown");
        }

        if (kind != "END")
            continue;
        if (candidatePending)
            throw Error("MCFREG2 call ends with a pending candidate");
        if (phase == "pricing") {
            if (nonnegative(event, "arcs_priced") != scans ||
                nonnegative(event, "nr_group") != pricingGroups)
                throw Error("MCFREG2 pricing count differs");
            const uint64_t endGroup = nonnegative(event, "group_pos");
            std::vector<uint64_t> expected;
            uint64_t group = pricingStartGroup;
            for (uint64_t pass = 0; pass < pricingGroups; ++pass) {
                for (uint64_t arc = group; arc < pricingM; arc += pricingGroups)
                    expected.push_back(arc);
                group = (group + 1U) % pricingGroups;
                if (group == endGroup)
                    break;
            }
            if (expected != scannedArcs)
                throw Error("MCFREG2 pricing scan order differs");
            for (size_t index = 0; index < liveOutBasket.size(); ++index) {
                if (liveOutBasket[index].slot != index + 1U ||
                    (index && liveOutBasket[index - 1U].absolute <
                                  liveOutBasket[index].absolute))
                    throw Error("MCFREG2 basket sort differs");
            }
            const int64_t selected = integer(event, "selected_arc_id");
            if (liveOutBasket.empty()) {
                if (selected != -1 || integer(event, "reduced_cost") != 0)
                    throw Error("MCFREG2 selected arc differs");
            } else if (selected < 0 ||
                       static_cast<uint64_t>(selected) !=
                           liveOutBasket.front().arc ||
                       integer(event, "reduced_cost") !=
                           liveOutBasket.front().cost) {
                throw Error("MCFREG2 selected arc differs");
            }
            ++summary.pricingCalls;
        } else {
            const uint64_t newArcs = nonnegative(event, "new_arcs");
            if (nonnegative(event, "live_out_m") != priceOutLiveIn + newArcs ||
                nonnegative(event, "capacity") != capacity ||
                nonnegative(event, "generation") != generation)
                throw Error("MCFREG2 price-out live-out differs");
            ++summary.priceOutCalls;
        }
        std::vector<Json> pre;
        std::vector<Json> post;
        for (const Json &row : frameRows) {
            const std::string rowKind = stringField(row, "kind");
            if (rowKind == "BEGIN" ||
                (rowKind == "BASKET" &&
                 stringField(row, "phase") == "live_in"))
                pre.push_back(row);
            if (rowKind == "END" || rowKind == "ARC_STATE" ||
                rowKind == "ARENA_REMAP" || rowKind == "ADJACENCY" ||
                (rowKind == "BASKET" &&
                 stringField(row, "phase") == "live_out"))
                post.push_back(row);
        }
        const auto boundary = boundaries.find(expectedOrder);
        if (boundary == boundaries.end() ||
            sha256Hex(canonicalJson(jsonArray(pre)) + "\n") !=
                boundary->second.first ||
            sha256Hex(canonicalJson(jsonArray(post)) + "\n") !=
                boundary->second.second)
            throw Error("MCFREG2 boundary digest differs");
        const auto indexed = callIndex.find(expectedOrder);
        if (indexed == callIndex.end() ||
            stringField(indexed->second, "phase") != activePhase ||
            nonnegative(indexed->second, "ordinal") != activeOrdinal ||
            nonnegative(indexed->second, "event_begin") != frameStart ||
            nonnegative(indexed->second, "event_count") !=
                eventIndex - frameStart + 1U)
            throw Error("MCFREG2 call index differs");
        emitTrace(
            trace, phase == "pricing" ? 1 : 2,
            matched_trace::Opcode::COMMIT, activeOrdinal, sequence, 0,
            expectedOrder, 0, expectedOrder);
        active = false;
        ++expectedOrder;
    }
    if (events.count() != package.header.eventCount)
        throw Error("MCFREG2 event count differs");
    if (active || summary.pricingCalls != package.header.pricingCalls ||
        summary.priceOutCalls != package.header.priceOutCalls ||
        expectedOrder != boundaries.size() ||
        expectedOrder != callIndex.size())
        throw Error("MCFREG2 replay call counts differ");
    if (canonicalJson(jsonArray(expectedBasket)) !=
            canonicalJson(jsonArray(recordedBasket)))
        throw Error("MCFREG2 basket section differs from events");
    if (canonicalJson(jsonArray(expectedDeltas)) !=
            canonicalJson(jsonArray(recordedDeltas)))
        throw Error("MCFREG2 delta section differs from events");
    summary.operations = sequence;
    summary.traceSha256 = trace.finish();
    std::filesystem::create_directories(outputRoot);
    std::ofstream validation(outputRoot + "/mcfreg2-replay.json");
    if (!validation)
        throw Error("cannot write MCFREG2 replay validation");
    validation << "{\"boundary_mismatches\":0,\"operations\":"
               << summary.operations << ",\"price_out_calls\":"
               << summary.priceOutCalls << ",\"pricing_calls\":"
               << summary.pricingCalls << ",\"status\":\"verified\","
               << "\"trace_sha256\":\"" << summary.traceSha256
               << "\"}\n";
    validation.flush();
    if (!validation)
        throw Error("cannot flush MCFREG2 replay validation");
    return summary;
}

} // namespace mcfreg2
