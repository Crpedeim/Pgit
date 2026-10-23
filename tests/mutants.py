#!/usr/bin/env python3
"""Checks that the differential tester catches real bugs.

Each mutant puts back one bug that was found and fixed in libwyag.py, then
runs difftest.py on the mutated copy. Every mutant must be caught; if one
survives, the tester has a blind spot.

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

# (description, code as fixed, code with the bug put back)
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
     '''        is_dir = os.path.isdir(os.path.join(repo.worktree, path))''',
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

    ("status trusts timestamps of racily clean entries",
     '''            racy = entry.mtime[0] >= index_mtime // 10**9''',
     '''            racy = False'''),

    ("status treats a file replaced by a directory as modified",
     '''        if not os.path.isfile(full_path): # Gone, or replaced by a directory''',
     '''        if not os.path.exists(full_path):'''),

    ("add leaves a file and a directory with the same name in the index",
     '''                     if e.name not in conflicts
                     and not any(e.name.startswith(d) for d in new_files)]''',
     '''                     if True]'''),

    ("rm crashes on a file already deleted from disk",
     '''            if os.path.lexists(path): # It may already be gone from the worktree
                os.unlink(path)''',
     '''            os.unlink(path)'''),

    ("rm leaves empty directories behind",
     '''            while parent != repo.worktree and os.path.isdir(parent) and not os.listdir(parent):''',
     '''            while False:'''),
]


def run(source, args):
    """Run difftest on a copy of libwyag.py with this source. Returns the
    name of the first check that failed, or None if none did."""
    work = tempfile.mkdtemp(prefix="pgit-mutant-")
    try:
        os.makedirs(os.path.join(work, "tests"))
        with open(os.path.join(work, "libwyag.py"), "w") as f:
            f.write(source)
        shutil.copy(os.path.join(ROOT, "tests", "difftest.py"),
                    os.path.join(work, "tests"))
        p = subprocess.run([sys.executable, "tests/difftest.py",
                            "--runs", str(args.runs), "--seed", str(args.seed)],
                           cwd=work, capture_output=True, text=True)
        for line in p.stdout.splitlines():
            if line.startswith("DIVERGENCE"):
                return line.split("]")[0].split("[")[1]
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
    if run(source, args) is not None:
        print("BASELINE FAILS: fix difftest divergences before checking mutants")
        return 1

    survived = 0
    for desc, fixed, buggy in MUTANTS:
        if source.count(fixed) != 1:
            print(f"  STALE     {desc}: fixed code not found in libwyag.py, update this mutant")
            survived += 1
            continue
        start = time.time()
        check = run(source.replace(fixed, buggy), args)
        print(f"  {'caught' if check else 'SURVIVED':9} {desc}  "
              f"[{check or '-'}, {time.time() - start:.0f}s]", flush=True)
        survived += not check

    print(f"\n{len(MUTANTS) - survived}/{len(MUTANTS)} reintroduced bugs caught")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
