# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]


HARNESS = r'''
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include <deque>
#include <stdexcept>
#include <string>

#include "gapbs_amu_line_cache.h"

struct Failure : public std::runtime_error {
  explicit Failure(const char *message) : std::runtime_error(message) {}
};

struct FakeBackend {
  uint64_t next_id = 1;
  uint64_t loads = 0;
  bool reverse = false;
  std::deque<uint64_t> completions;

  uint64_t load(void *destination, const void *source, size_t bytes) {
    if (bytes != gapbs_amu::kLineBytes)
      fail("wrong request size");
    memcpy(destination, source, bytes);
    const uint64_t id = next_id++;
    completions.push_back(id);
    ++loads;
    return id;
  }

  uint64_t getfin() {
    if (completions.empty())
      return 0;
    uint64_t id;
    if (reverse) {
      id = completions.back();
      completions.pop_back();
    } else {
      id = completions.front();
      completions.pop_front();
    }
    return id;
  }

  [[noreturn]] void fail(const char *message) { throw Failure(message); }
};

static void require(bool condition, const char *message) {
  if (!condition)
    throw Failure(message);
}

static int success_cases() {
  static_assert(gapbs_amu::kStagingBytesPerThread == 8192,
                "staging budget differs");
  static_assert(gapbs_amu::kCacheBytesPerThread == 8192,
                "cache budget differs");
  static_assert(gapbs_amu::kDataBytesPerThread == 16384,
                "thread budget differs");
  static_assert(gapbs_amu::kTotalDataBytes == 65536,
                "four-thread budget differs");

  alignas(64) unsigned char source[129 * 64] = {};
  *reinterpret_cast<uint32_t *>(source + 4) = 0x11223344;
  *reinterpret_cast<uint32_t *>(source + 8) = 0x55667788;
  *reinterpret_cast<uint32_t *>(source + 64) = 0x99aabbcc;
  *reinterpret_cast<uint32_t *>(source + 128 * 64) = 0xddeeff00;

  FakeBackend backend;
  gapbs_amu::LineStore<FakeBackend> store(backend);
  store.begin_trial();
  {
    gapbs_amu::LineBatch<FakeBackend> batch(store);
    const size_t first = batch.add(
        reinterpret_cast<const uint32_t *>(source + 4));
    const size_t second = batch.add(
        reinterpret_cast<const uint32_t *>(source + 8));
    batch.issue_all();
    batch.wait_all();
    require(batch.value<uint32_t>(first) == 0x11223344,
            "first coalesced value differs");
    require(batch.value<uint32_t>(second) == 0x55667788,
            "second coalesced value differs");
  }
  require(backend.loads == 1, "same-line values issued multiple loads");
  require(store.counters().coalesced_misses == 1,
          "coalesced miss count differs");

  {
    gapbs_amu::LineBatch<FakeBackend> hit(store);
    const size_t slot = hit.add(
        reinterpret_cast<const uint32_t *>(source + 4));
    hit.issue_all();
    hit.wait_all();
    require(hit.value<uint32_t>(slot) == 0x11223344,
            "cached value differs");
  }
  require(backend.loads == 1, "cache hit issued a request");
  require(store.counters().cache_hits == 1, "cache hit count differs");

  store.begin_trial();
  {
    gapbs_amu::LineBatch<FakeBackend> collision(store);
    const size_t low = collision.add(
        reinterpret_cast<const uint32_t *>(source));
    const size_t high = collision.add(
        reinterpret_cast<const uint32_t *>(source + 128 * 64));
    collision.issue_all();
    collision.wait_all();
    require(collision.value<uint32_t>(low) == 0,
            "colliding low tag staging changed");
    require(collision.value<uint32_t>(high) == 0xddeeff00,
            "colliding high tag staging changed");
  }

  const uint64_t before_reset = backend.loads;
  store.reset_iteration();
  {
    gapbs_amu::LineBatch<FakeBackend> reset(store);
    reset.add(reinterpret_cast<const uint32_t *>(source));
    reset.issue_all();
    reset.wait_all();
  }
  require(backend.loads == before_reset + 1,
          "iteration reset retained a cache hit");

  store.begin_trial();
  backend.reverse = true;
  {
    gapbs_amu::LineBatch<FakeBackend> reversed(store);
    const size_t first = reversed.add(
        reinterpret_cast<const uint32_t *>(source + 4));
    const size_t second = reversed.add(
        reinterpret_cast<const uint32_t *>(source + 64));
    reversed.issue_all();
    reversed.wait_all();
    require(reversed.value<uint32_t>(first) == 0x11223344,
            "reverse completion changed first value");
    require(reversed.value<uint32_t>(second) == 0x99aabbcc,
            "reverse completion changed second value");
  }
  return 0;
}

static void failure_case(const std::string &mode) {
  alignas(64) unsigned char source[129 * 64] = {};
  FakeBackend backend;
  gapbs_amu::LineStore<FakeBackend> store(backend);
  store.begin_trial();
  gapbs_amu::LineBatch<FakeBackend> batch(store);
  if (mode == "unmatched") {
    batch.add(reinterpret_cast<const uint32_t *>(source));
    batch.issue_all();
    backend.completions.push_front(999999);
    batch.wait_all();
  } else if (mode == "capacity") {
    for (size_t line = 0; line <= gapbs_amu::kStagingLinesPerThread; ++line)
      batch.add(reinterpret_cast<const uint32_t *>(source + line * 64));
  } else if (mode == "early-value") {
    const size_t slot = batch.add(
        reinterpret_cast<const uint32_t *>(source));
    (void)batch.value<uint32_t>(slot);
  } else if (mode == "cross-line") {
    batch.add(reinterpret_cast<const uint32_t *>(source + 63));
  } else {
    throw Failure("unknown failure mode");
  }
}

int main(int argc, char **argv) {
  try {
    if (argc == 1)
      return success_cases();
    failure_case(argv[1]);
  } catch (const Failure &) {
    return argc == 1 ? 2 : 0;
  }
  return 3;
}
'''


class AmuLineCacheNativeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = os.environ.get("CXX", "g++")
        if shutil.which(compiler) is None:
            raise unittest.SkipTest(f"C++ compiler not found: {compiler}")
        cls.temporary = tempfile.TemporaryDirectory()
        root = Path(cls.temporary.name)
        source = root / "line_cache_harness.cc"
        cls.binary = root / "line_cache_harness"
        source.write_text(HARNESS, encoding="utf-8")
        flags = os.environ.get("CXXFLAGS", "").split()
        subprocess.run(
            [
                compiler,
                "-std=c++11",
                "-Wall",
                "-Wextra",
                "-Werror",
                *flags,
                "-I",
                str(REPO / "util/amu"),
                str(source),
                "-o",
                str(cls.binary),
            ],
            check=True,
            text=True,
        )

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "temporary"):
            cls.temporary.cleanup()

    def run_mode(self, mode=None):
        command = [str(self.binary)]
        if mode is not None:
            command.append(mode)
        subprocess.run(command, check=True)

    def test_coalescing_cache_collision_reset_and_reverse_completion(self):
        self.run_mode()

    def test_unmatched_completion_fails_closed(self):
        self.run_mode("unmatched")

    def test_capacity_and_early_read_fail_closed(self):
        self.run_mode("capacity")
        self.run_mode("early-value")

    def test_cross_line_value_fails_closed(self):
        self.run_mode("cross-line")


if __name__ == "__main__":
    unittest.main()