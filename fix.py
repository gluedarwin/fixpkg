#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix — a simple, source-based and declarative package manager.

    $ fix init                  # Creating environment... (scaffolds fix.yml)
    $ fix install hello banner  # Building hello from source...
    $ fix shell                 # Starting development shell...

fix is a lightweight package manager designed for building packages
directly from source and creating reproducible development environments.
Inspired by Nix, fix keeps the same declarative idea while using a simpler
and more approachable design (https://gluedarwin.github.io/fixpkg/):

  * Build from source  — package recipes are shell steps run in a sandbox,
                         output staged into $FIX_OUT then atomically installed.
  * Declarative        — the environment is a simple YAML file (fix.yml):

                             packages:
                               - gcc
                               - make

                             shell: development

  * Reproducible       — every build lands in ~/.fix/store/<hash>-<name>-<version>/
                         where <hash> is derived from the package sources, build
                         steps and the hashes of its dependencies. Identical
                         inputs => identical path => the build is skipped.
  * Isolated shells    — `fix shell` builds dependencies, then execs $SHELL with
                         PATH/ENV composed from the resolved closure.
  * State tracking     — each project keeps a fix.lock recording the resolved
                         world; ~/.fix/state.json registers environments and
                         acts as the garbage-collector root set.

The whole package manager is this one readable file (~1.6k lines, stdlib only).
"""

import argparse
import difflib
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import yaml as _pyyaml  # optional; bundled mini parser is the fallback
except ImportError:  # pragma: no coverage — mini parser takes over
    _pyyaml = None

__version__ = "0.2.0"

FIX_HOME_DEFAULT = "~/.fix"
FIXFILE_NAME = "fix.yml"
FIXFILE_ALT = "fix.yaml"
LOCK_NAME = "fix.lock"
DERIVATION_SCHEMA = "fix-derivation-v1"
LOCK_SCHEMA = 1
STATE_SCHEMA = 1
# Fixed timestamps/locale => reproducible archives & compiler output.
SOURCE_DATE_EPOCH = "946684800"  # 2000-01-01T00:00:00Z


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------

class FixError(Exception):
    """Base class for all user-facing fix errors."""


class SpecError(FixError):
    pass


class ResolveError(FixError):
    pass


class BuildError(FixError):
    pass


class LockError(FixError):
    pass


# ---------------------------------------------------------------------------
# terminal colors / output helpers
# ---------------------------------------------------------------------------

class Colors:
    def __init__(self, enabled):
        self.enabled = enabled

    def _c(self, code, s):
        if not self.enabled:
            return str(s)
        return "\033[%sm%s\033[0m" % (code, s)

    def green(self, s):
        return self._c("32", s)

    def red(self, s):
        return self._c("31", s)

    def yellow(self, s):
        return self._c("33", s)

    def cyan(self, s):
        return self._c("36", s)

    def bold(self, s):
        return self._c("1", s)

    def dim(self, s):
        return self._c("2", s)


C = Colors(enabled=sys.stdout.isatty() and os.environ.get("NO_COLOR") is None)


def info(msg):
    print("%s %s" % (C.dim("fix:"), msg))


def ok(msg):
    print("%s %s" % (C.green("✔"), msg))


def warn(msg):
    print("%s %s" % (C.yellow("⚠"), msg), file=sys.stderr)


def die(msg, code=1):
    print("%s %s" % (C.red("✘"), msg), file=sys.stderr)
    sys.exit(code)


def human_size(n):
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return ("%d" % n) if unit == "B" else ("%.1f%s" % (n, unit))
        n /= 1024.0


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def relhome(p):
    """Render a path relative to HOME for pretty printing."""
    home = str(Path.home())
    p = str(p)
    if p.startswith(home + "/"):
        return "~" + p[len(home):]
    return p


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


# ---------------------------------------------------------------------------
# YAML — PyYAML when available, tiny bundled parser otherwise + an emitter.
# fix only needs a small subset: nested mappings, string lists, scalars and
# comments — exactly the shapes written by fix itself (fix.yml, package specs).
# ---------------------------------------------------------------------------

def _yaml_strip_comment(line):
    """Drop a trailing '# ...' comment that lives outside quotes."""
    out, quote = [], ""
    i = 0
    while i < len(line):
        c = line[i]
        if quote:
            out.append(c)
            if c == "\\" and quote == '"' and i + 1 < len(line):
                out.append(line[i + 1])
                i += 2
                continue
            if c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
            out.append(c)
        elif c == "#" and (not out or out[-1] in " \t"):
            break
        else:
            out.append(c)
        i += 1
    return "".join(out).rstrip()


def _yaml_scalar(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        body = s[1:-1]
        if s[0] == '"':
            body = body.replace('\\"', '"').replace("\\\\", "\\")
            body = body.replace("\\n", "\n").replace("\\t", "\t")
        else:  # YAML single quotes: '' is an escaped single quote
            body = body.replace("''", "'")
        return body
    low = s.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~", ""):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _yaml_lines(text):
    items = []
    for raw in text.splitlines():
        line = _yaml_strip_comment(raw.replace("\t", "  "))
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        items.append((indent, line.strip()))
    return items


def _yaml_block(items, i, indent):
    """Parse the block starting at items[i] with the given indentation."""
    if i >= len(items) or items[i][0] < indent:
        return None, i
    if items[i][1] == "-" or items[i][1].startswith("- "):
        out = []
        while i < len(items) and items[i][0] == indent and \
                (items[i][1] == "-" or items[i][1].startswith("- ")):
            content = "" if items[i][1] == "-" else items[i][1][2:].strip()
            i += 1
            if not content:
                val, i = _yaml_block(items, i, indent + 2)
                out.append(val)
            else:
                out.append(_yaml_scalar(content))
        return out, i
    out = {}
    while i < len(items):
        ind, content = items[i]
        if ind < indent or content == "-" or content.startswith("- "):
            break
        if ind > indent:  # defensive: skip unexpected deeper line
            i += 1
            continue
        m = re.match(r"^([^\s:][^:]*):(?:\s+(.*))?$", content)
        if not m:
            raise SpecError("yaml: cannot parse line: %r" % content)
        key = m.group(1).strip()
        rest = (m.group(2) or "").strip()
        i += 1
        if rest:
            out[key] = _yaml_scalar(rest)
        elif i < len(items) and items[i][0] > indent:
            out[key], i = _yaml_block(items, i, items[i][0])
        else:
            out[key] = None
    return out, i


def _mini_yaml_loads(text):
    value, _ = _yaml_block(_yaml_lines(text), 0, 0)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SpecError("yaml: top level must be a mapping")
    return value


def yaml_loads(text):
    if _pyyaml is None or os.environ.get("FIX_MINI_YAML"):
        return _mini_yaml_loads(text)
    return _pyyaml.safe_load(text) or {}


def yaml_load_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml_loads(f.read())


def _yaml_fmt(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if not isinstance(v, str):
        raise FixError("cannot serialize %r to YAML" % (v,))
    if v == "" or re.search(r"[:#\"'\n\t]|^\s|\s$|^-|\?$|^\d", v) or \
            v.lower() in ("true", "false", "null", "yes", "no", "~"):
        return '"%s"' % v.replace("\\", "\\\\").replace('"', '\\"')
    return v


def yaml_dumps(data):
    """Serialize nested dicts/lists to the YAML subset fix understands —
    the output shape matches the website's example exactly:

        packages:
          - gcc
          - make

        shell: development
    """
    lines = []

    def emit(key, value, indent):
        pad = " " * indent
        if isinstance(value, dict):
            lines.append("%s%s:" % (pad, key))
            for k, v in value.items():
                emit(k, v, indent + 2)
        elif isinstance(value, (list, tuple)):
            if not value:
                lines.append("%s%s: []" % (pad, key))
            else:
                lines.append("%s%s:" % (pad, key))
                for item in value:
                    lines.append("%s  - %s" % (pad, _yaml_fmt(item)))
        elif value is None:
            lines.append("%s%s:" % (pad, key))
        else:
            lines.append("%s%s: %s" % (pad, key, _yaml_fmt(value)))

    for k, v in data.items():
        emit(k, v, 0)
    return "\n".join(lines) + "\n"



# ---------------------------------------------------------------------------
# version constraints ("*", "1.2.0", "1.*", "^1.2", ">=1.2,<2")
# ---------------------------------------------------------------------------

def _vtup(v):
    parts = []
    for x in str(v).strip().split("."):
        if x == "*" or x == "x" or x == "":
            parts.append(0)
        else:
            m = re.match(r"^(\d+)", x)
            parts.append(int(m.group(1)) if m else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def version_match(constraint, version):
    """True if `version` satisfies `constraint` (comma-separated clauses)."""
    c = str(constraint).strip()
    if c in ("*", "", "x", "any"):
        return True
    for clause in [x.strip() for x in c.split(",") if x.strip()]:
        if clause.startswith("^"):
            base = _vtup(clause[1:])
            v = _vtup(version)
            if base[0] > 0:
                lo, hi = base, (base[0] + 1, 0, 0)
            elif base[1] > 0:
                lo, hi = base, (0, base[1] + 1, 0)
            else:
                lo, hi = base, (0, 0, base[2] + 1)
            if not (lo <= v < hi):
                return False
        elif clause.startswith(">="):
            if not _vtup(version) >= _vtup(clause[2:]):
                return False
        elif clause.startswith("<="):
            if not _vtup(version) <= _vtup(clause[2:]):
                return False
        elif clause.startswith(">"):
            if not _vtup(version) > _vtup(clause[1:]):
                return False
        elif clause.startswith("<"):
            if not _vtup(version) < _vtup(clause[1:]):
                return False
        elif clause.endswith(".*"):
            prefix = _vtup(clause[:-2])
            v = _vtup(version)
            depth = 1 if clause.count("*") == 1 and "." not in clause[:-2] else 2
            if clause[:-2].count(".") >= 1:
                depth = 2
            if v[:depth] != prefix[:depth]:
                return False
        elif clause.startswith("="):
            if _vtup(version) != _vtup(clause.lstrip("=")):
                return False
        else:
            if _vtup(version) != _vtup(clause):
                return False
    return True


# ---------------------------------------------------------------------------
# package specifications (specs)
# ---------------------------------------------------------------------------

NAME_RE = re.compile(r"^[a-z][a-z0-9._+-]*$")
VERSION_RE = re.compile(r"^[0-9A-Za-z.+-]+$")
INTERP_RE = re.compile(r"%\((out|src|name|version|dep:[A-Za-z0-9._+-]+)\)s")

DEFAULT_EXCLUDES = ["*.o", "*.a", "*.so", "*.pyc", ".git*", ".fix*"]


class Spec:
    """A parsed package recipe (fix.yml)."""

    def __init__(self, name, version, description, depends, source_dir,
                 excludes, steps, env, spec_path, repo):
        self.name = name
        self.version = str(version)
        self.description = description
        self.depends = depends          # {name: constraint}
        self.source_dir = source_dir    # Path with the sources to copy
        self.excludes = excludes
        self.steps = steps              # list of shell commands
        self.env = env                  # {VAR: value} exported in fix shell
        self.spec_path = spec_path
        self.repo = repo

    # -- parsing -----------------------------------------------------------

    @classmethod
    def from_file(cls, spec_path, repo):
        spec_path = Path(spec_path)
        data = yaml_load_file(spec_path)
        pkg = data.get("package")
        if not isinstance(pkg, dict):
            raise SpecError("%s: missing 'package:' mapping" % spec_path)
        name = pkg.get("name")
        version = pkg.get("version")
        if not name or not NAME_RE.match(str(name)):
            raise SpecError("%s: package.name must match %s (got %r)"
                            % (spec_path, NAME_RE.pattern, name))
        if version is None or not VERSION_RE.match(str(version)):
            raise SpecError("%s: package.version must look like 1.2.3 (got %r)"
                            % (spec_path, version))
        depends = data.get("depends") or {}
        if not isinstance(depends, dict):
            raise SpecError("%s: 'depends:' must be a name: constraint mapping" % spec_path)
        for dep, con in depends.items():
            if not NAME_RE.match(str(dep)):
                raise SpecError("%s: bad dependency name %r" % (spec_path, dep))
        source = data.get("source") or {}
        if not isinstance(source, dict):
            raise SpecError("%s: 'source:' must be a mapping" % spec_path)
        if str(source.get("type", "local")) != "local":
            raise SpecError("%s: only source.type: local is supported in v%s "
                            "(git/url planned — see README roadmap)" % (spec_path, __version__))
        src_dir = spec_path.parent / str(source.get("dir", "."))
        if not src_dir.is_dir():
            raise SpecError("%s: source dir %s does not exist" % (spec_path, src_dir))
        excludes = list(source.get("exclude") or [])
        build = data.get("build") or {}
        steps = build.get("steps")
        if not steps or not isinstance(steps, list):
            raise SpecError("%s: build.steps is a required list of shell commands" % spec_path)
        env = data.get("env") or {}
        if not isinstance(env, dict):
            raise SpecError("%s: 'env:' must be a VAR: value mapping" % spec_path)
        return cls(str(name), version, str(pkg.get("description") or ""),
                   {str(k): str(v) for k, v in depends.items()},
                   src_dir.resolve(), DEFAULT_EXCLUDES + excludes,
                   [str(s) for s in steps], {str(k): str(v) for k, v in env.items()},
                   spec_path, repo)

    # -- source hashing (also the exact set of files copied into builds) ----

    def source_files(self):
        """{relative/path: sha256} for every file that enters a build."""
        out = {}
        for path in sorted(self.source_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.source_dir).as_posix()
            if path.name in ("fix.yml", "fix.yaml") and path.parent == self.source_dir:
                continue
            if any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(path.name, pat)
                   for pat in self.excludes):
                continue
            out[rel] = sha256_file(path)
        return out

    def interp(self, text, out, deps):
        """Expand %(out)s %(src)s %(name)s %(version)s %(dep:NAME)s."""
        ctx = {"out": str(out), "src": str(self.source_dir),
               "name": self.name, "version": self.version}

        def repl(m):
            key = m.group(1)
            if key.startswith("dep:"):
                dep = key[4:]
                if dep not in deps:
                    raise BuildError("%s: '%s' used in build but is not a declared "
                                     "dependency of %s" % (self.spec_path, key, self.name))
                return str(deps[dep])
            return ctx[key]

        return INTERP_RE.sub(repl, text)


class Repo:
    """A local directory of specs: <repo>/<name>/fix.yml or <repo>/<name>.yml."""

    def __init__(self, name, path):
        self.name = name
        self.path = Path(path).expanduser()
        if not self.path.is_absolute():
            self.path = self.path.resolve()
        self._cache = None

    def exists(self):
        return self.path.is_dir()

    def _scan(self):
        if self._cache is None:
            self._cache = {}
            if self.exists():
                for child in sorted(self.path.iterdir()):
                    if child.is_dir():
                        for spec_name in ("fix.yml", "fix.yaml"):
                            f = child / spec_name
                            if f.is_file() and child.name not in self._cache:
                                self._cache[child.name] = f
                    elif child.is_file() and child.suffix in (".yml", ".yaml"):
                        self._cache[child.stem] = child
        return self._cache

    def find(self, pkg):
        return self._scan().get(pkg)

    def names(self):
        return sorted(self._scan().keys())


def open_repos(fixfile):
    """Ordered repo list from the fixfile, plus conventional fallbacks."""
    repos = []
    seen = set()

    def add(name, path):
        key = str(Path(path).expanduser().resolve() if Path(path).is_absolute()
                  else (fixfile.dir / path).resolve())
        if key not in seen and Path(key).is_dir():
            seen.add(key)
            repos.append(Repo(name, key))

    for name, path in fixfile.repos.items():
        add(name, path)
    add("vendor", fixfile.dir / "vendor")
    add("user", Path.home() / ".fix" / "repo")
    return repos


def find_spec(repos, name):
    for repo in repos:
        p = repo.find(name)
        if p:
            return Spec.from_file(p, repo.name)
    available = []
    for repo in repos:
        available.extend(repo.names())
    hint = ""
    close = difflib.get_close_matches(name, available, n=3)
    if close:
        hint = " (did you mean: %s?)" % ", ".join(close)
    elif available:
        hint = " (available here: %s)" % ", ".join(available[:12])
    raise ResolveError("package '%s' not found in any repo%s" % (name, hint))


# ---------------------------------------------------------------------------
# fix.yml — the per-project declarative environment file (YAML)
# ---------------------------------------------------------------------------

INIT_TEMPLATE = """\
# fix.yml — declarative environment for fix
# https://github.com/gluedarwin/fix · `fix help` for commands
#
# packages: software this environment needs (optionally name@version)
# shell:    name of the shell fix starts (`fix shell`)

packages:
%(packages)s
%(shell)s
# repos:                    # extra spec dirs (vendor/ is always searched)
#   extra = ../shared-pkgs
"""


def _parse_pkg_entry(entry):
    """'gcc' -> ('gcc', '*');  'gcc@^1.2' -> ('gcc', '^1.2')."""
    entry = str(entry).strip()
    if "@" in entry:
        name, _, con = entry.partition("@")
        return name.strip(), (con.strip() or "*")
    return entry, "*"


def _fmt_pkg_entry(name, constraint):
    return name if constraint in ("*", "") else "%s@%s" % (name, constraint)


class Fixfile:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.dir = self.path.parent
        self.shell_name = None      # `shell:` key — None means unnamed
        self.repos = {}
        self.packages = {}          # {name: constraint}, insertion-ordered

    @classmethod
    def locate(cls, path):
        """Accept fix.yml, fix.yaml — resolve to the one that exists."""
        p = Path(path).expanduser()
        if p.suffix in (".yml", ".yaml") or p.is_file():
            return cls(p)
        for cand in (p / FIXFILE_NAME if p.is_dir() else p,
                     (p / FIXFILE_ALT) if p.is_dir() else p):
            if Path(cand).is_file():
                return cls(cand)
        return cls(p if not p.is_dir() else p / FIXFILE_NAME)

    def load(self):
        if not self.path.is_file():
            alt = self.path.parent / FIXFILE_ALT
            if alt.is_file():
                self.path = alt
            else:
                raise FixError("no %s in %s — run `fix init` first"
                               % (FIXFILE_NAME, self.dir))
        data = yaml_load_file(self.path)
        if not isinstance(data, dict):
            raise FixError("%s: top level must be a YAML mapping" % self.path)
        shell = data.get("shell")
        self.shell_name = str(shell) if shell not in (None, "") else None
        repos = data.get("repos") or {}
        if not isinstance(repos, dict):
            raise FixError("%s: 'repos:' must be a name: path mapping" % self.path)
        self.repos = {str(k): str(v) for k, v in repos.items()}
        pkgs = data.get("packages") or []
        if isinstance(pkgs, str) or not isinstance(pkgs, (list, dict)):
            raise FixError("%s: 'packages:' must be a list" % self.path)
        self.packages = {}
        if isinstance(pkgs, dict):  # tolerant: name: constraint mapping
            for k, v in pkgs.items():
                self.packages[str(k)] = str(v or "*")
        else:
            for entry in pkgs:
                name, con = _parse_pkg_entry(entry)
                if name and name not in self.packages:
                    self.packages[name] = con
        return self

    def save(self):
        if self.path.exists():
            bak = self.path.with_suffix(".yml.bak")
            shutil.copy2(self.path, bak)
        data = {"packages": [_fmt_pkg_entry(n, c)
                             for n, c in self.packages.items()]}
        if self.repos and self.repos != {"vendor": "vendor"}:
            data["repos"] = dict(self.repos)
        if self.shell_name:
            data["shell"] = self.shell_name
        self.path.write_text(yaml_dumps(data), encoding="utf-8")

    @classmethod
    def create(cls, path, shell_name, packages):
        ff = cls(path)
        ff.shell_name = shell_name
        ff.packages = {p: "*" for p in packages}
        ff.repos = {}
        body = INIT_TEMPLATE % {
            "packages": "\n".join("  - %s" % p for p in packages) or
                        "  # - hello\n  # - banner",
            "shell": ("shell: %s" % shell_name) if shell_name else "",
        }
        ff.path.parent.mkdir(parents=True, exist_ok=True)
        ff.path.write_text(body, encoding="utf-8")
        return ff


# ---------------------------------------------------------------------------
# resolver — dependency closure + topological order (deps first)
# ---------------------------------------------------------------------------

def resolve(fixfile, repos, wanted=None):
    """Returns (order, specs) where order lists dependencies before dependents."""
    wanted = dict(fixfile.packages if wanted is None else wanted)
    specs = {}

    def visit(name, chain, constraint="*"):
        if name in specs:
            if not version_match(constraint, specs[name].version):
                raise ResolveError("dependency conflict: %s requires %s@%s but "
                                   "closure pins %s" % ("→".join(chain) or name,
                                                        name, constraint,
                                                        specs[name].version))
            return
        if name in chain:
            raise ResolveError("dependency cycle: %s→%s" % ("→".join(chain), name))
        spec = find_spec(repos, name)
        if not version_match(constraint, spec.version):
            raise ResolveError("package %s is version %s but constraint is %s "
                               "(from %s)" % (name, spec.version, constraint,
                                              "→".join(chain) or "fixfile"))
        specs[name] = spec
        for dep, con in spec.depends.items():
            visit(dep, chain + [name], con)

    for name, con in wanted.items():
        visit(name, [], con)

    # Kahn topological sort, stable by name for deterministic output.
    order, pending = [], {n: set(s.depends) & set(specs) for n, s in specs.items()}
    while pending:
        ready = sorted(n for n, d in pending.items() if not d)
        if not ready:
            raise ResolveError("dependency cycle among: %s" % ", ".join(pending))
        for n in ready:
            order.append(n)
            del pending[n]
        for d in pending.values():
            d.difference_update(ready)
    return order, specs


# ---------------------------------------------------------------------------
# store — ~/.fix/store/<hash>-<name>-<version>, with locking & GC
# ---------------------------------------------------------------------------

def fix_home():
    if os.environ.get("FIX_TEST_HOME"):
        p = Path(os.environ["FIX_TEST_HOME"]).expanduser()
    else:
        p = Path(FIX_HOME_DEFAULT).expanduser()
    return p


class Store:
    def __init__(self, root=None):
        self.root = Path(root) if root else fix_home() / "store"
        self.build_dir = self.root.parent / "build"
        self.state_path = self.root.parent / "state.json"

    def ensure(self):
        for d in (self.root, self.build_dir):
            d.mkdir(parents=True, exist_ok=True)

    def entry(self, deriv_hash, spec):
        safe = re.sub(r"[^A-Za-z0-9._+-]", "_", "%s-%s" % (spec.name, spec.version))
        return self.root / ("%s-%s" % (deriv_hash[:12], safe))

    def entries(self):
        if not self.root.is_dir():
            return []
        return sorted(p for p in self.root.iterdir() if p.is_dir())

    def total_size(self):
        return sum(dir_size(e) for e in self.entries())

    def lock(self):
        """Global mutex so concurrent fix processes don't corrupt the store."""
        self.ensure()
        f = open(self.root.parent / ".lock", "a+")
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        return f

    def gc(self, roots, dry_run=False):
        """Delete store entries whose basename is not in `roots`."""
        removed, freed = [], 0
        for entry in self.entries():
            if entry.name in roots:
                continue
            size = dir_size(entry)
            removed.append((entry, size))
            freed += size
            if not dry_run:
                shutil.rmtree(entry, ignore_errors=True)
        return removed, freed


def derivation_hash(spec, dep_hashes, source_files):
    """Deterministic hash over the full build inputs (the 'derivation')."""
    h = hashlib.sha256()

    def feed(s):
        h.update(s.encode("utf-8"))
        h.update(b"\x00")

    feed(DERIVATION_SCHEMA)
    feed("name=%s" % spec.name)
    feed("version=%s" % spec.version)
    for rel in sorted(source_files):
        feed("src:%s=%s" % (rel, source_files[rel]))
    feed("build=%s" % json.dumps(spec.steps, sort_keys=True))
    feed("env=%s" % json.dumps(spec.env, sort_keys=True))
    for dep in sorted(dep_hashes):
        feed("dep:%s=%s" % (dep, dep_hashes[dep]))
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# builder — sandboxed build, then atomic install into the store
# ---------------------------------------------------------------------------

class BuildResult:
    def __init__(self, spec, deriv_hash, path, cached, seconds, log):
        self.spec = spec
        self.hash = deriv_hash
        self.path = path
        self.cached = cached
        self.seconds = seconds
        self.log = log


def _interp_deps_env(spec, out, deps):
    """Mapping used both for %-interpolation and FIX_DEP_* variables."""
    return {name: str(path) for name, path in deps.items()}


def build_package(store, spec, dep_results, force=False, verbose=False):
    """Build one package (deps already built). Returns BuildResult."""
    dep_hashes = {n: r.hash for n, r in dep_results.items()}
    dep_paths = {n: r.path for n, r in dep_results.items()}
    source_files = spec.source_files()
    dhash = derivation_hash(spec, dep_hashes, source_files)
    target = store.entry(dhash, spec)

    if target.is_dir() and not force:
        return BuildResult(spec, dhash, target, cached=True, seconds=0.0, log=[])

    store.ensure()
    lockf = store.lock()
    tmp = Path(tempfile.mkdtemp(prefix="%s-" % spec.name, dir=store.build_dir))
    try:
        src = tmp / "src"
        out = tmp / "out"
        src.mkdir()
        for sub in ("bin", "lib", "share", "etc"):
            (out / sub).mkdir(parents=True, exist_ok=True)

        # copy exactly the hashed inputs — hash and build can never diverge
        for rel in sorted(source_files):
            dst = src / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(spec.source_dir / rel, dst)

        env = dict(os.environ)
        env.update({
            "FIX_OUT": str(out),
            "FIX_SRC": str(src),
            "FIX_ENTRY": str(target),  # final store path FIX_OUT becomes
            "FIX_STORE": str(store.root),
            "FIX_PKG": spec.name,
            "SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH,
            "TZ": "UTC",
        })
        path_pre = []
        for dep, dpath in sorted(dep_paths.items()):
            env["FIX_DEP_%s" % re.sub(r"[^A-Za-z0-9]", "_", dep.upper())] = str(dpath)
            if (dpath / "bin").is_dir():
                path_pre.append(str(dpath / "bin"))
        if path_pre:
            env["PATH"] = ":".join(path_pre) + ":" + env.get("PATH", "")

        log = []
        t0 = time.time()
        for i, raw_step in enumerate(spec.steps, 1):
            step = spec.interp(raw_step, out, dep_paths)
            cmd = "umask 022; set -e; set -o pipefail; %s" % step
            if verbose:
                print("  %s %s" % (C.dim("$"), C.dim(step)))
            proc = subprocess.run(["bash", "-c", cmd], cwd=str(src), env=env,
                                  text=True, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT)
            output = (proc.stdout or "").rstrip()
            log.append("$ %s\n%s" % (step, output))
            if proc.returncode != 0:
                tail = "\n".join(output.splitlines()[-25:])
                raise BuildError("build of %s-%s failed at step %d/%d "
                                 "(exit %d):\n%s%s"
                                 % (spec.name, spec.version, i, len(spec.steps),
                                    proc.returncode,
                                    ("    " + "\n    ".join(tail.splitlines()) + "\n")
                                    if tail else "",
                                    "  in sandbox %s" % relhome(tmp)))

        # sanity: did the build actually install anything?
        produced = any(out.rglob("*") for _ in [0]) and \
            any(p.is_file() and not p.name.startswith(".fix-")
                for p in out.rglob("*"))
        if not produced:
            raise BuildError("build of %s-%s produced no output — build steps "
                             "must install files into %%(out)s ($FIX_OUT)"
                             % (spec.name, spec.version))

        meta = {
            "schema": DERIVATION_SCHEMA,
            "name": spec.name,
            "version": spec.version,
            "hash": dhash,
            "deps": {n: r.hash for n, r in sorted(dep_results.items())},
            "spec": str(spec.spec_path),
            "repo": spec.repo,
            "built_at": now_iso(),
            "build_seconds": round(time.time() - t0, 3),
            "log_tail": log[-3:],
        }
        (out / ".fix-meta.json").write_text(json.dumps(meta, indent=2),
                                            encoding="utf-8")

        if target.is_dir() and force:
            shutil.rmtree(target)
        os.rename(str(out), str(target))
        seconds = time.time() - t0
        return BuildResult(spec, dhash, target, cached=False, seconds=seconds,
                           log=log)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)
            lockf.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# state recording — fix.lock per project + ~/.fix/state.json globally
