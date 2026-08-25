/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#include "mcf_capture.h"

#include <errno.h>
#include <inttypes.h>
#include <limits.h>
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

static char *capture_input;
static char *capture_root;
static char *capture_output;
static int capture_events;
static unsigned roi_begin_count;
static unsigned roi_end_count;
static allocation_state_t allocation_state;
static const network_t *capture_network;
static gzFile pricing_stream;
static uint64_t pricing_calls;
static uint64_t pricing_scan_count;
static long pricing_nr_group;
static long pricing_live_in_expected;
static long pricing_live_in_seen;
static long pricing_live_out_seen;
static int pricing_active;
static uint64_t call_order;
static uint64_t pricing_order;
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
    if (pricing_stream) {
        gzclose(pricing_stream);
        pricing_stream = NULL;
    }
    if (price_out_stream) {
        gzclose(price_out_stream);
        price_out_stream = NULL;
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
    pricing_nr_group = 0;
    pricing_live_in_expected = 0;
    pricing_live_in_seen = 0;
    pricing_live_out_seen = 0;
    pricing_active = 0;
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
    capture_arc_base = NULL;
    capture_arc_capacity = 0;
    capture_arc_generation = 0;
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
    *slot = requested;
    if (allocation_state.nodes > UINT64_MAX - allocation_state.dummy_arcs)
        return -1;
    total = allocation_state.nodes + allocation_state.dummy_arcs;
    if (total > UINT64_MAX - allocation_state.arcs)
        return -1;
    total += allocation_state.arcs;
    if (total > allocation_state.peak)
        allocation_state.peak = total;
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
    if (net != capture_network || pricing_active || price_out_active)
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
            "{\"kind\":\"BEGIN\",\"call\":%" PRIu64
            ",\"order\":%" PRIu64
            ",\"m\":%ld,\"nr_group\":%ld,\"group_pos\":%ld,"
            "\"initialize\":%s,\"basket_size\":%ld}\n",
            pricing_calls, call_order, m, nr_group, group_pos,
            initialize ? "true" : "false", basket_size) < 0)
        return -1;
    pricing_active = 1;
    pricing_order = call_order;
    pricing_scan_count = 0;
    pricing_nr_group = nr_group;
    pricing_live_in_expected = basket_size;
    pricing_live_in_seen = 0;
    pricing_live_out_seen = 0;
    return 0;
}

int
mcf_capture_pricing_basket(
    int live_out, long slot, const arc_t *arc, cost_t cost,
    cost_t abs_cost)
{
    uint64_t arc_id;
    const char *phase;
    if (!capture_events)
        return 0;
    if (!pricing_active || slot <= 0 || pricing_arc_index(arc, &arc_id))
        return -1;
    if (live_out) {
        if (slot != ++pricing_live_out_seen)
            return -1;
        phase = "live_out";
    } else {
        if (slot != ++pricing_live_in_seen ||
            pricing_live_in_seen > pricing_live_in_expected)
            return -1;
        phase = "live_in";
    }
    return gzprintf(
               pricing_stream,
               "{\"kind\":\"BASKET\",\"call\":%" PRIu64
               ",\"phase\":\"%s\",\"slot\":%ld,\"arc_id\":%" PRIu64
               ",\"cost\":%ld,\"abs_cost\":%ld}\n",
               pricing_calls, phase, slot, arc_id, (long)cost,
               (long)abs_cost) < 0 ? -1 : 0;
}

int
mcf_capture_pricing_scan(
    const arc_t *arc, cost_t reduced_cost, int candidate,
    long basket_slot)
{
    uint64_t arc_id;
    uint64_t tail_id;
    uint64_t head_id;
    cost_t recomputed;
    int expected_candidate;
    if (!capture_events)
        return 0;
    if (!pricing_active || pricing_live_in_seen != pricing_live_in_expected ||
        pricing_arc_index(arc, &arc_id) ||
        pricing_node_index(arc->tail, &tail_id) ||
        pricing_node_index(arc->head, &head_id))
        return -1;
    recomputed = arc->cost - arc->tail->potential + arc->head->potential;
    expected_candidate =
        ((recomputed < 0) && (arc->ident == AT_LOWER)) ||
        ((recomputed > 0) && (arc->ident == AT_UPPER));
    if (recomputed != reduced_cost || !!candidate != expected_candidate ||
        (basket_slot >= 0 && (!candidate || basket_slot == 0)))
        return -1;
    if (gzprintf(
            pricing_stream,
            "{\"kind\":\"SCAN\",\"call\":%" PRIu64
            ",\"arc_id\":%" PRIu64 ",\"tail_id\":%" PRIu64
            ",\"head_id\":%" PRIu64 ",\"arc_cost\":%ld,"
            "\"tail_potential\":%ld,\"head_potential\":%ld,"
            "\"ident\":%d,\"reduced_cost\":%ld,\"candidate\":%s,"
            "\"basket_slot\":%ld,\"group_pos\":%" PRIu64 "}\n",
            pricing_calls, arc_id, tail_id, head_id, (long)arc->cost,
            (long)arc->tail->potential, (long)arc->head->potential,
            arc->ident, (long)reduced_cost, candidate ? "true" : "false",
            basket_slot, arc_id % (uint64_t)pricing_nr_group) < 0)
        return -1;
    ++pricing_scan_count;
    return 0;
}

