/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#include "mcf_capture.h"

#include <errno.h>
#include <inttypes.h>
#include <limits.h>
#define OPENSSL_SUPPRESS_DEPRECATED
#include <openssl/sha.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <zlib.h>

typedef struct allocation_state
{
    uint64_t nodes;
    uint64_t dummy_arcs;
    uint64_t arcs;
    uint64_t peak;
} allocation_state_t;

typedef struct byte_buffer
{
    unsigned char *data;
    size_t size;
    size_t capacity;
} byte_buffer_t;

typedef struct arena_history
{
    uintptr_t base;
    uint64_t capacity;
    uint32_t generation;
} arena_history_t;

static char *capture_input;
static char *capture_root;
static char *capture_output;
static int capture_events;
static unsigned roi_begin_count;
static unsigned roi_end_count;
static allocation_state_t allocation_state;
static gzFile allocation_stream;
static gzFile boundary_stream;
static uint64_t allocation_events;
static const network_t *capture_network;
static gzFile pricing_stream;
static uint64_t pricing_calls;
static uint64_t pricing_scan_count;
static int pricing_scan_pending;
static const arc_t *pricing_scan_arc;
static long pricing_scan_position;
static long pricing_nr_group;
static long pricing_live_in_expected;
static long pricing_live_in_seen;
static long pricing_live_out_seen;
static int pricing_active;
static uint64_t call_order;
static uint64_t pricing_order;
static long pricing_m;
static long pricing_group_pos_in;
static int pricing_initialize_in;
static byte_buffer_t pricing_live_basket;
static byte_buffer_t pricing_scans;
static byte_buffer_t pricing_candidates;
static byte_buffer_t pricing_result_basket;
static gzFile price_out_stream;
static uint64_t price_out_calls;
static uint64_t price_out_candidates;
static long price_out_live_in_m;
static int price_out_active;
static uint64_t price_out_order;
static int price_out_candidate_pending;
static const node_t *price_out_tail;
static const node_t *price_out_head;
static cost_t price_out_reduced_cost;
static int price_out_remapped;
static const arc_t *capture_arc_base;
static uint64_t capture_arc_capacity;
static uint32_t capture_arc_generation;
static arena_history_t *arena_history;
static size_t arena_history_count;
static size_t arena_history_capacity;
static byte_buffer_t price_out_candidates_buffer;
static byte_buffer_t price_out_decisions_buffer;
static char price_out_pre_sha256[65];

static int resolved_arc_reference(
    const arc_t *arc, uint32_t *kind, uint32_t *generation,
    uint64_t *index);

static void
buffer_reset(byte_buffer_t *buffer)
{
    buffer->size = 0;
}

static void
buffer_free(byte_buffer_t *buffer)
{
    free(buffer->data);
    memset(buffer, 0, sizeof(*buffer));
}

static int
buffer_append(byte_buffer_t *buffer, const void *data, size_t bytes)
{
    size_t required;
    size_t capacity;
    unsigned char *resized;
    if (bytes > SIZE_MAX - buffer->size)
        return -1;
    required = buffer->size + bytes;
    if (required > buffer->capacity) {
        capacity = buffer->capacity ? buffer->capacity : 256;
        while (capacity < required) {
            if (capacity > SIZE_MAX / 2) {
                capacity = required;
                break;
            }
            capacity *= 2;
        }
        resized = (unsigned char *)realloc(buffer->data, capacity);
        if (!resized)
            return -1;
        buffer->data = resized;
        buffer->capacity = capacity;
    }
    if (bytes)
        memcpy(buffer->data + buffer->size, data, bytes);
    buffer->size = required;
    return 0;
}

static int
buffer_u8(byte_buffer_t *buffer, uint8_t value)
{
    return buffer_append(buffer, &value, sizeof(value));
}

static int
buffer_u32(byte_buffer_t *buffer, uint32_t value)
{
    unsigned char bytes[4];
    unsigned index;
    for (index = 0; index < sizeof(bytes); ++index)
        bytes[index] = (unsigned char)(value >> (8 * index));
    return buffer_append(buffer, bytes, sizeof(bytes));
}

static int
buffer_u64(byte_buffer_t *buffer, uint64_t value)
{
    unsigned char bytes[8];
    unsigned index;
    for (index = 0; index < sizeof(bytes); ++index)
        bytes[index] = (unsigned char)(value >> (8 * index));
    return buffer_append(buffer, bytes, sizeof(bytes));
}

static int
buffer_i64(byte_buffer_t *buffer, int64_t value)
{
    return buffer_u64(buffer, (uint64_t)value);
}

static int
buffer_ref(
    byte_buffer_t *buffer, uint32_t kind, uint32_t generation,
    uint64_t index)
{
    return buffer_u32(buffer, kind) || buffer_u32(buffer, generation) ||
           buffer_u64(buffer, index) ? -1 : 0;
}

static int
sha_update(SHA256_CTX *context, const void *data, size_t bytes)
{
    if (bytes == 0)
        return 0;
    return SHA256_Update(context, data, bytes) == 1 ? 0 : -1;
}

static int
sha_u8(SHA256_CTX *context, uint8_t value)
{
    return sha_update(context, &value, sizeof(value));
}

static int
sha_u32(SHA256_CTX *context, uint32_t value)
{
    unsigned char bytes[4];
    unsigned index;
    for (index = 0; index < sizeof(bytes); ++index)
        bytes[index] = (unsigned char)(value >> (8 * index));
    return sha_update(context, bytes, sizeof(bytes));
}

static int
sha_u64(SHA256_CTX *context, uint64_t value)
{
    unsigned char bytes[8];
    unsigned index;
    for (index = 0; index < sizeof(bytes); ++index)
        bytes[index] = (unsigned char)(value >> (8 * index));
    return sha_update(context, bytes, sizeof(bytes));
}

static int
sha_i64(SHA256_CTX *context, int64_t value)
{
    return sha_u64(context, (uint64_t)value);
}

static int
sha_ref(
    SHA256_CTX *context, uint32_t kind, uint32_t generation,
    uint64_t index)
{
    return sha_u32(context, kind) || sha_u32(context, generation) ||
           sha_u64(context, index) ? -1 : 0;
}

static int
sha_finish(SHA256_CTX *context, char output[65])
{
    unsigned char digest[SHA256_DIGEST_LENGTH];
    unsigned index;
    if (SHA256_Final(digest, context) != 1)
        return -1;
    for (index = 0; index < sizeof(digest); ++index)
        sprintf(output + 2 * index, "%02x", digest[index]);
    output[64] = '\0';
    return 0;
}

static int
sha_begin_call(SHA256_CTX *context, uint8_t tag, uint64_t ordinal)
{
    static const unsigned char magic[8] = {
        'M', 'C', 'F', 'C', 'S', '3', 0, 0
    };
    return SHA256_Init(context) != 1 ||
           sha_update(context, magic, sizeof(magic)) ||
           sha_u8(context, tag) || sha_u64(context, ordinal) ? -1 : 0;
}

static char *
copy_string(const char *value)
{
    size_t bytes;
    char *result;
    if (!value)
        return NULL;
    bytes = strlen(value) + 1;
    result = (char *)malloc(bytes);
    if (result)
        memcpy(result, value, bytes);
    return result;
}

static char *
join_path(const char *root, const char *name)
{
    size_t root_bytes;
    size_t name_bytes;
    char *result;
    if (!root || !name)
        return NULL;
    root_bytes = strlen(root);
    name_bytes = strlen(name);
    if (root_bytes > SIZE_MAX - name_bytes - 2)
        return NULL;
    result = (char *)malloc(root_bytes + name_bytes + 2);
    if (!result)
        return NULL;
    memcpy(result, root, root_bytes);
    result[root_bytes] = '/';
    memcpy(result + root_bytes + 1, name, name_bytes + 1);
    return result;
}

static int
write_bytes(FILE *stream, const void *value, size_t bytes)
{
    return bytes == 0 || fwrite(value, 1, bytes, stream) == bytes ? 0 : -1;
}

