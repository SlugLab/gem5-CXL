// Copyright (c) 2015, The Regents of the University of California (Regents)
// Copyright (c) 2026
// SPDX-License-Identifier: BSD-3-Clause

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <string>
#include <utility>

#include <immintrin.h>
#include <omp.h>
#include <unistd.h>

#include <gem5/m5ops.h>

#include "benchmark.h"
#include "command_line.h"
#include "graph.h"
#include "m2ndp_experiment_config.h"
#include "pr_row_offload.h"
#include "pvector.h"
#include "util.h"

#if defined(PR_OFFLOAD_AMU) == defined(PR_OFFLOAD_CIRA)
#error "define exactly one of PR_OFFLOAD_AMU or PR_OFFLOAD_CIRA"
#endif

#if defined(PR_OFFLOAD_AMU)
#include "amu.h"
#else
#include "cira.h"
#if (defined(PR_CIRA_POLICY_STATIC) + defined(PR_CIRA_POLICY_PGO) + \
     defined(PR_CIRA_POLICY_FEWSHOT)) != 1
#error "define exactly one CIRA runtime policy"
#endif
#endif

using ScoreT = float;

constexpr ScoreT kDamp = 0.85f;
constexpr int kPageRankIterations = 20;
constexpr uint32_t kWorkers = 4;

enum class Candidate : uint32_t
{
  A = 0,
  B = 1,
  C = 2,
};

struct CandidateConfig
{
  uint32_t rowWindow;
  uint32_t leadBlocks;
};

constexpr CandidateConfig kCandidates[] = {
    {64, 1},
    {2048, 32},
    {1024, 16},
};

enum class Phase : uint32_t
{
  Formation = 0,
  Sampling = 1,
  Selection = 2,
  Jit = 3,
  Execution = 4,
  Drain = 5,
};

struct PhaseLedger
{
  uint64_t formation = 0;
  uint64_t sampling = 0;
  uint64_t selection = 0;
  uint64_t jit = 0;
  uint64_t execution = 0;
  uint64_t drain = 0;
  uint64_t total = 0;
  uint64_t startTime = 0;
  uint64_t lastTime = 0;
  Phase current = Phase::Formation;

  uint64_t &bucket(Phase phase)
  {
    switch (phase) {
      case Phase::Formation: return formation;
      case Phase::Sampling: return sampling;
      case Phase::Selection: return selection;
      case Phase::Jit: return jit;
      case Phase::Execution: return execution;
      case Phase::Drain: return drain;
    }
    return drain;
  }

  void start(uint64_t now)
  {
    startTime = now;
    lastTime = now;
    current = Phase::Formation;
  }

  void transition(Phase next, uint64_t now)
  {
    if (now < lastTime)
      m5_fail(0, 200);
    bucket(current) += now - lastTime;
    lastTime = now;
    current = next;
  }

  void finish(uint64_t now)
  {
    transition(current, now);
    total = now - startTime;
  }

  bool valid(bool requireFewShot) const
  {
    const uint64_t sum = formation + sampling + selection + jit +
        execution + drain;
    return sum == total &&
        (!requireFewShot || (sampling > 0 && selection > 0 && jit > 0));
  }
};

#if defined(PR_OFFLOAD_CIRA) && defined(PR_CIRA_POLICY_FEWSHOT)
struct SampleOutputs
{
  pvector<ScoreT> a;
  pvector<ScoreT> b;
  pvector<ScoreT> c;

  explicit SampleOutputs(size_t nodes) : a(nodes), b(nodes), c(nodes) {}
};
#endif

static inline uint32_t
candidateIndex(Candidate candidate)
{
  return static_cast<uint32_t>(candidate);
}

static inline const CandidateConfig &
candidateConfig(Candidate candidate)
{
  return kCandidates[candidateIndex(candidate)];
}

static inline uint64_t
submitRows(const pr_row_offload_desc *desc)
{
#if defined(PR_OFFLOAD_AMU)
  return amu_pr_rows(desc);
#else
  return cira_pr_rows(desc);
#endif
}

static inline uint64_t
getFinished()
{
#if defined(PR_OFFLOAD_AMU)
  return amu_getfin();
#else
  return cira_getfin();
#endif
}

