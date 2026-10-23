#!/usr/bin/env python3
"""Pack tester: checks that Pgit reads packed repositories exactly like Git.

Repositories are built to cover the pack format: a history of edits packed
with --aggressive (deep OFS_DELTA chains), the same history repacked with
REF_DELTA, a file over 64 KiB (delta copies of exactly 0x10000 bytes),
annotated tags, binary files, nested directories, symlinks and a submodule
entry. Real repositories can
be added with --clone URL or --repo PATH.

For each repository:

  objects   every object, read by Pgit in pack order, is byte-identical to
            git cat-file --batch
  cold      a random sample read in random order with an empty cache
  abbrev    abbreviated SHAs (git rev-parse --short) resolve to the object
  refs      pgit show-ref vs git show-ref (refs live in packed-refs)
  log       pgit log HEAD reaches exactly git rev-list HEAD
  ls-tree   pgit ls-tree -r HEAD is identical
  commit    pgit status is clean on a fresh checkout, and a commit made by
            Pgit on top passes git fsck

Usage:
  python3 tests/packtest.py                      # built repositories only
  python3 tests/packtest.py --clone https://github.com/pallets/flask.git
  python3 tests/packtest.py --repo ~/src/git     # an existing clone
"""

import argparse
import glob
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WYAG = os.path.join(ROOT, "wyag")
sys.path.insert(0, ROOT)
import libwyag  # noqa: E402

ENV = None


class Failure(Exception):
    pass


def git(cwd, *args, check=True, binary=False):
    p = subprocess.run(["git", *args], cwd=cwd, env=ENV, capture_output=True,
                       text=not binary)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr}")
    return p


