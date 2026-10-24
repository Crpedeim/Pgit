#!/usr/bin/env python3
"""Checks that the differential tester catches real bugs.

Each mutant puts back one bug that was found and fixed in libwyag.py (or,
for the pack reader, breaks one rule of the pack format), then runs
difftest.py or packtest.py on the mutated copy. Every mutant must be
caught; if one survives, the tester has a blind spot.

Usage:
  python3 tests/mutants.py            # all mutants, 40 sequences each
  python3 tests/mutants.py --runs 100
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (description, code as fixed, code with the bug put back[, tester])
# The tester is difftest.py unless given.
MUTANTS = [
    ("status crashes before the first commit",
     '''    if not fmt or sha is None: # None: HEAD on a branch with no commits yet
        return sha''',
     '''    if not fmt:
        return sha'''),

    ("subtree mode written as 040000 instead of 40000",
     '''        ret += i.mode.lstrip(b"0")''',
     '''        ret += i.mode'''),

    ("subtrees sorted as \"name\" instead of \"name/\"",
     '''    if leaf.mode.lstrip(b"0").startswith(b"4"):''',
     '''    if leaf.mode.startswith(b"4"):'''),

    ("index written without its SHA-1 trailer",
     '''    raw.write(f.h.digest())''',
     '''    pass'''),

    ("index entries not sorted by path",
     '''    index.entries.sort(key=lambda e: e.name.encode("utf8"))''',
     '''    pass'''),

    ("negative timezone offsets malformed",
     '''    hours = abs(offset) // 3600
    minutes = (abs(offset) % 3600) // 60''',
     '''    hours = offset // 3600
    minutes = (offset % 3600) // 60'''),

    (".gitignore read from the index instead of the working tree",
     '''        if ".gitignore" in files:''',
     '''        if ".gitignore" in files and os.path.relpath(os.path.join(root, ".gitignore"), repo.worktree) in [e.name for e in index_read(repo).entries]:'''),

    ("ignore patterns matched with fnmatch (\"*\" crosses \"/\", no anchoring)",
     '''    if anchored:
        return re.compile(out + r"\\Z", re.S)''',
     '''    out = out.replace("[^/]*", ".*")
    if False:
        return re.compile(out + r"\\Z", re.S)'''),

    ("directory-only patterns (\"build/\") applied to files",
     '''        if rule.dir_only and not is_dir:
            continue''',
     '''        pass'''),

    ("files inside an ignored directory can be re-included",
     '''        if check_ignore_path(rules, "/".join(parts[:i]), True):
            return True''',
     '''        pass'''),

    ("check-ignore treats an existing directory as a file",
     '''        is_dir = os.path.isdir(full) and not os.path.islink(full)''',
     '''        is_dir = False'''),

    ("add ignores the executable bit",
     '''    mode_perms = 0o755 if stat.st_mode & 0o100 else 0o644

            ctime_s''',
     '''    mode_perms = 0o644

            ctime_s'''),

    ("status ignores chmod in the working tree",
     '''            if mode_changed:
                print("  modified:", entry.name)
            elif''',
     '''            if''' ),

    ("status ignores a staged mode change",
     '''            if head[entry.name] != (mode, entry.sha):''',
     '''            if head[entry.name][1] != entry.sha:'''),

    ("init writes core.filemode = false",
     '''    ret.set("core", "filemode", "false" if os.name == "nt" else "true")''',
     '''    ret.set("core", "filemode", "false")'''),

    ("index_write doesn't smudge racily clean entries",
     '''        f.write((0 if e.mtime[0] >= index_mtime_s else e.fsize).to_bytes(4, "big"))''',
     '''        f.write(e.fsize.to_bytes(4, "big"))'''),

    ("status treats a file replaced by a directory as modified",
     '''        elif not os.path.isfile(full_path): # Gone, or replaced by a directory''',
     '''        elif not os.path.exists(full_path):'''),

    ("add leaves a file and a directory with the same name in the index",
     '''                     if e.name not in conflicts
                     and not any(e.name.startswith(d) for d in new_files)]''',
     '''                     if True]'''),

    ("status follows symlinks instead of comparing the link",
     '''            elif not os.path.islink(full_path) or symlink_sha(full_path) != entry.sha:''',
     '''            elif open(full_path, "rb").read() != object_read(repo, entry.sha).blobdata:'''),

    ("add stages what a symlink points to instead of the link",
     '''        if os.path.islink(abspath): # Stage the link itself, not what it points to''',
     '''        if False:'''),

    ("status reports an unpopulated submodule as deleted",
     '''        if entry.mode_type == 0b1110: # Submodule: its contents belong to its own repository
            if not os.path.lexists(full_path):''',
     '''        if entry.mode_type == 0b1110:
            if not os.path.isfile(full_path):''', "packtest"),

    ("status follows a symlink that replaced a tracked file",
     '''        elif os.path.islink(full_path): # A file replaced by a symlink: a type change''',
     '''        elif False:'''),

    ("a symlinked .gitignore is followed (Git ignores it)",
     '''            if not os.path.islink(os.path.join(root, ".gitignore")):''',
     '''            if True:'''),

    ("rm crashes on a file already deleted from disk",
     '''            if os.path.lexists(path): # It may already be gone from the worktree
                os.unlink(path)''',
     '''            os.unlink(path)'''),

    ("rm leaves empty directories behind",
     '''            while parent != repo.worktree and os.path.isdir(parent) and not os.listdir(parent):''',
     '''            while False:'''),
]

PACK_MUTANTS = [
    # Caught by difftest's gc operation, which packs a repository mid-sequence.
    ("object lookup ignores packfiles",
     '''            found = pack.read(sha)''',
     '''            found = None'''),

    ("ref lookup ignores packed-refs",
     '''        return packed_refs(repo).get(name)''',
     '''        return None'''),

    ("loose lookup crashes when objects/xx/ doesn't exist",
     '''    path = repo_path(repo,"objects",sha[0:2],sha[2:])''',
     '''    path = repo_file(repo,"objects",sha[0:2],sha[2:])'''),

    # Caught by packtest.
    ("OFS_DELTA offset decoded without Git's +1 per byte",
     '''                    rel = ((rel + 1) << 7) | (c & 0x7f)''',
     '''                    rel = (rel << 7) | (c & 0x7f)''', "packtest"),

    ("delta copy of size 0 not read as 0x10000",
     '''            out += base[offset : offset + (size or 0x10000)]''',
     '''            out += base[offset : offset + size]''', "packtest"),

    ("REF_DELTA base SHA not skipped",
     '''                deltas.append((offset, self._inflate(pos + 20, size)))''',
     '''                deltas.append((offset, self._inflate(pos, size)))''', "packtest"),

    ("abbreviated SHAs don't search packs",
     '''            for sha in pack.index.prefix(name):''',
     '''            for sha in []:''', "packtest"),

    ("odd-length prefix search stops at the first non-match",
     '''        while i < hi and self.names[i].startswith(start):''',
     '''        while i < hi and self.names[i].hex().startswith(hexprefix):''', "packtest"),
]

MUTANTS += PACK_MUTANTS


def run(source, args, tester="difftest"):
    """Run a tester on a copy of libwyag.py with this source. Returns the
    name of the first check that failed, or None if none did."""
    work = tempfile.mkdtemp(prefix="pgit-mutant-")
    try:
        os.makedirs(os.path.join(work, "tests"))
        with open(os.path.join(work, "libwyag.py"), "w") as f:
            f.write(source)
        for name in ("wyag", "tests/difftest.py", "tests/packtest.py"):
            shutil.copy(os.path.join(ROOT, name), os.path.join(work, name))
        if tester == "difftest":
            cmd = ["tests/difftest.py", "--runs", str(args.runs), "--seed", str(args.seed)]
        else:
            cmd = ["tests/packtest.py", "--commits", "120"]
        p = subprocess.run([sys.executable, *cmd], cwd=work, capture_output=True, text=True)
        for line in p.stdout.splitlines():
            if line.startswith("DIVERGENCE"):
                return line.split("]")[0].split("[")[1]
            if line.strip().startswith("FAIL"):
                return "packtest"
        return "error" if p.returncode != 0 else None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", type=int, default=40, help="sequences per mutant")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    with open(os.path.join(ROOT, "libwyag.py")) as f:
        source = f.read()

    # The unmutated code must pass, or every mutant would look "caught".
    for tester in ("difftest", "packtest"):
        if run(source, args, tester) is not None:
            print(f"BASELINE FAILS: fix {tester} failures before checking mutants")
            return 1

    survived = 0
    for desc, fixed, buggy, *tester in MUTANTS:
        if source.count(fixed) != 1:
            print(f"  STALE     {desc}: fixed code not found in libwyag.py, update this mutant")
            survived += 1
            continue
        start = time.time()
        check = run(source.replace(fixed, buggy), args, *tester)
        print(f"  {'caught' if check else 'SURVIVED':9} {desc}  "
              f"[{check or '-'}, {time.time() - start:.0f}s]", flush=True)
        survived += not check

    print(f"\n{len(MUTANTS) - survived}/{len(MUTANTS)} reintroduced bugs caught")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