int
mcf_capture_pricing_end(
    const arc_t *selected, cost_t reduced_cost, long arcs_priced,
    long nr_group, long group_pos, long initialize, long basket_size)
{
    uint64_t selected_id = UINT64_MAX;
    long selected_json = -1;
    if (!capture_events)
        return 0;
    if (!pricing_active || nr_group != pricing_nr_group || group_pos < 0 ||
        group_pos >= nr_group || basket_size < 0 ||
        pricing_live_in_seen != pricing_live_in_expected ||
        pricing_live_out_seen != basket_size || arcs_priced < 0 ||
        (uint64_t)arcs_priced != pricing_scan_count)
        return -1;
    if (selected) {
        if (pricing_arc_index(selected, &selected_id) ||
            selected_id > (uint64_t)LONG_MAX)
            return -1;
        selected_json = (long)selected_id;
    } else if (basket_size != 0 || reduced_cost != 0) {
        return -1;
    }
    if (gzprintf(
            pricing_stream,
            "{\"kind\":\"END\",\"call\":%" PRIu64
            ",\"selected_arc_id\":%ld,\"reduced_cost\":%ld,"
            "\"arcs_priced\":%ld,\"nr_group\":%ld,"
            "\"group_pos\":%ld,\"initialize\":%s,"
            "\"basket_size\":%ld}\n",
            pricing_calls, selected_json, (long)reduced_cost, arcs_priced,
            nr_group, group_pos, initialize ? "true" : "false",
            basket_size) < 0)
        return -1;
    if (gzflush(pricing_stream, Z_SYNC_FLUSH) != Z_OK)
        return -1;
    pricing_active = 0;
    ++pricing_calls;
    if (pricing_order != call_order)
        return -1;
    ++call_order;
    return 0;
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
    if (gzprintf(
            price_out_stream,
            "{\"kind\":\"BEGIN\",\"call\":%" PRIu64
            ",\"order\":%" PRIu64
            ",\"live_in_m\":%ld,\"capacity\":%" PRIu64
            ",\"generation\":%u}\n",
            price_out_calls, call_order, net->m, capture_arc_capacity,
            capture_arc_generation) < 0)
        return -1;
    price_out_active = 1;
    price_out_order = call_order;
    price_out_candidate_pending = 0;
    price_out_candidates = 0;
    price_out_live_in_m = net->m;
    price_out_remapped = 0;
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
            "{\"kind\":\"CANDIDATE\",\"call\":%" PRIu64
            ",\"candidate\":%" PRIu64 ",\"tail_id\":%" PRIu64
            ",\"head_id\":%" PRIu64 ",\"arc_cost\":%ld,"
            "\"tail_potential\":%ld,\"head_potential\":%ld,"
            "\"reduced_cost\":%ld}\n",
            price_out_calls, price_out_candidates, tail_id, head_id,
            (long)arc_cost, (long)tail->potential, (long)head->potential,
            (long)reduced_cost) < 0)
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
                "{\"kind\":\"DECISION\",\"call\":%" PRIu64
                ",\"candidate\":%" PRIu64 ",\"decision\":\"%s\","
                "\"reference\":{}}\n",
                price_out_calls, price_out_candidates, names[decision]) < 0)
            return -1;
    } else if (gzprintf(
                   price_out_stream,
                   "{\"kind\":\"DECISION\",\"call\":%" PRIu64
                   ",\"candidate\":%" PRIu64
                   ",\"decision\":\"%s\",\"reference\":{"
                   "\"kind\":\"arc\",\"generation\":%u,"
                   "\"index\":%" PRIu64 "}}\n",
                   price_out_calls, price_out_candidates, names[decision],
                   capture_arc_generation, index) < 0) {
        return -1;
    }
    ++price_out_candidates;
    price_out_candidate_pending = 0;
    return 0;
}

