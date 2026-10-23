#!/usr/bin/env python3
"""Differential tester: runs random operation sequences through Pgit and real
Git side by side and checks that they never disagree.

Operations include the edge cases that broke Pgit before: rewriting a file
right after staging it (same size, same timestamp), and replacing a file
with a directory.

Each sequence builds two repositories from the same random operations, one
with Pgit and one with real Git. After every operation the harness compares:

  worktree   the files and directories each tool leaves on disk
  index      git ls-files -s on Pgit's index vs on Git's index
  status     Pgit's status vs git status, in both repos (so each tool reads
             the other's index)
  ignore     pgit check-ignore vs git check-ignore --no-index
  commit     commit SHAs (Pgit's commit is replayed in Git with the same
             author and timestamp, so the bytes must match exactly)
  fsck       git fsck --strict on Pgit's repository
  read-back  Pgit's ls-tree / cat-file / log on Git's repository vs Git's own

At the end of each sequence a canary is planted: a copy of Pgit's repository
gets one extra change that Git's repository doesn't have, and the checks must
report it. If they don't, the checker is blind and the run fails.

On a divergence the operation list is shrunk to a minimal reproduction and
saved as JSON, which can be replayed with --replay.

Usage:
  python3 tests/difftest.py                    # 50 sequences, random seed
  python3 tests/difftest.py --runs 500 --seed 1
  python3 tests/difftest.py --replay failure.json
"""

import argparse
import contextlib
import io
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import libwyag  # noqa: E402


# ── Random operation generation ─────────────────────────────────────────────

# Names chosen to stress tree sorting ("a" vs "a.b" vs "a-b" vs "a/"),
# ignore rules, non-ASCII paths and spaces.
DIRS = ["", "a", "a/b", "a-b", "build", "src", "doc/x", "sp ace"]
NAMES = ["f", "f.txt", "a", "a.b", "b.log", "keep.log", "x.tmp", "top.txt",
         "é.md", "z.o", "README", "build"]  # a file named like "build/"
CONTENTS = [b"", b"hello\n", b"hello", b"second line\nmore\n",
            b"\x00\x01binary\xff", b"x" * 300, "ünïcode\n".encode()]
# Commit timestamps are written in local time, so vary it: whole, half- and
# three-quarter-hour offsets on both sides of UTC.
TIMEZONES = ["UTC", "Asia/Kolkata", "America/New_York", "America/St_Johns",
             "Pacific/Chatham", "Europe/Berlin"]
IGNORE_LINES = ["*.log", "!keep.log", "build/", "/top.txt", "*.tmp",
                "a/*.o", "doc/**/*.md", "# comment", "*.o", "f"]


def rand_path(rng):
    d = rng.choice(DIRS)
    return os.path.join(d, rng.choice(NAMES)) if d else rng.choice(NAMES)


def gen_op(rng, n):
    r = rng.random()
    if r < 0.30:
        return {"op": "write", "path": rand_path(rng),
                "content": rng.choice(CONTENTS).hex()}
    if r < 0.38:
        d = rng.choice(DIRS)
        lines = rng.sample(IGNORE_LINES, rng.randint(1, 4))
        return {"op": "write", "path": os.path.join(d, ".gitignore"),
                "content": ("\n".join(lines) + "\n").encode().hex()}
    # delete, chmod and rm pick among the files that exist when they run,
    # so they rarely turn into no-ops.
    if r < 0.43:
        return {"op": "delete", "pick": rng.randrange(1 << 16)}
    if r < 0.48:
        return {"op": "chmod", "pick": rng.randrange(1 << 16),
                "exec": rng.random() < 0.5}
    if r < 0.70:
        return {"op": "add", "paths": "*" if rng.random() < 0.3
                else [rand_path(rng) for _ in range(rng.randint(1, 3))]}
    if r < 0.76:
        return {"op": "rm", "pick": rng.randrange(1 << 16)}
    if r < 0.80:
        return {"op": "add-rewrite", "pick": rng.randrange(1 << 16)}
    if r < 0.82:
        return {"op": "file-to-dir", "pick": rng.randrange(1 << 16),
                "name": rng.choice(NAMES)}
    return {"op": "commit", "message": f"commit {n}"}


