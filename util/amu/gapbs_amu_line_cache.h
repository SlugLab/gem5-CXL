#ifndef GAPBS_AMU_LINE_CACHE_H_
#define GAPBS_AMU_LINE_CACHE_H_

#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

namespace gapbs_amu
{

    constexpr size_t kLineBytes = 64;
    constexpr size_t kFormalThreads = 4;
    constexpr size_t kStagingLinesPerThread = 128;
    constexpr size_t kCacheLinesPerThread = 128;
    constexpr size_t kMaxLogicalValuesPerBatch = 128;
    constexpr size_t kStagingBytesPerThread =
        kStagingLinesPerThread * kLineBytes;
    constexpr size_t kCacheBytesPerThread = kCacheLinesPerThread * kLineBytes;
    constexpr size_t kDataBytesPerThread =
        kStagingBytesPerThread + kCacheBytesPerThread;
    constexpr size_t kTotalDataBytes = kFormalThreads * kDataBytesPerThread;

    static_assert(kStagingBytesPerThread == 8 * 1024,
                  "AMU staging must use 8 KiB per thread");
    static_assert(kCacheBytesPerThread == 8 * 1024,
                  "AMU line cache must use 8 KiB per thread");
    static_assert(kDataBytesPerThread == 16 * 1024,
                  "AMU data must use 16 KiB per thread");
    static_assert(kTotalDataBytes == 64 * 1024,
                  "four AMU workers must use exactly 64 KiB");
    static_assert(
        (kStagingLinesPerThread & (kStagingLinesPerThread - 1)) == 0,
        "AMU staging line count must be a power of two");

    static inline size_t physical_staging_slot(size_t logical)
    {
        size_t reversed = 0;
        for (size_t span = kStagingLinesPerThread; span > 1; span >>= 1)
        {
            reversed = (reversed << 1) | (logical & 1);
            logical >>= 1;
        }
        return reversed;
    }

    struct LineCounters
    {
        uint64_t logical_values;
        uint64_t line_requests;
        uint64_t cache_hits;
        uint64_t coalesced_misses;
    };

    template <class Backend>
    class LineBatch;

    template <class Backend>
    class LineStore
    {
    public:
        explicit LineStore(Backend &backend) : backend_(backend)
        {
            begin_trial();
        }

        void begin_trial()
        {
            memset(&counters_, 0, sizeof(counters_));
            memset(cache_valid_, 0, sizeof(cache_valid_));
        }

        void reset_iteration()
        {
            memset(cache_valid_, 0, sizeof(cache_valid_));
        }

        LineCounters counters() const { return counters_; }

        [[noreturn]] void fail_contract(const char *message)
        {
            fail(message);
        }

        unsigned char *staging_line(size_t line_slot)
        {
            if (line_slot >= kStagingLinesPerThread)
                fail("AMU staging line is outside the fixed budget");
            return staging_[physical_staging_slot(line_slot)];
        }

        bool cache_lookup(uintptr_t line_address, unsigned char *destination)
        {
            const size_t slot = cache_slot(line_address);
            if (!cache_valid_[slot] || cache_tags_[slot] != line_address)
                return false;
            memcpy(destination, cache_[slot], kLineBytes);
            return true;
        }

        void cache_install(
            uintptr_t line_address, const unsigned char *source)
        {
            const size_t slot = cache_slot(line_address);
            memcpy(cache_[slot], source, kLineBytes);
            cache_tags_[slot] = line_address;
            cache_valid_[slot] = true;
        }

    private:
        template <class OtherBackend>
        friend class LineBatch;

        static size_t cache_slot(uintptr_t line_address)
        {
            return (line_address / kLineBytes) % kCacheLinesPerThread;
        }

        [[noreturn]] void fail(const char *message)
        {
            backend_.fail(message);
            abort();
        }

        Backend &backend_;
        alignas(64) unsigned char staging_[kStagingLinesPerThread][kLineBytes];
        alignas(64) unsigned char cache_[kCacheLinesPerThread][kLineBytes];
        uintptr_t cache_tags_[kCacheLinesPerThread];
        bool cache_valid_[kCacheLinesPerThread];
        LineCounters counters_;
    };

    template <class Backend>
    class LineBatch
    {
    public:
        explicit LineBatch(LineStore<Backend> &store) : store_(store)
        {
            clear_unchecked();
        }

