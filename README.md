# Pgit — Git, Reimplemented From Scratch in Python

A working reimplementation of Git's internals in a single Python library (~1,600 lines). Not a wrapper around the `git` binary — a from-scratch implementation of the **object database**, **content-addressable storage**, the **staging index**, the **reference system**, and `.gitignore` matching, following the structure of Thibault Polge's *Write Yourself a Git*.

**The defining property: Pgit is byte-for-byte interoperable with real Git.** Objects it writes are read by real `git`; repositories real `git` creates are read by Pgit — because Pgit implements Git's exact on-disk formats rather than approximating them.

---

## Table of contents
- [Interoperability (verified)](#interoperability-verified)
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

`check-ignore` implements Git's actual precedence rules, not a naive glob. It reads rules from `.git/info/exclude` and from `.gitignore` files tracked in the index, supports **negation** (`!pattern`), and resolves **scoped** rules (a `.gitignore` applies to its own directory subtree) versus **absolute** rules, applying them in Git's priority order.

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

---

## What I learned

- Why content-addressing gives Git deduplication and integrity *for free* — the hash is both the name and the checksum.
- How the staging index sits between the working tree and history, and why `add` → `commit` is really "index → tree → commit object → move ref."
- The precise byte layout of Git objects, trees, and the binary index — proven correct by round-tripping through the real `git` binary.
- How much of Git is "just files": branches, tags, and HEAD are text files; the object store is a content-addressed key-value database on disk.

### Limitations & possible extensions
- **No `branch`/`merge`/`diff` yet.** Branches exist as refs and can be created by hand; a `branch` command and three-way `merge` are the natural next additions.
- A `tag` implementation exists in the code but isn't yet wired into the CLI.
- Packfiles and the network protocol are out of scope — this is the local object model, which is where Git's core ideas live.