def gen_ops(rng, count):
    return [gen_op(rng, i) for i in range(count)]


# ── Running the two tools ───────────────────────────────────────────────────

class Env:
    """Isolated HOME and git config shared by both tools."""

    def __init__(self, base):
        self.home = os.path.join(base, "home")
        os.makedirs(self.home)
        with open(os.path.join(self.home, ".gitconfig"), "w") as f:
            f.write("[user]\n\tname = Diff Tester\n\temail = diff@test.invalid\n"
                    "[commit]\n\tgpgsign = false\n"
                    "[init]\n\tdefaultBranch = master\n"
                    "[core]\n\tquotepath = off\n"
                    "[status]\n\trenames = false\n")
        self.env = dict(os.environ, HOME=self.home,
                        XDG_CONFIG_HOME=os.path.join(self.home, ".config"),
                        GIT_CONFIG_NOSYSTEM="1",
                        GIT_CONFIG_GLOBAL=os.path.join(self.home, ".gitconfig"))
        # Pgit runs in-process and reads its identity from HOME.
        os.environ.update(HOME=self.home,
                          XDG_CONFIG_HOME=os.path.join(self.home, ".config"))


class PgitError(Exception):
    pass


def git(env, cwd, *args, check=True, extra_env=None):
    e = env.env if not extra_env else dict(env.env, **extra_env)
    p = subprocess.run(["git", "--no-optional-locks", *args], cwd=cwd, env=e,
                       capture_output=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}:\n"
                           f"{p.stderr.decode(errors='replace')}")
    return p


def pgit(cwd, *args):
    """Run a Pgit command in-process and return its stdout as bytes.
    Any exception is a Pgit crash."""
    old = os.getcwd()
    raw = io.BytesIO()
    out = io.TextIOWrapper(raw, encoding="utf-8", write_through=True)
    try:
        os.chdir(cwd)
        with contextlib.redirect_stdout(out):
            libwyag.main(list(args))
        out.flush()
    except BaseException:
        raise PgitError(f"pgit {' '.join(args)} crashed:\n"
                        f"{traceback.format_exc(limit=-3)}")
    finally:
        os.chdir(old)
    return raw.getvalue()


# ── Applying operations ─────────────────────────────────────────────────────

def worktree_files(root):
    files = []
    for d, dirs, fs in os.walk(root):
        dirs[:] = [x for x in dirs if not (d == root and x == ".git")]
        for f in fs:
            files.append(os.path.relpath(os.path.join(d, f), root))
    return sorted(files)


def index_paths(env, repo):
    out = git(env, repo, "ls-files", "-z").stdout
    return set(p.decode() for p in out.split(b"\0") if p)


def can_write(root, path):
    full = os.path.join(root, path)
    if os.path.isdir(full):
        return False
    parent = os.path.dirname(path)
    while parent:
        if os.path.isfile(os.path.join(root, parent)):
            return False
        parent = os.path.dirname(parent)
    return True


def pick(op, paths):
    paths = sorted(paths)
    return paths[op["pick"] % len(paths)] if paths else None