static int
write_u32(FILE *stream, uint32_t value)
{
    uint8_t bytes[4];
    unsigned index;
    for (index = 0; index < 4; ++index)
        bytes[index] = (uint8_t)(value >> (index * 8));
    return write_bytes(stream, bytes, sizeof(bytes));
}

static int
write_u64(FILE *stream, uint64_t value)
{
    uint8_t bytes[8];
    unsigned index;
    for (index = 0; index < 8; ++index)
        bytes[index] = (uint8_t)(value >> (index * 8));
    return write_bytes(stream, bytes, sizeof(bytes));
}

static int
stable_index(
    const void *pointer, const void *base, uint64_t count, size_t item_bytes,
    uint64_t *index)
{
    uintptr_t address;
    uintptr_t begin;
    uintptr_t end;
    uintptr_t span;
    if (!pointer)
        return 1;
    if (!base || item_bytes == 0 || count > UINTPTR_MAX / item_bytes)
        return -1;
    begin = (uintptr_t)base;
    span = (uintptr_t)(count * item_bytes);
    if (begin > UINTPTR_MAX - span)
        return -1;
    end = begin + span;
    address = (uintptr_t)pointer;
    if (address < begin || address >= end ||
        (address - begin) % item_bytes != 0)
        return -1;
    *index = (uint64_t)((address - begin) / item_bytes);
    return 0;
}

static int
write_ref(FILE *stream, uint32_t kind, uint32_t generation, uint64_t index)
{
    return write_u32(stream, kind) || write_u32(stream, generation) ||
           write_u64(stream, index) ? -1 : 0;
}

static int
write_node_ref(FILE *stream, const network_t *net, const node_t *node)
{
    uint64_t index;
    int status = stable_index(
        node, net->nodes, (uint64_t)net->n + 1, sizeof(node_t), &index);
    if (status == 1)
        return write_ref(stream, 0, 0, UINT64_MAX);
    if (status)
        return -1;
    return write_ref(stream, 1, 0, index);
}

static int
write_arc_ref(FILE *stream, const network_t *net, const arc_t *arc)
{
    uint64_t index;
    int status = stable_index(
        arc, net->arcs, (uint64_t)net->m, sizeof(arc_t), &index);
    if (status == 0)
        return write_ref(stream, 2, capture_arc_generation, index);
    status = stable_index(
        arc, net->dummy_arcs, (uint64_t)net->n, sizeof(arc_t), &index);
    if (status == 0)
        return write_ref(stream, 3, 0, index);
    if (!arc)
        return write_ref(stream, 0, 0, UINT64_MAX);
    return -1;
}

static int
write_node(FILE *stream, const network_t *net, const node_t *node)
{
    if (write_u64(stream, (uint64_t)node->potential) ||
        write_u64(stream, (uint64_t)(int64_t)node->orientation) ||
        write_node_ref(stream, net, node->child) ||
        write_node_ref(stream, net, node->pred) ||
        write_node_ref(stream, net, node->sibling) ||
        write_node_ref(stream, net, node->sibling_prev) ||
        write_arc_ref(stream, net, node->basic_arc) ||
        write_arc_ref(stream, net, node->firstout) ||
        write_arc_ref(stream, net, node->firstin) ||
        write_arc_ref(stream, net, node->arc_tmp) ||
        write_u64(stream, (uint64_t)node->flow) ||
        write_u64(stream, (uint64_t)node->depth) ||
        write_u64(stream, (uint64_t)(int64_t)node->number) ||
        write_u64(stream, (uint64_t)(int64_t)node->time))
        return -1;
    return 0;
}

static int
write_arc(FILE *stream, const network_t *net, const arc_t *arc)
{
    if (write_u64(stream, (uint64_t)arc->cost) ||
        write_node_ref(stream, net, arc->tail) ||
        write_node_ref(stream, net, arc->head) ||
        write_u64(stream, (uint64_t)(int64_t)arc->ident) ||
        write_arc_ref(stream, net, arc->nextout) ||
        write_arc_ref(stream, net, arc->nextin) ||
        write_u64(stream, (uint64_t)arc->flow) ||
        write_u64(stream, (uint64_t)arc->org_cost))
        return -1;
    return 0;
}

static int
validate_network_layout(const network_t *net)
{
    if (!net || net->n < 0 || net->m < 0 || net->max_m < 0 ||
        net->m > net->max_m)
        return -1;
    if (!net->nodes || (net->m != 0 && !net->arcs) ||
        (net->n != 0 && !net->dummy_arcs))
        return -1;
    return 0;
}

static int
write_network_state(const char *name, const network_t *net)
{
    char *path;
    FILE *stream;
    uint64_t index;
    uint64_t optcost_bits;
    int status = -1;
    if (validate_network_layout(net))
        return -1;
    path = join_path(capture_root, name);
    if (!path)
        return -1;
    stream = fopen(path, "wb");
    free(path);
    if (!stream)
        return -1;
    memcpy(&optcost_bits, &net->optcost, sizeof(optcost_bits));
    if (write_bytes(stream, "MCFSTATE2", 9) ||
        write_u64(stream, (uint64_t)net->n) ||
        write_u64(stream, (uint64_t)net->n_trips) ||
        write_u64(stream, (uint64_t)net->max_m) ||
        write_u64(stream, (uint64_t)net->m) ||
        write_u64(stream, (uint64_t)net->m_org) ||
        write_u64(stream, (uint64_t)net->m_impl) ||
        write_u64(stream, (uint64_t)net->max_residual_new_m) ||
        write_u64(stream, (uint64_t)net->max_new_m) ||
        write_u64(stream, (uint64_t)net->primal_unbounded) ||
        write_u64(stream, (uint64_t)net->dual_unbounded) ||
        write_u64(stream, (uint64_t)net->perturbed) ||
        write_u64(stream, (uint64_t)net->feasible) ||
        write_u64(stream, (uint64_t)net->eps) ||
        write_u64(stream, (uint64_t)net->opt_tol) ||
        write_u64(stream, (uint64_t)net->feas_tol) ||
        write_u64(stream, (uint64_t)net->pert_val) ||
        write_u64(stream, (uint64_t)net->bigM) ||
        write_u64(stream, optcost_bits) ||
        write_u64(stream, (uint64_t)net->ignore_impl) ||
        write_u64(stream, (uint64_t)net->iterations) ||
        write_u64(stream, (uint64_t)net->bound_exchanges) ||
        write_u64(stream, (uint64_t)net->checksum))
        goto done;
    for (index = 0; index < (uint64_t)net->n + 1; ++index)
        if (write_node(stream, net, &net->nodes[index]))
            goto done;
    for (index = 0; index < (uint64_t)net->m; ++index)
        if (write_arc(stream, net, &net->arcs[index]))
            goto done;
    for (index = 0; index < (uint64_t)net->n; ++index)
        if (write_arc(stream, net, &net->dummy_arcs[index]))
            goto done;
    if (fflush(stream) || ferror(stream))
        goto done;
    status = 0;
done:
    if (fclose(stream))
        status = -1;
    return status;
}

static int
json_string(FILE *stream, const char *value)
{
    const unsigned char *position = (const unsigned char *)value;
    if (fputc('"', stream) == EOF)
        return -1;
    while (*position) {
        if (*position == '"' || *position == '\\') {
            if (fputc('\\', stream) == EOF || fputc(*position, stream) == EOF)
                return -1;
        } else if (*position < 0x20) {
            if (fprintf(stream, "\\u%04x", (unsigned)*position) < 0)
                return -1;
        } else if (fputc(*position, stream) == EOF) {
            return -1;
        }
        ++position;
    }
    return fputc('"', stream) == EOF ? -1 : 0;
}

