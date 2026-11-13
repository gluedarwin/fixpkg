# fix — Source-Based Package Manager

**A simple, source-based and declarative package manager.**

## Build your environment from source.

fix is a lightweight package manager designed for building packages
directly from source and creating reproducible development environments.
Inspired by Nix, fix keeps the same declarative idea while using a simpler
and more approachable design.

* **Build from Source** — build packages directly from their source code
  instead of relying on pre-built system packages.
* **Declarative** — define the packages and dependencies required by an
  environment using a simple declarative specification.
* **Reproducible** — keep the environment definition in a file so it can be
  recreated consistently across systems.
* **Isolated Shells** — fix creates a new shell containing the packages
  defined by the current environment.
* **State Tracking** — environment state is recorded in a file, making
  configurations easy to inspect, version and share.
* **Nix Inspired** — fix takes inspiration from Nix while focusing on a
  smaller, simpler and easier-to-understand workflow.

## Simple by Design

Traditional package managers usually install packages directly into the
system environment. fix takes a different approach. A project describes
what it needs, and fix creates an environment based on that description.

The goal is not to reproduce every feature of Nix. Instead, fix focuses on
the core ideas of declarative environments, source-based builds and
reproducibility while keeping the implementation and user experience
simple.

## Declarative Environments

A fix environment describes the software required by a project. Instead of
manually installing every dependency, the environment can be defined once
and recreated when needed:

```yaml
packages:
  - gcc
  - make
  - git
  - curl

shell: development
```

## Enter the Environment

Once the environment is defined, fix builds the required packages and
starts a new shell with the requested software available:

```
$ fix shell

Building dependencies...
Building gcc...
Building make...
Building git...

Starting development shell...

$ gcc --version
$ git --version
```

## Why fix?

* **Less Complexity** — a smaller conceptual model makes fix easier to
  understand, configure and maintain.
* **Source First** — packages are built from source, giving developers more
  control over how software is produced.
* **Project Focused** — environments can be defined per project instead of
  modifying the global system environment.
* **Version Control Friendly** — environment definitions can live alongside
  project source code and be tracked with Git.

## A Lightweight Alternative

fix is designed for developers who want the benefits of declarative and
reproducible environments without adopting the full complexity of a large
package management system:

```
$ fix init

Creating environment...

$ fix install gcc

Building gcc from source...

$ fix shell

Entering fix environment...
```

## Get Involved

