/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#include "mem/cache/tags/partitioning_policies/spm_partition_manager.hh"

#include "base/logging.hh"

namespace gem5
{

namespace partitioning_policy
{

SpmPartitionManager::SpmPartitionManager(const Params &p)
  : PartitionManager(p), spmPartitionId(p.spm_partition_id)
{
    fatal_if(spmPartitionId == 0,
             "The SPM partition ID must differ from the CPU partition");
    fatal_if(partitioningPolicies.empty(),
             "The SPM partition manager requires an allocation policy");
}

uint64_t
SpmPartitionManager::readPacketPartitionID(PacketPtr pkt) const
{
    panic_if(!pkt || !pkt->req, "Cannot partition a request-less packet");
    if (pkt->req->isSpmAccess()) {
        return spmPartitionId;
    }
    return 0;
}

} // namespace partitioning_policy
} // namespace gem5