int
mcf_capture_configure(
    const char *input, const char *output_root, int capture_enabled)
{
    char *pricing_path = NULL;
    char *price_out_path = NULL;
    char *allocation_path = NULL;
    char *boundary_path = NULL;
    if (pricing_stream) {
        gzclose(pricing_stream);
        pricing_stream = NULL;
    }
    if (price_out_stream) {
        gzclose(price_out_stream);
        price_out_stream = NULL;
    }
    if (allocation_stream) {
        if (gzclose(allocation_stream) != Z_OK) {
            allocation_stream = NULL;
            return -1;
        }
        allocation_stream = NULL;
    }
    if (boundary_stream) {
        if (gzclose(boundary_stream) != Z_OK) {
            boundary_stream = NULL;
            return -1;
        }
        boundary_stream = NULL;
    }
    free(capture_input);
    free(capture_root);
    free(capture_output);
    capture_input = copy_string(input);
    capture_root = copy_string(output_root);
    capture_output = join_path(output_root, "mcf.out");
    capture_events = capture_enabled ? 1 : 0;
    roi_begin_count = 0;
    roi_end_count = 0;
    capture_network = NULL;
    pricing_calls = 0;
    pricing_scan_count = 0;
    pricing_scan_pending = 0;
    pricing_scan_arc = NULL;
    pricing_scan_position = -1;
    pricing_nr_group = 0;
    pricing_live_in_expected = 0;
    pricing_live_in_seen = 0;
    pricing_live_out_seen = 0;
    pricing_active = 0;
    pricing_m = 0;
    pricing_group_pos_in = 0;
    pricing_initialize_in = 0;
    buffer_reset(&pricing_live_basket);
    buffer_reset(&pricing_scans);
    buffer_reset(&pricing_candidates);
    buffer_reset(&pricing_result_basket);
    call_order = 0;
    pricing_order = 0;
    price_out_calls = 0;
    price_out_candidates = 0;
    price_out_live_in_m = 0;
    price_out_active = 0;
    price_out_order = 0;
    price_out_candidate_pending = 0;
    price_out_tail = NULL;
    price_out_head = NULL;
    price_out_reduced_cost = 0;
    price_out_remapped = 0;
    buffer_reset(&price_out_candidates_buffer);
    buffer_reset(&price_out_decisions_buffer);
    price_out_pre_sha256[0] = '\0';
    capture_arc_base = NULL;
    capture_arc_capacity = 0;
    capture_arc_generation = 0;
    free(arena_history);
    arena_history = NULL;
    arena_history_count = 0;
    arena_history_capacity = 0;
    allocation_events = 0;
    memset(&allocation_state, 0, sizeof(allocation_state));
    if (!capture_input || !capture_root || !capture_output)
        return -1;
    if (capture_events) {
        pricing_path = join_path(output_root, "pricing.jsonl.gz");
        if (!pricing_path)
            return -1;
        pricing_stream = gzopen(pricing_path, "wb1");
        free(pricing_path);
        if (!pricing_stream)
            return -1;
        price_out_path = join_path(output_root, "price_out.jsonl.gz");
        if (!price_out_path)
            return -1;
        price_out_stream = gzopen(price_out_path, "wb1");
        free(price_out_path);
        if (!price_out_stream)
            return -1;
        allocation_path = join_path(output_root, "allocation.jsonl.gz");
        if (!allocation_path)
            return -1;
        allocation_stream = gzopen(allocation_path, "wb1");
        free(allocation_path);
        if (!allocation_stream)
            return -1;
        boundary_path = join_path(output_root, "boundaries.jsonl.gz");
        if (!boundary_path)
            return -1;
        boundary_stream = gzopen(boundary_path, "wb1");
        free(boundary_path);
        if (!boundary_stream)
            return -1;
    }
    return 0;
}

int
mcf_capture_set_inputfile(char *destination, size_t destination_bytes)
{
    size_t input_bytes;
    if (!destination || !capture_input || destination_bytes == 0)
        return -1;
    input_bytes = strlen(capture_input) + 1;
    if (input_bytes > destination_bytes)
        return -1;
    memcpy(destination, capture_input, input_bytes);
    return 0;
}

const char *
mcf_capture_output_file(void)
{
    return capture_output;
}

int
mcf_capture_allocation(
    const char *kind, uint64_t elements, uint64_t element_bytes,
    uint64_t current_bytes)
{
    uint64_t requested;
    uint64_t total;
    uint64_t old_bytes;
    uint64_t old_capacity;
    uint64_t *slot;
    if (!kind ||
        (elements != 0 && element_bytes > UINT64_MAX / elements))
        return -1;
    requested = elements * element_bytes;
    if (requested != current_bytes)
        return -1;
    if (strcmp(kind, "nodes") == 0)
        slot = &allocation_state.nodes;
    else if (strcmp(kind, "dummy_arcs") == 0)
        slot = &allocation_state.dummy_arcs;
    else if (strcmp(kind, "arcs") == 0)
        slot = &allocation_state.arcs;
    else
        return -1;
    old_bytes = *slot;
    old_capacity = element_bytes == 0 ? 0 : old_bytes / element_bytes;
    *slot = requested;
    if (allocation_state.nodes > UINT64_MAX - allocation_state.dummy_arcs)
        return -1;
    total = allocation_state.nodes + allocation_state.dummy_arcs;
    if (total > UINT64_MAX - allocation_state.arcs)
        return -1;
    total += allocation_state.arcs;
    if (total > allocation_state.peak)
        allocation_state.peak = total;
    if (capture_events && (!allocation_stream || gzprintf(
            allocation_stream,
            "{\"kind\":\"ALLOC\",\"role\":\"live_in\","
            "\"allocation_kind\":\"%s\",\"elements\":%" PRIu64
            ",\"element_bytes\":%" PRIu64
            ",\"old_capacity\":%" PRIu64
            ",\"new_capacity\":%" PRIu64
            ",\"requested_bytes\":%" PRIu64
            ",\"current_bytes\":%" PRIu64
            ",\"peak_bytes\":%" PRIu64 "}\n",
            kind, elements, element_bytes, old_capacity, elements,
            requested, total, allocation_state.peak) < 0))
        return -1;
    ++allocation_events;
    return 0;
}

int
mcf_capture_roi_begin(const network_t *net)
{
    if (!net || roi_begin_count != 0 || roi_end_count != 0)
        return -1;
    if (validate_network_layout(net))
        return -1;
    capture_network = net;
    capture_arc_base = net->arcs;
    capture_arc_capacity = (uint64_t)net->max_m;
    capture_arc_generation = 0;
    if (write_network_state("initial.state", net))
        return -1;
    ++roi_begin_count;
    printf("MCF_CAPTURE_ROI_BEGIN=after_primal_start_artificial\n");
    fflush(stdout);
    return 0;
}

int
mcf_capture_roi_end(const network_t *net)
{
    if (!net || roi_begin_count != 1 || roi_end_count != 0)
        return -1;
    if (net != capture_network || pricing_active || pricing_scan_pending ||
        price_out_active || price_out_candidate_pending)
        return -1;
    if (write_network_state("final.state", net))
        return -1;
    ++roi_end_count;
    printf("MCF_CAPTURE_ROI_END=after_global_opt\n");
    fflush(stdout);
    return 0;
}

static int
pricing_arc_index(const arc_t *arc, uint64_t *index)
{
    if (!capture_network || !index)
        return -1;
    return stable_index(
        arc, capture_network->arcs, (uint64_t)capture_network->m,
        sizeof(arc_t), index);
}

static int
pricing_node_index(const node_t *node, uint64_t *index)
{
    if (!capture_network || !index)
        return -1;
    return stable_index(
        node, capture_network->nodes, (uint64_t)capture_network->n + 1,
        sizeof(node_t), index);
}

static int
gz_reference(gzFile stream, const char *kind, uint32_t generation,
             uint64_t index)
{
    return gzprintf(
               stream,
               "{\"kind\":\"%s\",\"generation\":%u,\"index\":%" PRIu64
               "}", kind, generation, index) < 0 ? -1 : 0;
}

static int
gz_node_reference(gzFile stream, const node_t *node)
{
    uint64_t index;
    if (!node)
        return gz_reference(stream, "null", 0, UINT64_MAX);
    if (pricing_node_index(node, &index))
        return -1;
    return gz_reference(stream, "node", 0, index);
}

static int
gz_arc_reference(gzFile stream, const arc_t *arc)
{
    uint32_t kind;
    uint32_t generation;
    uint64_t index;
    static const char *const names[] = {
        "null", "node", "arc", "dummy_arc"
    };
    if (resolved_arc_reference(arc, &kind, &generation, &index) || kind > 3)
        return -1;
    return gz_reference(stream, names[kind], generation, index);
}

