/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 *
 * Run the compiler-emitted CIRA ORC dispatch twice and serialize the selected
 * steady-state plan for a gem5 replay.  The first call is deliberately before
 * the replay ROI; the second must reuse the same ORC cache entry.
 */

#include <cstdint>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>

#include "CiraRuntime.h"
#include "cira_jit.h"
#include "cira_jit_engine.h"
#include "cira_vortex_jit.h"

namespace {

uint64_t
parseU64(const char *value, const char *name)
{
    try {
        return std::stoull(value);
    } catch (const std::exception &) {
        throw std::runtime_error(std::string("invalid ") + name);
    }
}

struct Profile
{
    uint64_t hostNs = 0;
    uint64_t deviceNs = 0;
    uint64_t elements = 0;
    uint64_t templateTag = 0;
    bool hardwareCeilingPlan = false;
    std::string vortexTemplate;
    std::string vortexTemplateId;
};

Profile
parseArgs(int argc, char **argv, std::string &out)
{
    Profile profile;
    for (int index = 1; index < argc; index += 2) {
        if (index + 1 >= argc)
            throw std::runtime_error("JIT option has no value");
        const std::string option = argv[index];
        const char *value = argv[index + 1];
        if (option == "--out")
            out = value;
        else if (option == "--host-ns")
            profile.hostNs = parseU64(value, "host-ns");
        else if (option == "--device-ns")
            profile.deviceNs = parseU64(value, "device-ns");
        else if (option == "--elements")
            profile.elements = parseU64(value, "elements");
        else if (option == "--template-tag")
            profile.templateTag = parseU64(value, "template-tag");
        else if (option == "--hardware-ceiling-plan")
            profile.hardwareCeilingPlan =
                parseU64(value, "hardware-ceiling-plan") != 0;
        else if (option == "--vortex-template")
            profile.vortexTemplate = value;
        else if (option == "--vortex-template-id")
            profile.vortexTemplateId = value;
        else
            throw std::runtime_error("unknown JIT option: " + option);
    }
    if (out.empty() || profile.hostNs == 0 || profile.deviceNs == 0 ||
        profile.elements == 0 || profile.templateTag == 0)
        throw std::runtime_error("JIT profile is incomplete");
    return profile;
}

} // anonymous namespace

