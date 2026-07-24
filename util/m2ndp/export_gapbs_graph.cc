// Copyright (c) 2026
// SPDX-License-Identifier: BSD-3-Clause

#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>

#include <sys/stat.h>
#include <sys/types.h>

#include "graph.h"
#include "reader.h"

namespace {

[[noreturn]] void Fail(const std::string &message) {
  throw std::runtime_error(message);
}

std::string Join(const std::string &root, const std::string &name) {
  return root + "/" + name;
}

class AtomicBinaryWriter {
 public:
  explicit AtomicBinaryWriter(const std::string &path)
      : path_(path), temporary_(path + ".tmp"),
        stream_(temporary_, std::ios::binary | std::ios::trunc) {
    if (!stream_)
      Fail("cannot open " + temporary_);
  }

  template <typename T>
  void Write(T value) {
    stream_.write(reinterpret_cast<const char *>(&value), sizeof(value));
    if (!stream_)
      Fail("write failed for " + temporary_);
  }

  void Commit() {
    stream_.flush();
    if (!stream_)
      Fail("flush failed for " + temporary_);
    stream_.close();
    if (!stream_)
      Fail("close failed for " + temporary_);
    if (std::rename(temporary_.c_str(), path_.c_str()) != 0)
      Fail("rename failed for " + path_ + ": " + std::strerror(errno));
    committed_ = true;
  }

  ~AtomicBinaryWriter() {
    if (!committed_) {
      stream_.close();
      std::remove(temporary_.c_str());
    }
  }

 private:
  std::string path_;
  std::string temporary_;
  std::ofstream stream_;
  bool committed_ = false;
};

void EnsureDirectory(const std::string &path) {
  if (mkdir(path.c_str(), 0755) != 0 && errno != EEXIST)
    Fail("cannot create output directory " + path + ": " +
         std::strerror(errno));
  struct stat status {};
  if (stat(path.c_str(), &status) != 0 || !S_ISDIR(status.st_mode))
    Fail("output path is not a directory: " + path);
}

}  // namespace

int main(int argc, char **argv) {
  if (argc != 3) {
    std::cerr << "usage: export_gapbs_graph GRAPH.sg OUTPUT_DIR\n";
    return 64;
  }
  try {
    const std::string graph_path = argv[1];
    const std::string output_dir = argv[2];
    EnsureDirectory(output_dir);

    using SerializedGraph = CSRGraph<SGID>;
    SerializedGraph graph =
        Reader<SGID>(graph_path).ReadSerializedGraph();
    if (graph.num_nodes() < 0 || graph.num_edges_directed() < 0)
      Fail("graph reports a negative size");

    AtomicBinaryWriter offsets(Join(output_dir, "in_offsets.u64"));
    AtomicBinaryWriter neighbors(Join(output_dir, "in_neighbors.i32"));
    AtomicBinaryWriter degrees(Join(output_dir, "out_degree.u32"));
    uint64_t offset = 0;
    offsets.Write<uint64_t>(offset);
    for (SGID vertex = 0; vertex < graph.num_nodes(); ++vertex) {
      for (SGID neighbor : graph.in_neigh(vertex)) {
        if (neighbor < 0 || neighbor >= graph.num_nodes())
          Fail("neighbor outside vertex range");
        neighbors.Write<int32_t>(neighbor);
        ++offset;
      }
      offsets.Write<uint64_t>(offset);
      const int64_t degree = graph.out_degree(vertex);
      if (degree < 0 ||
          static_cast<uint64_t>(degree) >
              std::numeric_limits<uint32_t>::max())
        Fail("out degree exceeds uint32");
      degrees.Write<uint32_t>(static_cast<uint32_t>(degree));
    }
    if (offset != static_cast<uint64_t>(graph.num_edges_directed()))
      Fail("directed edge count mismatch");

    neighbors.Commit();
    offsets.Commit();
    degrees.Commit();
    std::cout << "M2NDP_GRAPH_EXPORT nodes=" << graph.num_nodes()
              << " directed_edges=" << graph.num_edges_directed()
              << " directed=" << (graph.directed() ? 1 : 0) << "\n";
    return 0;
  } catch (const std::exception &error) {
    std::cerr << "export_gapbs_graph: " << error.what() << "\n";
    return 1;
  }
}
