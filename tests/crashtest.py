#!/usr/bin/env python3
"""Crash tester: kills Pgit at every point where it changes the disk and
checks that the repository survives.

Three modes, all run by default:

  points  Deterministic crash injection. Pgit runs in a child process whose
          file APIs are wrapped; the child dies (os._exit, like kill -9:
          unflushed buffers are lost) right before its Nth disk change, or
          halfway through its Nth write (a torn write). Every crash point of
          every scenario is tried.

  kill    Real SIGKILLs at random moments while Pgit commits, with no
          instrumentation at all.

  concurrent  Several pgit add processes at once. Each must either land in
          the index or be refused by index.lock; none may be silently lost.

After every crash the repository must satisfy:

  fsck      git fsck --strict reports no corrupt or missing objects
  atomic    HEAD and the index are each either entirely before or entirely
            after the command, never a mix
  readable  pgit status still runs
  recover   after removing stale .lock files (as Git asks you to), rerunning
            the command succeeds and gives the same result as a clean run

This tests process crashes. Power loss also depends on fsync, which Pgit
calls but which can't be exercised without a VM.

Usage:
  python3 tests/crashtest.py
  python3 tests/crashtest.py --mode kill --kills 500
  python3 tests/crashtest.py --mode concurrent
"""

import argparse
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WYAG = os.path.join(ROOT, "wyag")

GIT_ENV = None  # Isolated environment shared by Git and Pgit


# ── Child side: crash injection ─────────────────────────────────────────────

