/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#include "mcf_capture.h"

#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

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
        return write_ref(stream, 2, 0, index);
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
    free(capture_input);
    free(capture_root);
    free(capture_output);
    capture_input = copy_string(input);
    capture_root = copy_string(output_root);
    capture_output = join_path(output_root, "mcf.out");
    capture_events = capture_enabled ? 1 : 0;
    roi_begin_count = 0;
    roi_end_count = 0;
    memset(&allocation_state, 0, sizeof(allocation_state));
    return capture_input && capture_root && capture_output ? 0 : -1;
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
    if (write_network_state("final.state", net))
        return -1;
    ++roi_end_count;
    printf("MCF_CAPTURE_ROI_END=after_global_opt\n");
    fflush(stdout);
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
        stat(mcf_output, &output_stat))
        return -1;
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
            "  \"mcf_output_bytes\": %" PRIu64 "\n}\n",
            capture_events ? "true" : "false",
            roi_begin_count,
            roi_end_count,
            allocation_state.nodes,
            allocation_state.dummy_arcs,
            allocation_state.arcs,
            allocation_state.peak,
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
