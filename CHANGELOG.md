# Changelog

All notable changes to `fix` are recorded here. The format follows
Keep a Changelog; versions follow SemVer in spirit (the derivation hash
is part of the contract, so a hash change is a breaking change).

## 0.4.0 — Unreleased

Tarball sources and remote repos — the first roadmap feature. The
derivation schema stays `fix-derivation-v2`: tarball packages could
not exist before, so no store orphaning is introduced by this change,
and local packages hash exactly as in 0.3.0.

### Added

- Tarball package sources: `source: {type: tarball, url:, sha256:,
  strip:, dir:}` for `.tar.gz`, `.tgz`, `.tar.bz2`, `.tar.xz`, `.tar`.
  The sha256 is mandatory and verified before extraction; downloads
  are cached content-addressed under `~/.fix/cache/`, extractions are
  staged per-sha256 and reused; extraction rejects absolute paths,
  `..`, device files, hard links and escaping symlinks by hand (safe
  on Python >= 3.8, where `tarfile`'s `filter='data'` does not exist).
- Remote repos: a `repos:` value that is a URL
  (`https://…/repo.tar.gz#sha256=<hex>`, sha256 fragment required) is
  fetched, verified and extracted into `~/.fix/repos/<name>/`, then
  used like a local repo by `install`, `build`, `shell`, `tree`,
  `status`, `search` and `path`.
- `fix repo list` and `fix repo update [name]`; a failed update keeps
  the previous cached copy and errors loudly.
- Global `--offline` flag (and `FIX_OFFLINE=1`): no network access,
  cached tarballs/repos only. `file://` URLs are local files, so they
  still work offline (and are still checksum-verified).

### Fixed

- The bwrap sandbox never actually engaged on usr-merged systems
  (Debian/Ubuntu): the sandbox root starts empty, and the argv only
  bound `/usr` and `/etc`, so `/lib64` — the dynamic loader path —
  did not exist inside it and every build step failed to exec, making
  the probe fail and `fix` silently fall back to unisolated builds
  even with bwrap installed. The sandbox argv now recreates the
  usr-merge symlinks (`/lib`, `/lib64`, `/lib32`, `/libx32`, `/bin`,
  `/sbin`) and the probe falls back to a no-`/proc` sandbox when the
  host refuses to mount `/proc`, instead of giving up on isolation.
  New `tests/test_sandbox.py` (12 checks) covers the argv, the probe,
  and a real sandboxed build; the sandbox path has now executed live
  for the first time (bubblewrap 0.9.0 on Ubuntu 24.04).

### Changed

- The derivation hash now also feeds the source descriptor (url,
  sha256, strip, dir) for tarball sources, so changing the pinned
  tarball changes the store path.
- `repos:` values are now documented with `:` syntax
  (`upstream: https://…`); the `=` form in old comments was never
  valid YAML for either parser and is corrected.
- `fix help` lists `repo list` / `repo update` and `--offline`.

## 0.3.0 — Unreleased

**Breaking:** the derivation hash changed (schema `fix-derivation-v2`).
Store entries built by 0.2.0 have different paths and become unreachable;
`fix gc` collects them. Rebuilds after upgrading are expected.

### Added

- Best-effort build sandboxing: builds run under `bwrap` on Linux when it
  is available (user/pid/net namespaces, read-only system, only the build
  temp dir writable); otherwise fix warns once per run that it is building
  unisolated. `--no-sandbox` opts out explicitly; `fix doctor` reports
  bwrap availability.
- `fix init --shell NAME` writes the `shell:` name into the new `fix.yml`.
- `.github/workflows/ci.yml`: smoke suite, unit tests, and the binary
  build run in CI on Python 3.11 and 3.12.
- `CHANGELOG.md`, `CONTRIBUTING.md`, `.gitignore`.

### Changed

- Derivation hash now covers file modes, symlink targets, empty
  directories, and a `cc`/`bash` toolchain fingerprint — a `chmod +x` or a
  different compiler no longer gives a false "cached" hit. Reproducibility
  is per-toolchain, not cross-machine; the README's "Build trust model"
  section states the model honestly.
- Builds run with a scrubbed, deterministic environment instead of
  inheriting the caller's shell; spec `env:` keys that could hijack the
  shell (`PATH`, `HOME`, `SHELL`, `LD_*`, `FIX_*`, …) are rejected.
- `source.dir` and symlinks may not escape the package directory.
- `fix shell` re-resolves and re-derives every hash instead of trusting
  `fix.lock` on `fix.yml` mtime alone — edited recipes/sources are picked
  up without touching `fix.yml`.
- Corrupt `~/.fix/state.json` is never silently discarded: a last-good
  `state.json.bak` is kept, restored with a loud warning, and `fix gc`
  refuses when no usable backup exists. All lock/state/`fix.yml` writes
  are atomic (temp file + fsync + `os.replace`).
- Concurrent builds re-check the cache under the store lock; a lost
  rename race is treated as cached instead of crashing.
- `gc --dry-run` is fully side-effect-free and `gc` takes the store lock.
- `fix doctor` tests writability with a real temp file, creates nothing,
  and checks lock parseability and orphaned build dirs.
- `fix purge` refuses without `-y` when stdin is not a TTY.
- Version constraints are strict: leading `v` allowed, `~` and `==`
  supported, garbage rejected (prereleases such as `1.2.3-rc1` are now
  rejected loudly).
- The mini-YAML parser supports lists of mappings, flow values, quoted
  keys, and 4-space indent, and both parsers reject duplicate keys and
  bad indentation instead of silently changing meaning.
- The resolver's cycle error names the actual cycle path
  (`a -> b -> a`).
- `fix shell -- cmd` preserves quoting (`shlex.join` for multiple args);
  the nesting warning correctly names `FIX_ALLOW_NEST=1`.
- `fix.yml` unknown top-level keys survive a save; comments still do not
  (documented in the README). The backup of `fix.yaml` is `fix.yaml.bak`.
- `make install` is portable to macOS (`mkdir -p` + `install -m 755`);
  `make clean` also removes the compiled `./fix`. `scripts/build_binary.sh`
  pins PyInstaller 6.22.3 and installs it into a local `.venv` (PEP 668).
- `examples/fix.yml` works from the `examples/` directory via
  `repos: {shared: ../vendor}`.
- `vendor/hello` installs a real doc file (`HELLO_DOC` points at it);
  `vendor/greet` warns on stderr if its baked banner file cannot be opened.

### Fixed

- Wrong repository links (`github.com/gluedarwin/fix` → `fixpkg`).
- `fix search` crashed with a `TypeError` traceback on every query (its
  format string had five conversions and four arguments) and ignored
  `repos:` entries in `fix.yml` — it now prints results and respects the
  configured repos.
- `docs/index.html` had no `<!DOCTYPE html>`; it now has Install and
  Commands sections and shows the version.
- The README install section no longer implies a prebuilt binary is
  committed; a fresh clone must run `make build` /
  `bash scripts/build_binary.sh` first.
- Dead code: the unused `_interp_deps_env` wrapper and the always-true
  half of the "build produced output" check are gone.

## 0.2.0 — 2026-08-26

Initial public version (single commit, "adding the source").

- Single-file manager (`fix.py`, stdlib only): declarative `fix.yml`
  environments, content-addressed store in `~/.fix/store/`, `fix.lock`
  state tracking.
- Commands: `init`, `install`, `shell`, `build`, `remove`, `upgrade`,
  `list`, `status`, `tree`, `search`, `path`, `gc`, `purge`, `doctor`.
- Bundled mini-YAML parser as a zero-dependency fallback for PyYAML.
- Smoke suite (`tests/smoke.sh`) and a Makefile target to compile a
  standalone binary with PyInstaller.
