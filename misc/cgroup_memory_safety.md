# cgroup memory safety for engine generation

The engine-generation loop runs a long-lived `./db` process per runner. A single run can
grow to hundreds of GB (see the SF50 incident: a builder process ratcheted to 480 GB RSS
on a 755 GB host and poisoned every other tenant). Two nested cgroup v2 ceilings bound
this:

1. **Per-runner ceiling** - each runner's `./db` and its whole stage tree run in a
   dedicated cgroup with `memory.max`. A breach is OOM-killed as a group
   (`memory.oom.group=1`), so the failure is a clean whole-engine death, not a half-dead
   pipeline.
2. **Aggregate ceiling** - those per-runner cgroups nest under a shared parent slice that
   itself carries a host-wide `memory.max`. The kernel enforces the *sum* across every
   runner and every orchestrator under the slice. The per-runner cap is per-run fairness
   and early-fail; the shared slice is the host-safety guarantee.

`RLIMIT_AS` (virtual address space) is kept as a cheap fast-fail, but it caps VA, not RSS,
so the cgroup ceilings are the authoritative ones.

The implementation lives in [`cgroup.py`](../src/synnodb/cpp_runner/hotpatch/cgroup.py)
(parent discovery, `RunnerCgroup`), [`db_launch.cpp`](../src/synnodb/cpp_runner/hotpatch/db_launch.cpp)
(the exec-in-place launcher that joins the cgroup), and the launch wiring in
[`hotpatch_proc.py`](../src/synnodb/cpp_runner/hotpatch/hotpatch_proc.py).

## Configuration

All knobs are environment variables; the feature is **opt-in** and off by default.

| Variable | Meaning |
| --- | --- |
| `SYNNO_ENABLE_CGROUP` | Enable the cgroup launch path. Off by default; the run uses `RLIMIT_AS` only. |
| `SYNNO_REQUIRE_CGROUP` | Fail closed: if cgroup delegation is unavailable, refuse to launch rather than silently dropping to `RLIMIT_AS`. Set this in production. |
| `SYNNO_CGROUP_PARENT` | Path (absolute, or relative to `/sys/fs/cgroup`) of the shared parent slice under which runner cgroups nest. When set, it is the **only** acceptable parent. |
| `SYNNO_CGROUP_PARENT_MAX` | Aggregate budget written to the shared parent's `memory.max` (bytes, or a `K`/`M`/`G`/`T` suffix), if the operator has not set one on the slice. |

The per-runner cap itself comes from the run's memory limit (`--memory-limit-mb`, else
~0.9 of physical RAM); see `run.py`.

Booleans are parsed strictly: `0`, `false`, `no`, `off`, empty, and unset are false;
anything else is true.

### Parent selection

* **`SYNNO_CGROUP_PARENT` set** - runner cgroups are created directly under that slice, so
  the kernel enforces one aggregate `memory.max` across all orchestrators that use it. The
  slice must already distribute `memory` to its children and hold **no processes of its
  own** (each orchestrator runs in its own leaf under the slice, not in the slice itself).
  If the configured slice is missing, unbounded, or holds processes, launch **fails closed**
  - it never silently falls back to a per-orchestrator cgroup, which would drop the
  aggregate guarantee.
* **`SYNNO_CGROUP_PARENT` unset** - runner cgroups nest under the orchestrator's own
  delegated cgroup (the leader pattern). This still gives per-runner caps, but the
  aggregate is only enforced if that cgroup happens to sit under a memory-capped ancestor
  (see the systemd model below).

## Single-victim vs kill-all

By default an aggregate breach kills **one** runner:

* Each per-runner **child** cgroup sets `memory.oom.group=1`.
* The shared **parent** slice does **not**.

So when the slice's `memory.max` is breached, the kernel picks a victim by `oom_score`,
and the victim's nearest `oom.group=1` ancestor is its own child runner cgroup - exactly
that runner's loader/builder/query tree dies as a unit; the other runners survive.

Kill-all (every runner dies on any aggregate breach) is an explicit, deliberate opt-in:
set `memory.oom.group=1` on the slice yourself (systemd `OOMPolicy`/manual). The code never
does this for you, and logs a warning if it detects the slice already has it set.