def child_main(crash_at, torn, argv):
    """Run Pgit with its disk-changing calls counted. Die right before call
    number crash_at, or halfway through it if torn. Prints the count."""
    import builtins
    import io

    sys.path.insert(0, ROOT)
    count = [0]

    def point(what, half=None):
        if count[0] == crash_at:
            if torn and half is not None:
                half()
            os._exit(137)
        count[0] += 1

    class Proxy:
        def __init__(self, f):
            self._f = f

        def write(self, data):
            def half():
                self._f.write(data[:len(data) // 2])
                self._f.flush()
            point("write", half)
            return self._f.write(data)

        def __getattr__(self, name):
            return getattr(self._f, name)

        def __iter__(self):
            return iter(self._f)

        def __enter__(self):
            self._f.__enter__()
            return self

        def __exit__(self, *exc):
            return self._f.__exit__(*exc)

    def writing(mode):
        return any(c in mode for c in "wax+")

    real_open = builtins.open
    def open_(file, mode="r", *a, **k):
        if writing(mode) and not isinstance(file, int):
            point("open")
            return Proxy(real_open(file, mode, *a, **k))
        f = real_open(file, mode, *a, **k)
        return Proxy(f) if writing(mode) else f
    builtins.open = open_
    io.open = open_

    real_fdopen = os.fdopen
    def fdopen(fd, mode="r", *a, **k):
        f = real_fdopen(fd, mode, *a, **k)
        return Proxy(f) if writing(mode) else f
    os.fdopen = fdopen

    real_osopen = os.open
    def osopen(path, flags, *a, **k):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            point("os.open")
        return real_osopen(path, flags, *a, **k)
    os.open = osopen

    real_oswrite = os.write
    def oswrite(fd, data):
        def half():
            real_oswrite(fd, data[:len(data) // 2])
        point("os.write", half)
        return real_oswrite(fd, data)
    os.write = oswrite

    for name in ("replace", "rename", "unlink", "remove", "rmdir", "mkdir",
                 "makedirs", "chmod"):
        real = getattr(os, name)
        def wrapped(*a, _real=real, **k):
            point(name)
            return _real(*a, **k)
        setattr(os, name, wrapped)

    import libwyag
    try:
        libwyag.main(argv)
    finally:
        sys.stderr.write(f"\nCRASHPOINTS {count[0]}\n")


# ── Parent side ─────────────────────────────────────────────────────────────

def make_env(base):
    home = os.path.join(base, "home")
    os.makedirs(home)
    cfg = os.path.join(home, ".gitconfig")
    with open(cfg, "w") as f:
        f.write("[user]\n\tname = Crash Tester\n\temail = crash@test.invalid\n"
                "[commit]\n\tgpgsign = false\n")
    return dict(os.environ, HOME=home, XDG_CONFIG_HOME=os.path.join(home, ".config"),
                GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=cfg)


def git(cwd, *args, check=True):
    p = subprocess.run(["git", "--no-optional-locks", *args], cwd=cwd,
                       env=GIT_ENV, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr}")
    return p


def pgit(cwd, *args, check=True):
    p = subprocess.run([sys.executable, WYAG, *args], cwd=cwd, env=GIT_ENV,
                       capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"pgit {' '.join(args)}: {p.stderr}")
    return p


def pgit_crashing(cwd, args, crash_at, torn):
    p = subprocess.run([sys.executable, os.path.abspath(__file__), "--child",
                        str(crash_at), "1" if torn else "0", *args],
                       cwd=cwd, env=GIT_ENV, capture_output=True, text=True)
    return p


def write(root, path, data):
    full = os.path.join(root, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(data)


def snapshot(repo):
    """What a command is allowed to change: HEAD's commit and the index."""
    head = git(repo, "rev-parse", "-q", "--verify", "HEAD", check=False).stdout.strip()
    index = git(repo, "ls-files", "-s", check=False)
    return head, index.stdout if index.returncode == 0 else f"UNREADABLE: {index.stderr.strip()}"


def commit_shape(repo):
    """A commit compared without its timestamp: tree, parents and message."""
    if not git(repo, "rev-parse", "-q", "--verify", "HEAD", check=False).stdout:
        return None
    return git(repo, "log", "-1", "--format=%T %P %s").stdout


# Each scenario: setup(repo) prepares a repo, then args is the command crashed.
def setup_base(repo):
    os.makedirs(repo)
    pgit(repo, "init", ".")
    for i in range(6):
        write(repo, f"src/mod{i}.py", f"print({i})\n" * 50)
    write(repo, "README", "readme\n")
    pgit(repo, "add", "README", *[f"src/mod{i}.py" for i in range(6)])
    pgit(repo, "commit", "-m", "base")


def setup_staged(repo):
    setup_base(repo)
    write(repo, "src/mod0.py", "changed\n")
    write(repo, "docs/a/b/guide.md", "guide\n")
    write(repo, "docs/c.md", "c\n")
    pgit(repo, "add", "src/mod0.py", "docs/a/b/guide.md", "docs/c.md")


def setup_modified(repo):
    setup_base(repo)
    write(repo, "src/mod1.py", "edited\n")
    write(repo, "new/file.txt", "new\n")
    write(repo, "new/other.txt", "other\n")


def setup_unhashed(repo):
    setup_base(repo)
    write(repo, "big.txt", "data\n" * 20000)


SCENARIOS = [
    ("commit", setup_staged, ["commit", "-m", "crash me"]),
    ("add", setup_modified, ["add", "src/mod1.py", "new/file.txt", "new/other.txt"]),
    ("rm", setup_base, ["rm", "src/mod2.py", "src/mod3.py"]),
    ("hash-object", setup_unhashed, ["hash-object", "-w", "-t", "blob", "big.txt"]),
]


class Failure(Exception):
    pass


def check_after_crash(repo, before, after, clean_shape, args):
    fsck = git(repo, "fsck", "--strict", "--no-dangling", check=False)
    if fsck.returncode != 0 or "error" in fsck.stderr or "missing" in fsck.stderr:
        raise Failure("fsck", fsck.stderr.strip().splitlines()[:3])

    head, index = snapshot(repo)
    if head not in (before[0], after[0]) and not (
            args[0] == "commit" and commit_shape(repo) == clean_shape):
        raise Failure("atomic", f"HEAD is {head!r}, neither before nor after")
    if index not in (before[1], after[1]):
        raise Failure("atomic", f"index is neither before nor after:\n{index[:300]}")

    st = pgit(repo, "status", check=False)
    if st.returncode != 0:
        raise Failure("readable", st.stderr.strip().splitlines()[-1:])

    locks = [os.path.join(d, f) for d, _, fs in os.walk(os.path.join(repo, ".git"))
             for f in fs if f.endswith(".lock")]
    for lock in locks:
        os.unlink(lock)
    # A crash after the command's last change leaves it complete; rerunning
    # a commit then would just make a second one.
    if args[0] == "commit" and commit_shape(repo) == clean_shape:
        return len(locks)
    rerun = pgit(repo, *args, check=False)
    if rerun.returncode != 0:
        raise Failure("recover", rerun.stderr.strip().splitlines()[-1:])
    if snapshot(repo)[1] != after[1] or (args[0] == "commit" and commit_shape(repo) != clean_shape):
        raise Failure("recover", "rerun gave a different result than a clean run")
    return len(locks)


def run_points(base, verbose):
    total = failures = stale_locks = 0
    by_check = {}
    for name, setup, args in SCENARIOS:
        template = os.path.join(base, f"template-{name}")
        setup(template)
        before = snapshot(template)

        clean = os.path.join(base, "clean")
        shutil.rmtree(clean, ignore_errors=True)
        shutil.copytree(template, clean, symlinks=True)
        p = pgit_crashing(clean, args, -1, False)
        if p.returncode != 0:
            raise SystemExit(f"{name}: clean run failed:\n{p.stderr}")
        points = int(p.stderr.rsplit("CRASHPOINTS ", 1)[1].split()[0])
        after, clean_shape = snapshot(clean), commit_shape(clean)

        scen_fail = 0
        for crash_at in range(points):
            for torn in (False, True):
                repo = os.path.join(base, "crash")
                shutil.rmtree(repo, ignore_errors=True)
                shutil.copytree(template, repo, symlinks=True)
                pgit_crashing(repo, args, crash_at, torn)
                total += 1
                try:
                    stale_locks += check_after_crash(repo, before, after, clean_shape, args)
                except Failure as e:
                    failures += 1
                    scen_fail += 1
                    by_check[e.args[0]] = by_check.get(e.args[0], 0) + 1
                    if verbose or scen_fail <= 2:
                        print(f"  FAIL {name} crash point {crash_at}{' (torn)' if torn else ''}: "
                              f"[{e.args[0]}] {e.args[1]}")
        print(f"  {name:12} {points:3} crash points x 2 (clean + torn): "
              f"{scen_fail} broken repositories", flush=True)
    return total, failures, stale_locks, by_check


def run_kills(base, kills, seed):
    rng = random.Random(seed)
    repo = os.path.join(base, "kill")
    setup_base(repo)
    # Learn how long a commit takes so kills land inside it.
    start = time.time()
    write(repo, "warmup.txt", "w\n")
    pgit(repo, "add", "warmup.txt")
    pgit(repo, "commit", "-m", "warmup")
    duration = time.time() - start

    failures, landed, by_check = 0, 0, {}
    for n in range(kills):
        for i in range(rng.randint(5, 30)):
            write(repo, f"k/{rng.randrange(40)}/{rng.randrange(40)}.txt", f"{n} {i}\n" * rng.randint(1, 500))
        files = sorted(os.path.relpath(os.path.join(d, f), repo)
                       for d, _, fs in os.walk(os.path.join(repo, "k")) for f in fs)
        before = snapshot(repo)
        delay = rng.uniform(0, duration)
        p = subprocess.Popen(f'"{sys.executable}" "{WYAG}" add {" ".join(files)} && '
                             f'"{sys.executable}" "{WYAG}" commit -m "kill {n}"',
                             shell=True, cwd=repo, env=GIT_ENV, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(delay)
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGKILL)
            landed += 1
        p.wait()
        try:
            fsck = git(repo, "fsck", "--strict", "--no-dangling", check=False)
            if fsck.returncode != 0 or "error" in fsck.stderr:
                raise Failure("fsck", fsck.stderr.strip().splitlines()[:2])
            if pgit(repo, "status", check=False).returncode != 0:
                raise Failure("readable", "pgit status fails")
            for d, _, fs in os.walk(os.path.join(repo, ".git")):
                for f in fs:
                    if f.endswith(".lock"):
                        os.unlink(os.path.join(d, f))
        except Failure as e:
            failures += 1
            by_check[e.args[0]] = by_check.get(e.args[0], 0) + 1
            if failures <= 3:
                print(f"  FAIL kill {n} after {delay * 1000:.0f} ms: [{e.args[0]}] {e.args[1]}")
            shutil.rmtree(repo)
            setup_base(repo)
        if (n + 1) % 100 == 0:
            print(f"  {n + 1}/{kills} kills", flush=True)
    return kills, landed, failures, by_check


def run_concurrent(base, rounds, width):
    """Start several pgit add processes at once, each staging its own file.
    Each must either end up in the index or fail on the lock: an add that
    reports success but whose file is missing is a lost update, and any
    other failure (such as reading another process's half-written index)
    is an error."""
    repo = os.path.join(base, "concurrent")
    setup_base(repo)
    lost = refused = landed = errors = unreadable = 0
    for n in range(rounds):
        names = [f"c/{n}-{i}.txt" for i in range(width)]
        for name in names:
            write(repo, name, name + "\n")
        procs = [subprocess.Popen([sys.executable, WYAG, "add", name], cwd=repo, env=GIT_ENV,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                 for name in names]
        results = [(p.wait(), p.stderr.read()) for p in procs]
        ls = git(repo, "ls-files", check=False)
        if ls.returncode != 0: # The index itself was corrupted
            unreadable += 1
            shutil.rmtree(repo)
            setup_base(repo)
            continue
        staged = set(ls.stdout.split())
        for name, (rc, err) in zip(names, results):
            if rc == 0:
                if name in staged:
                    landed += 1
                else:
                    lost += 1
            elif "index.lock" in err:
                refused += 1
            else:
                errors += 1
    return landed, refused, lost, errors, unreadable


def main():
    global GIT_ENV
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        GIT_ENV = os.environ
        return child_main(int(sys.argv[2]), sys.argv[3] == "1", sys.argv[4:])

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--mode", choices=["points", "kill", "concurrent", "all"], default="all")
    ap.add_argument("--kills", type=int, default=200)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    base = tempfile.mkdtemp(prefix="pgit-crashtest-")
    GIT_ENV = make_env(base)
    broken = 0
    try:
        if args.mode in ("points", "all"):
            print("Crash injection at every disk change:")
            total, failures, locks, by_check = run_points(base, args.verbose)
            print(f"  {total} crashes, {failures} broken repositories {by_check or ''}, "
                  f"{locks} stale locks cleared\n")
            broken += failures
        if args.mode in ("kill", "all"):
            print(f"Random SIGKILL during add + commit (seed {args.seed}):")
            kills, landed, failures, by_check = run_kills(base, args.kills, args.seed)
            print(f"  {kills} runs, {landed} killed mid-command, "
                  f"{failures} broken repositories {by_check or ''}")
            broken += failures
        if args.mode in ("concurrent", "all"):
            print("Concurrent pgit add (8 processes x 25 rounds):")
            landed, refused, lost, errors, unreadable = run_concurrent(base, 25, 8)
            print(f"  {landed} staged, {refused} refused by index.lock, "
                  f"{lost} lost updates, {errors} other errors, "
                  f"{unreadable} rounds left the index unreadable")
            broken += lost + errors + unreadable
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
