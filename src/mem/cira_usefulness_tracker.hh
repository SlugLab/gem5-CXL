/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#ifndef __MEM_CIRA_USEFULNESS_TRACKER_HH__
#define __MEM_CIRA_USEFULNESS_TRACKER_HH__

#include <cassert>
#include <cstdint>
#include <unordered_map>

namespace gem5
{

class CiraLineUsefulnessTracker
{
  public:
    enum class DemandAttribution
    {
        None,
        Useful,
        Late
    };

    explicit CiraLineUsefulnessTracker(uint64_t line_size)
        : lineSize(line_size)
    {
        assert(lineSize != 0);
    }

    void
    issue(uint64_t addr)
    {
        ++lines[lineAddress(addr)].outstandingRefs;
    }

    void
    prefetchHit(uint64_t addr)
    {
        const auto line = lineAddress(addr);
        const auto it = lines.find(line);
        if (it == lines.end())
            return;

        LineState &state = it->second;
        if (state.outstandingRefs == 0) {
            if (state.suppressFill && !state.completed)
                lines.erase(it);
            return;
        }

        --state.outstandingRefs;
        if (state.outstandingRefs == 0 && !state.completed)
            lines.erase(it);
    }

    void
    fill(uint64_t addr, bool cira_origin)
    {
        const auto it = lines.find(lineAddress(addr));
        if (it == lines.end())
            return;

        LineState &state = it->second;
        if (!cira_origin || state.suppressFill) {
            lines.erase(it);
            return;
        }

        if (state.outstandingRefs != 0) {
            state.outstandingRefs = 0;
            state.completed = true;
        }
    }

    DemandAttribution
    demand(uint64_t addr)
    {
        const auto it = lines.find(lineAddress(addr));
        if (it == lines.end())
            return DemandAttribution::None;

        LineState &state = it->second;
        if (state.completed) {
            lines.erase(it);
            return DemandAttribution::Useful;
        }

        if (state.outstandingRefs != 0) {
            state.outstandingRefs = 0;
            state.suppressFill = true;
            return DemandAttribution::Late;
        }

        return DemandAttribution::None;
    }

    uint32_t
    outstandingRefs(uint64_t addr) const
    {
        const auto it = lines.find(lineAddress(addr));
        return it == lines.end() ? 0 : it->second.outstandingRefs;
    }

    void
    clear()
    {
        lines.clear();
    }

  private:
    struct LineState
    {
        uint32_t outstandingRefs = 0;
        bool completed = false;
        bool suppressFill = false;
    };

    uint64_t
    lineAddress(uint64_t addr) const
    {
        return addr - addr % lineSize;
    }

    const uint64_t lineSize;
    std::unordered_map<uint64_t, LineState> lines;
};

} // namespace gem5

#endif // __MEM_CIRA_USEFULNESS_TRACKER_HH__