int
mcf_capture_price_out_arc_state(const arc_t *slot)
{
    uint64_t index;
    uint64_t tail_id;
    uint64_t head_id;
    if (!capture_events)
        return 0;
    if (!price_out_active || !price_out_candidate_pending ||
        !slot || stable_index(
            slot, capture_arc_base, capture_arc_capacity,
            sizeof(arc_t), &index) ||
        pricing_node_index(slot->tail, &tail_id) ||
        pricing_node_index(slot->head, &head_id))
        return -1;
    return gzprintf(
               price_out_stream,
               "{\"kind\":\"ARC_STATE\",\"call\":%" PRIu64
               ",\"candidate\":%" PRIu64 ",\"reference\":{"
               "\"kind\":\"arc\",\"generation\":%u,"
               "\"index\":%" PRIu64 "},\"tail_id\":%" PRIu64
               ",\"head_id\":%" PRIu64 ",\"cost\":%ld,"
               "\"org_cost\":%ld,\"flow\":%ld,\"ident\":%d}\n",
               price_out_calls, price_out_candidates,
               capture_arc_generation, index, tail_id, head_id,
               (long)slot->cost, (long)slot->org_cost, (long)slot->flow,
               slot->ident) < 0 ? -1 : 0;
}

int
mcf_capture_arena_remap(
    const arc_t *old_base, uint64_t old_capacity, const arc_t *new_base,
    uint64_t new_capacity)
{
    uint32_t new_generation;
    if ((capture_events &&
         (!price_out_active || price_out_candidate_pending)) ||
        !capture_network || !new_base || old_base != capture_arc_base ||
        old_capacity != capture_arc_capacity ||
        new_capacity <= old_capacity || capture_arc_generation == UINT32_MAX)
        return -1;
    new_generation = capture_arc_generation + 1;
    if (capture_events && gzprintf(
            price_out_stream,
            "{\"kind\":\"ARENA_REMAP\",\"call\":%" PRIu64
            ",\"old_generation\":%u,\"new_generation\":%u,"
            "\"mapped_elements\":%ld,\"old_capacity\":%" PRIu64
            ",\"new_capacity\":%" PRIu64 "}\n",
            price_out_calls, capture_arc_generation, new_generation,
            price_out_live_in_m, old_capacity, new_capacity) < 0)
        return -1;
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
    uint64_t firstout;
    uint64_t firstin;
    int out_status;
    int in_status;
    for (node_index = 0; node_index < (uint64_t)net->n + 1; ++node_index) {
        out_status = stable_index(
            net->nodes[node_index].firstout, net->arcs, (uint64_t)net->m,
            sizeof(arc_t), &firstout);
        in_status = stable_index(
            net->nodes[node_index].firstin, net->arcs, (uint64_t)net->m,
            sizeof(arc_t), &firstin);
        if (out_status < 0 || in_status < 0)
            return -1;
        if (gzprintf(
                price_out_stream,
                "{\"kind\":\"ADJACENCY\",\"call\":%" PRIu64
                ",\"node_id\":%" PRIu64 ",\"generation\":%u,"
                "\"firstout_index\":%ld,\"firstin_index\":%ld}\n",
                price_out_calls, node_index, capture_arc_generation,
                out_status == 1 ? -1L : (long)firstout,
                in_status == 1 ? -1L : (long)firstin) < 0)
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
    if ((new_arcs != 0 || price_out_remapped) && price_out_adjacency(net))
        return -1;
    if (gzprintf(
            price_out_stream,
            "{\"kind\":\"END\",\"call\":%" PRIu64
            ",\"new_arcs\":%ld,\"live_out_m\":%ld,"
            "\"candidates\":%" PRIu64 ",\"capacity\":%" PRIu64
            ",\"generation\":%u,\"m_impl\":%ld,"
            "\"max_residual_new_m\":%ld}\n",
            price_out_calls, new_arcs, net->m, price_out_candidates,
            capture_arc_capacity, capture_arc_generation, net->m_impl,
            net->max_residual_new_m) < 0)
        return -1;
    if (gzflush(price_out_stream, Z_SYNC_FLUSH) != Z_OK)
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
        pricing_active || price_out_active ||
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
    return status;
}