# ---------------------------------------------------------------------------

class Lock:
    """The per-project state file (fix.lock) recording the resolved world."""

    def __init__(self, fixfile):
        self.path = fixfile.dir / LOCK_NAME
        self.fixfile = fixfile
        self.data = None

    def load(self):
        if not self.path.is_file():
            return None
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise LockError("%s is corrupt (%s) — run `fix build` to regenerate"
                            % (self.path, e))
        return self.data

    def write(self, order, results):
        now = now_iso()
        prev = {}
        if self.data:
            prev = self.data.get("packages", {})
        packages = {}
        for name in order:
            r = results[name]
            packages[name] = {
                "version": r.spec.version,
                "hash": r.hash,
                "store": str(r.path),
                "deps": sorted(d for d in r.spec.depends if d in results),
                "constraint": self.fixfile.packages.get(name, "*"),
                "cached": bool(r.cached),
                "build_seconds": round(r.seconds, 3),
                "built_at": now,
                "description": r.spec.description,
                **({"first_built": prev[name]["first_built"]}
                   if name in prev and "first_built" in prev[name]
                   else {"first_built": now}),
            }
        self.data = {
            "schema": LOCK_SCHEMA,
            "shell": self.fixfile.shell_name or self.fixfile.dir.name,
            "fix_version": __version__,
            "created": (self.data or {}).get("created", now),
            "updated": now,
            "packages": packages,
            "closure": list(order),
        }
        self.path.write_text(json.dumps(self.data, indent=2) + "\n",
                             encoding="utf-8")
        return self.data

    def store_roots(self):
        if not self.data:
            return []
        return [Path(p["store"]).name for p in self.data["packages"].values()]


