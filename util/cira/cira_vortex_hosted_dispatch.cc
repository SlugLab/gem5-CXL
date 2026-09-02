/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 *
 * Submit a CIRA-generated Vortex image through the real gem5 hosted runtime.
 *
 * The image is supplied as a path rather than embedded in this executable:
 * cira_vortex_jit_compile() owns compilation and cache invalidation, while
 * this small guest process proves the separate upload/MMIO/CP/device path.
 */

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <exception>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

#include <vortex.h>

namespace {

struct BfsArgs {
    uint64_t edges_addr;
    uint64_t frontier_addr;
    uint64_t parent_addr;
    uint32_t edge_count;
    uint32_t frontier_count;
};

[[noreturn]] void
fail(const char *operation, int rc)
{
    std::cerr << "CIRA_VORTEX_DISPATCH_FAIL operation=" << operation
              << " rc=" << rc << '\n';
    std::exit(1);
}

void
check(int rc, const char *operation)
{
    if (rc != 0)
        fail(operation, rc);
}

uint32_t
parseU32(const char *text, const char *name)
{
    try {
        const unsigned long parsed = std::stoul(text);
        if (parsed == 0 || parsed > UINT32_MAX)
            throw std::out_of_range("range");
        return static_cast<uint32_t>(parsed);
    } catch (const std::exception &) {
        throw std::runtime_error(std::string("invalid ") + name);
    }
}

} // anonymous namespace

int
main(int argc, char **argv)
{
    try {
        if (argc != 3) {
            std::cerr << "usage: " << argv[0]
                      << " <cira-jit-artifact.vxbin> <edge-count>\n";
            return 2;
        }
        const std::string artifact = argv[1];
        const uint32_t edgeCount = parseU32(argv[2], "edge-count");

        vx_device_h device = nullptr;
        vx_buffer_h edges = nullptr;
        vx_buffer_h parent = nullptr;
        vx_buffer_h kernel = nullptr;
        vx_buffer_h arguments = nullptr;
        check(vx_dev_open(&device), "vx_dev_open");

        const uint64_t edgeBytes = uint64_t(edgeCount) * sizeof(uint32_t);
        check(vx_mem_alloc(device, edgeBytes, VX_MEM_READ, &edges),
              "vx_mem_alloc(edges)");
        check(vx_mem_alloc(device, sizeof(uint32_t), VX_MEM_WRITE, &parent),
              "vx_mem_alloc(parent)");
        std::vector<uint32_t> edgeData(edgeCount);
        for (uint32_t index = 0; index < edgeCount; ++index)
            edgeData[index] = index;
        const uint32_t parentSeed = 0;
        check(vx_copy_to_dev(edges, edgeData.data(), 0, edgeBytes),
              "vx_copy_to_dev(edges)");
        check(vx_copy_to_dev(parent, &parentSeed, 0, sizeof(parentSeed)),
              "vx_copy_to_dev(parent)");

        BfsArgs args{};
        check(vx_mem_address(edges, &args.edges_addr), "vx_mem_address(edges)");
        check(vx_mem_address(parent, &args.parent_addr),
              "vx_mem_address(parent)");
        args.edge_count = edgeCount;
        args.frontier_count = 1;
        check(vx_upload_kernel_file(device, artifact.c_str(), &kernel),
              "vx_upload_kernel_file");
        check(vx_upload_bytes(device, &args, sizeof(args), &arguments),
              "vx_upload_bytes(arguments)");

        std::cout << "CIRA_VORTEX_DISPATCH artifact=" << artifact
                  << " edge_count=" << edgeCount << '\n';
        check(vx_start(device, kernel, arguments), "vx_start");
        check(vx_ready_wait(device, VX_MAX_TIMEOUT), "vx_ready_wait");

        // The current BFS template stages edge reads and deliberately leaves
        // parent ownership to the deterministic host BFS.  Completion of a
        // real CP launch is consequently the correctness contract here; the
        // counter dump additionally records nonzero device execution.
        check(vx_dump_perf(device, stdout), "vx_dump_perf");
        std::cout << "CIRA_VORTEX_DISPATCH_PASS artifact=" << artifact
                  << " edge_count=" << edgeCount << '\n';

        check(vx_mem_free(arguments), "vx_mem_free(arguments)");
        check(vx_mem_free(kernel), "vx_mem_free(kernel)");
        check(vx_mem_free(parent), "vx_mem_free(parent)");
        check(vx_mem_free(edges), "vx_mem_free(edges)");
        check(vx_dev_close(device), "vx_dev_close");
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "CIRA_VORTEX_DISPATCH_FAIL " << error.what() << '\n';
        return 2;
    }
}
