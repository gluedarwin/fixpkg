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

* **Source Code** — [GitHub Repository](https://github.com/gluedarwin/fix):
  source code, issues and development.
* **Website** — [gluedarwin.github.io/fixpkg](https://gluedarwin.github.io/fixpkg/):
  the fix project page. Explore the
  [GlueDarwin project](https://gluedarwin.github.io) and related software.

---

## Install

**Prebuilt standalone binary** (Linux x86_64, no Python needed) — this repo:

```sh
./fix --version          # self-contained binary
sudo make install        # optional: copy to /usr/local/bin
```

**From source** (any Python ≥ 3.8; PyYAML is used when present and a
bundled mini-YAML parser is the zero-dependency fallback):

```sh
python3 fix.py --version
```

**Compile it yourself:**

```sh
make build        # PyInstaller --onefile --strip → ./fix
make test         # smoke suite against the source
make test-binary  # smoke suite against the compiled binary
make test-mini    # same suite on the fallback mini-YAML parser
# or: bash scripts/build_binary.sh   (test → compile → re-test)
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
init [pkg...]     create a new environment (scaffold fix.yml + vendor/)
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
path [pkg]        print store path(s) — scripting friendly
gc [--dry-run]    delete store entries unreachable from any environment
purge [-y]        remove ~/.fix entirely
doctor            check toolchain, store, permissions
help / version
```

Global options: `--file PATH` (use another environment file), `--no-color`,
`-v` (show build steps), `-q`.

## The environment file (fix.yml)

```yaml
packages:
  - hello            # plain name — any version
  - banner           # optional pin:  - banner@1.0.0  or  - greet@^1.2

shell: development   # name used by `fix shell` (Starting development shell...)

# repos:             # extra spec dirs (vendor/ and ~/.fix/repo always searched)
#   extra = ../shared-pkgs
```

Repo lookup order: `repos:` entries → `./vendor/` → `~/.fix/repo/`.
Spec files are `<repo>/<name>/fix.yml` or `<repo>/<name>.yml`.

## Package recipes (fix.yml in the package dir)

```yaml
package:
  name: greet                     # lowercase, [a-z0-9._+-]
  version: 1.2.0
  description: Friendly greeter

depends:                          # constraints: "*" "1.2.0" "^1.2" ">=1,<2"
  banner: "*"

source:
  type: local                     # v0.2: local dirs only (git/url on roadmap)
  # dir = "."                     # defaults to the spec's own directory
  # exclude = ["*.o"]             # plus built-in excludes (*.o, *.a, .git*, ...)

build:
  steps:                          # shell steps, run with bash -e
    - "bash gen.sh %(dep:banner)s %(out)s"

env:                              # exported inside `fix shell`
  GREET_BANNER: "%(out)s/share/greet/banner.txt"
```

Interpolations available in `steps` and `env`:

| token | expands to |
|---|---|
| `%(out)s` | output prefix — everything your build installs (also `$FIX_OUT`) |
| `%(src)s` | staged copy of the sources (also `$FIX_SRC`) |
| `%(name)s` / `%(version)s` | package name/version |
| `%(dep:NAME)s` | store path of dependency NAME (also `$FIX_DEP_NAME`) |

Build sandbox guarantees: builds run in a private temp dir under
`~/.fix/build/`, with exactly the hashed input files (hash and build can
never diverge), dependency `bin/` dirs prepended to `PATH`, a global `flock`
so concurrent builds don't collide, `SOURCE_DATE_EPOCH` + `TZ=UTC` pinned,
and the final output atomically renamed into the store only after every
step succeeds. Each store entry keeps its own `.fix-meta.json` (hashes,
deps, build log tail).

## How the store works

```
~/.fix/store/f6873bfdab08-hello-1.0.0/     ← content-addressed: first 12 hex
    bin/hello                                  chars of the derivation hash
    .fix-meta.json                          ← name, version, dep hashes, log
~/.fix/build/                               ← transient build sandboxes
~/.fix/state.json                           ← env registry = GC roots
./fix.yml                                   ← what you WANT (declarative)
./fix.lock                                  ← what you HAVE (recorded state)
```

The derivation hash feeds on: schema tag, name, version, sha256 of every
source file (sorted paths), the exact build steps, the `env:` mapping, and
the derivation hashes of all dependencies — so any change anywhere in the
dependency tree produces a new store path while old entries remain
untouched until `fix gc`.

## Limitations (v0.2)

* Local repos only — no fetching tarballs/git over the network yet.
* One version per package name per repo.
* Linux/macOS focus; builds need `bash` + whatever the recipes invoke (`cc`…).
* No binary caches, signatures, rollbacks/profile generations (see roadmap).

### Roadmap ideas

remote repos & tarball sources · binary cache (`fix push/pull`) · profile
generations with `fix rollback` · per-env shims instead of PATH prepending ·
checksums/signatures for remote specs

## Development

```sh
bash tests/smoke.sh                  # 43 end-to-end checks (isolated FIX_TEST_HOME)
FIX_MINI_YAML=1 bash tests/smoke.sh  # same suite on the fallback parser
FIX_BIN=./fix bash tests/smoke.sh    # same suite against the binary
```

The entire manager is [`fix.py`](fix.py) — read it top to bottom:
utils → mini-YAML → specs/repos → resolver → store → sandbox builder →
lock/state recording → env composition & shell → commands.
The project website lives in [`docs/index.html`](docs/index.html) and is
served via GitHub Pages.

## License

BSD 3-Clause — see [LICENSE](LICENSE).