class GlobalState:
    """~/.fix/state.json — registry of environments (the GC root set)."""

    def __init__(self, store):
        self.path = store.root.parent / "state.json"
        self.data = {"schema": STATE_SCHEMA, "envs": {}}
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("envs"), dict):
                    self.data = loaded
            except ValueError:
                pass  # a corrupt state file must never brick the CLI

    def touch(self, project_dir, env_name, lockfile, roots, entered_shell=False):
        entry = self.data["envs"].get(project_dir, {"created": now_iso()})
        entry.update({
            "name": env_name,
            "lockfile": str(lockfile),
            "roots": sorted(set(roots)),
            "updated": now_iso(),
        })
        if entered_shell:
            entry["last_shell"] = now_iso()
        self.data["envs"][project_dir] = entry

    def prune_stale(self):
        """Drop registry entries whose project directory disappeared."""
        stale = [d for d in self.data["envs"] if not Path(d).is_dir()]
        for d in stale:
            del self.data["envs"][d]
        return stale

    def all_roots(self):
        roots = set()
        for entry in self.data["envs"].values():
            roots.update(entry.get("roots", []))
        return roots

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2) + "\n",
                             encoding="utf-8")


# ---------------------------------------------------------------------------
# environment composition + shell spawning
# ---------------------------------------------------------------------------