static int
resolved_arc_reference(
    const arc_t *arc, uint32_t *kind, uint32_t *generation,
    uint64_t *index)
{
    int status;
    if (!arc) {
        *kind = 0;
        *generation = 0;
        *index = UINT64_MAX;
        return 0;
    }
    status = stable_index(
        arc, capture_arc_base, capture_arc_capacity, sizeof(arc_t), index);
    if (status == 0) {
        *kind = 2;
        *generation = capture_arc_generation;
        return 0;
    }
    {
        size_t history = arena_history_count;
        while (history) {
            const arena_history_t *entry = &arena_history[--history];
            status = stable_index(
                arc, (const void *)entry->base, entry->capacity,
                sizeof(arc_t), index);
            if (status == 0) {
                *kind = 2;
                *generation = entry->generation;
                return 0;
            }
        }
    }
    status = stable_index(
        arc, capture_network->dummy_arcs, (uint64_t)capture_network->n,
        sizeof(arc_t), index);
    if (status)
        return -1;
    *kind = 3;
    *generation = 0;
    return 0;
}

static int
sha_node_reference(SHA256_CTX *context, const node_t *node)
{
    uint64_t index;
    if (!node)
        return sha_ref(context, 0, 0, UINT64_MAX);
    if (pricing_node_index(node, &index))
        return -1;
    return sha_ref(context, 1, 0, index);
}

static int
sha_arc_reference(SHA256_CTX *context, const arc_t *arc)
{
    uint32_t kind;
    uint32_t generation;
    uint64_t index;
    return resolved_arc_reference(arc, &kind, &generation, &index) ||
           sha_ref(context, kind, generation, index) ? -1 : 0;
}

int
mcf_capture_pricing_begin(
    long m, const arc_t *arcs, const arc_t *stop_arcs, long nr_group,
    long group_pos, long initialize, long basket_size)
{
    if (!capture_events)
        return 0;
    if (!pricing_stream || !capture_network || pricing_active || m < 0 ||
        nr_group <= 0 || group_pos < 0 || group_pos >= nr_group ||
        basket_size < 0 || m != capture_network->m ||
        arcs != capture_network->arcs || stop_arcs != arcs + m)
        return -1;
    if (gzprintf(
            pricing_stream,
            "{\"kind\":\"CALL_BEGIN\",\"role\":\"live_in\","
            "\"call\":%" PRIu64 ",\"order\":%" PRIu64
            ",\"ordinal\":%" PRIu64 ",\"phase\":\"pricing\","
            "\"m\":%ld,\"nr_group\":%ld,\"group_pos\":%ld,"
            "\"initialize\":%s}\n",
            pricing_calls, call_order, pricing_calls, m, nr_group, group_pos,
            initialize ? "true" : "false") < 0)
        return -1;
    pricing_active = 1;
    pricing_order = call_order;
    pricing_scan_count = 0;
    pricing_scan_pending = 0;
    pricing_nr_group = nr_group;
    pricing_m = m;
    pricing_group_pos_in = group_pos;
    pricing_initialize_in = initialize ? 1 : 0;
    pricing_live_in_expected = basket_size;
    pricing_live_in_seen = 0;
    pricing_live_out_seen = 0;
    buffer_reset(&pricing_live_basket);
    buffer_reset(&pricing_scans);
    buffer_reset(&pricing_candidates);
    buffer_reset(&pricing_result_basket);
    return 0;
}

int
mcf_capture_pricing_basket(
    int live_out, long slot, const arc_t *arc, cost_t cost,
    cost_t abs_cost)
{
    uint64_t arc_id;
    const char *kind;
    const char *role;
    if (!capture_events)
        return 0;
    if (!pricing_active || slot <= 0 || pricing_arc_index(arc, &arc_id))
        return -1;
    if (live_out) {
        if (slot != ++pricing_live_out_seen)
            return -1;
        kind = "BASKET_LIVE_OUT_OBSERVED";
        role = "observed_result";
    } else {
        if (slot != ++pricing_live_in_seen ||
            pricing_live_in_seen > pricing_live_in_expected)
            return -1;
        kind = "BASKET_LIVE_IN";
        role = "live_in";
    }
    if (gzprintf(
            pricing_stream,
            "{\"kind\":\"%s\",\"role\":\"%s\",\"call\":%" PRIu64
            ",\"slot\":%ld,\"arc\":",
            kind, role, pricing_calls, slot) < 0 ||
        gz_reference(
            pricing_stream, "arc", capture_arc_generation, arc_id) ||
        gzprintf(
            pricing_stream, ",\"cost\":%ld,\"abs_cost\":%ld}\n",
            (long)cost, (long)abs_cost) < 0)
        return -1;
    {
        byte_buffer_t *buffer = live_out ? &pricing_result_basket :
                                          &pricing_live_basket;
        if (buffer_u64(buffer, (uint64_t)slot) ||
            buffer_ref(buffer, 2, capture_arc_generation, arc_id) ||
            buffer_i64(buffer, (int64_t)cost) ||
            buffer_i64(buffer, (int64_t)abs_cost))
            return -1;
    }
    return 0;
}

int
mcf_capture_pricing_scan_live_in(
    const arc_t *arc, long group_pos, long scan_position)
{
    uint64_t arc_id;
    uint64_t tail_id;
    uint64_t head_id;
    if (!capture_events)
        return 0;
    if (!pricing_active || pricing_live_in_seen != pricing_live_in_expected ||
        pricing_scan_pending || scan_position < 0 ||
        (uint64_t)scan_position != pricing_scan_count || group_pos < 0 ||
        group_pos >= pricing_nr_group || pricing_arc_index(arc, &arc_id) ||
        pricing_node_index(arc->tail, &tail_id) ||
        pricing_node_index(arc->head, &head_id))
        return -1;
    if (gzprintf(
            pricing_stream,
            "{\"kind\":\"PRICING_SCAN_LIVE_IN\","
            "\"role\":\"live_in\",\"call\":%" PRIu64
            ",\"scan_position\":%ld,\"group_pos\":%ld,\"arc\":",
            pricing_calls, scan_position, group_pos) < 0 ||
        gz_reference(
            pricing_stream, "arc", capture_arc_generation, arc_id) ||
        gzprintf(pricing_stream, ",\"tail\":") < 0 ||
        gz_reference(pricing_stream, "node", 0, tail_id) ||
        gzprintf(pricing_stream, ",\"head\":") < 0 ||
        gz_reference(pricing_stream, "node", 0, head_id) ||
        gzprintf(
            pricing_stream,
            ",\"cost\":%ld,\"ident\":%d,\"tail_potential\":%ld,"
            "\"head_potential\":%ld}\n",
            (long)arc->cost, arc->ident, (long)arc->tail->potential,
            (long)arc->head->potential) < 0)
        return -1;
    pricing_scan_pending = 1;
    pricing_scan_arc = arc;
    pricing_scan_position = scan_position;
    if (buffer_u64(&pricing_scans, (uint64_t)scan_position) ||
        buffer_ref(
            &pricing_scans, 2, capture_arc_generation, arc_id) ||
        buffer_ref(&pricing_scans, 1, 0, tail_id) ||
        buffer_ref(&pricing_scans, 1, 0, head_id) ||
        buffer_i64(&pricing_scans, (int64_t)arc->cost) ||
        buffer_i64(&pricing_scans, (int64_t)arc->ident) ||
        buffer_i64(&pricing_scans, (int64_t)arc->tail->potential) ||
        buffer_i64(&pricing_scans, (int64_t)arc->head->potential))
        return -1;
    return 0;
}