static void
waitForExact(uint64_t expected, uint64_t failure)
{
  if (expected == 0)
    m5_fail(0, failure);
  for (;;) {
    const uint64_t completed = getFinished();
    if (completed == expected)
      return;
    if (completed != 0)
      m5_fail(0, failure + 1);
#if defined(PR_OFFLOAD_AMU)
    m5_amu_waitfin();
#else
    asm volatile("pause" ::: "memory");
#endif
  }
}

static inline void
configureCandidate(Candidate candidate)
{
#if defined(PR_OFFLOAD_CIRA)
  const CandidateConfig &config = candidateConfig(candidate);
  if (cira_cfgwr(CIRA_CFG_PR_ROW_WINDOW, config.rowWindow) == 0 ||
      cira_cfgwr(CIRA_CFG_PR_LEAD_BLOCKS, config.leadBlocks) == 0)
    m5_fail(0, 210 + candidateIndex(candidate));
#else
  (void)candidate;
#endif
}

static pr_row_offload_desc
makeDescriptor(const pvector<uint64_t> &inOffsets,
               const NodeID *inNeighbors,
               const pvector<int64_t> &outDegrees,
               const pvector<ScoreT> &scores,
               pvector<ScoreT> &contributions,
               pvector<ScoreT> &nextScores,
               uint64_t begin, uint64_t end, uint64_t iteration,
               uint32_t phase, Candidate candidate)
{
  const CandidateConfig &config = candidateConfig(candidate);
  pr_row_offload_desc desc = {};
  desc.in_offsets_addr = reinterpret_cast<uint64_t>(inOffsets.data());
  desc.in_neighbors_addr = reinterpret_cast<uint64_t>(inNeighbors);
  desc.out_degree_addr = reinterpret_cast<uint64_t>(outDegrees.data());
  desc.scores_in_addr = reinterpret_cast<uint64_t>(scores.data());
  desc.contributions_addr =
      reinterpret_cast<uint64_t>(contributions.data());
  desc.scores_out_addr = reinterpret_cast<uint64_t>(nextScores.data());
  desc.row_begin = begin;
  desc.row_count = end - begin;
  desc.node_count = scores.size();
  desc.iteration = iteration;
  desc.phase = phase;
  desc.row_window = config.rowWindow;
  desc.lead_blocks = config.leadBlocks;
  const ScoreT baseScore = (1.0f - kDamp) / scores.size();
  std::memcpy(&desc.damping_bits, &kDamp, sizeof(kDamp));
  std::memcpy(&desc.base_score_bits, &baseScore, sizeof(baseScore));
  return desc;
}

static void
submitAndWait(const pr_row_offload_desc &desc, uint64_t failure)
{
  waitForExact(submitRows(&desc), failure);
}

static void flushRange(const void *data, uint64_t size);

#if defined(PR_OFFLOAD_CIRA) && defined(PR_CIRA_POLICY_FEWSHOT)
static uint64_t
sampleCandidate(Candidate candidate, const pr_row_offload_desc &full,
                pvector<ScoreT> &discardedOutput)
{
  flushRange(
      reinterpret_cast<const void *>(full.contributions_addr),
      full.node_count * sizeof(ScoreT));
  _mm_mfence();
  configureCandidate(candidate);
  pr_row_offload_desc sample = full;
  sample.row_begin = 0;
  sample.row_count = std::min<uint64_t>(64, full.node_count);
  sample.scores_out_addr = reinterpret_cast<uint64_t>(discardedOutput.data());
  sample.row_window = candidateConfig(candidate).rowWindow;
  sample.lead_blocks = candidateConfig(candidate).leadBlocks;
  const uint64_t start = m5_rpns();
  submitAndWait(sample, 220 + candidateIndex(candidate) * 2);
  const uint64_t end = m5_rpns();
  return end - start;
}