        template <class Value>
        size_t add(const Value *address)
        {
            if (issued_)
                store_.fail("cannot add an AMU value after issue");
            if (logical_count_ >= kMaxLogicalValuesPerBatch)
                store_.fail("AMU logical batch exceeds its fixed capacity");
            const uintptr_t value_address = reinterpret_cast<uintptr_t>(address);
            const uintptr_t line_address =
                value_address & ~uintptr_t(kLineBytes - 1);
            const size_t offset = value_address - line_address;
            if (sizeof(Value) > kLineBytes || offset + sizeof(Value) > kLineBytes)
                store_.fail("AMU logical value crosses a cache-line boundary");

            size_t batch_line = line_count_;
            for (size_t line = 0; line < line_count_; ++line)
            {
                if (line_addresses_[line] == line_address)
                {
                    batch_line = line;
                    if (line_miss_[line])
                        ++store_.counters_.coalesced_misses;
                    break;
                }
            }
            if (batch_line == line_count_)
            {
                if (line_count_ >= kStagingLinesPerThread)
                    store_.fail("AMU line batch exceeds its staging capacity");
                line_addresses_[batch_line] = line_address;
                line_ids_[batch_line] = 0;
                line_complete_[batch_line] = false;
                unsigned char *staging = store_.staging_line(batch_line);
                if (store_.cache_lookup(line_address, staging))
                {
                    line_miss_[batch_line] = false;
                    line_complete_[batch_line] = true;
                    ++store_.counters_.cache_hits;
                }
                else
                {
                    line_miss_[batch_line] = true;
                }
                ++line_count_;
            }

            logical_lines_[logical_count_] = batch_line;
            logical_offsets_[logical_count_] = offset;
            logical_sizes_[logical_count_] = sizeof(Value);
            ++store_.counters_.logical_values;
            return logical_count_++;
        }

        void issue_all()
        {
            if (issued_)
                store_.fail("AMU line batch was issued more than once");
            issued_ = true;
            outstanding_ = 0;
            for (size_t line = 0; line < line_count_; ++line)
            {
                if (!line_miss_[line])
                    continue;
                const uint64_t id = store_.backend_.load(
                    store_.staging_line(line),
                    reinterpret_cast<const void *>(line_addresses_[line]),
                    kLineBytes);
                if (id == 0)
                    store_.fail("AMU line request returned an invalid ID");
                line_ids_[line] = id;
                ++outstanding_;
                ++store_.counters_.line_requests;
            }
            complete_ = outstanding_ == 0;
        }

        void wait_all()
        {
            if (!issued_)
                store_.fail("cannot wait for an unissued AMU line batch");
            while (outstanding_ != 0)
            {
                const uint64_t id = store_.backend_.getfin();
                if (id == 0)
                    continue;
                bool matched = false;
                for (size_t line = 0; line < line_count_; ++line)
                {
                    if (line_miss_[line] && !line_complete_[line] &&
                        line_ids_[line] == id)
                    {
                        line_complete_[line] = true;
                        store_.cache_install(
                            line_addresses_[line], store_.staging_line(line));
                        --outstanding_;
                        matched = true;
                        break;
                    }
                }
                if (!matched)
                    store_.fail("AMU line batch received an unmatched completion");
            }
            complete_ = true;
        }

        template <class Value>
        Value value(size_t logical_slot) const
        {
            if (!complete_ || logical_slot >= logical_count_)
                store_.fail("AMU line value read before batch completion");
            if (logical_sizes_[logical_slot] != sizeof(Value))
                store_.fail("AMU line value type differs from its request");
            Value result;
            const size_t batch_line = logical_lines_[logical_slot];
            memcpy(
                &result,
                store_.staging_line(batch_line) +
                    logical_offsets_[logical_slot],
                sizeof(Value));
            return result;
        }

        void clear()
        {
            if (issued_ && !complete_)
                store_.fail("cannot clear an incomplete AMU line batch");
            clear_unchecked();
        }

    private:
        void clear_unchecked()
        {
            line_count_ = 0;
            logical_count_ = 0;
            outstanding_ = 0;
            issued_ = false;
            complete_ = false;
            memset(line_ids_, 0, sizeof(line_ids_));
            memset(line_complete_, 0, sizeof(line_complete_));
            memset(line_miss_, 0, sizeof(line_miss_));
        }

        LineStore<Backend> &store_;
        uintptr_t line_addresses_[kStagingLinesPerThread];
        uint64_t line_ids_[kStagingLinesPerThread];
        bool line_miss_[kStagingLinesPerThread];
        bool line_complete_[kStagingLinesPerThread];
        size_t logical_lines_[kMaxLogicalValuesPerBatch];
        size_t logical_offsets_[kMaxLogicalValuesPerBatch];
        size_t logical_sizes_[kMaxLogicalValuesPerBatch];
        size_t line_count_;
        size_t logical_count_;
        size_t outstanding_;
        bool issued_;
        bool complete_;
    };

} // namespace gapbs_amu

#endif // GAPBS_AMU_LINE_CACHE_H_