int
mcf_capture_pricing_candidate_observed(
    const arc_t *arc, cost_t reduced_cost, int candidate, long basket_slot)
{
    if (!capture_events)
        return 0;
    if (!pricing_active || !pricing_scan_pending || arc != pricing_scan_arc ||
        (basket_slot >= 0 && (!candidate || basket_slot == 0)))
        return -1;
    if (gzprintf(
            pricing_stream,
            "{\"kind\":\"PRICING_CANDIDATE_OBSERVED\","
            "\"role\":\"observed_result\",\"call\":%" PRIu64
            ",\"scan_position\":%ld,\"reduced_cost\":%ld,"
            "\"candidate\":%s,\"basket_slot\":%ld}\n",
            pricing_calls, pricing_scan_position, (long)reduced_cost,
            candidate ? "true" : "false", basket_slot) < 0)
        return -1;
    if (buffer_u64(
            &pricing_candidates, (uint64_t)pricing_scan_position) ||
        buffer_i64(&pricing_candidates, (int64_t)reduced_cost) ||
        buffer_u8(&pricing_candidates, candidate ? 1 : 0) ||
        buffer_i64(&pricing_candidates, (int64_t)basket_slot))
        return -1;
    pricing_scan_pending = 0;
    pricing_scan_arc = NULL;
    pricing_scan_position = -1;
    ++pricing_scan_count;
    return 0;
}

static int
pricing_boundaries(
    const arc_t *selected, uint64_t selected_id, cost_t reduced_cost,
    long arcs_priced, long nr_group, long group_pos, long initialize,
    long basket_size)
{
    SHA256_CTX context;
    char pre[65];
    char post[65];
    if (!boundary_stream || pricing_m < 0 || pricing_live_in_expected < 0 ||
        basket_size < 0 || arcs_priced < 0 || nr_group < 0 || group_pos < 0 ||
        pricing_group_pos_in < 0)
        return -1;
    if (sha_begin_call(&context, 1, pricing_calls) ||
        sha_u64(&context, (uint64_t)pricing_m) ||
        sha_u64(&context, (uint64_t)pricing_nr_group) ||
        sha_u64(&context, (uint64_t)pricing_group_pos_in) ||
        sha_u8(&context, pricing_initialize_in ? 1 : 0) ||
        sha_u64(&context, (uint64_t)pricing_live_in_expected) ||
        sha_update(
            &context, pricing_live_basket.data, pricing_live_basket.size) ||
        sha_u64(&context, pricing_scan_count) ||
        sha_update(&context, pricing_scans.data, pricing_scans.size) ||
        sha_finish(&context, pre))
        return -1;
    if (sha_begin_call(&context, 2, pricing_calls) ||
        sha_u64(&context, pricing_scan_count) ||
        sha_update(
            &context, pricing_candidates.data, pricing_candidates.size) ||
        sha_u64(&context, (uint64_t)basket_size) ||
        sha_update(
            &context, pricing_result_basket.data,
            pricing_result_basket.size) ||
        (selected ? sha_ref(
             &context, 2, capture_arc_generation, selected_id) :
         sha_ref(&context, 0, 0, UINT64_MAX)) ||
        sha_i64(&context, (int64_t)reduced_cost) ||
        sha_u64(&context, (uint64_t)arcs_priced) ||
        sha_u64(&context, (uint64_t)nr_group) ||
        sha_u64(&context, (uint64_t)group_pos) ||
        sha_u8(&context, initialize ? 1 : 0) ||
        sha_finish(&context, post))
        return -1;
    return gzprintf(
               boundary_stream,
               "{\"call\":%" PRIu64 ",\"order\":%" PRIu64
               ",\"phase\":\"pricing\",\"pre_sha256\":\"%s\","
               "\"post_sha256\":\"%s\"}\n",
               pricing_calls, pricing_order, pre, post) < 0 ? -1 : 0;
}

int
mcf_capture_pricing_end(
    const arc_t *selected, cost_t reduced_cost, long arcs_priced,
    long nr_group, long group_pos, long initialize, long basket_size)
{
    uint64_t selected_id = UINT64_MAX;
    if (!capture_events)
        return 0;
    if (!pricing_active || pricing_scan_pending ||
        nr_group != pricing_nr_group || group_pos < 0 ||
        group_pos >= nr_group || basket_size < 0 ||
        pricing_live_in_seen != pricing_live_in_expected ||
        pricing_live_out_seen != basket_size || arcs_priced < 0 ||
        (uint64_t)arcs_priced != pricing_scan_count)
        return -1;
    if (selected) {
        if (pricing_arc_index(selected, &selected_id))
            return -1;
    } else if (basket_size != 0 || reduced_cost != 0) {
        return -1;
    }
    if (pricing_boundaries(
            selected, selected_id, reduced_cost, arcs_priced, nr_group,
            group_pos, initialize, basket_size))
        return -1;
    if (gzprintf(
            pricing_stream,
            "{\"kind\":\"PRICING_END_OBSERVED\","
            "\"role\":\"observed_result\",\"call\":%" PRIu64
            ",\"selected_arc\":",
            pricing_calls) < 0 ||
        (selected ? gz_reference(
             pricing_stream, "arc", capture_arc_generation, selected_id) :
         gz_reference(pricing_stream, "null", 0, UINT64_MAX)) ||
        gzprintf(
            pricing_stream, ",\"selected_reduced_cost\":%ld,"
            "\"arcs_priced\":%ld,\"nr_group\":%ld,"
            "\"group_pos\":%ld,\"initialize\":%s}\n",
            (long)reduced_cost, arcs_priced, nr_group, group_pos,
            initialize ? "true" : "false") < 0 ||
        gzprintf(
            pricing_stream,
            "{\"kind\":\"CALL_END\","
            "\"role\":\"observed_result\",\"call\":%" PRIu64
            ",\"order\":%" PRIu64 ",\"ordinal\":%" PRIu64
            ",\"phase\":\"pricing\"}\n",
            pricing_calls, pricing_order, pricing_calls) < 0)
        return -1;
    if (gzflush(pricing_stream, Z_SYNC_FLUSH) != Z_OK ||
        gzflush(boundary_stream, Z_SYNC_FLUSH) != Z_OK)
        return -1;
    pricing_active = 0;
    ++pricing_calls;
    if (pricing_order != call_order)
        return -1;
    ++call_order;
    return 0;
}

static int
gz_object_words(gzFile stream, const int64_t *words, size_t count)
{
    size_t index;
    if (gzprintf(stream, "[") < 0)
        return -1;
    for (index = 0; index < count; ++index)
        if (gzprintf(
                stream, "%s%" PRId64, index ? "," : "", words[index]) < 0)
            return -1;
    return gzprintf(stream, "]") < 0 ? -1 : 0;
}

static int
gz_node_object(gzFile stream, const network_t *net, uint64_t index)
{
    const node_t *node = &net->nodes[index];
    int64_t words[] = {
        (int64_t)node->potential,
        (int64_t)node->orientation,
        (int64_t)node->flow,
        (int64_t)node->depth,
        (int64_t)node->number,
        (int64_t)node->time,
    };
    if (gzprintf(stream, "{\"reference\":") < 0 ||
        gz_reference(stream, "node", 0, index) ||
        gzprintf(stream, ",\"words\":") < 0 ||
        gz_object_words(stream, words, sizeof(words) / sizeof(words[0])) ||
        gzprintf(stream, ",\"links\":[") < 0 ||
        gz_node_reference(stream, node->child) ||
        gzprintf(stream, ",") < 0 || gz_node_reference(stream, node->pred) ||
        gzprintf(stream, ",") < 0 ||
        gz_node_reference(stream, node->sibling) ||
        gzprintf(stream, ",") < 0 ||
        gz_node_reference(stream, node->sibling_prev) ||
        gzprintf(stream, ",") < 0 ||
        gz_arc_reference(stream, node->basic_arc) ||
        gzprintf(stream, ",") < 0 ||
        gz_arc_reference(stream, node->firstout) ||
        gzprintf(stream, ",") < 0 ||
        gz_arc_reference(stream, node->firstin) ||
        gzprintf(stream, ",") < 0 ||
        gz_arc_reference(stream, node->arc_tmp) ||
        gzprintf(stream, "]}") < 0)
        return -1;
    return 0;
}