def compose_env(fixfile, order, results):
    """Environment dict for `fix shell`: deps' bins + per-package [env]."""
    env = dict(os.environ)
    path_pre = []
    for name in order:                      # order = dependencies first
        r = results[name]
        if (r.path / "bin").is_dir():
            path_pre.append(str(r.path / "bin"))
        env["FIX_PKG_%s" % re.sub(r"[^A-Za-z0-9]", "_", name.upper())] = str(r.path)
        deps = {d: results[d].path for d in r.spec.depends if d in results}
        for key, val in r.spec.env.items():
            env[key] = r.spec.interp(val, r.path, deps)
    if path_pre:
        env["PATH"] = ":".join(path_pre) + ":" + env.get("PATH", "")
    env["FIX_ENV"] = fixfile.shell_name or fixfile.dir.name
    env["FIX_STORE"] = str(Store().root)
    env["FIX_LOCK"] = str(fixfile.dir / LOCK_NAME)
    env["FIX_VERSION"] = __version__
    return env


def spawn_shell(fixfile, env, cmd_args=None):
    """Replace this process with the user's shell (or run cmd_args in it)."""
    shell = os.environ.get("SHELL") or "/bin/bash"
    if env.get("FIX_SHELL") and not env.get("FIX_ALLOW_NEST"):
        warn("already inside a fix shell — nesting anyway (set FIX_SHELL "
             "by hand to suppress)")

    if cmd_args:
        # args are joined and run through the shell so that env vars &
        # pipes behave exactly like an interactive `fix shell` session
        cmd = " ".join(cmd_args)
        rc = subprocess.run([shell, "-c", cmd], env=env).returncode
        return rc

    env["FIX_SHELL"] = "1"
    if sys.stdout.isatty():
        env.setdefault("PS1", "")
        env["PS1"] = "(fix:%s) %s" % (fixfile.shell_name or fixfile.dir.name,
                                    env["PS1"])
    try:
        # execve() replaces the process image — flush std streams first or
        # buffered output (e.g. the shell banner) is silently dropped in
        # frozen/PyInstaller builds.
        sys.stdout.flush()
        sys.stderr.flush()
        os.execve(shell, [shell, "-i"], env)
    except OSError as e:
        raise FixError("cannot spawn shell %s: %s" % (shell, e))


