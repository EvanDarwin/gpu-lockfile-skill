#!/usr/bin/env python3
"""Take turns on the shared GPU, one invocation at a time, so agents on this machine never benchmark on top of
each other.

You wrap the command you want to run: `gpu_lock.py run "why" -- <cmd>`. That reserves a place in line for THIS
invocation, waits until it is this invocation's turn, runs the command, and releases when it exits. The
reservation's pid is the run process itself — alive for exactly the wait-and-run — so nothing is held between
commands and nothing is ever freed on a timer: a bench that runs longer than its eta keeps the GPU until it is
actually done, and one that crashes frees the GPU the instant its pid is gone.

Background several of these and they resolve on their own: each waits its turn, runs, and exits, serialized by
arrival order. Not always the order you would pick, but they never contend.

The board is a directory of tiny JSON files (/tmp/agent-gpu-lock.d on POSIX, %TEMP%\\agent-gpu-lock.d on Windows,
or $AGENT_GPU_LOCK plus ".d"), one per live invocation. There is nothing to lock — each invocation writes and
removes only its own file, and whose turn it is is derived, fair FIFO:

  - exclusive (default): runs only when the board is otherwise clear; nobody behind it barges ahead.
  - shared (--shared): may run alongside other shared invocations, never alongside an exclusive one.

    gpu_lock.py run "why" [--eta=SEC] [--shared] -- <cmd...>   # wait our turn, run <cmd>, release (background it)
    gpu_lock.py status                                          # who's on the GPU and who's waiting, with tickets
    gpu_lock.py release <ticket|pid>                            # stop/cancel one invocation (rarely needed)
    gpu_lock.py --help                                          # this text

Exit codes: run exits with the command's own status; a usage error is 1.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime

# the board's home: /tmp on POSIX (one board for every agent on the machine), the user's %TEMP% on Windows
BASE = os.environ.get("AGENT_GPU_LOCK") or (
    os.path.join(tempfile.gettempdir(), "agent-gpu-lock") if os.name == "nt" else "/tmp/agent-gpu-lock"
)
DIR = BASE + ".d"
HOST = socket.gethostname()
POLL = float(os.environ.get("AGENT_GPU_LOCK_POLL", "2"))


def _alive(pid: int, host: str) -> bool:
    """whether an invocation is still running — the whole staleness test. A reservation from another host (never
    expected on a local board) is left be, since its pid cannot be checked here."""
    if host != HOST:
        return True
    if pid <= 0:
        return False
    if os.name == "nt":
        return _alive_nt(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _alive_nt(pid: int) -> bool:
    """Windows has no signal 0: os.kill(pid, 0) there is TerminateProcess. Ask the process for its exit code
    instead; STILL_ACTIVE (259) means running, and one we may not open exists."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return k32.GetLastError() == 5  # ERROR_ACCESS_DENIED: another user's, but there
    try:
        code = wintypes.DWORD()
        return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259
    finally:
        k32.CloseHandle(h)


def _path(ticket: str) -> str:
    return os.path.join(DIR, ticket + ".json")


