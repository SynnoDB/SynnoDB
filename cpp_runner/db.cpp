#include "builder_api.hpp"
#include "loader_api.hpp"
#include "query_api.hpp"
#include "pipeline.hpp"

#include <chrono>
#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <poll.h>
#include <signal.h>
#include <stdexcept>
#include <sys/prctl.h>
#include <unistd.h>

// FILE_VERSION: 3

struct State {
    std::string parquet_path;
    ParquetTables* parquet_tables = nullptr;
    Database* database = nullptr;
    int trace_write_fd = -1;
};

static State state;

static void clear_storage_dir_if_configured() {
    const char* env = std::getenv("STORAGE_DIR");
    if (!env || env[0] == '\0') {
        return;
    }

    std::filesystem::path storage_dir(env);

    // Refuse to delete a directory we didn't create: run.py drops a
    // .bespoke_storage_dir marker into every storage dir it sets up, so a
    // misconfigured STORAGE_DIR pointing at unrelated data is rejected here.
    if (!std::filesystem::exists(storage_dir / ".bespoke_storage_dir")) {
        throw std::runtime_error(
            "Refusing to clear STORAGE_DIR " + storage_dir.string() +
            ": missing .bespoke_storage_dir sentinel");
    }

    std::error_code ec;
    std::filesystem::remove_all(storage_dir, ec);
    if (ec) {
        throw std::runtime_error(
            "Failed to remove STORAGE_DIR " + storage_dir.string() + ": " + ec.message());
    }

    std::filesystem::create_directories(storage_dir, ec);
    if (ec) {
        throw std::runtime_error(
            "Failed to create STORAGE_DIR " + storage_dir.string() + ": " + ec.message());
    }

    // Re-create the sentinel file after clearing the directory.
    std::ofstream(storage_dir / ".bespoke_storage_dir").close();
}

// ---------------------------------------------------------------------------
// JSON string escaping for the c2p response
// ---------------------------------------------------------------------------
static std::string json_escape(const std::string& s) {
    std::string r;
    r.reserve(s.size() + 16);
    for (unsigned char c : s) {
        switch (c) {
            case '"':  r += "\\\""; break;
            case '\\': r += "\\\\"; break;
            case '\n': r += "\\n";  break;
            case '\r': r += "\\r";  break;
            case '\t': r += "\\t";  break;
            default:
                if (c < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", static_cast<unsigned>(c));
                    r += buf;
                } else {
                    r += static_cast<char>(c);
                }
        }
    }
    return r;
}

// Write a length-prefixed byte string to fd.
static void write_length_prefixed(int fd, const std::string& data) {
    uint32_t len = static_cast<uint32_t>(data.size());
    ipc::write_exact(fd, len);
    const char* p = data.data();
    size_t rem = len;
    while (rem > 0) {
        ssize_t w = ::write(fd, p, rem);
        if (w > 0) {
            p   += static_cast<size_t>(w);
            rem -= static_cast<size_t>(w);
        } else if (errno != EINTR) {
            break;
        }
    }
}

// Read a length-prefixed byte string from fd.
static std::string read_length_prefixed(int fd) {
    uint32_t len = 0;
    ipc::read_exact(fd, len);
    if (len == 0) return "";
    std::string result(len, '\0');
    char* p = result.data();
    size_t rem = len;
    while (rem > 0) {
        ssize_t r = ::read(fd, p, rem);
        if (r > 0) {
            p   += static_cast<size_t>(r);
            rem -= static_cast<size_t>(r);
        } else if (r == 0 || errno != EINTR) {
            break;
        }
    }
    return result;
}

struct DoneAndTrace {
    DoneToken token{};
    std::string trace_data;
};