# ---------------------------------------------------------------------------
# build orchestration (resolve → build closure → record state)
# ---------------------------------------------------------------------------

def build_closure(fixfile, store, force=False, verbose=False, quiet=False,
                 targets=None):
    """Resolve the fixfile and build every package; records fix.lock + state.

    `targets` (from `fix install`) mark packages printed as
    "Building <name> from source..." instead of plain "Building <name>...".
    """
    targets = targets or set()
    repos = open_repos(fixfile)
    order, specs = resolve(fixfile, repos)
    if not order:
        if not quiet:
            info("no packages declared in %s" % fixfile.path.name)
        return {}, None, None

    if not quiet:
        print("Building dependencies...")

    results = {}
    for name in order:
        spec = specs[name]
        dep_results = {d: results[d] for d in spec.depends if d in results}
        r = build_package(store, spec, dep_results, force=force, verbose=verbose)
        results[name] = r
        if not quiet:
            if r.cached:
                print("Building %s... %s" % (spec.name, C.dim("(cached)")))
            elif spec.name in targets:
                print("Building %s from source..." % spec.name)
            elif verbose:
                print("Building %s... %s" % (spec.name, C.dim(relhome(r.path))))
            else:
                print("Building %s..." % spec.name)

    lock = Lock(fixfile)
    lock.load()
    lock.write(order, results)

    state = GlobalState(store)
    state.touch(str(fixfile.dir), fixfile.shell_name or fixfile.dir.name,
                lock.path, lock.store_roots())
    state.save()

    if not quiet:
        fresh = [n for n in order if not results[n].cached]
        cached = len(order) - len(fresh)
        info("environment ready — %d package(s) [%d built, %d cached], state "
             "recorded in %s" % (len(order), len(fresh), cached, LOCK_NAME))
    return results, lock, state