int
main(int argc, char **argv)
{
    try {
        std::string output;
        const Profile profile = parseArgs(argc, argv, output);
        // A trace replay contains no modeled compute instructions.  Its device
        // timing is therefore classified wholly as memory stall, rather than
        // inventing a compute/stall split.
        const uint64_t deviceCycles = profile.deviceNs * 2000 / 1000;
        cira_jit_workload_t workload{};
        workload.host_independent_work_ns = profile.hostNs;
        workload.vortex_total_time_ns = profile.deviceNs;
        workload.vortex_memory_stall_cycles = deviceCycles;
        workload.vortex_total_cycles = deviceCycles;
        workload.num_elements = profile.elements;
        workload.host_per_elem_ns =
            static_cast<double>(profile.hostNs) / profile.elements;
        cira_jit_decision_t decision{};
        const cira_hw_limits_t limits = cira_jit_default_limits();
        cira_jit_decide(&workload, &limits, &decision);
        const cira_jit_decision_t costModelDecision = decision;
        // A labelled upper-envelope sensitivity point. This retains the real
        // ORC dispatch/cache-hit validation below, but does not claim that
        // every region benefits from saturating every hardware resource.
        if (profile.hardwareCeilingPlan) {
            decision.batch_size = limits.max_batch_size;
            decision.traversal_depth = limits.max_traversal_depth;
            decision.pipeline_distance = limits.max_pipeline_distance;
        }

        // Compile a PGO-specialized device image twice before the modeled ROI.
        // This verifies the real .vxbin cache without falsely claiming that
        // this host-only preflight uploaded or executed the image.
        std::string vortexPath;
        if (!profile.vortexTemplate.empty()) {
            cira_vortex_jit_spec_t deviceSpec{};
            deviceSpec.source_path = profile.vortexTemplate.c_str();
            deviceSpec.template_id = profile.vortexTemplateId.empty() ?
                nullptr : profile.vortexTemplateId.c_str();
            deviceSpec.decision = decision;
            char firstPath[4096] = {};
            char secondPath[4096] = {};
            const int firstResult = cira_vortex_jit_compile(
                &deviceSpec, firstPath, sizeof(firstPath));
            const int secondResult = cira_vortex_jit_compile(
                &deviceSpec, secondPath, sizeof(secondPath));
            if (firstResult != CIRA_VORTEX_JIT_OK ||
                secondResult != CIRA_VORTEX_JIT_OK ||
                std::string(firstPath).empty() ||
                std::string(firstPath) != std::string(secondPath)) {
                throw std::runtime_error(
                    "Vortex device JIT compile/cache verification failed");
            }
            vortexPath = firstPath;
        }

        auto &engine = cira::CiraJitEngine::shared();
        engine.resetCache();
        void *operands[] = {nullptr};
        void *first = cira::runtime::cira_future_alloc();
        const int coldRc = cira::runtime::cira_jit_dispatch(
            nullptr, operands, 1, first, profile.hostNs, profile.deviceNs,
            0, deviceCycles, deviceCycles, 0, 0, profile.elements,
            profile.templateTag);
        if (first) {
            (void)cira::runtime::cira_future_await(first);
            cira::runtime::cira_future_free(first);
        }
        const size_t coldCacheEntries = engine.cacheSize();
        void *second = cira::runtime::cira_future_alloc();
        const int warmRc = cira::runtime::cira_jit_dispatch(
            nullptr, operands, 1, second, profile.hostNs, profile.deviceNs,
            0, deviceCycles, deviceCycles, 0, 0, profile.elements,
            profile.templateTag);
        if (second) {
            (void)cira::runtime::cira_future_await(second);
            cira::runtime::cira_future_free(second);
        }
        const size_t warmCacheEntries = engine.cacheSize();
        if (coldRc != 1 || warmRc != 1 || coldCacheEntries == 0 ||
            warmCacheEntries != coldCacheEntries)
            throw std::runtime_error("ORC cold/cache-hit dispatch verification failed");

        std::ofstream stream(output);
        if (!stream)
            throw std::runtime_error("cannot write JIT plan");
        stream << "{\"schema\":1,\"status\":\"pass\","
               << "\"steady_state\":true,\"vortex_device_jit\":"
               << (!vortexPath.empty() ? "true" : "false") << ","
               << "\"selection\":\""
               << (profile.hardwareCeilingPlan ? "hardware_ceiling_sensitivity"
                                               : "cost_model")
               << "\","
               << "\"profile\":{\"host_ns\":" << profile.hostNs
               << ",\"device_ns\":" << profile.deviceNs
               << ",\"elements\":" << profile.elements
               << ",\"device_compute_cycles\":0,\"device_memory_stall_cycles\":"
               << deviceCycles << "},\"orc\":{\"cold_rc\":" << coldRc
               << ",\"warm_rc\":" << warmRc << ",\"cold_cache_entries\":"
               << coldCacheEntries << ",\"warm_cache_entries\":"
               << warmCacheEntries << "},\"plan\":{\"batch_size\":"
               << decision.batch_size << ",\"traversal_depth\":"
               << decision.traversal_depth << ",\"pipeline_distance\":"
               << decision.pipeline_distance << ",\"template_tag\":"
               << profile.templateTag << ",\"should_offload\":"
               << (decision.should_offload ? "true" : "false")
               << ",\"reason_bits\":" << decision.reason_bits
               << "},\"cost_model_plan\":{\"batch_size\":"
               << costModelDecision.batch_size << ",\"traversal_depth\":"
               << costModelDecision.traversal_depth
               << ",\"pipeline_distance\":"
               << costModelDecision.pipeline_distance
               << "},\"vortex\":{\"artifact\":\"" << vortexPath
               << "\",\"compiled_and_cached\":"
               << (!vortexPath.empty() ? "true" : "false")
               << ",\"uploaded_or_executed\":false}}\n";
        if (!stream)
            throw std::runtime_error("JIT plan write failed");
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "CIRA_JIT_STEADY_STATE_ERROR " << error.what() << '\n';
        return 1;
    }
}