// The query child writes trace_data before the final stage writes DoneToken.
// Drain trace_pipe concurrently with done_pipe; otherwise large traces can fill
// the pipe, blocking the query child before it can report done.
static DoneAndTrace read_done_and_trace(int done_fd, int trace_fd) {
    DoneAndTrace result;
    bool have_done = false;
    bool have_trace_len = false;
    bool have_trace = false;
    uint32_t trace_len = 0;
    std::string trace_buf;

    while (true) {
        struct pollfd fds[2];
        int nfds = 0;
        fds[nfds].fd = done_fd;
        fds[nfds].events = POLLIN;
        nfds++;
        if (trace_fd >= 0 && !have_trace) {
            fds[nfds].fd = trace_fd;
            fds[nfds].events = POLLIN;
            nfds++;
        }

        int rc = poll(fds, nfds, -1);
        if (rc < 0) {
            if (errno == EINTR) {
                continue;
            }
            throw ipc::io_error(std::string("poll done/trace: ") + std::strerror(errno));
        }

        for (int i = 0; i < nfds; ++i) {
            short re = fds[i].revents;
            if (re == 0) {
                continue;
            }
            // POLLERR / POLLNVAL are unrecoverable: throw rather than
            // busy-looping (poll() would keep returning the same flags).
            if (re & (POLLERR | POLLNVAL)) {
                throw ipc::io_error(
                    "read_done_and_trace: poll returned POLLERR/POLLNVAL");
            }

            if (fds[i].fd == done_fd) {
                if (re & POLLIN) {
                    ipc::read_exact(done_fd, result.token);
                    have_done = true;
                } else if (re & POLLHUP) {
                    // Done-pipe writer is gone without a token; no point
                    // waiting any longer.
                    throw ipc::io_error(
                        "read_done_and_trace: done pipe hung up before token");
                }
                continue;
            }

            if (fds[i].fd == trace_fd) {
                if (re & POLLIN) {
                    char buf[8192];
                    ssize_t n = ::read(trace_fd, buf, sizeof(buf));
                    if (n > 0) {
                        trace_buf.append(buf, static_cast<size_t>(n));
                        if (!have_trace_len && trace_buf.size() >= sizeof(uint32_t)) {
                            std::memcpy(&trace_len, trace_buf.data(), sizeof(uint32_t));
                            have_trace_len = true;
                        }
                        if (have_trace_len && trace_buf.size() >= sizeof(uint32_t) + trace_len) {
                            result.trace_data.assign(
                                trace_buf.data() + sizeof(uint32_t),
                                static_cast<size_t>(trace_len));
                            have_trace = true;
                        }
                    } else if (n == 0) {
                        // EOF on trace pipe — no more trace data coming.
                        // Mark trace as complete so we stop polling it.
                        have_trace = true;
                    } else if (errno != EINTR) {
                        throw ipc::io_error(std::string("read trace pipe: ") + std::strerror(errno));
                    }
                } else if (re & POLLHUP) {
                    // All writers of trace_pipe are gone. Stop polling it;
                    // we'll exit once the done token arrives (or has already
                    // arrived).
                    have_trace = true;
                }
            }
        }

        if (have_done && (result.token.term_signal != 0 || trace_fd < 0 || have_trace)) {
            return result;
        }
    }
}


// ---------------------------------------------------------------------------
// Hotpatch / Plugins
// ---------------------------------------------------------------------------