def ensure_built(fixfile, store, verbose=False):
    """Build only if needed (no lock, or fixfile newer than lock)."""
    lockpath = fixfile.dir / LOCK_NAME
    needs = (not lockpath.is_file()) or \
        (fixfile.path.stat().st_mtime > lockpath.stat().st_mtime)
    if needs:
        return build_closure(fixfile, store, verbose=verbose)
    lock = Lock(fixfile)
    lock.load()
    results = {}
    for name in (lock.data or {}).get("closure", []):
        meta = (lock.data or {}).get("packages", {}).get(name)
        if meta and Path(meta["store"]).is_dir():
            spec = None
            try:
                repos = open_repos(fixfile)
                _, specs = resolve(fixfile, repos)
                spec = specs.get(name)
            except FixError:
                spec = None
            if spec is not None:
                results[name] = BuildResult(spec, meta["hash"],
                                            Path(meta["store"]), cached=True,
                                            seconds=0.0, log=[])
    if len(results) == len((lock.data or {}).get("closure", [])) and results:
        return results, lock, None
    return build_closure(fixfile, store, verbose=verbose)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

HELP = """\
fix %s — a simple, source-based and declarative package manager

usage: fix [options] <command> [args]

commands:
  init [pkg...]     create a new environment (scaffold fix.yml + vendor/)
  install <pkg...>  add packages to fix.yml and build them from source
  shell [-- cmd]    build dependencies, then start a new shell with them;
                    with `-- cmd` run cmd inside the environment instead
  build [-f]        build every package in fix.yml; record fix.lock
  remove <pkg...>   remove packages from fix.yml (refuses if others depend)
  upgrade           rebuild every package from scratch; refresh state
  list              list known environments (from ~/.fix/state.json)
  status            show this environment's recorded state, paths & sizes
  tree              print the dependency tree
  search <term>     search package specs in the configured repos
  path [pkg]        print store path(s) (scripting friendly)
  gc [--dry-run]    delete store entries not referenced by any environment
  purge             remove the entire fix store & state (~/.fix)
  doctor            sanity-check toolchain, store & permissions

options:
  --file PATH       use this environment file (default: fix.yml)
  --no-color        disable colored output
  -v, --verbose     show build steps as they run
  -q, --quiet       only report errors

store:   %s
example: fix init gcc make && fix shell
""" % (__version__, relhome(fix_home()))


def _load_fixfile(args):
    ff = Fixfile.locate(args.file)
    ff.load()
    return ff


def cmd_init(args):
    ffpath = Path(args.file).expanduser().resolve()
    if ffpath.exists() and not args.force:
        die("%s already exists (use --force to overwrite)" % ffpath)
    for pkg in args.packages:
        if not NAME_RE.match(pkg):
            die("invalid package name '%s' (must match %s)" % (pkg, NAME_RE.pattern))
    print("Creating environment...")
    Fixfile.create(ffpath, None, args.packages)
    vendor = ffpath.parent / "vendor"
    if not vendor.exists():
        vendor.mkdir(parents=True, exist_ok=True)
        (vendor / "README").write_text(
            "put package specs here as <name>/fix.yml or <name>.yml\n"
            "see `fix search .` and the project README for the spec format\n",
            encoding="utf-8")
    print("\nWrote %s" % relhome(ffpath))
    info("add packages with `fix install <name>`, name the shell with a "
         "`shell:` line, then run `fix shell`")
    if args.packages:
        ff = Fixfile.locate(args.file).load()
        try:
            order, _ = resolve(ff, open_repos(ff))
            info("resolvable here: %s" % " ".join(order))
        except FixError as e:
            warn(str(e))
            warn("place specs under vendor/ or run `fix search <term>`")


