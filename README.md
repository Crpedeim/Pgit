# Pgit — Git, Reimplemented From Scratch in Python

[![difftest](https://github.com/Crpedeim/Pgit/actions/workflows/difftest.yml/badge.svg)](https://github.com/Crpedeim/Pgit/actions/workflows/difftest.yml)

A working reimplementation of Git's internals in a single Python library (~1,600 lines). Not a wrapper around the `git` binary — a from-scratch implementation of the **object database**, **content-addressable storage**, the **staging index**, the **reference system**, and `.gitignore` matching, following the structure of Thibault Polge's *Write Yourself a Git*.

**The defining property: Pgit is byte-for-byte interoperable with real Git.** Objects it writes are read by real `git`; repositories real `git` creates are read by Pgit — because Pgit implements Git's exact on-disk formats rather than approximating them.

---

## Table of contents
- [Interoperability (verified)](#interoperability-verified)
- [Differential testing against real Git](#differential-testing-against-real-git)
- [Crash safety](#crash-safety)
- [Packfiles: reading real repositories](#packfiles-reading-real-repositories)
- [How Git works — and what Pgit implements](#how-git-works--and-what-pgit-implements)
  - [1. Content-addressable object storage](#1-content-addressable-object-storage)
  - [2. The four object types](#2-the-four-object-types)
  - [3. The staging index](#3-the-staging-index)
  - [4. References, HEAD, and name resolution](#4-references-head-and-name-resolution)
  - [5. .gitignore matching](#5-gitignore-matching)
  - [6. status: three-way comparison](#6-status-three-way-comparison)
- [Command reference](#command-reference)
- [Usage](#usage)
- [Design notes](#design-notes)
- [What I learned / limitations / extensions](#what-i-learned)

---

## Interoperability (verified)

The correctness bar for this project isn't "it runs" — it's "real Git agrees with it." Both directions were tested:

```bash
# ── Pgit → Git ──────────────────────────────────────────────
# Pgit hashes a file; its SHA is identical to real git's, and
# real git can read the object Pgit wrote:
$ pgit hash-object -w -t blob file.txt
ee33377b2541b35f847859a388f3259522b2247d
$ git hash-object file.txt
ee33377b2541b35f847859a388f3259522b2247d          # identical SHA
$ git cat-file -p ee33377…
hello from pgit                                    # real git reads Pgit's object

# ── Git → Pgit ──────────────────────────────────────────────
# Pgit reads objects and commits created by real git, with
# byte-identical output to `git cat-file -p`.

# ── Full native round-trip ──────────────────────────────────
# A repository built ENTIRELY by Pgit is accepted by real git:
$ pgit init .
$ pgit add hello.txt
$ pgit commit -m "authored by pgit"
$ git log --oneline
f06fc2c authored by pgit                           # real git accepts Pgit's commit
$ git ls-tree HEAD
100644 blob 4cfd8e96…  hello.txt                   # real git reads Pgit's tree
```

Identical SHAs are the strong signal: because Git addresses objects by the hash of their content, a matching SHA means Pgit reproduced the object's bytes *exactly* — header, encoding, and all.

---

## Differential testing against real Git

Hand-written examples only prove the cases someone thought of. `tests/difftest.py` generates random sequences of operations (writing files in nested directories, `.gitignore` rules, `chmod +x`, deletes, `add`, `rm`, `commit`) and runs each sequence through **Pgit and real Git side by side**, in a random timezone. After every operation it requires the two to agree on:

| Check | What must match |
|---|---|
| worktree | the files and directories each tool leaves on disk |
| index | `git ls-files -s` on Pgit's index vs on Git's index |
| status | `pgit status` vs `git status`, run in **both** repos, so each tool reads the other's index |
| ignore | `pgit check-ignore` vs `git check-ignore --no-index` over a pool of tricky paths |
| commit | commit SHAs: Pgit's commit is replayed in Git with the same author and timestamp, so the bytes must be identical |
| fsck | `git fsck --strict` accepts Pgit's repository |
| read-back | Pgit's `ls-tree`, `cat-file` and `log` on Git's repository vs Git's own output |

When the two disagree, the harness **shrinks** the sequence to a minimal reproduction (usually 1–5 operations) and saves it as JSON to replay with `--replay`.

**Testing the tester.** A checker that never fails proves nothing, so the harness checks itself two ways:
- **Canaries:** at the end of every sequence it plants one extra change in a copy of Pgit's repo and requires the checks to notice it. If any canary slips through, the run fails.
- **Mutants:** `tests/mutants.py` puts back, one at a time, each bug the harness found (below) and requires it to be caught.

**Results** on the current code: 300 sequences (seeds 800000–800299), 9,682 operations, 1,366 commits, 300 of 300 canaries caught, **0 divergences**; the mutation check catches **31 of 31** reintroduced bugs (across this harness, the crash-safety fixes and the pack reader). An earlier 1,000-sequence run (32,072 operations, 8,731 commits) is what found the racy-git and file-to-directory bugs below.

**Bugs it found and fixed** (each one is now a mutant):

| Bug | Caught by |
|---|---|
| `status` crashed in a repo with no commits | crash |
| Subtrees written with mode `040000` instead of `40000`, so tree SHAs didn't match Git's | commit |
| Subtrees sorted as `name` instead of `name/` | status |
| Index written without its SHA-1 trailer (`git fsck`: "index file corrupt") | fsck |
| Index entries not sorted by path ("unordered stage entries") | index |
| Malformed timezone for negative UTC offsets (e.g. `--500`) | commit |
| `.gitignore` read from the index instead of the working tree | ignore |
| Ignore patterns matched with `fnmatch`: `*` crossed `/`, no anchoring | ignore |
| Directory-only patterns (`build/`) not handled | ignore |
| Files inside an ignored directory could be re-included | ignore |
| `check-ignore` treated an existing directory as a file, so `build/` didn't match `build` | ignore |
| `add` ignored the executable bit (always `100644`) | index |
| `status` missed `chmod +x` in the working tree | status |
| `status` missed a staged mode change | status |
| `init` wrote `core.filemode = false`, so real Git ignored exec bits too | status |
| `rm` crashed on a file already deleted from disk | crash |
| `rm` left emptied directories behind | worktree |
| `status` treated a file replaced by a directory as modified (or crashed opening it) | status |
| Racy git: a file rewritten in the same second as `add` looked unchanged to Git, because Pgit didn't "smudge" racily clean entries | status |
| `add` kept a file and a directory with the same name in the index (after a file became a directory) | index |

```bash
python3 tests/difftest.py                   # 50 random sequences
python3 tests/difftest.py --runs 1000 --seed 1
python3 tests/mutants.py                    # every reintroduced bug must be caught
```

Both run in CI on every push.

---

## Crash safety

A version control system has to survive being killed at any moment. Pgit originally wrote objects, the index and refs in place, so a crash mid-write left truncated files behind. It now follows Git's own protocols:

- **Objects** are written to a temp file, fsynced, made read-only and **renamed into place** (`write_atomic`). An object file either doesn't exist or is complete, and leftover temp files are ignored by Git.
- **The index and refs** use Git's **lockfile protocol** (`LockFile`): create `index.lock` with `O_EXCL`, write and fsync it, then rename it over `index`. Readers see the old file or the new one, never a mix. A second process can't take the lock, and `add`/`rm` hold it across the whole read-modify-write, so concurrent updates can't be lost.
- **Ordering:** blobs, trees and the commit are all durable before the branch ref moves, so a ref can never point at a missing object.

`tests/crashtest.py` proves it three ways:

- **Crash injection at every disk change.** Pgit runs with its file APIs wrapped. The child dies right before its Nth disk change, or halfway through its Nth write (a torn write). Every crash point of `commit`, `add`, `rm` and `hash-object` is tried.
- **Real `kill -9`** at random moments during `add` + `commit`.
- **Concurrency:** 8 `pgit add`s started at once, 25 times.

After every crash the repository must pass `git fsck --strict`. HEAD and the index must each be entirely before or entirely after the command, `pgit status` must still run, and rerunning the command (after removing a stale lock, as Git asks you to) must give the same result as a clean run.

| | Before | After |
|---|---|---|
| Injected crashes leaving a broken repository | **668 of 718** | **0 of 1,032** |
| Random `kill -9`s leaving a broken repository | **33 of 200** | **0 of 200** |
| Concurrent `add`s: lost updates / crashes | **29 / 46** (of 200) | **0 / 0** (the rest cleanly refused by `index.lock`) |

(The "after" code has more crash points because each atomic write adds a temp file, an fsync and a rename.) This tests process crashes. Surviving power loss also depends on the `fsync` calls, which can't be exercised without a VM.

```bash
python3 tests/crashtest.py                  # all three modes
python3 tests/crashtest.py --mode kill --kills 1000
```

---

## Packfiles: reading real repositories

Loose objects (one zlib file per object) are only how *new* objects start out. `git clone`, `git gc` and `git push` store objects in **packfiles**, so before this, Pgit couldn't read almost any real repository. Most objects in a pack are stored as a **delta** against another object. Pgit now reads them all, in pure Python:

- **Pack index (`.idx` v2):** a 256-entry fanout table narrows the search to objects sharing a first byte, then binary search over the sorted SHAs gives the object's offset in the pack (with the 8-byte table for packs over 2 GiB). The same structure answers abbreviated SHAs like `rev-parse 00bee`.
- **Packfile objects:** a variable-length type/size header, then a zlib stream. Commits, trees, blobs and tags are stored whole.
- **Deltas:** `OFS_DELTA` names its base by a backwards offset in the same pack, and `REF_DELTA` by SHA. A delta is a list of *copy a range of the base* and *insert these bytes* instructions. Chains can be hundreds deep, so they're walked with a loop, not recursion, and recently built objects are kept in a small LRU cache.
- **`packed-refs`:** branches and tags packed into one file. A loose ref file still wins, so `pgit commit` on a packed branch writes a loose ref, just as Git does.

`tests/packtest.py` builds repositories that hit each part of the format (deep `OFS_DELTA` chains via `gc --aggressive`, a `REF_DELTA` repack, a file over 64 KiB so deltas use the special 0x10000-byte copy, annotated tags, symlinks and a submodule) and can add any real clone. For every repository, **every object** Pgit reads must be byte-identical to `git cat-file`, including a random sample read cold in random order. Abbreviated SHAs, `show-ref`, `log`, `ls-tree`, a clean `status` and a commit on top that passes `git fsck` are checked too. The differential tester also runs `git gc` mid-sequence, so Pgit keeps working after Git packs everything, and creates symlinks (some dangling).

| Repository | Objects | Stored as deltas | Longest delta chain | Result |
|---|---|---|---|---|
| Built: `gc --aggressive` | 2,361 | 1,200 (`OFS_DELTA`) | 24 | all byte-identical |
| Built: `REF_DELTA` repack | 2,361 | 1,188 (`REF_DELTA`) | 24 | all byte-identical |
| [pallets/flask](https://github.com/pallets/flask) | 26,390 | 17,638 | 22 | all byte-identical, ~13,000 objects/s |
| Flask repacked `--depth=250` | 26,390 | 18,814 | 173 | all byte-identical |
| [git/git](https://github.com/git/git) | 410,700 | 312,684 | 50 | all byte-identical, ~7,500 objects/s |

On Flask and git/git, `log` walks the whole history, `show-ref`, `ls-tree` and abbreviated SHAs match Git, `status` is clean on a fresh checkout, and a commit made by Pgit on top passes `git fsck`.

**Bugs this found and fixed** (each is now a mutant, alongside eight that break one rule of the pack format, such as the `+1` in `OFS_DELTA` offsets or the size-0 copy meaning 0x10000):

| Bug | Found by |
|---|---|
| Every lookup crashed in a fully packed clone (no `objects/xx/` directories) | Flask clone |
| Odd-length abbreviated SHAs (`00bee`) found nothing: the scan stopped at `00be8d…` | packtest |
| `log` recursed once per commit, far past Python's recursion limit on real histories | review |
| `status` followed symlinks and compared the target's content (Git stores the link text) | git/git |
| `add` staged what a symlink points to instead of the link (mode `120000`) | difftest |
| An unpopulated submodule (gitlink, mode `160000`) showed as deleted | git/git |
| A symlink to a directory was missing from untracked files | review |

```bash
python3 tests/packtest.py                     # built repositories, offline
python3 tests/packtest.py --clone https://github.com/pallets/flask.git
python3 tests/packtest.py --repo ~/src/git    # any existing clone
```

---

## How Git works — and what Pgit implements

### 1. Content-addressable object storage

Every object is stored under the SHA-1 hash of its content, so identical content always lands at the same address and is deduplicated for free.

The write path (`object_write`):
1. Serialize the object's payload to bytes.
2. Prepend a header: `<type> <length>\0` (e.g. `blob 15\0`).
3. SHA-1 the header + payload → the object's 40-hex-char address.
4. zlib-compress the whole thing.
5. Write to `.git/objects/<first 2 hex>/<remaining 38 hex>`.

The read path (`object_read`) reverses it: read the file, zlib-decompress, split on the first space and NUL to recover type and length, validate the length, and dispatch to the right class. This is Git's entire storage layer, and it's why integrity is built in — a corrupted byte changes the hash.

### 2. The four object types

| Type | Represents | Serialization |
|---|---|---|
| **blob** | raw file contents | the bytes themselves |
| **tree** | one directory snapshot | sorted list of `mode name\0<20-byte SHA>` entries |
| **commit** | a snapshot + history | key-value list (`tree`, `parent`, `author`, `committer`) + message |
| **tag** | annotated tag object | same KV-list format as a commit |

Trees reference blobs and other trees by SHA; commits reference a tree and parent commit(s). Together they form the **commit DAG**. Tree parsing/serialization (`tree_parse`, `tree_serialize`) handles Git's exact binary layout, including the 20-byte big-endian raw SHA (not hex) and the mode-normalization quirk where a 5-digit mode gets a leading zero.

The commit/tag format is parsed by a recursive **key-value list with message** parser (`kvlm_parse` / `kvlm_serialise`) that handles multi-valued keys (e.g. multiple `parent` lines for merges) and continuation-line folding.

### 3. The staging index

The piece most people never see. `add` does **not** create commits — it writes entries into `.git/index`, Git's **binary** staging file. Pgit implements the real format (`GitIndex`, `GitIndexEntry`, `index_read`, `index_write`): a header followed by fixed-layout entries carrying ctime/mtime, device/inode, mode, size, the blob SHA, and the path. `commit` then turns the current index into a tree, wraps that tree in a commit object, and advances the branch reference.

### 4. References, HEAD, and name resolution

Branches and tags are just files under `.git/refs/` containing a 40-char SHA, or a symbolic `ref: refs/heads/...` that points at another ref. `HEAD` names the current branch. `ref_resolve` follows symbolic refs recursively; `ref_list` walks the `refs/` tree.

Name resolution (`object_resolve` / `object_find`) is richer than it looks — it accepts the `HEAD` literal, full and **abbreviated** SHAs, and branch/tag names, then optionally "follows" an object to a desired type (e.g. resolving a tag or commit down to its tree for `ls-tree`).

### 5. .gitignore matching

`check-ignore` implements the rules from gitignore(5), not a naive glob. Each pattern is compiled to a regex: `*` and `?` never cross `/`, a slash at the start or middle **anchors** the pattern to its `.gitignore`'s directory (otherwise it matches a name at any depth), a trailing `/` matches **directories only**, `**` matches across directories, and `!pattern` **negates**. Rules come from the global ignore file, `.git/info/exclude`, and every `.gitignore` in the working tree. A deeper `.gitignore` beats a shallower one, and within a file the last matching pattern wins. Like Git, nothing inside an ignored directory can be re-included.

### 6. `status`: three-way comparison

`status` reconstructs the picture by comparing three states: **HEAD** (last commit's tree, via `tree_to_dict`), the **index** (staged), and the **working tree** (on disk). "Changes to be committed" is HEAD↔index; "changes not staged" is index↔working-tree. It also handles detached-HEAD reporting and skips the `.git` directory during the working-tree walk.

---

## Command reference

All 14 commands are wired into the CLI and tested against real Git.

| Command | Example | Purpose |
|---|---|---|
| `init` | `pgit init .` | Create a new repository |
| `hash-object` | `pgit hash-object -w -t blob f.txt` | Hash (and optionally store) a file as a blob |
| `cat-file` | `pgit cat-file commit HEAD` | Print an object's content by type + name |
| `log` | `pgit log HEAD` | Walk the commit DAG, emitting Graphviz DOT |
| `ls-tree` | `pgit ls-tree -r HEAD` | Pretty-print a tree (optionally recursive) |
| `checkout` | `pgit checkout HEAD ./out` | Instantiate a commit's tree into a directory |
| `show-ref` | `pgit show-ref` | List all references |
| `rev-parse` | `pgit rev-parse HEAD` | Resolve a name/abbreviation to a full SHA |
| `ls-files` | `pgit ls-files --verbose` | List staged files (the index) |
| `check-ignore` | `pgit check-ignore build/ x.log` | Test paths against ignore rules |
| `status` | `pgit status` | Compare HEAD ↔ index ↔ working tree |
| `add` | `pgit add file.txt` | Stage file contents into the index |
| `rm` | `pgit rm file.txt` | Remove files from the index and working tree |
| `commit` | `pgit commit -m "msg"` | Record the index as a new commit |

> `log` outputs the commit graph in Graphviz DOT — pipe it to `dot` to render the history visually: `pgit log HEAD | dot -Tpng > history.png`.

---

## Usage

```bash
# no third-party dependencies — standard library only
python3 wyag init .
python3 wyag add file.txt
python3 wyag commit -m "your message"
python3 wyag log HEAD
python3 wyag status

# verify interoperability yourself:
python3 wyag hash-object -w -t blob file.txt   # note the SHA
git cat-file -p <that-sha>                      # real git reads it
```

---

## Design notes

- **Single-file library + thin launcher.** All logic lives in `libwyag.py`; `wyag` is a 4-line entry point. Commands are dispatched via a `match` on the argparse subcommand.
- **Class-per-object-type with a shared base.** `GitObject` defines the `serialise`/`deserialise` contract; `GitBlob`, `GitTree`, `GitCommit`, `GitTag` implement it. `object_read` reads the type tag and constructs the right subclass polymorphically.
- **Exact formats over convenient ones.** Trees store raw 20-byte SHAs (not hex); the index is packed binary; refs are plain files. Matching these exactly is what buys interoperability.
- **Standard library only** — `hashlib` for SHA-1, `zlib` for compression, no external packages.
- **Crash-safe writes.** Temp-file-and-rename for objects, Git's lockfile protocol for the index and refs, and objects made durable before any ref points at them.

---

## What I learned

- Why content-addressing gives Git deduplication and integrity *for free* — the hash is both the name and the checksum.
- How the staging index sits between the working tree and history, and why `add` → `commit` is really "index → tree → commit object → move ref."
- The precise byte layout of Git objects, trees, and the binary index — proven correct by round-tripping through the real `git` binary.
- How much of Git is "just files": branches, tags, and HEAD are text files; the object store is a content-addressed key-value database on disk.

### Limitations & possible extensions
- **No `branch`/`merge`/`diff` yet.** Branches exist as refs and can be created by hand; a `branch` command and three-way `merge` are the natural next additions.
- A `tag` implementation exists in the code but isn't yet wired into the CLI.
- **Packfiles are read but not written.** Pgit writes new objects as loose files (as Git does too until `gc`); writing packs with delta compression is a natural extension.
- No network protocol yet (`clone`/`fetch`/`push`).