def apply_op(env, op, pg, rg):
    """Apply one operation to both repos. Returns False if its preconditions
    don't hold (it is then skipped, which keeps shrunk sequences valid)."""
    kind = op["op"]

    if kind == "write":
        if not (can_write(rg, op["path"]) and can_write(pg, op["path"])):
            return False
        for root in (pg, rg):
            full = os.path.join(root, op["path"])
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as f:
                f.write(bytes.fromhex(op["content"]))
        return True

    if kind == "delete":
        path = pick(op, worktree_files(rg))
        if not path:
            return False
        for root in (pg, rg):
            os.unlink(os.path.join(root, path))
        return True

    if kind == "chmod":
        path = pick(op, worktree_files(rg))
        if not path:
            return False
        for root in (pg, rg):
            os.chmod(os.path.join(root, path),
                     0o755 if op["exec"] else 0o644)
        return True

    if kind == "add":
        if op["paths"] == "*":
            paths = worktree_files(rg)
        else:
            paths = sorted(set(p for p in op["paths"]
                               if os.path.isfile(os.path.join(rg, p))))
        if not paths:
            return False
        # -f: Pgit's add doesn't consult ignore rules, so Git mustn't either.
        git(env, rg, "add", "-f", "--", *paths)
        pgit(pg, "add", *paths)
        return True

    if kind == "rm":
        path = pick(op, index_paths(env, rg))
        # Git refuses to rm a path that has become a directory.
        if not path or os.path.isdir(os.path.join(rg, path)):
            return False
        git(env, rg, "rm", "-q", "-f", "--", path)
        pgit(pg, "rm", path)
        return True

    if kind == "add-rewrite":
        # Stage a file, then at once overwrite it with different content of
        # the same size: the timestamps may not change ("racy git").
        path = pick(op, worktree_files(rg))
        if not path:
            return False
        git(env, rg, "add", "-f", "--", path)
        pgit(pg, "add", path)
        for root in (rg, pg):
            full = os.path.join(root, path)
            with open(full, "rb") as f:
                data = f.read()
            with open(full, "wb") as f:
                f.write(bytes(b ^ 0x20 for b in data) if data else b"")
        return True

    if kind == "file-to-dir":
        # Replace a file with a directory of the same name.
        path = pick(op, worktree_files(rg))
        if not path:
            return False
        for root in (pg, rg):
            os.unlink(os.path.join(root, path))
            os.makedirs(os.path.join(root, path))
            with open(os.path.join(root, path, op["name"]), "wb") as f:
                f.write(b"now a directory\n")
        return True

    if kind == "commit":
        pgit(pg, "commit", "-m", op["message"])
        # Replay the commit in Git with Pgit's exact author line and date.
        head = git(env, pg, "cat-file", "-p", "HEAD").stdout.decode()
        author = next(l for l in head.splitlines() if l.startswith("author "))
        ident, date = author[7:].rsplit(" ", 2)[0], " ".join(author.rsplit(" ", 2)[1:])
        name, email = ident[:-1].split(" <")
        git(env, rg, "commit", "-q", "--allow-empty", "--no-verify",
            "-m", op["message"],
            extra_env=dict(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email,
                           GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email,
                           GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date))
        return True

    raise ValueError(f"unknown op {kind}")


# ── Comparing the two repos ─────────────────────────────────────────────────

class Divergence(Exception):
    def __init__(self, check, detail):
        super().__init__(f"[{check}] {detail}")
        self.check = check


def git_status_set(env, repo):
    out = git(env, repo, "status", "--porcelain=v1", "-z", "-uall").stdout
    result = set()
    for rec in out.split(b"\0"):
        if not rec:
            continue
        rec = rec.decode()
        xy, path = rec[:2], rec[3:]
        if xy == "??":
            result.add(("untracked", path))
            continue
        if xy[0] in "AMD":
            result.add(({"A": "added", "M": "modified", "D": "deleted"}[xy[0]], path))
        if xy[1] in "MD":
            result.add(({"M": "wt-modified", "D": "wt-deleted"}[xy[1]], path))
    return result


def pgit_status_set(repo):
    out = pgit(repo, "status").decode()
    result, section = set(), None
    for line in out.splitlines():
        if line.startswith("Changes to be committed"):
            section = "staged"
        elif line.startswith("Changes not staged"):
            section = "unstaged"
        elif line.startswith("Untracked files"):
            section = "untracked"
        elif line.startswith("  ") and section:
            body = line[2:]
            if section == "untracked":
                result.add(("untracked", body))
            else:
                kind, path = body.split(":", 1)
                kind = kind.strip()
                result.add((kind if section == "staged" else "wt-" + kind,
                            path.strip()))
    return result


def ignore_candidates(root):
    cands = set(worktree_files(root))
    for d in DIRS:
        for n in NAMES:
            cands.add(os.path.join(d, n) if d else n)
    return sorted(c for c in cands if not c.endswith(".gitignore"))


def expect_equal(check, what, got, want):
    if got != want:
        if isinstance(got, set) and isinstance(want, set):
            raise Divergence(check, f"{what}\n    only in Pgit: {sorted(got - want)}"
                                    f"\n    only in Git:  {sorted(want - got)}")
        raise Divergence(check, f"{what}\n    Pgit: {got!r}\n    Git:  {want!r}")