def cmd_build(args):
    ff = _load_fixfile(args)
    results, lock, _ = build_closure(ff, Store(), force=args.force,
                                     verbose=args.verbose, quiet=args.quiet)
    if not results and not args.quiet:
        info("nothing to do")


def cmd_shell(args):
    ff = _load_fixfile(args)
    store = Store()
    results, lock, _ = ensure_built(ff, store, verbose=args.verbose)
    if not results:
        die("no packages in this environment — add some with `fix install <pkg>`")
    order = (lock.data or {}).get("closure", list(results))
    state = GlobalState(store)
    state.touch(str(ff.dir), ff.shell_name or ff.dir.name, ff.dir / LOCK_NAME,
                (lock.store_roots() if lock else []), entered_shell=True)
    state.save()
    env = compose_env(ff, order, results)
    if not args.cmd_args:
        print()  # blank line between build output and the shell banner, as on
        # the website:  "Starting development shell..."
        if ff.shell_name:
            print("Starting %s shell..." % ff.shell_name)
        else:
            print("Entering fix environment...")
    rc = spawn_shell(ff, env, cmd_args=args.cmd_args or None)
    if args.cmd_args:
        sys.exit(rc or 0)


def cmd_install(args):
    ff = _load_fixfile(args)
    repos = open_repos(ff)
    added = []
    for pkg in args.packages:
        name, constraint = _parse_pkg_entry(pkg)
        spec = find_spec(repos, name)  # raises with suggestions if unknown
        if name in ff.packages:
            warn("%s already in %s" % (name, ff.path.name))
        ff.packages[name] = constraint
        added.append((name, spec.version))
    ff.save()
    build_closure(ff, Store(), verbose=args.verbose, quiet=args.quiet,
                 targets={n for n, _ in added})
    if not args.quiet:
        info("installed %s" % ", ".join("%s %s" % a for a in added))


def cmd_remove(args):
    ff = _load_fixfile(args)
    targets = list(args.packages)
    for t in targets:
        if t not in ff.packages:
            die("%s is not declared in %s" % (t, ff.path.name))
    repos = open_repos(ff)
    remaining = {n: c for n, c in ff.packages.items() if n not in targets}
    try:
        order, specs = resolve(ff, repos, wanted=remaining)
    except FixError as e:
        die("cannot remove: %s" % e)
    # refuse removal while a remaining package still depends on a target
    for name, spec in specs.items():
        for dep in spec.depends:
            if dep in targets:
                die("refusing: %s depends on %s (remove both together)"
                    % (spec.name, dep))
    ff.packages = remaining
    ff.save()
    ok("removed from %s: %s (store entries kept until `fix gc`)"
       % (ff.path.name, ", ".join(targets)))
    build_closure(ff, Store(), verbose=args.verbose, quiet=args.quiet)


def cmd_upgrade(args):
    ff = _load_fixfile(args)
    warn("rebuilding every package from scratch (new hashes only if inputs "
         "changed)")
    build_closure(ff, Store(), force=True, verbose=args.verbose,
                  quiet=args.quiet)


def cmd_list(args):
    state = GlobalState(Store())
    envs = state.data.get("envs", {})
    if not envs:
        info("no environments recorded yet — `fix build` in a project to "
             "register one")
        return
    print("%-18s %-6s %s" % (C.bold("ENV"), C.bold("PKGS"), C.bold("PROJECT")))
    for d in sorted(envs, key=lambda x: envs[x].get("updated", "")):
        e = envs[d]
        print("%-18s %-6s %s" % (e.get("name", "?"), len(e.get("roots", [])),
                                 relhome(d)))
    info("store: %s (%s in %d entries)"
         % (relhome(Store().root), human_size(Store().total_size()),
            len(Store().entries())))


def cmd_status(args):
    ff = _load_fixfile(args)
    store = Store()
    lock = Lock(ff)
    data = lock.load()
    state = GlobalState(store)
    entry = state.data["envs"].get(str(ff.dir), {})
    print("%s %s" % (C.bold("environment"), C.cyan(ff.shell_name or ff.dir.name)))
    print("  fixfile   %s" % relhome(ff.path))
    if ff.shell_name:
        print("  shell     %s" % ff.shell_name)
    if not data:
        print("  state     %s (run `fix build`)"
              % C.yellow("no fix.lock yet"))
        return
    print("  lock      %s (updated %s)"
          % (relhome(lock.path), data.get("updated", "?")))
    if entry.get("last_shell"):
        print("  last shell %s" % entry["last_shell"])
    print("\n%s" % C.bold("packages"))
    for name in data.get("closure", []):
        p = data["packages"][name]
        size = human_size(dir_size(p["store"])) if Path(p["store"]).is_dir() else "-"
        deps = ("needs " + " ".join(p["deps"])) if p["deps"] else "-"
        print("  %s%-28s%s %s  %-7s %-8s %s"
              % (C.green("●"), "%s %s" % (name, p["version"]), C.dim(""),
                 p["hash"][:12], size, deps, relhome(p["store"]))[:100])
    print("\n  store %s — %s total"
          % (relhome(store.root), human_size(store.total_size())))


def cmd_tree(args):
    ff = _load_fixfile(args)
    order, specs = resolve(ff, open_repos(ff))
    print("%s (env %s)" % (C.bold(ff.shell_name or ff.dir.name),
                          relhome(ff.path)))
    shown = set()

    def walk(name, prefix, last):
        spec = specs[name]
        connector = "└── " if last else "├── "
        print("%s%s%s %s" % (prefix, connector, C.bold(name),
                             C.dim("v" + spec.version)))
        if name in shown:
            print("%s    %s" % (prefix + ("" if last else "│   "),
                                C.dim("↺ (already shown)")))
            return
        shown.add(name)
        deps = [d for d in spec.depends if d in specs]
        for i, d in enumerate(deps):
            walk(d, prefix + ("" if last else "│   "), i == len(deps) - 1)

    roots = [n for n in order if n in ff.packages]
    for i, r in enumerate(sorted(roots)):
        walk(r, "", i == len(roots) - 1)
    if len(order) > len(roots):
        info("(+%d transitive dependencies shown above)"
             % (len(order) - len(roots)))


