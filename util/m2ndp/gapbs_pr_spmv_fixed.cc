// Copyright (c) 2015, The Regents of the University of California (Regents)
// Copyright (c) 2026
// SPDX-License-Identifier: BSD-3-Clause

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <iostream>
#include <string>
#include <stdexcept>
#include <utility>
#include <vector>

#include <fcntl.h>
#include <unistd.h>

#include <gem5/m5ops.h>

#include "benchmark.h"
#include "command_line.h"
#include "graph.h"
#include "m2ndp_experiment_config.h"
#include "pvector.h"
#include "util.h"

using ScoreT = float;

constexpr ScoreT kDamp = 0.85f;
constexpr int kPageRankIterations = 20;

void PageRankPullFixed(const Graph &g, pvector<ScoreT> &scores,
                       pvector<ScoreT> &outgoing_contrib) {
  const ScoreT init_score = 1.0f / g.num_nodes();
  const ScoreT base_score = (1.0f - kDamp) / g.num_nodes();
#pragma omp parallel for
  for (NodeID node = 0; node < g.num_nodes(); ++node)
    scores[node] = init_score;
  for (int iteration = 0; iteration < kPageRankIterations; ++iteration) {
#pragma omp parallel for
    for (NodeID node = 0; node < g.num_nodes(); ++node)
      outgoing_contrib[node] = scores[node] / g.out_degree(node);
#pragma omp parallel for schedule(dynamic, 16384)
    for (NodeID u = 0; u < g.num_nodes(); ++u) {
      ScoreT incoming_total = 0.0f;
      for (NodeID v : g.in_neigh(u))
        incoming_total = incoming_total + outgoing_contrib[v];
      const ScoreT product = kDamp * incoming_total;
      scores[u] = base_score + product;
    }
  }
}

bool PRVerifier(const Graph &g, const pvector<ScoreT> &scores,
                double target_error) {
  const ScoreT base_score = (1.0f - kDamp) / g.num_nodes();
  pvector<ScoreT> incoming_sums(g.num_nodes(), 0);
  double error = 0;
  for (NodeID u : g.vertices()) {
    const ScoreT outgoing_contrib = scores[u] / g.out_degree(u);
    for (NodeID v : g.out_neigh(u))
      incoming_sums[v] += outgoing_contrib;
  }
  for (NodeID node : g.vertices()) {
    error += std::fabs(
        base_score + kDamp * incoming_sums[node] - scores[node]);
    incoming_sums[node] = 0;
  }
  PrintTime("Total Error", error);
  return error < target_error;
}

void WriteAll(int descriptor, const void *data, size_t size) {
  const auto *cursor = static_cast<const uint8_t *>(data);
  while (size) {
    const ssize_t written = write(descriptor, cursor, size);
    if (written < 0) {
      if (errno == EINTR)
        continue;
      throw std::runtime_error("reference write failed");
    }
    if (written == 0)
      throw std::runtime_error("reference write made no progress");
    cursor += written;
    size -= static_cast<size_t>(written);
  }
}

void WriteScoreBits(const std::string &path,
                    const pvector<ScoreT> &scores) {
  const std::string temporary = path + ".tmp";
  const int descriptor =
      open(temporary.c_str(), O_CREAT | O_TRUNC | O_WRONLY, 0644);
  if (descriptor < 0)
    throw std::runtime_error("cannot open reference output");
  try {
    for (ScoreT score : scores) {
      uint32_t bits = 0;
      static_assert(sizeof(bits) == sizeof(score), "float32 required");
      std::memcpy(&bits, &score, sizeof(bits));
      const uint8_t encoded[4] = {
          static_cast<uint8_t>(bits),
          static_cast<uint8_t>(bits >> 8),
          static_cast<uint8_t>(bits >> 16),
          static_cast<uint8_t>(bits >> 24),
      };
      WriteAll(descriptor, encoded, sizeof(encoded));
    }
    if (fsync(descriptor) != 0)
      throw std::runtime_error("reference fsync failed");
    if (close(descriptor) != 0)
      throw std::runtime_error("reference close failed");
  } catch (...) {
    close(descriptor);
    unlink(temporary.c_str());
    throw;
  }
  if (rename(temporary.c_str(), path.c_str()) != 0) {
    unlink(temporary.c_str());
    throw std::runtime_error("reference rename failed");
  }
}

int main(int argc, char **argv) {
  CLPageRank cli(argc, argv, "pagerank", 1e-4, kPageRankIterations);
  if (!cli.ParseArgs())
    return 64;
  Builder builder(cli);
  Graph g = builder.MakeGraph();

  for (int trial = 0; trial < cli.num_trials(); ++trial) {
    pvector<ScoreT> scores(g.num_nodes());
    pvector<ScoreT> outgoing_contrib(g.num_nodes());
    std::fill(scores.begin(), scores.end(), 0.0f);
    std::fill(outgoing_contrib.begin(), outgoing_contrib.end(), 0.0f);

    m5_work_begin(trial, 0);
    PageRankPullFixed(g, scores, outgoing_contrib);
    m5_work_end(trial, 0);

    if (trial == cli.num_trials() - 1) {
      bool verification_passed = true;
      if (cli.do_verify())
        verification_passed =
            PRVerifier(g, scores, cli.tolerance());
      const char *marker = verification_passed ? "Verification: PASS\n" :
                                                 "Verification: FAIL\n";
      WriteAll(STDERR_FILENO, marker, std::strlen(marker));
      if (!verification_passed)
        m5_fail(0, 1);
      WriteScoreBits(M2NDP_REFERENCE_RAW_PATH, scores);
    }
  }
  m5_exit(0);
  return 0;
}