static int
gz_arc_object(
    gzFile stream, const network_t *net, const arc_t *arc,
    const char *kind, uint32_t generation, uint64_t index)
{
    int64_t words[] = {
        (int64_t)arc->cost,
        (int64_t)arc->ident,
        (int64_t)arc->flow,
        (int64_t)arc->org_cost,
    };
    (void)net;
    if (gzprintf(stream, "{\"reference\":") < 0 ||
        gz_reference(stream, kind, generation, index) ||
        gzprintf(stream, ",\"words\":") < 0 ||
        gz_object_words(stream, words, sizeof(words) / sizeof(words[0])) ||
        gzprintf(stream, ",\"links\":[") < 0 ||
        gz_node_reference(stream, arc->tail) ||
        gzprintf(stream, ",") < 0 || gz_node_reference(stream, arc->head) ||
        gzprintf(stream, ",") < 0 ||
        gz_arc_reference(stream, arc->nextout) ||
        gzprintf(stream, ",") < 0 ||
        gz_arc_reference(stream, arc->nextin) ||
        gzprintf(stream, "]}") < 0)
        return -1;
    return 0;
}

static int
price_out_network_words(const network_t *net, int64_t words[23])
{
    uint64_t stop_index;
    uint64_t optcost_bits;
    if (validate_network_layout(net) || net->stop_arcs < net->arcs ||
        net->stop_arcs > net->arcs + net->max_m)
        return -1;
    stop_index = (uint64_t)(net->stop_arcs - net->arcs);
    if (sizeof(net->optcost) != sizeof(optcost_bits))
        return -1;
    memcpy(&optcost_bits, &net->optcost, sizeof(optcost_bits));
    words[0] = (int64_t)net->n;
    words[1] = (int64_t)net->n_trips;
    words[2] = (int64_t)net->max_m;
    words[3] = (int64_t)net->m;
    words[4] = (int64_t)net->m_org;
    words[5] = (int64_t)net->m_impl;
    words[6] = (int64_t)net->max_residual_new_m;
    words[7] = (int64_t)net->max_new_m;
    words[8] = (int64_t)net->primal_unbounded;
    words[9] = (int64_t)net->dual_unbounded;
    words[10] = (int64_t)net->perturbed;
    words[11] = (int64_t)net->feasible;
    words[12] = (int64_t)net->eps;
    words[13] = (int64_t)net->opt_tol;
    words[14] = (int64_t)net->feas_tol;
    words[15] = (int64_t)net->pert_val;
    words[16] = (int64_t)net->bigM;
    words[17] = (int64_t)optcost_bits;
    words[18] = (int64_t)net->ignore_impl;
    words[19] = (int64_t)net->iterations;
    words[20] = (int64_t)net->bound_exchanges;
    words[21] = (int64_t)net->checksum;
    words[22] = (int64_t)stop_index;
    return 0;
}

static int
gz_price_out_state(
    gzFile stream, const char *kind, const char *role, const network_t *net)
{
    uint64_t index;
    int64_t words[23];
    if (price_out_network_words(net, words))
        return -1;
    if (gzprintf(
            stream,
            "{\"kind\":\"%s\",\"role\":\"%s\",\"call\":%" PRIu64
            ",\"network_words\":",
            kind, role, price_out_calls) < 0 ||
        gz_object_words(stream, words, sizeof(words) / sizeof(words[0])) ||
        gzprintf(stream, ",\"objects\":[") < 0)
        return -1;
    for (index = 0; index < (uint64_t)net->n + 1; ++index) {
        if (index && gzprintf(stream, ",") < 0)
            return -1;
        if (gz_node_object(stream, net, index))
            return -1;
    }
    for (index = 0; index < (uint64_t)net->m; ++index) {
        if (gzprintf(stream, ",") < 0 ||
            gz_arc_object(
                stream, net, &net->arcs[index], "arc",
                capture_arc_generation, index))
            return -1;
    }
    for (index = 0; index < (uint64_t)net->n; ++index) {
        if (gzprintf(stream, ",") < 0 ||
            gz_arc_object(
                stream, net, &net->dummy_arcs[index], "dummy_arc", 0,
                index))
            return -1;
    }
    return gzprintf(
               stream,
               "],\"arena_generation\":%u,\"arena_capacity\":%" PRIu64
               ",\"heap\":[]}\n",
               capture_arc_generation, capture_arc_capacity) < 0 ? -1 : 0;
}

static int
sha_node_object(SHA256_CTX *context, const network_t *net, uint64_t index)
{
    const node_t *node = &net->nodes[index];
    int64_t words[] = {
        (int64_t)node->potential,
        (int64_t)node->orientation,
        (int64_t)node->flow,
        (int64_t)node->depth,
        (int64_t)node->number,
        (int64_t)node->time,
    };
    size_t word;
    if (sha_ref(context, 1, 0, index) ||
        sha_u64(context, sizeof(words) / sizeof(words[0])))
        return -1;
    for (word = 0; word < sizeof(words) / sizeof(words[0]); ++word)
        if (sha_i64(context, words[word]))
            return -1;
    return sha_u64(context, 8) ||
           sha_node_reference(context, node->child) ||
           sha_node_reference(context, node->pred) ||
           sha_node_reference(context, node->sibling) ||
           sha_node_reference(context, node->sibling_prev) ||
           sha_arc_reference(context, node->basic_arc) ||
           sha_arc_reference(context, node->firstout) ||
           sha_arc_reference(context, node->firstin) ||
           sha_arc_reference(context, node->arc_tmp) ? -1 : 0;
}

static int
sha_arc_object(
    SHA256_CTX *context, const arc_t *arc, uint32_t kind,
    uint32_t generation, uint64_t index)
{
    int64_t words[] = {
        (int64_t)arc->cost,
        (int64_t)arc->ident,
        (int64_t)arc->flow,
        (int64_t)arc->org_cost,
    };
    size_t word;
    if (sha_ref(context, kind, generation, index) ||
        sha_u64(context, sizeof(words) / sizeof(words[0])))
        return -1;
    for (word = 0; word < sizeof(words) / sizeof(words[0]); ++word)
        if (sha_i64(context, words[word]))
            return -1;
    return sha_u64(context, 4) ||
           sha_node_reference(context, arc->tail) ||
           sha_node_reference(context, arc->head) ||
           sha_arc_reference(context, arc->nextout) ||
           sha_arc_reference(context, arc->nextin) ? -1 : 0;
}

static int
sha_price_out_common(SHA256_CTX *context, const network_t *net)
{
    int64_t words[23];
    uint64_t objects;
    uint64_t index;
    size_t word;
    if (price_out_network_words(net, words) || net->n < 0 || net->m < 0)
        return -1;
    if ((uint64_t)net->n > UINT64_MAX - (uint64_t)net->n - 1 ||
        (uint64_t)net->m >
            UINT64_MAX - ((uint64_t)net->n * 2 + 1))
        return -1;
    objects = (uint64_t)net->n * 2 + 1 + (uint64_t)net->m;
    if (sha_u64(context, sizeof(words) / sizeof(words[0])))
        return -1;
    for (word = 0; word < sizeof(words) / sizeof(words[0]); ++word)
        if (sha_i64(context, words[word]))
            return -1;
    if (sha_u64(context, objects))
        return -1;
    for (index = 0; index < (uint64_t)net->n + 1; ++index)
        if (sha_node_object(context, net, index))
            return -1;
    for (index = 0; index < (uint64_t)net->m; ++index)
        if (sha_arc_object(
                context, &net->arcs[index], 2,
                capture_arc_generation, index))
            return -1;
    for (index = 0; index < (uint64_t)net->n; ++index)
        if (sha_arc_object(
                context, &net->dummy_arcs[index], 3, 0, index))
            return -1;
    return sha_u32(context, capture_arc_generation) ||
           sha_u64(context, capture_arc_capacity) ||
           sha_u64(context, 0) ? -1 : 0;
}

static int
price_out_pre_boundary(const network_t *net)
{
    SHA256_CTX context;
    return sha_begin_call(&context, 3, price_out_calls) ||
           sha_price_out_common(&context, net) ||
           sha_finish(&context, price_out_pre_sha256) ? -1 : 0;
}