def worktree_entries(root):
    """Every file and directory in the working tree, with its kind."""
    entries = set()
    for d, dirs, fs in os.walk(root):
        dirs[:] = [x for x in dirs if not (d == root and x == ".git")]
        rel = os.path.relpath(d, root)
        if rel != ".":
            entries.add((rel, "dir"))
        for f in fs:
            entries.add((os.path.relpath(os.path.join(d, f), root), "file"))
    return entries


def compare(env, pg, rg, committed=False):
    # Working trees: Pgit's commands must leave the same files and dirs.
    expect_equal("worktree", "files and directories",
                 worktree_entries(pg), worktree_entries(rg))

    # Index: Git reads Pgit's index and its own.
    expect_equal("index", "git ls-files -s",
                 git(env, pg, "ls-files", "-s", "-z").stdout.decode(),
                 git(env, rg, "ls-files", "-s", "-z").stdout.decode())

    # Status, four ways: each tool on each repo must agree with Git on its own.
    truth = git_status_set(env, rg)
    expect_equal("status", "pgit status (Pgit repo) vs git status",
                 pgit_status_set(pg), truth)
    expect_equal("status", "git status (Pgit repo) vs git status",
                 git_status_set(env, pg), truth)
    expect_equal("status", "pgit status (Git repo) vs git status",
                 pgit_status_set(rg), truth)

    # Ignore rules.
    cands = ignore_candidates(rg)
    got = set(pgit(pg, "check-ignore", *cands).decode().splitlines())
    want = set(git(env, rg, "check-ignore", "--no-index", "--", *cands,
                   check=False).stdout.decode().splitlines())
    expect_equal("ignore", "check-ignore", got, want)

    has_head = git(env, rg, "rev-parse", "-q", "--verify", "HEAD",
                   check=False).returncode == 0
    if not has_head:
        return

    # Commits must be byte-identical.
    expect_equal("commit", "HEAD commit SHA",
                 git(env, pg, "rev-parse", "HEAD").stdout.decode().strip(),
                 git(env, rg, "rev-parse", "HEAD").stdout.decode().strip())

    if not committed:
        return

    fsck = git(env, pg, "fsck", "--strict", "--no-dangling", check=False)
    if fsck.returncode != 0 or fsck.stderr.strip():
        raise Divergence("fsck", fsck.stderr.decode(errors="replace").strip())

    # Pgit reading Git's repository.
    expect_equal("read-back", "ls-tree -r HEAD",
                 pgit(rg, "ls-tree", "-r", "HEAD").decode(),
                 git(env, rg, "ls-tree", "-r", "HEAD").stdout.decode())
    expect_equal("read-back", "cat-file commit HEAD",
                 pgit(rg, "cat-file", "commit", "HEAD"),
                 git(env, rg, "cat-file", "commit", "HEAD").stdout)
    dot = pgit(rg, "log", "HEAD").decode()
    logged = set(w[2:42] for w in dot.split() if w.startswith("c_"))
    expect_equal("read-back", "commits reached by pgit log",
                 logged,
                 set(git(env, rg, "rev-list", "HEAD").stdout.decode().split()))


# ── Running sequences ───────────────────────────────────────────────────────

class Stats:
    def __init__(self):
        self.sequences = self.ops = self.applied = self.commits = 0
        self.canaries = 0


def run_sequence(env, base, ops, stats=None, canary_rng=None, tz="UTC"):
    """Run ops on fresh repos. Returns None, or (index of failing op, Divergence)."""
    os.environ["TZ"] = tz
    time.tzset()
    pg, rg = os.path.join(base, "pgit"), os.path.join(base, "git")
    for d in (pg, rg):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
    pgit(pg, "init", ".")
    git(env, rg, "init", "-q", ".")

    for i, op in enumerate(ops):
        try:
            applied = apply_op(env, op, pg, rg)
            if applied:
                compare(env, pg, rg, committed=op["op"] == "commit")
        except (Divergence, PgitError) as e:
            if isinstance(e, PgitError):
                e = Divergence("crash", str(e))
            return i, e
        if stats:
            stats.ops += 1
            stats.applied += applied
            stats.commits += applied and op["op"] == "commit"

    if canary_rng:
        plant_canary(env, base, pg, rg, canary_rng)
        if stats:
            stats.canaries += 1
    return None


