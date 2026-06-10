import os
import subprocess


def run_perf_record_db():
    # Pipes:
    # - control pipe: app writes -> perf reads
    # - ack pipe: perf writes -> app reads
    ctl_r, ctl_w = os.pipe()
    ack_r, ack_w = os.pipe()

    # Make fds inheritable across exec (Python makes them non-inheritable by default)
    for fd in (ctl_r, ctl_w, ack_r, ack_w):
        os.set_inheritable(fd, True)

    env = os.environ.copy()
    env["PERF_CTL_FD"] = str(ctl_w)  # db writes commands here
    env["PERF_ACK_FD"] = str(ack_r)  # db reads ack here (optional)

    cmd = [
        "perf",
        "record",
        "--delay=-1",
        f"--control=fd:{ctl_r},{ack_w}",
        "-g",
        "--call-graph=dwarf",
        "-F",
        "999",
        "--",
        "./perf_test",
    ]

    # Pass all fds into perf so they are available and inherited by ./db
    p = subprocess.Popen(
        cmd,
        env=env,
        pass_fds=(ctl_r, ctl_w, ack_r, ack_w),
    )

    # Parent should close its copies to avoid keeping pipes open forever
    os.close(ctl_r)
    os.close(ctl_w)
    os.close(ack_r)
    os.close(ack_w)

    return p


if __name__ == "__main__":
    p = run_perf_record_db()
    rc = p.wait()
    print("perf exit code:", rc)
