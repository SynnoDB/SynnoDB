#include "../utils/pipeline.hpp"
#include "iface_ingest.hpp"
#include "iface_process.hpp"
#include "iface_query.hpp"

#include <iostream>
#include <signal.h>

static auto build_pipeline() {
    return make_pipeline(
        stage<RunPolicy::OnChange>("./build/libmylib.so", [](Plugin& plugin) {
            auto api = plugin.get<IngestApi>();
            std::cerr << "running ingest\n";
            return api.ingest(1);
        }),
        stage<RunPolicy::OnChange>("./build/libmylib2.so", [](Plugin& plugin, int value) {
            auto api = plugin.get<ProcessApi>();
            std::cerr << "running process\n";
            return api.process(value);
        }),
        stage<RunPolicy::Always>("./build/libmylib3.so", [](Plugin& plugin, int value) {
            auto api = plugin.get<QueryApi>();
            std::cerr << "running query\n";
            api.query(value);
            return 0;
        }));
}

static void run_child(int read_fd, int done_fd) {
    auto pipeline = build_pipeline();
    pipeline.run(read_fd, done_fd, false);
}

static void run_parent(PipelineControl& control) {
    std::string line;
    while (std::getline(std::cin, line)) {
        // if (line.empty()) {
        //     break;
        // }
        std::cerr << "Got input: " << line << "\n";
        try {
            control.send_run(RunBatch{.query_lines = {line}});
            DoneToken token = control.read_done();
            std::cerr << "exit_code: " << token.exit_code << " signal: " << token.term_signal
                      << "\n";
        } catch (const std::exception& ex) {
            std::cerr << "pipeline error: " << ex.what() << "\n";
        }
    }

    control.send_terminate();
}

int main() {
    signal(SIGPIPE, SIG_IGN);
    int p2c[2];
    int done_pipe[2];
    if (pipe(p2c) == -1) {
        perror("pipe");
        return 1;
    }
    if (pipe(done_pipe) == -1) {
        perror("pipe");
        close(p2c[0]);
        close(p2c[1]);
        return 1;
    }

    pid_t pid = fork();
    if (pid == 0) {
        close(p2c[1]);
        close(done_pipe[0]);
        run_child(p2c[0], done_pipe[1]);
        _exit(0);
    }
    if (pid < 0) {
        perror("fork");
        close(p2c[0]);
        close(p2c[1]);
        close(done_pipe[0]);
        close(done_pipe[1]);
        return 1;
    }

    close(p2c[0]);
    close(done_pipe[1]);
    PipelineControl control(p2c[1], done_pipe[0], true);
    run_parent(control);
    waitpid(pid, nullptr, 0);
    return 0;
}
