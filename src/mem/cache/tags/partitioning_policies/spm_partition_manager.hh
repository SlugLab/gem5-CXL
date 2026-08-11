/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#ifndef __MEM_CACHE_TAGS_PARTITIONING_POLICIES_SPM_PARTITION_MANAGER_HH__
#define __MEM_CACHE_TAGS_PARTITIONING_POLICIES_SPM_PARTITION_MANAGER_HH__

#include "mem/cache/tags/partitioning_policies/partition_manager.hh"
#include "params/SpmPartitionManager.hh"

namespace gem5
{

namespace partitioning_policy
{

/** Select a dedicated cache partition for explicitly flagged SPM packets. */
class SpmPartitionManager : public PartitionManager
{
  public:
    PARAMS(SpmPartitionManager);
    SpmPartitionManager(const Params &p);

    uint64_t readPacketPartitionID(PacketPtr pkt) const override;

  private:
    const uint64_t spmPartitionId;
};

} // namespace partitioning_policy
} // namespace gem5

#endif // __MEM_CACHE_TAGS_PARTITIONING_POLICIES_SPM_PARTITION_MANAGER_HH__