* **Source Code** — [GitHub Repository](https://github.com/gluedarwin/fixpkg):
  source code, issues and development.
* **Website** — [gluedarwin.github.io/fixpkg](https://gluedarwin.github.io/fixpkg/):
  the fix project page. Explore the
  [GlueDarwin project](https://gluedarwin.github.io) and related software.

---

## Install

**There is no prebuilt binary committed to this repo.** A fresh clone must
build one first (Linux or macOS; PyInstaller is pinned to 6.22.3 and is
installed into a local `.venv` by the script, never into the system Python):

```sh
bash scripts/build_binary.sh   # test → compile → re-test; produces ./fix
./fix --version                # self-contained binary, no Python needed
sudo make install              # optional: copy to /usr/local/bin
```

Or compile it yourself with Make (needs PyInstaller already installed):

```sh
make build        # PyInstaller --onefile --strip → ./fix
make test         # smoke suite against the source
make test-binary  # smoke suite against the compiled binary
make test-mini    # same suite on the fallback mini-YAML parser
```

**From source** (any Python ≥ 3.8; PyYAML is used when present and a
bundled mini-YAML parser is the zero-dependency fallback):

```sh
python3 fix.py --version
```

## Quickstart

```sh
mkdir demo && cd demo
cp -r /path/to/fix/vendor .      # bundled example repo (hello, banner, greet)

fix init                          # Creating environment... (writes fix.yml)
fix install hello banner          # Building hello from source...
fix shell                         # Starting <shell> shell... / Entering fix environment...
fix shell -- hello world          # or run one command inside the environment
exit

fix install greet                 # adds a package with a build-time dependency
fix tree                          # show the dependency graph
fix status                        # recorded state, hashes, sizes
fix list                          # all environments known to fix
fix remove greet banner           # refuses while deps remain
fix gc --dry-run                  # what would be collected?
fix gc                            # collect unreachable store entries
fix doctor                        # sanity-check toolchain & store
```

## Commands

```
init [--shell NAME] [pkg...]  create a new environment (scaffold fix.yml + vendor/)
install <pkg...>  add packages to fix.yml and build them from source
shell [-- cmd]    build dependencies, then start a new shell with them;
                  with `-- cmd` run cmd inside the environment instead
build [-f]        build every package in fix.yml; record fix.lock
remove <pkg...>   remove packages from fix.yml (refuses if others depend)
upgrade           rebuild every package from scratch; refresh state
list              list known environments
status            per-env recorded state: hashes, store paths, sizes
tree              dependency tree (deps before dependents, cycles detected)
search <term>     search repos ('.' lists all)
repo list         list configured repos (local/remote, cache status)
repo update [name]  re-fetch remote repos (or the named one)
path [pkg]        print store path(s) — scripting friendly
gc [--dry-run]    delete store entries unreachable from any environment
purge [-y]        remove ~/.fix entirely
doctor            check toolchain, store, permissions
help / version
```

Global options: `--file PATH` (use another environment file), `--no-color`,
`-v` (show build steps), `-q`, `--offline` (no network access: use cached
tarballs/repos only; also `FIX_OFFLINE=1`).

## The environment file (fix.yml)

```yaml
packages:
  - hello            # plain name — any version
  - banner           # optional pin:  - banner@1.0.0  or  - greet@^1.2

shell: development   # name used by `fix shell` (Starting development shell...)

# repos:             # extra spec dirs (vendor/ and ~/.fix/repo always searched)
#   extra: ../shared-pkgs
#   upstream: https://example.com/fix-repo.tar.gz#sha256=<64 hex>
```

Repo lookup order: `repos:` entries → `./vendor/` → `~/.fix/repo/`.
Spec files are `<repo>/<name>/fix.yml` or `<repo>/<name>.yml`.

Unknown top-level keys in `fix.yml` are preserved when fix rewrites the
file (e.g. on `fix install`); comments are not — fix parses the file and
writes it back from the parsed data, so `# ...` lines are lost on save.

## Remote repos

A `repos:` value that is a URL is a remote repo — a tarball whose
extracted tree is a repo directory, fetched on first use:

```yaml
repos:
  upstream: https://example.com/fix-repo.tar.gz#sha256=<64 hex>
```

The `#sha256=` fragment is **required**: a remote repo is never fetched
without a pinned checksum. The tarball is verified before extraction
and unpacked into a cache under `~/.fix/repos/<name>/` (with a
`.fix-repo.json` recording the pinned url and sha256), then used exactly
like a local repo — `fix install`, `build`, `shell`, `tree`, `search`
and `path` all see it. A cached repo with a matching sha256 is reused
without a network round-trip.

```sh
fix repo list             # name, kind (local/remote), location, cache status
fix repo update           # re-fetch all remote repos (verify + replace cache)
fix repo update upstream  # re-fetch just one
```

A failed re-fetch never replaces the working copy: the download is
verified and the extraction completes fully before the cache is swapped
in, so a bad checksum keeps the old repo and errors loudly.

Downloads live in a content-addressed cache under `~/.fix/cache/`:
a tarball whose sha256 matches is never re-downloaded. `--offline`
(or `FIX_OFFLINE=1`) forbids network access outright — cached
tarballs/repos are used, anything uncached fails with a clear error
saying it is unavailable offline. (`file://` URLs are local files,
not network, so they still work offline — and are still
checksum-verified.)

## Package recipes (fix.yml in the package dir)

```yaml
package:
  name: greet                     # lowercase, [a-z0-9._+-]
  version: 1.2.0
  description: Friendly greeter

depends:                          # constraints: "*" "1.2.0" "^1.2" "~1.2" ">=1,<2"
  banner: "*"                     # "~1.2" = same minor; "v1.2" also works; garbage is rejected

source:
  type: local                     # or: tarball (see below)
  # dir = "."                     # defaults to the spec's own directory
  # exclude = ["*.o"]             # plus built-in excludes (*.o, *.a, .git*, ...)

build:
  steps:                          # shell steps, run with bash -e
    - "bash gen.sh %(dep:banner)s %(out)s"

env:                              # exported inside `fix shell`
  GREET_BANNER: "%(out)s/share/greet/banner.txt"
```

A package can also be built from a tarball instead of a local
directory. The tarball is downloaded, checksum-verified and extracted
into a per-sha256 staging dir under `~/.fix/cache/extract/`; the build
then runs on the extracted tree exactly as for a local source:

```yaml
source:
  type: tarball
  url: https://example.com/hello-1.0.0.tar.gz
  sha256: <64 lowercase hex, REQUIRED — no checksum, no fetch>
  strip: 1        # leading path components to strip on extract, default 1
  # dir: .        # subdir inside the extracted tree, default "."
```

Supported formats: `.tar.gz`, `.tgz`, `.tar.bz2`, `.tar.xz`, `.tar`.
The url must be `https://` or `file://` (`http://` only for
`localhost`, for tests). Extraction is safe by construction: absolute
paths, `..` components, device files, hard links and symlinks escaping
the extraction dir are rejected, and the extracted tree is checked
again after unpacking. `strip` must be a non-negative integer.
```

Interpolations available in `steps` and `env`:

| token | expands to |
|---|---|
| `%(out)s` | output prefix — everything your build installs (also `$FIX_OUT`) |
| `%(src)s` | staged copy of the sources (also `$FIX_SRC`) |
| `%(name)s` / `%(version)s` | package name/version |
| `%(dep:NAME)s` | store path of dependency NAME (also `$FIX_DEP_NAME`) |

Build trust model: builds run in a private temp dir under
`~/.fix/build/`, with exactly the hashed input files (hash and build can
never diverge), dependency `bin/` dirs prepended to `PATH`, a global `flock`
so concurrent builds don't collide, `SOURCE_DATE_EPOCH` + `TZ=UTC` pinned,
a scrubbed build environment (nothing from the caller's environment leaks
in — no `CC`/`CFLAGS`/`PATH`/`HOME`), and the final output atomically
renamed into the store only after every step succeeds. Each store entry
keeps its own `.fix-meta.json` (hashes, deps, toolchain, sandbox used,
build log tail).

Isolation is best-effort, not a security boundary. On Linux, when
`bwrap` is installed, build steps run in a user/pid/network namespace
with the system read-only and only the build temp dir (plus dependency
store paths, read-only) visible. When `bwrap` is missing or cannot start
— and on macOS, where it does not exist — fix prints a warning once and
builds unisolated, with the full powers of your user account. **Only
build recipes you trust.** Use `--no-sandbox` on `fix build/install/shell`
to opt out of bwrap explicitly (for example, when it breaks a recipe).
`fix doctor` reports whether bwrap is available. Recipes are also
constrained at load time: `source.dir` must stay inside the package
directory, symlinks may not escape it, and `env:` keys that could hijack
the shell (`PATH`, `HOME`, `SHELL`, `LD_*`, `DYLD_*`, `FIX_*`, `IFS`,
`PYTHON*`, `BASH_ENV`, `ENV`) are rejected.

Fetching a tarball does **not** mean trusting it blindly. A remote
source is fetched only with a pinned sha256, the checksum is verified
before anything is extracted, extraction rejects unsafe members (see
above), and the build then runs under the same best-effort isolation
as a local recipe. The checksum pins the *bytes*, not the author:
a tarball whose build steps exfiltrate or damage your system will do
so exactly as a local recipe would. Only fetch tarballs and repos from
sources you trust — the sha256 proves the download is the one you
pinned, not that it is safe.

## The YAML subset

With PyYAML installed, `fix.yml` files accept full YAML. Without it
(or with `FIX_MINI_YAML=1`), the bundled mini parser handles the subset
fix itself writes: nested mappings, lists (including lists of mappings),
scalars, comments, and single-line flow values (`[hello, banner]`,
`{banner: "*"}`). Both parsers agree on that subset — the test suite
checks them against each other — and both reject duplicate keys,
unexpected indentation, and malformed flow instead of silently changing
meaning. Anything beyond the subset (anchors, multi-line strings, ...)
needs PyYAML installed.

## How the store works

```
~/.fix/store/f6873bfdab08-hello-1.0.0/     ← content-addressed: first 12 hex
    bin/hello                                  chars of the derivation hash
    .fix-meta.json                          ← name, version, dep hashes, log
~/.fix/build/                               ← transient build temp dirs
~/.fix/cache/                               ← verified tarballs (by sha256) + extractions
~/.fix/repos/<name>/                        ← extracted remote repos
~/.fix/state.json                           ← env registry = GC roots
~/.fix/state.json.bak                       ← last good state (auto-backup)
./fix.yml                                   ← what you WANT (declarative)
./fix.lock                                  ← what you HAVE (recorded state)
```

State is written atomically (temp file + `fsync` + rename), as are
`fix.lock`, `fix.yml` and `.fix-meta.json`, so a crash can never leave a
half-written file behind. If `state.json` is corrupt, `fix` restores the
last good state from `state.json.bak` and says so loudly; if no usable
backup exists, `fix gc` refuses to run rather than risk deleting live
store entries, and the corrupt bytes are preserved as
`state.json.corrupt-<timestamp>` before any fresh state is recorded.
`fix gc` takes the store lock for its whole run (including `--dry-run`,
which is side-effect-free: it never prunes or saves state), so it is
safe to run while a build is in flight — it waits for the build's lock.
`fix shell` never trusts `fix.lock` on timestamps: it re-resolves the
closure and re-derives every hash, rebuilding on any mismatch (edited
recipe/source, changed mode, missing or tampered store entry).

The derivation hash (schema `fix-derivation-v2`) feeds on: schema tag,
name, version, the source descriptor for tarball sources (type, url,
sha256, strip, dir), sha256 of every source file (sorted paths) **plus each
file's permission mode and each symlink's target**, empty directories,
the exact build steps, the `env:` mapping, the derivation hashes of all
dependencies, and a toolchain fingerprint (first lines of `cc --version`
and `bash --version`) — so any change anywhere in the dependency tree, a
`chmod`, a retargeted symlink, a different compiler, or a different
tarball url/checksum produces a new store path, while old entries remain
untouched until `fix gc`.
Reproducibility is therefore **per-toolchain, not cross-machine**: the
same recipe built with a different compiler gets a different hash
instead of a false "cached" hit.

> **Breaking change in v0.3.0:** the v2 hash differs from v0.2's v1 hash,
> so store entries built by v0.2 have different paths, become
> unreachable, and are collected by `fix gc`. Rebuild once after
> upgrading; `fix.lock` files are refreshed by the next `fix build`.

## Limitations (v0.4)

* No git sources yet — tarballs only (see "Remote repos" above).
* No signatures: a sha256 pins the bytes, not the author.
* One version per package name per repo.
* Linux/macOS focus; builds need `bash` + whatever the recipes invoke (`cc`…).
* Build isolation is best-effort (bwrap on Linux when available) and is
  not a security boundary — see "Build trust model" above. Without bwrap,
  recipes run unisolated as your user.
* Reproducibility is per-toolchain: the derivation hash includes a
  compiler fingerprint, so the same recipe hashes differently under a
  different `cc`/`bash`. File modes are also hashed, so a recipe checked
  out with a different umask rebuilds.
* No binary caches, rollbacks/profile generations (see roadmap).

### Roadmap ideas

binary cache (`fix push/pull`) · profile generations with `fix rollback` ·
per-env shims instead of PATH prepending · checksums/signatures for
remote specs

## Development

```sh
bash tests/smoke.sh                  # 94 end-to-end checks (isolated FIX_TEST_HOME)
python3 tests/test_hashing.py        # 42 unit checks (hashing, symlinks, env allowlist)
python3 tests/test_p0.py             # 53 unit checks (corrupt state, atomic writes, stale shell)
python3 tests/test_p1.py             # 165 unit checks (yaml parity, versions, cycles, gc, doctor, purge, shell, save fidelity, init)
python3 tests/test_remote.py         # 65 unit checks (tarballs, remote repos, offline; local http.server, no external network)
FIX_MINI_YAML=1 bash tests/smoke.sh  # same suite on the fallback parser
FIX_BIN=./fix bash tests/smoke.sh    # same suite against the binary
```

The entire manager is [`fix.py`](fix.py) — read it top to bottom:
utils → mini-YAML → specs/repos → resolver → store → staged builder →
lock/state recording → env composition & shell → commands.
The project website lives in [`docs/index.html`](docs/index.html) and is
served via GitHub Pages.

## License

BSD 3-Clause — see [LICENSE](LICENSE).