def pgit(cwd, *args):
    p = subprocess.run([sys.executable, WYAG, *args], cwd=cwd, env=ENV,
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise Failure(f"pgit {' '.join(args)} failed: {p.stderr.strip().splitlines()[-1:]}")
    return p.stdout


# ── Building repositories ───────────────────────────────────────────────────

def build_history(repo, rng, commits):
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "main", ".")
    words = [f"word{i}" for i in range(500)]
    texts = {f"src/mod{i}.py": [rng.choice(words) for _ in range(200)] for i in range(8)}
    big = bytearray(rng.randbytes(200 * 1024)) # > 64 KiB: copies of 0x10000 bytes
    for n in range(commits):
        for path in rng.sample(sorted(texts), 3):
            lines = texts[path]
            i = rng.randrange(len(lines))
            if rng.random() < 0.5:
                lines.insert(i, " ".join(rng.choices(words, k=5)))
            else:
                del lines[i]
        if n % 5 == 0:
            big[rng.randrange(len(big))] = rng.randrange(256)
        files = {p: ("\n".join(l) + "\n").encode() for p, l in texts.items()}
        files["data/big.bin"] = bytes(big)
        files[f"docs/{n % 7}/note.md"] = f"note {n}\n".encode() * (n % 13 + 1)
        for path, data in files.items():
            full = os.path.join(repo, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as f:
                f.write(data)
        if n == 3:
            os.symlink("src/mod0.py", os.path.join(repo, "link-to-mod0"))
            os.symlink("docs", os.path.join(repo, "link-to-docs"))
            # A submodule entry (gitlink); a clone checks it out as an empty dir.
            os.makedirs(os.path.join(repo, "vendor/lib"))
            git(repo, "update-index", "--add", "--cacheinfo",
                f"160000,{'ab' * 20},vendor/lib")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", f"commit {n}\n\nbody of commit {n}")
        if n % 40 == 0:
            git(repo, "tag", "-a", f"v{n}", "-m", f"release {n}")
        if n % 55 == 0:
            git(repo, "tag", f"light{n}")
            git(repo, "branch", f"topic{n}")


def build(base, commits, seed):
    rng = random.Random(seed)
    ofs = os.path.join(base, "ofs-delta")
    build_history(ofs, rng, commits)
    git(ofs, "gc", "-q", "--aggressive", "--prune=now")

    ref = os.path.join(base, "ref-delta")
    git(base, "clone", "-q", "--no-local", ofs, ref)
    git(ref, "-c", "repack.useDeltaBaseOffset=false", "repack", "-a", "-d", "-f", "-q")
    git(ref, "pack-refs", "--all")
    return [("built: aggressive (OFS_DELTA)", ofs), ("built: REF_DELTA", ref)]


# ── Checks ──────────────────────────────────────────────────────────────────

def pack_stats(repo):
    gitdir = git(repo, "rev-parse", "--git-dir").stdout.strip()
    deltas = depth = 0
    for idx in glob.glob(os.path.join(repo, gitdir, "objects/pack/*.idx")):
        out = git(repo, "verify-pack", "-v", idx).stdout
        for line in out.splitlines():
            f = line.split()
            if len(f) >= 7 and len(f[0]) == 40:
                deltas += 1
                depth = max(depth, int(f[5]))
    return deltas, depth


def check_objects(repo, sample_size, rng):
    r = libwyag.GitRepository(repo)
    p = subprocess.Popen(["git", "cat-file", "--batch-all-objects", "--batch", "--unordered"],
                         cwd=repo, env=ENV, stdout=subprocess.PIPE)
    count = 0
    pgit_time = 0.0
    sample = {}
    while True:
        header = p.stdout.readline()
        if not header:
            break
        sha, kind, size = header.split()
        body = p.stdout.read(int(size))
        p.stdout.read(1)
        start = time.time()
        got = libwyag.object_read_raw(r, sha.decode())
        pgit_time += time.time() - start
        if got != (kind, body):
            raise Failure(f"object {sha.decode()}: Pgit read "
                          f"{'nothing' if got is None else got[0].decode() + f' of {len(got[1])} bytes'}, "
                          f"Git has {kind.decode()} of {len(body)} bytes")
        count += 1
        # Reservoir sample for the cold-cache check.
        if len(sample) < sample_size:
            sample[sha.decode()] = (kind, body)
        elif rng.randrange(count) < sample_size:
            sample.pop(next(iter(sample)))
            sample[sha.decode()] = (kind, body)
    if p.wait() != 0:
        raise RuntimeError("git cat-file failed")
    return count, pgit_time, sample


def check_cold(repo, sample, rng):
    shas = list(sample)
    rng.shuffle(shas)
    r = libwyag.GitRepository(repo) # Fresh repository: empty delta cache
    for pack in libwyag.repo_packs(r):
        pack.CACHE_SIZE = 0
    for sha in shas:
        if libwyag.object_read_raw(r, sha) != sample[sha]:
            raise Failure(f"cold read of {sha} differs")


def check_abbrev(repo, shas):
    r = libwyag.GitRepository(repo)
    for sha in shas:
        short = git(repo, "rev-parse", "--short=4", sha).stdout.strip()
        got = libwyag.object_resolve(r, short)
        if got != [sha]:
            raise Failure(f"abbreviated {short} resolved to {got}, Git says {sha}")


def check_cli(repo, worktree):
    refs = set(pgit(repo, "show-ref").splitlines())
    want = set(git(repo, "show-ref").stdout.splitlines())
    if refs != want:
        raise Failure(f"show-ref differs: only Pgit {sorted(refs - want)[:3]}, "
                      f"only Git {sorted(want - refs)[:3]}")

    logged = set(w[2:42] for w in pgit(repo, "log", "HEAD").split() if w.startswith("c_"))
    want = set(git(repo, "rev-list", "HEAD").stdout.split())
    if logged != want:
        raise Failure(f"log reached {len(logged)} commits, git rev-list {len(want)}")

    if pgit(repo, "ls-tree", "-r", "HEAD") != git(repo, "ls-tree", "-r", "HEAD").stdout:
        raise Failure("ls-tree -r HEAD differs")

    if worktree:
        status = pgit(repo, "status").splitlines()
        changes = [l for l in status if l.startswith("  ")]
        if changes:
            raise Failure(f"pgit status on a fresh checkout isn't clean: {changes[:3]}")
        tracked = next(line.split("\t", 1)[1] for line in
                       git(repo, "ls-files", "-s").stdout.splitlines()
                       if line.startswith("100644 "))
        with open(os.path.join(repo, tracked), "ab") as f:
            f.write(b"\nedited by pgit\n")
        before = git(repo, "rev-parse", "HEAD").stdout.strip()
        pgit(repo, "add", tracked)
        pgit(repo, "commit", "-m", "commit by pgit on a packed repository")
        if git(repo, "rev-parse", "HEAD^").stdout.strip() != before:
            raise Failure("Pgit's commit isn't on top of HEAD")
        # Old repositories can contain legacy trees with zero-padded modes,
        # which --strict reports; only check that every object is sound.
        fsck = git(repo, "fsck", "--no-dangling", check=False)
        if fsck.returncode != 0:
            raise Failure(f"git fsck after Pgit's commit: {fsck.stderr.strip().splitlines()[:2]}")
        if git(repo, "status", "--porcelain").stdout.strip():
            raise Failure("git status isn't clean after Pgit's commit")


def test_repo(name, repo, worktree, sample_size, seed):
    rng = random.Random(seed)
    deltas, depth = pack_stats(repo)
    count, t, sample = check_objects(repo, sample_size, rng)
    check_cold(repo, sample, rng)
    check_abbrev(repo, sorted(sample)[:200])
    check_cli(repo, worktree)
    print(f"  ok  {name}: {count} objects ({deltas} deltas, chains up to {depth} deep), "
          f"all byte-identical; {count / t:.0f} objects/s", flush=True)


def main():
    global ENV
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--commits", type=int, default=250, help="history length of built repos")
    ap.add_argument("--clone", action="append", default=[], help="also test a clone of URL")
    ap.add_argument("--repo", action="append", default=[], help="also test an existing clone")
    ap.add_argument("--sample", type=int, default=2000, help="objects for the cold-cache check")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    base = tempfile.mkdtemp(prefix="pgit-packtest-")
    home = os.path.join(base, "home")
    os.makedirs(home)
    cfg = os.path.join(home, ".gitconfig")
    with open(cfg, "w") as f:
        f.write("[user]\n\tname = Pack Tester\n\temail = pack@test.invalid\n"
                "[commit]\n\tgpgsign = false\n[tag]\n\tgpgsign = false\n"
                "[gc]\n\tauto = 0\n")
    ENV = dict(os.environ, HOME=home, XDG_CONFIG_HOME=os.path.join(home, ".config"),
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=cfg)
    os.environ.update(HOME=home, XDG_CONFIG_HOME=os.path.join(home, ".config"))

    failures = 0
    try:
        print("Building repositories...", flush=True)
        repos = [(name, path, True) for name, path in build(base, args.commits, args.seed)]
        for url in args.clone:
            dest = os.path.join(base, "clone-" + str(len(repos)))
            p = git(base, "clone", "-q", url, dest, check=False)
            if p.returncode != 0:
                print(f"  skip {url}: clone failed ({p.stderr.strip()})")
                continue
            repos.append((url, dest, True))
        for path in args.repo:
            # Test a copy: the commit check writes to the repository.
            dest = os.path.join(base, "repo-" + str(len(repos)))
            git(base, "clone", "-q", "--no-local", os.path.abspath(os.path.expanduser(path)), dest)
            repos.append((path, dest, True))

        for name, repo, worktree in repos:
            try:
                test_repo(name, repo, worktree, args.sample, args.seed)
            except Failure as e:
                failures += 1
                print(f"  FAIL {name}: {e}", flush=True)
            except Exception as e: # Pgit raised while reading the repository
                failures += 1
                print(f"  FAIL {name}: {type(e).__name__}: {e}", flush=True)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