static Candidate
selectMinimumPositive(const uint64_t durations[3])
{
  Candidate selected = Candidate::A;
  uint64_t best = durations[0];
  if (best == 0)
    m5_fail(0, 230);
  for (uint32_t index = 1; index < 3; ++index) {
    if (durations[index] == 0)
      m5_fail(0, 231 + index);
    if (durations[index] < best) {
      best = durations[index];
      selected = static_cast<Candidate>(index);
    }
  }
  return selected;
}
#endif

static void
flushRange(const void *data, uint64_t size)
{
  const auto *bytes = static_cast<const uint8_t *>(data);
  for (uint64_t offset = 0; offset < size; offset += 64)
    _mm_clflush(bytes + offset);
}

static void
initializePageRank(const Graph &g, pvector<ScoreT> &scores,
                   pvector<ScoreT> &nextScores,
                   pvector<ScoreT> &contributions,
                   pvector<uint64_t> &inOffsets,
                   pvector<int64_t> &outDegrees)
{
  const ScoreT initial = 1.0f / g.num_nodes();
  const pvector<SGOffset> sourceOffsets = g.VertexOffsets(true);
  for (NodeID node = 0; node < g.num_nodes(); ++node) {
    scores[node] = initial;
    nextScores[node] = 0.0f;
    contributions[node] = 0.0f;
    inOffsets[node] = static_cast<uint64_t>(sourceOffsets[node]);
    outDegrees[node] = g.out_degree(node);
  }
  inOffsets[g.num_nodes()] =
      static_cast<uint64_t>(sourceOffsets[g.num_nodes()]);
}

static bool
verifyPageRank(const Graph &g, const pvector<ScoreT> &scores,
               double targetError)
{
  const ScoreT baseScore = (1.0f - kDamp) / g.num_nodes();
  pvector<ScoreT> incomingSums(g.num_nodes(), 0);
  double error = 0;
  for (NodeID u : g.vertices()) {
    const ScoreT contribution = scores[u] / g.out_degree(u);
    for (NodeID v : g.out_neigh(u))
      incomingSums[v] += contribution;
  }
  for (NodeID node : g.vertices()) {
    error += std::fabs(baseScore + kDamp * incomingSums[node] - scores[node]);
    incomingSums[node] = 0;
  }
  PrintTime("Total Error", error);
  return error < targetError;
}

static void
writeAll(int descriptor, const void *data, size_t size)
{
  const auto *cursor = static_cast<const uint8_t *>(data);
  while (size) {
    const ssize_t written = write(descriptor, cursor, size);
    if (written < 0) {
      if (errno == EINTR)
        continue;
      throw std::runtime_error("verification marker write failed");
    }
    if (written == 0)
      throw std::runtime_error("verification marker write made no progress");
    cursor += written;
    size -= static_cast<size_t>(written);
  }
}

static void
commitScoreBits(const std::string &path, const pvector<ScoreT> &scores)
{
  const uint64_t size = scores.size() * sizeof(ScoreT);
  flushRange(scores.data(), size);
  _mm_mfence();
  const uint64_t written = m5_write_file(scores.data(), size, 0, path.c_str());
  if (written != size)
    throw std::runtime_error("reference m5_write_file was short");
}

static Candidate
initialCandidate()
{
#if defined(PR_OFFLOAD_AMU)
  return Candidate::A;
#elif defined(PR_CIRA_POLICY_STATIC)
  return Candidate::A;
#elif defined(PR_CIRA_POLICY_PGO)
#if PR_CIRA_SOURCE_ROW == 0
  return Candidate::A;
#elif PR_CIRA_SOURCE_ROW == 1
  return Candidate::B;
#elif PR_CIRA_SOURCE_ROW == 2
  return Candidate::C;
#else
#error "PR_CIRA_SOURCE_ROW must be 0, 1, or 2"
#endif
#elif defined(PR_CIRA_POLICY_FEWSHOT)
  return Candidate::A;
#else
#error "CIRA build requires exactly one runtime policy"
#endif
}

static bool
isFewShot()
{
#if defined(PR_OFFLOAD_CIRA) && defined(PR_CIRA_POLICY_FEWSHOT)
  return true;
#else
  return false;
#endif
}