## Production setup (systemd)

Run the orchestrators under a dedicated slice that carries the host budget. The memory
controller is hierarchical, so the slice's `MemoryMax=` bounds every descendant, whether
runners nest directly under the slice or under per-orchestrator service subtrees.

`/etc/systemd/system/synnodb.slice`:

```ini
[Unit]
Description=SynnoDB engine-generation slice

[Slice]
MemoryAccounting=yes
MemoryMax=500G          # host-wide aggregate budget (~0.7 of RAM)
```

Run each orchestrator as a service under the slice, with its own delegated subtree:

```ini
[Service]
Slice=synnodb.slice
Delegate=yes            # delegate this unit's cgroup subtree to the service user
Environment=SYNNO_ENABLE_CGROUP=1
Environment=SYNNO_REQUIRE_CGROUP=1
# Either let runners nest under this unit's own delegated cgroup (leave
# SYNNO_CGROUP_PARENT unset) - the slice MemoryMax above still enforces the aggregate -
# or point every orchestrator at one shared sub-cgroup of the slice:
#   Environment=SYNNO_CGROUP_PARENT=/sys/fs/cgroup/synnodb.slice/runners
ExecStart=...
```

Two equivalent ways to get the aggregate:

* **Per-orchestrator subtrees (recommended for isolation).** Leave `SYNNO_CGROUP_PARENT`
  unset. Each orchestrator's runners nest under `synnodb.slice/<unit>.service/...` via the
  delegated leader pattern. The slice's `MemoryMax` enforces the aggregate hierarchically,
  and orchestrators cannot touch each other's runner cgroups.
* **One shared runner parent.** Create `synnodb.slice/runners`, grant the orchestrators
  write access to it, and set `SYNNO_CGROUP_PARENT=/sys/fs/cgroup/synnodb.slice/runners`.
  All runners are siblings directly under one parent. Simpler to reason about as a single
  pool, at the cost of cross-orchestrator visibility into the shared parent.

## Dev / non-systemd setup (chowned cgroup)

Without systemd-managed slices you can hand a subtree to an unprivileged user directly.
This is also exactly what the test suite uses to verify the privileged paths:

```bash
# As root: create the slice, give it a budget, and delegate it to the run user.
mkdir /sys/fs/cgroup/synnodb.slice
echo 500G > /sys/fs/cgroup/synnodb.slice/memory.max
chown -R synno:synno /sys/fs/cgroup/synnodb.slice

# The run user then exports:
export SYNNO_ENABLE_CGROUP=1 SYNNO_REQUIRE_CGROUP=1
export SYNNO_CGROUP_PARENT=/sys/fs/cgroup/synnodb.slice
```

The parent's own parent must already distribute `memory` (`+memory` in its
`cgroup.subtree_control`); on most hosts the cgroup root already does.

## Verifying

This repo's box runs under `system.slice` with no delegation, so the privileged tests
(`tests/test_cgroup_aggregate.py`, `tests/test_hotpatch_cgroup.py`) skip there. To run them,
synthesize a delegated cgroup the way an admin would - create it as root, chown the subtree
to the unprivileged uid, move a root shell in (root can cross the unowned ancestor), then
drop privileges with `setpriv` and run pytest from inside it:

```bash
CG=/sys/fs/cgroup/synno-verify
sudo mkdir -p "$CG" && sudo chown -R "$(id -u):$(id -g)" "$CG"
sudo bash -c "echo \$\$ > '$CG/cgroup.procs'; \
  exec setpriv --reuid $(id -u) --regid $(id -g) --clear-groups -- \
    .venv/bin/python -m pytest tests/test_cgroup_aggregate.py -q"
# cleanup: echo 1 > "$CG/cgroup.kill"; rmdir "$CG"/* "$CG"
```

The key invariant test, `test_shared_parent_aggregate_kills_single_victim`, places two
runners under a small shared-parent budget such that each fits its own cap but their sum
exceeds the parent, and asserts the kernel OOM-kills exactly one runner tree while the
other survives.