def _rm(path: str | None) -> None:
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def _write(entry: dict) -> None:
    """write a reservation atomically (temp file + rename), so a reader ever sees it whole or not at all"""
    os.makedirs(DIR, exist_ok=True)
    clean = {k: v for k, v in entry.items() if not k.startswith("_")}
    tmp = os.path.join(DIR, f".tmp.{os.getpid()}.{os.urandom(4).hex()}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2)
        f.write("\n")
    os.replace(tmp, _path(entry["ticket"]))


def _load(reap: bool = False) -> list[dict]:
    """every live reservation, in FIFO order. One whose invocation pid is gone is dead — dropped here, and
    deleted when `reap` is set (which readers do to keep the board tidy). Nothing is dropped for age or eta."""
    try:
        names = os.listdir(DIR)
    except FileNotFoundError:
        return []
    live: list[dict] = []
    for n in names:
        if not n.endswith(".json") or n.startswith(".tmp."):
            continue
        p = os.path.join(DIR, n)
        try:
            with open(p, encoding="utf-8") as f:
                e = json.load(f)
        except (FileNotFoundError, ValueError, OSError):
            continue
        if not _alive(int(e.get("pid", -1)), str(e.get("host", ""))):
            if reap:
                _rm(p)
            continue
        e["_path"] = p
        live.append(e)
    live.sort(key=lambda e: (e.get("created_ns", 0), e.get("ticket", "")))
    return live


def _active(live: list[dict]) -> list[dict]:
    """the invocations whose turn it is now: the front of the line, sharing among shared ones and stopping at the
    first exclusive (which then runs alone)"""
    active: list[dict] = []
    for e in live:
        if not active:
            active.append(e)
            if e.get("exclusive", True):
                break
        elif e.get("exclusive", True):
            break
        else:
            active.append(e)
    return active


def _remaining(e: dict) -> float | None:
    """seconds an invocation is expected to still take (from its eta hint): the whole eta while it waits, the
    balance once running. None when no eta was given."""
    eta = e.get("eta")
    if not eta:
        return None
    started = e.get("started_epoch")
    return float(eta) if started is None else max(0.0, eta - (time.time() - started))


def _est_wait(live: list[dict], e: dict) -> tuple[float | None, bool]:
    """a rough seconds-until-your-turn for a waiting invocation: the work ahead that actually blocks it (all of it
    for an exclusive, only the exclusive entries for a shared one). Second value flags an unknown eta ahead."""
    idx = [x["ticket"] for x in live].index(e["ticket"])
    excl = e.get("exclusive", True)
    total, unknown = 0.0, False
    for x in live[:idx]:
        if not excl and not x.get("exclusive", True):
            continue
        r = _remaining(x)
        if r is None:
            unknown = True
        else:
            total += r
    return total, unknown


def _fmt(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m}m{sec:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _describe(e: dict) -> str:
    kind = "excl " if e.get("exclusive", True) else "shared"
    return f"[{kind}] {e.get('purpose')!r} (pid {e.get('pid')}, eta {_fmt(e.get('eta'))})"


def _position(live: list[dict], ticket: str) -> tuple[int, int]:
    active = {a["ticket"] for a in _active(live)}
    waiting = [x["ticket"] for x in live if x["ticket"] not in active]
    return (waiting.index(ticket) + 1, len(waiting)) if ticket in waiting else (0, len(waiting))


def run(purpose: str, exclusive: bool, eta: int | None, cmd: list[str]) -> int:
    """reserve for THIS invocation, wait until it is our turn, run `cmd`, release on exit"""
    if not cmd:
        print("run needs a command after --", file=sys.stderr)
        return 1
    ticket = f"{time.time_ns()}-{os.getpid()}-{os.urandom(3).hex()}"
    entry = {
        "ticket": ticket, "pid": os.getpid(), "host": HOST, "purpose": purpose, "exclusive": bool(exclusive),
        "eta": eta, "created": datetime.now().astimezone().isoformat(timespec="seconds"), "created_ns": time.time_ns(),
        "started_epoch": None,
    }
    _write(entry)
    child: subprocess.Popen | None = None

    def _signal(signum: int, _frame: object) -> None:
        if child is not None and child.poll() is None:
            # our turn is underway: pass it to the command (Windows cannot forward a signal; it terminates)
            if os.name == "nt":
                child.terminate()
            else:
                child.send_signal(signum)
        else:
            raise SystemExit(130)  # still waiting in line: stand down, the finally releases us

    signal.signal(signal.SIGINT, _signal)
    signal.signal(signal.SIGTERM, _signal)
    try:
        announced = False
        while ticket not in {a["ticket"] for a in _active(_load(reap=True))}:
            if not announced:
                pos, n = _position(_load(), ticket)
                est, unknown = _est_wait(_load(), entry)
                print(f"queued {ticket}: position {pos} of {n}, est ~{_fmt(est)}{'?' if unknown else ''}", flush=True)
                announced = True
            time.sleep(POLL)
        entry["started_epoch"] = time.time()
        _write(entry)
        print(f"GPU is ours — running {_describe(entry)}", flush=True)
        child = subprocess.Popen(cmd)
        entry["child_pid"] = child.pid  # so a release can stop the command itself, not only this wrapper
        _write(entry)
        return child.wait()
    finally:
        if child is not None and child.poll() is None:
            child.kill()  # never leave the command running once we have given up our slot
        _rm(_path(ticket))
        print(f"released {ticket}", flush=True)


def status() -> int:
    """print who is on the GPU and who is waiting; always 0 so it is safe to run anywhere"""
    live = _load(reap=True)
    if not live:
        print("GPU board empty — free")
        return 0
    active = _active(live)
    on = {a["ticket"] for a in active}
    print("ON THE GPU:")
    for e in active:
        used = _fmt(time.time() - e["started_epoch"]) if e.get("started_epoch") else "starting"
        print(f"  {_describe(e)} — running {used}, ~{_fmt(_remaining(e))} left")
        print(f"      ticket {e['ticket']}")
    waiting = [e for e in live if e["ticket"] not in on]
    if waiting:
        print("WAITING (next first):")
        for i, e in enumerate(waiting, 1):
            est, unknown = _est_wait(live, e)
            print(f"  {i}. {_describe(e)} — est wait ~{_fmt(est)}{'?' if unknown else ''}")
            print(f"      ticket {e['ticket']}")
    return 0


def release(ticket: str) -> int:
    """stop or cancel one invocation, named by its ticket, its pid, or any fragment of the ticket that picks out
    exactly one: signal its process and remove its file. Rarely needed — a run releases itself on exit, and a
    crashed one is reclaimed the moment its pid is gone."""
    live = _load()
    hits = [x for x in live if x["ticket"] == ticket] or [
        x for x in live if ticket in x["ticket"] or str(x.get("pid")) == ticket
    ]
    if len(hits) > 1:
        print(f"{ticket!r} names {len(hits)} reservations; give the whole ticket:", file=sys.stderr)
        for x in hits:
            print(f"  {x['ticket']}  {_describe(x)}", file=sys.stderr)
        return 1
    e = hits[0] if hits else None
    if e is None:
        _rm(_path(ticket))
        print(f"{ticket} not on the board")
        return 0
    ticket = e["ticket"]
    host = str(e.get("host", ""))
    # the wrapper first (on POSIX its handler stops the command and it removes its own file), then the command
    # itself, for Windows, where termination is abrupt and would orphan it on the GPU
    for pid in (int(e.get("pid", -1)), int(e.get("child_pid") or -1)):
        if pid > 0 and _alive(pid, host) and pid not in (os.getpid(), os.getppid()):
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
    _rm(e.get("_path"))
    print(f"released {ticket} ({_describe(e)})")
    return 0


def _opt(rest: list[str], name: str) -> str | None:
    for a in rest:
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 1
    cmd, rest = argv[0], argv[1:]
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if cmd == "status":
        return status()
    if cmd == "run":
        if "--" not in rest:
            print('run needs: run "why" [--eta=SEC] [--shared] -- <command> ...', file=sys.stderr)
            return 1
        sep = rest.index("--")
        head, tail = rest[:sep], rest[sep + 1 :]
        args = [a for a in head if not a.startswith("--")]
        if not args:
            print("run needs a purpose before --", file=sys.stderr)
            return 1
        eta = _opt(head, "--eta")
        return run(args[0], "--shared" not in head, int(eta) if eta else None, tail)
    if cmd == "release":
        args = [a for a in rest if not a.startswith("--")]
        if not args:
            print("release needs a ticket", file=sys.stderr)
            return 1
        return release(args[0])
    print(f"unknown command {cmd!r}; try status | run | release, or --help", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
