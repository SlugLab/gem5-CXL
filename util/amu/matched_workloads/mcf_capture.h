/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#ifndef MCF_CAPTURE_H
#define MCF_CAPTURE_H

#include "defines.h"

#include <stddef.h>
#include <stdint.h>

int mcf_capture_configure(
    const char *input, const char *output_root, int capture_enabled);
int mcf_capture_set_inputfile(char *destination, size_t destination_bytes);
const char *mcf_capture_output_file(void);
int mcf_capture_allocation(
    const char *kind, uint64_t elements, uint64_t element_bytes,
    uint64_t current_bytes);
int mcf_capture_roi_begin(const network_t *net);
int mcf_capture_roi_end(const network_t *net);
int mcf_capture_finish(const char *mcf_output);

#endif /* MCF_CAPTURE_H */