static auto build_pipeline() {
    return make_pipeline(
        // ── Loader stage ────────────────────────────────────────────────────
        // Behaviour differs by storage mode (controlled at code-generation time):
        //
        //   In-memory mode:  api.load() reads all Parquet files via Arrow and
        //     materialises them as Arrow tables in RAM.  The builder stage then
        //     converts those tables into the in-memory Database struct.
        //
        //   Persistent-storage (SSD) mode:  api.load() is a trivial no-op that
        //     only records the per-scale-factor Parquet directory as file-path
        //     strings inside ParquetTables (e.g. tables->lineitem_path).  No
        //     Arrow data is read here.  The builder stage later opens those
        //     paths itself, streams columns row-group by row-group, and writes
        //     them to binary column files on disk.
        //
        // The stage is kept in both modes so that the builder always receives a
        // populated ParquetTables* with the correct file paths, without needing
        // to know the parquet directory itself.
        stage<RunPolicy::OnChange>("./build/libloader.so",
            [](Plugin& plugin) {
                auto api = plugin.get<LoaderApi>();
                std::cerr << "loader start\n";
                state.parquet_tables = api.load(state.parquet_path);
                std::cerr << "loader done\n";
                return 0;
            },
            [](Plugin& plugin) {
                // Destroy old tables with the old plugin BEFORE dlclose so that
                // shared_ptr deleters and Arrow statics in libloader.so are still
                // mapped when the destructor chain runs.
                auto api = plugin.get<LoaderApi>();
                if (state.parquet_tables) {
                    api.destroy(state.parquet_tables);
                    state.parquet_tables = nullptr;
                }
            }),
        // ── Builder stage ────────────────────────────────────────────────────
        // Behaviour differs by storage mode:
        //
        //   In-memory mode:  api.build() converts the Arrow tables produced by
        //     the loader into an optimised in-memory Database struct (column
        //     vectors, CSR indexes, pre-joined columns, etc.).  All data lives
        //     in RAM for the lifetime of the process.
        //
        //   Persistent-storage (SSD) mode:  api.build() opens the Parquet files
        //     via the paths in ParquetTables, serialises each column to a flat
        //     binary file under STORAGE_DIR (set by run.py per scale-factor),
        //     and returns a Database whose fields
        //     are ColumnHandle<T> descriptors backed by a shared BufferPool.
        //     Column pages are loaded from SSD on demand at query time.
        //     The runner clears STORAGE_DIR exactly when this builder stage
        //     reruns, so query-only hotpatches keep the existing files while
        //     storage-layout or loader/builder reloads rebuild them from scratch.
        stage<RunPolicy::OnChange>("./build/libbuilder.so",
            [](Plugin& plugin, int) {
                auto api = plugin.get<BuilderApi>();
                clear_storage_dir_if_configured();
                std::cerr << "builder start\n";
                const auto t0 = std::chrono::steady_clock::now();
                state.database = api.build(state.parquet_tables);
                std::cerr << "builder done\n";
                const auto t1 = std::chrono::steady_clock::now();
                const float ms =
                    std::chrono::duration<float, std::milli>(t1 - t0).count();
                std::cerr << "Ingest ms: " << ms << "\n";
                return 0;
            },
            [](Plugin& plugin) {
                auto api = plugin.get<BuilderApi>();
                if (state.database) {
                    api.destroy(state.database);
                    state.database = nullptr;
                }
            }),
        // The query stage receives batch.query_lines from run_parent via the
        // framed IPC protocol.  Previously the lines were written to stdin
        // before the RUN signal, which could leave stale lines buffered and
        // cause them to be consumed by a subsequent invocation.
        stage<RunPolicy::AlwaysReload>("./build/libquery.so", [](Plugin& plugin, int, const RunBatch& batch) {
            auto api = plugin.get<QueryApi>();
            std::cerr << "query start\n";

            // Catch any C++ exception thrown out of api.query() so the child
            // process can still report a structured response instead of
            // aborting (which would surface only as a SIGABRT to the parent).
            // Note: this does NOT catch async signals like SIGSEGV; those
            // still terminate the child and are reported via term_signal.
            std::vector<QueryResult> results;
            std::string stage_error;
            try {
                results = api.query(state.database, batch.query_lines);
            } catch (const std::exception& e) {
                stage_error = std::string("query stage threw std::exception: ") + e.what();
                std::cerr << stage_error << "\n";
            } catch (...) {
                stage_error = "query stage threw unknown exception";
                std::cerr << stage_error << "\n";
            }
            std::cerr << "query done\n";

            // Serialize per-query results plus any stage-level error as a JSON
            // object and send to run_parent via the trace pipe, before
            // write_done() fires on done_pipe.
            if (state.trace_write_fd >= 0) {
                std::string payload = "{\"query_results\":[";
                for (std::size_t i = 0; i < results.size(); ++i) {
                    if (i > 0) payload += ",";
                    payload += "{\"trace\":\"";
                    payload += json_escape(results[i].trace);
                    payload += "\",\"elapsed_ms\":";
                    payload += std::to_string(results[i].elapsed_ms);
                    payload += ",\"error\":\"";
                    payload += json_escape(results[i].error);
                    payload += "\"}";
                }
                payload += "],\"stage_error\":\"";
                payload += json_escape(stage_error);
                payload += "\"}";
                write_length_prefixed(state.trace_write_fd, payload);
            }
            return 0;
        }));
}

static void run_child(int read_fd, int done_fd) {
    auto pipeline = build_pipeline();
    pipeline.run(read_fd, done_fd, false);
}

static int getenv_fd(const char* name) {
    const char* v = std::getenv(name);
    if (!v) {
        throw std::runtime_error(std::string(name) + " not supplied");
    }
    return std::atoi(v);
}