def plant_canary(env, base, pg, rg, rng):
    """Change a copy of Pgit's repo in one small way and require the checker
    to notice. Proves the comparisons above aren't vacuous."""
    copy = os.path.join(base, "canary")
    shutil.rmtree(copy, ignore_errors=True)
    shutil.copytree(pg, copy, symlinks=True)
    kind = rng.choice(["stage", "commit"])
    files = worktree_files(copy)
    path = rng.choice(files) if files else "canary.txt"
    with open(os.path.join(copy, path), "ab") as f:
        f.write(b"canary\n")
    pgit(copy, "add", path)
    if kind == "commit":
        pgit(copy, "commit", "-m", "canary")
    try:
        compare(env, copy, rg, committed=kind == "commit")
    except Divergence:
        return
    raise SystemExit(f"CHECKER IS BLIND: a planted {kind} of {path!r} "
                     "was not detected")


def shrink(env, base, ops, check, tz):
    """Greedily drop operations while the same check still fails."""
    def fails(candidate):
        r = run_sequence(env, base, candidate, tz=tz)
        return r is not None and r[1].check == check

    ops = list(ops)
    chunk = max(1, len(ops) // 2)
    while chunk >= 1:
        i = 0
        while i < len(ops):
            candidate = ops[:i] + ops[i + chunk:]
            if candidate and fails(candidate):
                ops = candidate
            else:
                i += chunk
        chunk //= 2
    return ops


def report_failure(env, base, ops, failure, seed, tz):
    idx, err = failure
    ops = ops[:idx + 1]
    print(f"\nDIVERGENCE {err}")
    print(f"  after op #{idx} of a sequence (seed {seed}); shrinking...")
    small = shrink(env, base, ops, err.check, tz)
    final = run_sequence(env, base, small, tz=tz)
    out = os.path.abspath(f"difftest-failure-{seed}.json")
    with open(out, "w") as f:
        json.dump({"tz": tz, "ops": small}, f, indent=1)
    print(f"  minimal reproduction: {len(small)} ops (from {len(ops)}), TZ={tz}")
    for op in small:
        print("   ", json.dumps(op, ensure_ascii=False))
    if final:
        print(f"  {final[1]}")
    print(f"  saved to {out}; replay with: python3 tests/difftest.py --replay {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", type=int, default=50, help="sequences to run")
    ap.add_argument("--ops", type=int, default=40, help="operations per sequence")
    ap.add_argument("--seed", type=int, help="seed for the first sequence")
    ap.add_argument("--replay", help="JSON op list to replay")
    ap.add_argument("--keep-going", action="store_true",
                    help="report every failing sequence instead of stopping")
    args = ap.parse_args()

    base = tempfile.mkdtemp(prefix="pgit-difftest-")
    env = Env(base)
    try:
        if args.replay:
            with open(args.replay) as f:
                saved = json.load(f)
            ops = saved["ops"]
            failure = run_sequence(env, base, ops, tz=saved["tz"])
            if failure:
                print(f"op #{failure[0]}: {failure[1]}")
                return 1
            print(f"replayed {len(ops)} ops: no divergence")
            return 0

        seed0 = args.seed if args.seed is not None else random.randrange(1 << 30)
        stats, failures, start = Stats(), 0, time.time()
        for n in range(args.runs):
            seed = seed0 + n
            rng = random.Random(seed)
            ops = gen_ops(rng, args.ops)
            tz = rng.choice(TIMEZONES)
            failure = run_sequence(env, base, ops, stats, canary_rng=rng, tz=tz)
            stats.sequences += 1
            if failure:
                failures += 1
                report_failure(env, base, ops, failure, seed, tz)
                if not args.keep_going:
                    break
            elif (n + 1) % 10 == 0:
                print(f"  {n + 1}/{args.runs} sequences ok", flush=True)

        print(f"\n{stats.sequences} sequences (seeds {seed0}..{seed0 + stats.sequences - 1}), "
              f"{stats.applied} operations applied, {stats.commits} commits, "
              f"{stats.canaries} canaries detected, {failures} divergences "
              f"[{time.time() - start:.1f}s]")
        return 1 if failures else 0
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