static int
price_out_boundary(const network_t *net)
{
    SHA256_CTX context;
    char post[65];
    if (!boundary_stream || !price_out_pre_sha256[0] ||
        sha_begin_call(&context, 4, price_out_calls) ||
        sha_price_out_common(&context, net) ||
        sha_u64(&context, price_out_candidates) ||
        sha_update(
            &context, price_out_candidates_buffer.data,
            price_out_candidates_buffer.size) ||
        sha_u64(&context, price_out_candidates) ||
        sha_update(
            &context, price_out_decisions_buffer.data,
            price_out_decisions_buffer.size) ||
        sha_finish(&context, post))
        return -1;
    return gzprintf(
               boundary_stream,
               "{\"call\":%" PRIu64 ",\"order\":%" PRIu64
               ",\"phase\":\"price_out\",\"pre_sha256\":\"%s\","
               "\"post_sha256\":\"%s\"}\n",
               price_out_calls, price_out_order, price_out_pre_sha256,
               post) < 0 ? -1 : 0;
}

int
mcf_capture_price_out_begin(const network_t *net)
{
    if (!capture_events)
        return 0;
    if (!price_out_stream || !capture_network || net != capture_network ||
        price_out_active || pricing_active || net->m < 0 || net->max_m < 0 ||
        net->arcs != capture_arc_base ||
        (uint64_t)net->max_m != capture_arc_capacity)
        return -1;
    price_out_active = 1;
    price_out_order = call_order;
    price_out_candidate_pending = 0;
    price_out_candidates = 0;
    price_out_live_in_m = net->m;
    price_out_remapped = 0;
    buffer_reset(&price_out_candidates_buffer);
    buffer_reset(&price_out_decisions_buffer);
    price_out_pre_sha256[0] = '\0';
    if (gzprintf(
            price_out_stream,
            "{\"kind\":\"CALL_BEGIN\",\"role\":\"live_in\","
            "\"call\":%" PRIu64 ",\"order\":%" PRIu64
            ",\"ordinal\":%" PRIu64 ",\"phase\":\"price_out\"}\n",
            price_out_calls, call_order, price_out_calls) < 0 ||
        gz_price_out_state(
            price_out_stream, "PRICE_OUT_STATE_LIVE_IN", "live_in", net))
        return -1;
    if (price_out_pre_boundary(net))
        return -1;
    return 0;
}

int
mcf_capture_price_out_candidate(
    const node_t *tail, const node_t *head, cost_t arc_cost,
    cost_t reduced_cost)
{
    uint64_t tail_id;
    uint64_t head_id;
    cost_t recomputed;
    if (!capture_events)
        return 0;
    if (!price_out_active || price_out_candidate_pending ||
        pricing_node_index(tail, &tail_id) ||
        pricing_node_index(head, &head_id))
        return -1;
    recomputed = arc_cost - tail->potential + head->potential;
    if (recomputed != reduced_cost)
        return -1;
    if (gzprintf(
            price_out_stream,
            "{\"kind\":\"PRICE_OUT_CANDIDATE_OBSERVED\","
            "\"role\":\"observed_result\",\"call\":%" PRIu64
            ",\"candidate\":%" PRIu64 ",\"tail\":",
            price_out_calls, price_out_candidates) < 0 ||
        gz_reference(price_out_stream, "node", 0, tail_id) ||
        gzprintf(price_out_stream, ",\"head\":") < 0 ||
        gz_reference(price_out_stream, "node", 0, head_id) ||
        gzprintf(
            price_out_stream, ",\"cost\":%ld,\"reduced_cost\":%ld}\n",
            (long)arc_cost, (long)reduced_cost) < 0)
        return -1;
    if (buffer_u64(&price_out_candidates_buffer, price_out_candidates) ||
        buffer_ref(&price_out_candidates_buffer, 1, 0, tail_id) ||
        buffer_ref(&price_out_candidates_buffer, 1, 0, head_id) ||
        buffer_i64(&price_out_candidates_buffer, (int64_t)arc_cost) ||
        buffer_i64(&price_out_candidates_buffer, (int64_t)reduced_cost))
        return -1;
    price_out_tail = tail;
    price_out_head = head;
    price_out_reduced_cost = reduced_cost;
    price_out_candidate_pending = 1;
    return 0;
}

int
mcf_capture_price_out_decision(
    int decision, const arc_t *slot, const node_t *tail,
    const node_t *head)
{
    static const char *const names[] = {
        "NO_CHANGE", "INSERT", "REPLACE"
    };
    uint64_t index = UINT64_MAX;
    if (!capture_events)
        return 0;
    if (!price_out_active || !price_out_candidate_pending || decision < 0 ||
        decision > 2 || tail != price_out_tail || head != price_out_head)
        return -1;
    if (decision == 0) {
        if (slot)
            return -1;
    } else {
        if (!slot || stable_index(
                slot, capture_arc_base, capture_arc_capacity,
                sizeof(arc_t), &index))
            return -1;
        if (price_out_reduced_cost >= 0)
            return -1;
    }
    if (decision == 0) {
        if (gzprintf(
                price_out_stream,
                "{\"kind\":\"PRICE_OUT_DECISION_OBSERVED\","
                "\"role\":\"observed_result\",\"call\":%" PRIu64
                ",\"candidate\":%" PRIu64 ",\"decision\":\"%s\","
                "\"reference\":",
                price_out_calls, price_out_candidates, names[decision]) < 0 ||
            gz_reference(price_out_stream, "null", 0, UINT64_MAX) ||
            gzprintf(price_out_stream, "}\n") < 0)
            return -1;
    } else if (gzprintf(
                   price_out_stream,
                   "{\"kind\":\"PRICE_OUT_DECISION_OBSERVED\","
                   "\"role\":\"observed_result\",\"call\":%" PRIu64
                   ",\"candidate\":%" PRIu64
                   ",\"decision\":\"%s\",\"reference\":{"
                   "\"kind\":\"arc\",\"generation\":%u,"
                   "\"index\":%" PRIu64 "}}\n",
                   price_out_calls, price_out_candidates, names[decision],
                   capture_arc_generation, index) < 0) {
        return -1;
    }
    if (buffer_u64(&price_out_decisions_buffer, price_out_candidates) ||
        buffer_u8(&price_out_decisions_buffer, (uint8_t)decision) ||
        (decision == 0 ?
         buffer_ref(&price_out_decisions_buffer, 0, 0, UINT64_MAX) :
         buffer_ref(
             &price_out_decisions_buffer, 2, capture_arc_generation,
             index)))
        return -1;
    ++price_out_candidates;
    price_out_candidate_pending = 0;
    return 0;
}

int
mcf_capture_arena_remap(
    const arc_t *old_base, uint64_t old_capacity, const arc_t *new_base,
    uint64_t new_capacity)
{
    uint32_t new_generation;
    uint64_t index;
    arena_history_t *resized_history;
    if ((capture_events &&
         (!price_out_active || price_out_candidate_pending)) ||
        !capture_network || !new_base || old_base != capture_arc_base ||
        old_capacity != capture_arc_capacity ||
        new_capacity <= old_capacity || capture_arc_generation == UINT32_MAX)
        return -1;
    new_generation = capture_arc_generation + 1;
    if (capture_events) {
        for (index = 0; index < (uint64_t)price_out_live_in_m; ++index) {
            if (gzprintf(
                    price_out_stream,
                    "{\"kind\":\"REMAP_OBSERVED\","
                    "\"role\":\"observed_result\",\"call\":%" PRIu64
                    ",\"old_reference\":",
                    price_out_calls) < 0 ||
                gz_reference(
                    price_out_stream, "arc", capture_arc_generation, index) ||
                gzprintf(price_out_stream, ",\"new_reference\":") < 0 ||
                gz_reference(
                    price_out_stream, "arc", new_generation, index) ||
                gzprintf(price_out_stream, "}\n") < 0)
                return -1;
        }
    }
    if (arena_history_count == arena_history_capacity) {
        size_t new_capacity;
        if (arena_history_capacity > SIZE_MAX / 2)
            return -1;
        new_capacity = arena_history_capacity ?
            arena_history_capacity * 2 : 8;
        if (
            new_capacity > SIZE_MAX / sizeof(*arena_history))
            return -1;
        resized_history = (arena_history_t *)realloc(
            arena_history, new_capacity * sizeof(*arena_history));
        if (!resized_history)
            return -1;
        arena_history = resized_history;
        arena_history_capacity = new_capacity;
    }
    arena_history[arena_history_count].base = (uintptr_t)old_base;
    arena_history[arena_history_count].capacity = old_capacity;
    arena_history[arena_history_count].generation = capture_arc_generation;
    ++arena_history_count;
    capture_arc_base = new_base;
    capture_arc_capacity = new_capacity;
    capture_arc_generation = new_generation;
    price_out_remapped = 1;
    return 0;
}