int
main(int argc, char **argv)
{
  CLPageRank cli(argc, argv, "pagerank", 1e-4, kPageRankIterations);
  if (!cli.ParseArgs())
    return 64;
  Builder builder(cli);
  Graph g = builder.MakeGraph();
  if (g.num_nodes() <= 0)
    return 65;

  const NodeID *inNeighbors = g.in_neigh(0).begin();
  pvector<ScoreT> scores(g.num_nodes());
  pvector<ScoreT> nextScores(g.num_nodes());
  pvector<ScoreT> contributions(g.num_nodes());
  pvector<uint64_t> inOffsets(g.num_nodes() + 1);
  pvector<int64_t> outDegrees(g.num_nodes());
#if defined(PR_OFFLOAD_CIRA) && defined(PR_CIRA_POLICY_FEWSHOT)
  SampleOutputs discardedSampleOutputs(
      std::min<size_t>(64, g.num_nodes()));
#endif
  const std::string referencePath = M2NDP_REFERENCE_RAW_PATH;

  for (int trial = 0; trial < cli.num_trials(); ++trial) {
    initializePageRank(g, scores, nextScores, contributions,
                       inOffsets, outDegrees);
#if defined(PR_OFFLOAD_CIRA) && defined(PR_CIRA_POLICY_FEWSHOT)
    std::fill(discardedSampleOutputs.a.begin(),
              discardedSampleOutputs.a.end(), 0.0f);
    std::fill(discardedSampleOutputs.b.begin(),
              discardedSampleOutputs.b.end(), 0.0f);
    std::fill(discardedSampleOutputs.c.begin(),
              discardedSampleOutputs.c.end(), 0.0f);
#endif
    flushRange(inOffsets.data(), inOffsets.size() * sizeof(uint64_t));
    flushRange(inNeighbors,
               static_cast<uint64_t>(g.num_edges_directed()) *
                   sizeof(NodeID));
    flushRange(outDegrees.data(), outDegrees.size() * sizeof(int64_t));
    flushRange(scores.data(), scores.size() * sizeof(ScoreT));
    flushRange(nextScores.data(), nextScores.size() * sizeof(ScoreT));
    flushRange(contributions.data(), contributions.size() * sizeof(ScoreT));
    _mm_mfence();

    PhaseLedger ledger;
    Candidate selectedCandidate = initialCandidate();
#if defined(PR_OFFLOAD_CIRA) && defined(PR_CIRA_POLICY_FEWSHOT)
    uint64_t sampleDurations[3] = {0, 0, 0};
#endif

    m5_work_begin(trial, 0);
    ledger.start(m5_rpns());
#pragma omp parallel num_threads(4) shared(selectedCandidate)
    {
      if (omp_get_num_threads() != 4)
        m5_fail(0, 240);
      const uint32_t worker = omp_get_thread_num();
      uint64_t begin = 0;
      uint64_t end = 0;
      pr_static_partition(g.num_nodes(), kWorkers, worker, &begin, &end);

      for (uint64_t iteration = 0;
           iteration < kPageRankIterations; ++iteration) {
#pragma omp barrier
#pragma omp single
        ledger.transition(Phase::Formation, m5_rpns());
        configureCandidate(selectedCandidate);
        const pr_row_offload_desc contribution = makeDescriptor(
            inOffsets, inNeighbors, outDegrees, scores, contributions,
            nextScores, begin, end, iteration, PR_ROW_CONTRIB,
            selectedCandidate);
#pragma omp barrier
#pragma omp single
        ledger.transition(Phase::Execution, m5_rpns());
        submitAndWait(contribution, 250 + worker);
#if defined(PR_OFFLOAD_CIRA)
        flushRange(contributions.data() + begin,
                   (end - begin) * sizeof(ScoreT));
        _mm_mfence();
#endif
#pragma omp barrier

#if defined(PR_OFFLOAD_CIRA) && defined(PR_CIRA_POLICY_FEWSHOT)
        if (iteration == 0) {
#pragma omp single
          {
            ledger.transition(Phase::Sampling, m5_rpns());
            pr_row_offload_desc pilot = makeDescriptor(
                inOffsets, inNeighbors, outDegrees, scores, contributions,
                discardedSampleOutputs.a, 0, g.num_nodes(), iteration,
                PR_ROW_PULL, Candidate::A);
            sampleDurations[0] = sampleCandidate(Candidate::A, pilot,
                                                 discardedSampleOutputs.a);
            sampleDurations[1] = sampleCandidate(Candidate::B, pilot,
                                                 discardedSampleOutputs.b);
            sampleDurations[2] = sampleCandidate(Candidate::C, pilot,
                                                 discardedSampleOutputs.c);
            ledger.transition(Phase::Selection, m5_rpns());
            selectedCandidate = selectMinimumPositive(sampleDurations);
            ledger.transition(Phase::Jit, m5_rpns());
            configureCandidate(selectedCandidate);
            const uint64_t reconfiguration = cira_cfgwr(
                CIRA_CFG_PR_RECONFIGURE, candidateIndex(selectedCandidate));
            waitForExact(reconfiguration, 270);
            ledger.transition(Phase::Execution, m5_rpns());
          }
        }
#endif

#pragma omp barrier
#pragma omp single
        ledger.transition(Phase::Formation, m5_rpns());
        configureCandidate(selectedCandidate);
        const pr_row_offload_desc pull = makeDescriptor(
            inOffsets, inNeighbors, outDegrees, scores, contributions,
            nextScores, begin, end, iteration, PR_ROW_PULL,
            selectedCandidate);
#pragma omp barrier
#pragma omp single
        ledger.transition(Phase::Execution, m5_rpns());
        submitAndWait(pull, 280 + worker);
#if defined(PR_OFFLOAD_CIRA)
        flushRange(nextScores.data() + begin,
                   (end - begin) * sizeof(ScoreT));
        _mm_mfence();
#endif
#pragma omp barrier
#pragma omp single
        scores.swap(nextScores);
#pragma omp barrier
      }

#pragma omp barrier
#pragma omp single
      ledger.transition(Phase::Drain, m5_rpns());
#pragma omp barrier
#if defined(PR_OFFLOAD_AMU)
      if (amu_cfgrd(AMU_CFG_OUTSTANDING) != 0)
        m5_fail(0, 290 + worker);
#else
      if (cira_cfgrd(CIRA_CFG_OUTSTANDING) != 0)
        m5_fail(0, 290 + worker);
#endif
#pragma omp barrier
    }
    ledger.finish(m5_rpns());
    if (!ledger.valid(isFewShot()))
      m5_fail(0, 300);
    m5_work_end(trial, 0);

    std::fprintf(
        stderr,
        "PR_E2E_PHASES formation=%llu sampling=%llu selection=%llu "
        "jit=%llu execution=%llu drain=%llu total=%llu\n",
        static_cast<unsigned long long>(ledger.formation),
        static_cast<unsigned long long>(ledger.sampling),
        static_cast<unsigned long long>(ledger.selection),
        static_cast<unsigned long long>(ledger.jit),
        static_cast<unsigned long long>(ledger.execution),
        static_cast<unsigned long long>(ledger.drain),
        static_cast<unsigned long long>(ledger.total));
#if defined(PR_OFFLOAD_CIRA)
    std::fprintf(
        stderr,
        "PR_CIRA_POLICY selected=%c sample_a=%llu sample_b=%llu "
        "sample_c=%llu\n",
        static_cast<char>('A' + candidateIndex(selectedCandidate)),
#if defined(PR_CIRA_POLICY_FEWSHOT)
        static_cast<unsigned long long>(sampleDurations[0]),
        static_cast<unsigned long long>(sampleDurations[1]),
        static_cast<unsigned long long>(sampleDurations[2]));
#else
        0ULL, 0ULL, 0ULL);
#endif
#endif

    if (trial == cli.num_trials() - 1) {
      bool passed = true;
      if (cli.do_verify())
        passed = verifyPageRank(g, scores, cli.tolerance());
      const char *marker = passed ? "Verification: PASS\n" :
                                    "Verification: FAIL\n";
      writeAll(STDERR_FILENO, marker, std::strlen(marker));
      if (!passed)
        m5_fail(0, 301);
      commitScoreBits(referencePath, scores);
    }
  }
  m5_exit(0);
  return 0;
}
