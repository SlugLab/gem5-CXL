/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#ifndef __UTIL_AMU_MATCHED_WORKLOADS_MCFREG2_FORMAT_H__
#define __UTIL_AMU_MATCHED_WORKLOADS_MCFREG2_FORMAT_H__

#include <stdint.h>

#define MCFREG2_SCHEMA UINT16_C(3)
#define MCFREG2_ENDIAN_TAG UINT16_C(0x0102)
#define MCFREG2_OPTIONAL_FLAG UINT32_C(1)

enum
{
    MCFREG2_PROVENANCE = 1,
    MCFREG2_NETWORK = 2,
    MCFREG2_NODES = 3,
    MCFREG2_ARCS = 4,
    MCFREG2_BASKET = 5,
    MCFREG2_CALL_INDEX = 6,
    MCFREG2_EVENTS = 7,
    MCFREG2_DELTAS = 8,
    MCFREG2_BOUNDARIES = 9,
    MCFREG2_FINAL = 10,
};

enum McfReg2RecordRole
{
    MCFREG2_ROLE_LIVE_IN = 1,
    MCFREG2_ROLE_OBSERVED_RESULT = 2,
};

enum McfReg2RecordKind
{
    MCFREG2_CALL_BEGIN = 1,
    MCFREG2_PRICING_SCAN_LIVE_IN = 2,
    MCFREG2_PRICING_CANDIDATE_OBSERVED = 3,
    MCFREG2_BASKET_LIVE_IN = 4,
    MCFREG2_BASKET_LIVE_OUT_OBSERVED = 5,
    MCFREG2_PRICING_END_OBSERVED = 6,
    MCFREG2_PRICE_OUT_STATE_LIVE_IN = 7,
    MCFREG2_PRICE_OUT_CANDIDATE_OBSERVED = 8,
    MCFREG2_PRICE_OUT_DECISION_OBSERVED = 9,
    MCFREG2_ARC_FINAL_OBSERVED = 10,
    MCFREG2_REMAP_OBSERVED = 11,
    MCFREG2_ADJACENCY_FINAL_OBSERVED = 12,
    MCFREG2_PRICE_OUT_END_OBSERVED = 13,
    MCFREG2_CALL_END = 14,
};

enum
{
    MCFREG2_OBJECT_NULL = 0,
    MCFREG2_OBJECT_NODE = 1,
    MCFREG2_OBJECT_ARC = 2,
    MCFREG2_OBJECT_DUMMY_ARC = 3,
};

#pragma pack(push, 1)
typedef struct McfReg2Header
{
    char magic[8];
    uint16_t schema;
    uint16_t endianTag;
    uint32_t headerBytes;
    uint64_t flags;
    uint64_t sectionCount;
    uint64_t directoryOffset;
    uint64_t nodes;
    uint64_t activeArcs;
    uint64_t dummyArcs;
    uint64_t arenaCapacity;
    uint64_t pricingCalls;
    uint64_t priceOutCalls;
    uint64_t eventCount;
    uint64_t reserved;
} McfReg2Header;

typedef struct McfReg2DirectoryEntry
{
    uint16_t sectionType;
    uint16_t schema;
    uint32_t flags;
    uint64_t offset;
    uint64_t storedBytes;
    uint64_t elementCount;
    uint64_t elementSize;
    uint8_t sha256[32];
} McfReg2DirectoryEntry;

typedef struct McfStableRef
{
    uint32_t kind;
    uint32_t generation;
    uint64_t objectId;
} McfStableRef;
#pragma pack(pop)

#ifdef __cplusplus
static_assert(sizeof(McfReg2Header) == 104, "MCFREG2 header drift");
static_assert(
    sizeof(McfReg2DirectoryEntry) == 72, "MCFREG2 directory drift");
static_assert(sizeof(McfStableRef) == 16, "MCFREG2 reference drift");
#else
_Static_assert(sizeof(McfReg2Header) == 104, "MCFREG2 header drift");
_Static_assert(
    sizeof(McfReg2DirectoryEntry) == 72, "MCFREG2 directory drift");
_Static_assert(sizeof(McfStableRef) == 16, "MCFREG2 reference drift");
#endif

#endif /* __UTIL_AMU_MATCHED_WORKLOADS_MCFREG2_FORMAT_H__ */