def cmd_search(args):
    ff = Fixfile.locate(args.file)
    repos = open_repos(ff) if ff.path.is_file() else \
        [Repo("vendor", ff.dir / "vendor"), Repo("user", fix_home() / "repo")]
    term = (args.term or "").lower()
    hits = 0
    for repo in repos:
        for name in repo.names():
            try:
                spec = Spec.from_file(repo.find(name), repo.name)
            except FixError:
                continue
            hay = (name + " " + spec.description).lower()
            if term and term != "." and term not in hay:
                continue
            hits += 1
            print("%s%-12s%s %-8s %s" % (C.green(name), "", spec.version,
                                         spec.description or C.dim("(no description)")))
    if not hits:
        info("no packages matched %r (repos: %s)"
             % (term, ", ".join(r.name for r in repos) or "none"))


def cmd_path(args):
    ff = _load_fixfile(args)
    lock = Lock(ff)
    data = lock.load()
    if not data:
        die("no %s yet — run `fix build` first" % LOCK_NAME)
    for name in ([args.package] if args.package else data["closure"]):
        p = data["packages"].get(name)
        if not p:
            die("package '%s' not in this environment" % name)
        print(p["store"])


def cmd_gc(args):
    store = Store()
    state = GlobalState(store)
    stale = state.prune_stale()
    if stale:
        info("pruned %d stale environment(s) from state: %s"
             % (len(stale), ", ".join(relhome(s) for s in stale)))
        state.save()
    roots = state.all_roots()
    # the current project (if any) is always a live root
    try:
        ff = Fixfile.locate(args.file)
        ff.load()
        lock = Lock(ff)
        if lock.load():
            roots.update(lock.store_roots())
    except FixError:
        pass
    removed, freed = store.gc(roots, dry_run=args.dry_run)
    if not removed:
        ok("nothing to collect — all %d store entries are reachable"
           % len(store.entries()))
        return
    verb = "would remove" if args.dry_run else "removed"
    for entry, size in removed:
        print("  %s %s" % (C.yellow("−"), C.dim("%s (%s)" % (entry.name,
                                                             human_size(size)))))
    ok("%s %d entr%s, %s %s" % (verb, len(removed), "y" if len(removed) == 1
                                else "ies", "would free" if args.dry_run
                                else "freed", human_size(freed)))
    if not args.dry_run:
        state.save()


def cmd_purge(args):
    home = fix_home()
    if not home.exists():
        info("nothing to purge (%s does not exist)" % relhome(home))
        return
    if not args.yes and sys.stdin.isatty():
        try:
            answer = input("fix: remove %s entirely? [y/N] " % relhome(home))
        except EOFError:
            answer = "n"
        if answer.strip().lower() not in ("y", "yes"):
            die("aborted")
    shutil.rmtree(home, ignore_errors=True)
    ok("removed %s" % relhome(home))


def cmd_doctor(args):
    checks = []
    home = fix_home()
    checks.append(("home %s writable" % relhome(home),
                   home.exists() and os.access(home, os.W_OK) or _try_mkdir(home)))
    store = Store()
    checks.append(("store %s" % relhome(store.root),
                   store.root.is_dir() or _try_mkdir(store.root)))
    for tool, needed in (("bash", True), ("cc", True), ("make", False),
                         ("tar", False)):
        path = shutil.which(tool)
        checks.append(("%-5s %s" % (tool, path or C.red("NOT FOUND")),
                       path is not None if needed else True))
    if not all(r for _, r in checks):
        warn("missing tools marked NOT FOUND are only needed if packages "
             "you build require them")
    failed = [name for name, r in checks if not r]
    for name, r in checks:
        print("  %s %s" % (C.green("✔") if r else C.red("✘"), name))
    if failed:
        die("doctor: %d check(s) failed" % len(failed))
    ok("all critical checks passed")


def _try_mkdir(p):
    try:
        Path(p).mkdir(parents=True, exist_ok=True)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# argument parsing / entry point
# ---------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(prog="fix", add_help=False,
                                 description="a simplified source-based "
                                             "package manager")
    ap.add_argument("--file", default=FIXFILE_NAME)
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("-h", "--help", action="store_true")
    ap.add_argument("--version", action="store_true")
    sub = ap.add_subparsers(dest="cmd", metavar="<command>")

    p = sub.add_parser("init")
    p.add_argument("packages", nargs="*")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("build")
    p.add_argument("-f", "--force", action="store_true")

    p = sub.add_parser("shell")
    p.add_argument("cmd_args", nargs="*")

    p = sub.add_parser("install", aliases=["add"])
    p.add_argument("packages", nargs="+")

    p = sub.add_parser("remove", aliases=["rm"])
    p.add_argument("packages", nargs="+")

    sub.add_parser("upgrade")
    sub.add_parser("list")
    sub.add_parser("status")
    sub.add_parser("tree")

    p = sub.add_parser("search")
    p.add_argument("term", nargs="?")

    p = sub.add_parser("path")
    p.add_argument("package", nargs="?")

    p = sub.add_parser("gc")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("purge")
    p.add_argument("-y", "--yes", action="store_true")

    sub.add_parser("doctor")
    return ap


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--no-color" in argv:
        C.enabled = False
    # accept bare `fix help` / `fix version` like the options they mirror
    if argv and argv[0] in ("help", "version"):
        argv[0] = "--" + argv[0]
    ap = build_parser()
    args, unknown = ap.parse_known_args(argv)
    if unknown:
        die("unknown argument(s): %s\n\n%s" % (" ".join(unknown), HELP))
    if args.version:
        print("fix %s" % __version__)
        return 0
    if args.help or not args.cmd:
        print(HELP)
        return 0 if args.help else 2

    dispatch = {
        "init": cmd_init, "build": cmd_build, "shell": cmd_shell,
        "install": cmd_install, "add": cmd_install,
        "remove": cmd_remove, "rm": cmd_remove, "upgrade": cmd_upgrade,
        "list": cmd_list, "status": cmd_status, "tree": cmd_tree,
        "search": cmd_search, "path": cmd_path, "gc": cmd_gc,
        "purge": cmd_purge, "doctor": cmd_doctor,
    }
    try:
        rc = dispatch[args.cmd](args)
        return rc if isinstance(rc, int) else 0
    except FixError as e:
        die(str(e))
    except KeyboardInterrupt:
        die("interrupted", 130)


if __name__ == "__main__":
    sys.exit(main())
