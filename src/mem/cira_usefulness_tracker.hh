/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#ifndef __MEM_CIRA_USEFULNESS_TRACKER_HH__
#define __MEM_CIRA_USEFULNESS_TRACKER_HH__

#include <cassert>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <unordered_map>
#include <utility>

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

    explicit CiraLineUsefulnessTracker(
        uint64_t line_size, size_t max_completed_lines = 4096)
        : lineSize(line_size), maxCompletedLines(max_completed_lines)
    {
        assert(lineSize != 0);
        assert(maxCompletedLines != 0);
    }

    void
    issue(uint64_t addr)
    {
        const auto line = lineAddress(addr);
        auto [it, inserted] = lines.try_emplace(line);
        if (inserted)
            it->second.generation = nextGeneration++;
        ++it->second.outstandingRefs;
    }

    bool
    issueIfAbsent(uint64_t addr)
    {
        const auto line = lineAddress(addr);
        auto [it, inserted] = lines.try_emplace(line);
        if (!inserted)
            return false;

        it->second.outstandingRefs = 1;
        it->second.generation = nextGeneration++;
        return true;
    }

    bool
    tracked(uint64_t addr) const
    {
        return lines.find(lineAddress(addr)) != lines.end();
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
            if (!state.completed) {
                state.completed = true;
                completedLines.emplace_back(it->first, state.generation);
                trimCompletedLines();
            }
        }
    }

    DemandAttribution
    demand(uint64_t addr, bool hit)
    {
        const auto it = lines.find(lineAddress(addr));
        if (it == lines.end())
            return DemandAttribution::None;

        LineState &state = it->second;
        if (hit) {
            if (state.completed) {
                lines.erase(it);
                return DemandAttribution::Useful;
            }

            if (state.outstandingRefs != 0)
                lines.erase(it);
            return DemandAttribution::None;
        }

        if (state.outstandingRefs != 0) {
            state.outstandingRefs = 0;
            state.completed = false;
            state.suppressFill = true;
            return DemandAttribution::Late;
        }

        if (state.completed)
            lines.erase(it);
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
        completedLines.clear();
        nextGeneration = 1;
    }

  private:
    struct LineState
    {
        uint32_t outstandingRefs = 0;
        bool completed = false;
        bool suppressFill = false;
        uint64_t generation = 0;
    };

    void
    trimCompletedLines()
    {
        while (completedLines.size() > maxCompletedLines) {
            const auto [line, generation] = completedLines.front();
            completedLines.pop_front();

            const auto it = lines.find(line);
            if (it != lines.end() && it->second.completed &&
                it->second.generation == generation) {
                lines.erase(it);
            }
        }
    }

    uint64_t
    lineAddress(uint64_t addr) const
    {
        return addr - addr % lineSize;
    }

    const uint64_t lineSize;
    const size_t maxCompletedLines;
    uint64_t nextGeneration = 1;
    std::unordered_map<uint64_t, LineState> lines;
    std::deque<std::pair<uint64_t, uint64_t>> completedLines;
};

} // namespace gem5

#endif // __MEM_CIRA_USEFULNESS_TRACKER_HH__