// Bridges the Python-side IPC (p2c / c2p FDs) and the internal pipeline
// PipelineControl.  Reads framed ControlMessages from P2C_FD, forwards RUN
// commands (including query_lines) to the pipeline, and writes a JSON result
// line containing the echoed batch_id back to C2P_FD so the Python caller can
// verify it received the response for the correct request.
// Note: diagnostic logging here goes to stdout, not stderr — see run.py which
// captures them separately as HotpatchProcRunResult.stdout.
static void run_parent(PipelineControl& control, int trace_r) {
    int in_fd = getenv_fd("P2C_FD");  // read from parent
    int out_fd = getenv_fd("C2P_FD"); // write to parent

    std::ofstream out("/proc/self/fd/" + std::to_string(out_fd));
    if (!out.is_open()) {
        throw std::runtime_error("open C2P_FD failed");
    }

    while (true) {
        ipc::ControlMessage msg = ipc::read_control_message(in_fd);
        std::cout << "got: "
                  << (msg.action == Action::RUN ? "run" : "stop")
                  << " batch_id=" << msg.batch.batch_id
                  << " query_lines=" << msg.batch.query_lines.size()
                  << "\n";

        if (msg.action == Action::TERMINATE) {
            break;
        }

        control.send_run(msg.batch);
        DoneAndTrace done_trace = read_done_and_trace(control.done_fd(), trace_r);
        DoneToken token = done_trace.token;
        std::string trace_data = std::move(done_trace.trace_data);

        // Emit a single JSON line on the c2p channel.  The trace_data payload
        // is already a JSON object {"query_results":[...],"stage_error":"..."}
        // so we splice it in by stripping its outer braces.  When the child
        // was killed by a signal there is no trace_data
        out << "{\"batch_id\":" << msg.batch.batch_id
            << ",\"exit_code\":" << token.exit_code
            << ",\"signal\":"    << token.term_signal;
        if (trace_data.size() >= 2 && trace_data.front() == '{' && trace_data.back() == '}') {
            out << "," << trace_data.substr(1, trace_data.size() - 2);
        } else {
            std::string sig_msg;
            if (token.term_signal != 0) {
                const char* name = strsignal(token.term_signal);
                sig_msg = std::string("query child killed by signal ") +
                          std::to_string(token.term_signal) +
                          " (" + (name ? name : "unknown") + ")";
            }
            out << ",\"query_results\":[],\"stage_error\":\""
                << json_escape(sig_msg) << "\"";
        }
        out << "}\n";
        out.flush();
    }

    control.send_terminate();
}


int main(int argc, char** argv) {
    if (argc < 2) {
        std::cerr << "Usage: " << argv[0] << " <PARQUET_DIR\n";
        return 1;
    }
    std::string base_parquet = argv[1];
    state.parquet_path = base_parquet;

    signal(SIGPIPE, SIG_IGN);

    int p2c[2];
    int done_pipe[2];
    int trace_pipe[2];

    if (pipe(p2c) == -1)        {
        perror("pipe p2c");
        return 1;
    }
    if (pipe(done_pipe) == -1) {
        perror("pipe done_pipe");
        close(p2c[0]);
        close(p2c[1]);
        return 1;
    }
    if (pipe(trace_pipe) == -1) {
        perror("pipe trace_pipe"); 
        close(p2c[0]); 
        close(p2c[1]); 
        close(done_pipe[0]); 
        close(done_pipe[1]); 
        return 1; 
    }

    // The Python preexec_fn already set PR_SET_PDEATHSIG=SIGKILL on this
    // process, but that setting is *cleared* across fork(2) in the child. So
    // the forked child below must re-arm it; the parent keeps its inherited
    // setting and doesn't need to do anything here.
    pid_t pid = fork();
    if (pid == 0) {
        // Re-arm PR_SET_PDEATHSIG so this child is SIGKILL'd if its immediate
        // parent (the C++ run_parent process) dies. Without this re-arming
        // the child would be left running attached to init when the parent
        // exits via an uncaught exception.
        prctl(PR_SET_PDEATHSIG, SIGKILL);
        close(p2c[1]);
        close(done_pipe[0]);
        close(trace_pipe[0]);          // child does not read from trace pipe
        state.trace_write_fd = trace_pipe[1];
        run_child(p2c[0], done_pipe[1]);
        _exit(0);
    }
    if (pid < 0) {
        perror("fork");
        close(p2c[0]);
        close(p2c[1]);
        close(done_pipe[0]);
        close(done_pipe[1]);
        close(trace_pipe[0]);
        close(trace_pipe[1]);
        return 1;
    }

    close(p2c[0]);
    close(done_pipe[1]);
    close(trace_pipe[1]);          // parent does not write to trace pipe
    PipelineControl control(p2c[1], done_pipe[0], true);
    run_parent(control, trace_pipe[0]);
    close(trace_pipe[0]);
    waitpid(pid, nullptr, 0);
    return 0;
}