static int
price_out_adjacency(const network_t *net)
{
    uint64_t node_index;
    for (node_index = 0; node_index < (uint64_t)net->n + 1; ++node_index) {
        if (gzprintf(
                price_out_stream,
                "{\"kind\":\"ADJACENCY_FINAL_OBSERVED\","
                "\"role\":\"observed_result\",\"call\":%" PRIu64
                ",\"reference\":",
                price_out_calls) < 0 ||
            gz_reference(price_out_stream, "node", 0, node_index) ||
            gzprintf(price_out_stream, ",\"firstout\":") < 0 ||
            gz_arc_reference(
                price_out_stream, net->nodes[node_index].firstout) ||
            gzprintf(price_out_stream, ",\"firstin\":") < 0 ||
            gz_arc_reference(
                price_out_stream, net->nodes[node_index].firstin) ||
            gzprintf(price_out_stream, "}\n") < 0)
            return -1;
    }
    return 0;
}

static int
price_out_final_arcs(const network_t *net)
{
    uint64_t index;
    uint64_t begin = price_out_remapped ? 0 : (uint64_t)price_out_live_in_m;
    const arc_t *arc;
    for (index = begin; index < (uint64_t)net->m; ++index) {
        arc = &net->arcs[index];
        if (gzprintf(
                price_out_stream,
                "{\"kind\":\"ARC_FINAL_OBSERVED\","
                "\"role\":\"observed_result\",\"call\":%" PRIu64
                ",\"reference\":",
                price_out_calls) < 0 ||
            gz_reference(
                price_out_stream, "arc", capture_arc_generation, index) ||
            gzprintf(price_out_stream, ",\"tail\":") < 0 ||
            gz_node_reference(price_out_stream, arc->tail) ||
            gzprintf(price_out_stream, ",\"head\":") < 0 ||
            gz_node_reference(price_out_stream, arc->head) ||
            gzprintf(
                price_out_stream,
                ",\"cost\":%ld,\"org_cost\":%ld,\"flow\":%ld,"
                "\"ident\":%d,\"nextout\":",
                (long)arc->cost, (long)arc->org_cost, (long)arc->flow,
                arc->ident) < 0 ||
            gz_arc_reference(price_out_stream, arc->nextout) ||
            gzprintf(price_out_stream, ",\"nextin\":") < 0 ||
            gz_arc_reference(price_out_stream, arc->nextin) ||
            gzprintf(price_out_stream, "}\n") < 0)
            return -1;
    }
    return 0;
}

int
mcf_capture_price_out_end(const network_t *net, long new_arcs)
{
    if (!capture_events)
        return 0;
    if (!price_out_active || price_out_candidate_pending ||
        net != capture_network || new_arcs < 0 ||
        price_out_live_in_m > LONG_MAX - new_arcs ||
        net->m != price_out_live_in_m + new_arcs ||
        net->arcs != capture_arc_base || net->max_m < 0 ||
        (uint64_t)net->max_m != capture_arc_capacity)
        return -1;
    if (price_out_final_arcs(net) || price_out_adjacency(net))
        return -1;
    if (price_out_boundary(net))
        return -1;
    if (gz_price_out_state(
            price_out_stream, "PRICE_OUT_END_OBSERVED",
            "observed_result", net) ||
        gzprintf(
            price_out_stream,
            "{\"kind\":\"CALL_END\","
            "\"role\":\"observed_result\",\"call\":%" PRIu64
            ",\"order\":%" PRIu64 ",\"ordinal\":%" PRIu64
            ",\"phase\":\"price_out\"}\n",
            price_out_calls, price_out_order, price_out_calls) < 0)
        return -1;
    if (gzflush(price_out_stream, Z_SYNC_FLUSH) != Z_OK ||
        gzflush(boundary_stream, Z_SYNC_FLUSH) != Z_OK)
        return -1;
    price_out_active = 0;
    ++price_out_calls;
    if (price_out_order != call_order)
        return -1;
    ++call_order;
    return 0;
}

int
mcf_capture_finish(const char *mcf_output)
{
    char *path;
    FILE *stream;
    struct stat output_stat;
    int status = -1;
    if (!mcf_output || roi_begin_count != 1 || roi_end_count != 1 ||
        pricing_active || pricing_scan_pending || price_out_active ||
        stat(mcf_output, &output_stat))
        return -1;
    if (pricing_stream) {
        if (gzclose(pricing_stream) != Z_OK) {
            pricing_stream = NULL;
            return -1;
        }
        pricing_stream = NULL;
    }
    if (price_out_stream) {
        if (gzclose(price_out_stream) != Z_OK) {
            price_out_stream = NULL;
            return -1;
        }
        price_out_stream = NULL;
    }
    if (allocation_stream) {
        if (gzclose(allocation_stream) != Z_OK) {
            allocation_stream = NULL;
            return -1;
        }
        allocation_stream = NULL;
    }
    if (boundary_stream) {
        if (gzclose(boundary_stream) != Z_OK) {
            boundary_stream = NULL;
            return -1;
        }
        boundary_stream = NULL;
    }
    path = join_path(capture_root, "run.json");
    if (!path)
        return -1;
    stream = fopen(path, "w");
    free(path);
    if (!stream)
        return -1;
    if (fprintf(stream, "{\n  \"schema\": 1,\n  \"input\": ") < 0 ||
        json_string(stream, capture_input) ||
        fprintf(stream, ",\n  \"output_root\": ") < 0 ||
        json_string(stream, capture_root) ||
        fprintf(stream, ",\n  \"mcf_output\": ") < 0 ||
        json_string(stream, mcf_output) ||
        fprintf(
            stream,
            ",\n  \"capture_enabled\": %s,\n"
            "  \"roi_begin\": \"after_primal_start_artificial\",\n"
            "  \"roi_end\": \"after_global_opt\",\n"
            "  \"roi_begin_count\": %u,\n"
            "  \"roi_end_count\": %u,\n"
            "  \"nodes_allocated_bytes\": %" PRIu64 ",\n"
            "  \"dummy_arcs_allocated_bytes\": %" PRIu64 ",\n"
            "  \"arcs_allocated_bytes\": %" PRIu64 ",\n"
            "  \"peak_allocated_bytes\": %" PRIu64 ",\n"
            "  \"allocation_events\": %" PRIu64 ",\n"
            "  \"pricing_calls\": %" PRIu64 ",\n"
            "  \"price_out_calls\": %" PRIu64 ",\n"
            "  \"mcf_output_bytes\": %" PRIu64 "\n}\n",
            capture_events ? "true" : "false",
            roi_begin_count,
            roi_end_count,
            allocation_state.nodes,
            allocation_state.dummy_arcs,
            allocation_state.arcs,
            allocation_state.peak,
            allocation_events,
            pricing_calls,
            price_out_calls,
            (uint64_t)output_stat.st_size) < 0)
        goto done;
    if (fflush(stream) || ferror(stream))
        goto done;
    status = 0;
done:
    if (fclose(stream))
        status = -1;
    buffer_free(&pricing_live_basket);
    buffer_free(&pricing_scans);
    buffer_free(&pricing_candidates);
    buffer_free(&pricing_result_basket);
    buffer_free(&price_out_candidates_buffer);
    buffer_free(&price_out_decisions_buffer);
    free(arena_history);
    arena_history = NULL;
    arena_history_count = 0;
    arena_history_capacity = 0;
    return status;
}
