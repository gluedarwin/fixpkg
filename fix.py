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

  * Build from source  — package recipes are shell steps run with staged
                         inputs and best-effort isolation (bwrap on Linux
                         when available; otherwise unisolated — only build
                         recipes you trust, see README), output staged into
                         $FIX_OUT then atomically installed.
  * Declarative        — the environment is a simple YAML file (fix.yml):

                             packages:
                               - gcc
                               - make

                             shell: development

  * Remote sources — a package recipe can build from a tarball
                         (`source: {type: tarball, url: ..., sha256: ...}`)
                         and an environment can point at a remote repo
                         (`repos: {upstream: https://...#sha256=...}`).
                         Nothing is fetched without a pinned sha256, and
                         downloads are verified before extraction.
  * Reproducible       — every build lands in ~/.fix/store/<hash>-<name>-<version>/
                         where <hash> is derived from the package sources
                         (content, file modes, symlink targets), build
                         steps, the hashes of its dependencies, and a
                         toolchain fingerprint. Identical inputs on the
                         same toolchain => identical path => the build is
                         skipped. The hash is per-toolchain, not
                         cross-machine.
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
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import yaml as _pyyaml  # optional; bundled mini parser is the fallback
except ImportError:  # pragma: no coverage — mini parser takes over
    _pyyaml = None

__version__ = "0.4.0"

FIX_HOME_DEFAULT = "~/.fix"
FIXFILE_NAME = "fix.yml"
FIXFILE_ALT = "fix.yaml"
LOCK_NAME = "fix.lock"
# v2: the derivation hash now also covers file modes, symlink targets,
# empty directories, and a toolchain fingerprint. This is a breaking
# change: store entries built under v1 have different hashes, so they
# become unreachable and `fix gc` collects them.
DERIVATION_SCHEMA = "fix-derivation-v2"
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


def write_atomic(path, text, encoding="utf-8"):
    """Write `text` to `path` atomically: temp file in the same directory,
    fsync, then `os.replace`. A crash mid-write can never leave a
    half-written file behind, and a failed write leaves the previous
    contents untouched."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".",
                                    suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))
        try:  # best effort: make the rename itself durable
            dfd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# toolchain fingerprint — part of the derivation hash (v2)
# ---------------------------------------------------------------------------

_TOOLCHAIN_FP = None


def toolchain_fingerprint():
    """First line of `cc --version` + `bash --version`, or "unknown".

    The hash is therefore per-toolchain, not cross-machine: the same
    recipe built with a different compiler (or a missing one) gets a
    different store path instead of a false "cached" hit. Cached for
    the life of the process.
    """
    global _TOOLCHAIN_FP
    if _TOOLCHAIN_FP is not None:
        return _TOOLCHAIN_FP
    parts = []
    for tool in ("cc", "bash"):
        try:
            proc = subprocess.run([tool, "--version"], stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True,
                                  timeout=10)
            first = (proc.stdout or "").strip().splitlines()
            parts.append("%s:%s" % (tool, first[0].strip() if first else "unknown"))
        except Exception:
            parts.append("%s:unknown" % tool)
    _TOOLCHAIN_FP = ";".join(parts) if parts else "unknown"
    return _TOOLCHAIN_FP


# ---------------------------------------------------------------------------
# spec env: allowlist — keys that could hijack the shell are forbidden
# ---------------------------------------------------------------------------

BLOCKED_ENV_KEYS = frozenset({
    "PATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "HOME", "SHELL", "IFS",
    "PYTHONPATH", "PYTHONHOME", "BASH_ENV", "ENV",
})
BLOCKED_ENV_PREFIXES = ("DYLD_", "FIX_")


def is_blocked_env_key(key):
    """True for env keys a recipe must not be allowed to set."""
    return key in BLOCKED_ENV_KEYS or key.startswith(BLOCKED_ENV_PREFIXES)


def validate_spec_env(env, spec_path):
    """Reject hijack-capable keys in a recipe's `env:` mapping."""
    for key in env:
        if is_blocked_env_key(key):
            raise SpecError(
                "%s: env key %r is not allowed — recipes may not set "
                "PATH, HOME, SHELL, LD_*/DYLD_*, FIX_*, IFS, PYTHON*, "
                "BASH_ENV or ENV (they could hijack the shell or the "
                "build environment)" % (spec_path, key))


# ---------------------------------------------------------------------------
# YAML — PyYAML when available, tiny bundled parser otherwise + an emitter.
# fix only needs a small subset: nested mappings, string lists, scalars and
# comments — exactly the shapes written by fix itself (fix.yml, package specs).
# The mini parser also handles lists of mappings and single-line flow
# values ([a, b], {k: v}); duplicate keys, unexpected indentation and
# malformed flow are errors, never silently dropped. Anything beyond that
# (anchors, multi-line strings, ...) needs PyYAML — see README.
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
    """One scalar value: quoted string, flow value, bool/null/number, else str.

    Flow values ([a, b], {k: v}) are parsed by _parse_flow. Anything that
    looks like flow but is malformed raises SpecError instead of being
    kept as a literal string — parsing must never silently change meaning
    depending on which YAML implementation is in use.
    """
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        body = s[1:-1]
        if s[0] == '"':
            body = body.replace('\\"', '"').replace("\\\\", "\\")
            body = body.replace("\\n", "\n").replace("\\t", "\\t")
        else:  # YAML single quotes: '' is an escaped single quote
            body = body.replace("''", "'")
        return body
    if s.startswith("[") or s.startswith("{"):
        return _parse_flow(s)
    low = s.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
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


def _parse_flow(s):
    """Parse a single-line flow value: [a, b, {k: v}] or {k: v, ...}.

    Recursive descent; quoted strings and nested flow are handled, and
    duplicate map keys or truncated/malformed input raise SpecError.
    """
    s = s.strip()
    pos = [0]

    def err(msg):
        raise SpecError("yaml: %s in flow value %r" % (msg, s))

    def skip_ws():
        while pos[0] < len(s) and s[pos[0]] in " \t":
            pos[0] += 1

    def parse_quoted():
        q = s[pos[0]]
        start = pos[0]
        pos[0] += 1
        while pos[0] < len(s):
            c = s[pos[0]]
            if q == '"' and c == "\\" and pos[0] + 1 < len(s):
                pos[0] += 2
                continue
            if q == "'" and c == "'" and pos[0] + 1 < len(s) \
                    and s[pos[0] + 1] == "'":
                pos[0] += 2
                continue
            if c == q:
                pos[0] += 1
                return _yaml_scalar(s[start:pos[0]])
            pos[0] += 1
        err("unterminated quoted string")

    def parse_bare():
        start = pos[0]
        while pos[0] < len(s):
            c = s[pos[0]]
            if c in ",]}":
                break
            # ':' ends a bare token only when it is a key separator
            # (followed by space, a flow delimiter, or end of input) —
            # 'http://x' is one token, 'a: b' is not.
            if c == ":" and (pos[0] + 1 >= len(s)
                             or s[pos[0] + 1] in " \t,]}"):
                break
            pos[0] += 1
        tok = s[start:pos[0]].strip()
        if not tok:
            err("empty value")
        return _yaml_scalar(tok)

    def parse_value():
        skip_ws()
        if pos[0] >= len(s):
            err("truncated value")
        c = s[pos[0]]
        if c == "[":
            pos[0] += 1
            out = []
            skip_ws()
            if pos[0] < len(s) and s[pos[0]] == "]":
                pos[0] += 1
                return out
            while True:
                out.append(parse_value())
                skip_ws()
                if pos[0] >= len(s):
                    err("unterminated flow list")
                if s[pos[0]] == ",":
                    pos[0] += 1
                    continue
                if s[pos[0]] == "]":
                    pos[0] += 1
                    return out
                err("expected ',' or ']'")
        if c == "{":
            pos[0] += 1
            out = {}
            skip_ws()
            if pos[0] < len(s) and s[pos[0]] == "}":
                pos[0] += 1
                return out
            while True:
                key = parse_value()
                if not isinstance(key, str):
                    err("flow map keys must be strings")
                if key in out:
                    err("duplicate key %r" % key)
                skip_ws()
                if pos[0] >= len(s) or s[pos[0]] != ":":
                    err("expected ':' in flow map")
                pos[0] += 1
                out[key] = parse_value()
                skip_ws()
                if pos[0] >= len(s):
                    err("unterminated flow map")
                if s[pos[0]] == ",":
                    pos[0] += 1
                    continue
                if s[pos[0]] == "}":
                    pos[0] += 1
                    return out
                err("expected ',' or '}'")
        if c in "\"'":
            return parse_quoted()
        return parse_bare()

    value = parse_value()
    skip_ws()
    if pos[0] != len(s):
        err("trailing characters after flow value")
    return value


def _yaml_lines(text):
    items = []
    for raw in text.splitlines():
        line = _yaml_strip_comment(raw.replace("\t", "  "))
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        items.append((indent, line.strip()))
    return items


def _split_key(content):
    """Split a 'key:' or 'key: value' line -> (key, rest) or None.

    A ':' only starts a key when followed by a space or end of line, so
    'http://x' is a scalar, not a mapping. Quoted keys are supported.
    """
    if content[:1] in ("\"", "'"):
        q = content[0]
        i = 1
        while i < len(content):
            c = content[i]
            if q == '"' and c == "\\" and i + 1 < len(content):
                i += 2
                continue
            if q == "'" and c == "'" and i + 1 < len(content) \
                    and content[i + 1] == "'":
                i += 2
                continue
            if c == q:
                break
            i += 1
        else:
            return None
        # A quoted token is a key only when ':' follows it ('"k": v').
        # A line that is just one quoted scalar ('"cc -O2 ..."', i.e. a
        # build step) is a value, not a mapping entry.
        if i + 1 >= len(content):
            return None
        if content[i + 1] != ":" or (i + 2 < len(content)
                                      and content[i + 2] not in " \t"):
            return None
        return (_yaml_scalar(content[:i + 1]),
                content[i + 2:].strip() if i + 2 < len(content) else "")
    m = re.match(r"^([^\s:][^:]*):(?:\s+(.*))?$", content)
    if not m:
        return None
    return (m.group(1).strip(), (m.group(2) or "").strip())


def _yaml_block(items, i, indent):
    """Parse the block starting at items[i] with the given indentation.

    Supports mappings, lists (including lists of mappings, where the
    first 'key: value' follows the '-' and the rest are indented under
    it), flow values, and nested blocks. Duplicate keys, unexpectedly
    indented lines, and lines that are neither a mapping entry nor a
    list item raise SpecError — nothing is silently dropped.
    """
    if i >= len(items) or items[i][0] < indent:
        return None, i
    if items[i][1] == "-" or items[i][1].startswith("- "):
        out = []
        while i < len(items) and items[i][0] == indent and \
                (items[i][1] == "-" or items[i][1].startswith("- ")):
            content = "" if items[i][1] == "-" else items[i][1][2:].strip()
            i += 1
            if not content:
                if i < len(items) and items[i][0] > indent:
                    val, i = _yaml_block(items, i, items[i][0])
                    out.append(val)
                else:
                    out.append(None)
                continue
            kv = _split_key(content)
            if kv is None:
                out.append(_yaml_scalar(content))
                continue
            # List item that starts a mapping: '- name: x' plus any
            # deeper lines belonging to the same item.
            m = {}
            key, rest = kv
            if rest:
                m[key] = _yaml_scalar(rest)
            elif i < len(items) and items[i][0] > indent:
                m[key], i = _yaml_block(items, i, items[i][0])
            else:
                m[key] = None
            while i < len(items) and items[i][0] > indent:
                sub, i = _yaml_block(items, i, items[i][0])
                if not isinstance(sub, dict):
                    raise SpecError(
                        "yaml: unexpected block under list item %r" % content)
                for k, v in sub.items():
                    if k in m:
                        raise SpecError("yaml: duplicate key %r" % k)
                    m[k] = v
            out.append(m)
        return out, i
    out = {}
    while i < len(items):
        ind, content = items[i]
        if ind < indent or content == "-" or content.startswith("- "):
            break
        if ind > indent:
            raise SpecError(
                "yaml: unexpected indentation for line %r — it is deeper "
                "than the mapping it follows (check the indent)" % content)
        kv = _split_key(content)
        if kv is None:
            raise SpecError("yaml: cannot parse line: %r" % content)
        key, rest = kv
        if key in out:
            raise SpecError("yaml: duplicate key %r" % key)
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
    # PyYAML silently lets a later duplicate key win; the mini parser
    # rejects duplicates, and the two backends must not disagree about
    # the same file. Check the node tree first (no constructors run).
    try:
        root = _pyyaml.compose(text)
    except Exception as e:
        raise SpecError("yaml: cannot parse: %s" % e)
    _check_no_duplicate_keys(root)
    data = _pyyaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SpecError("yaml: top level must be a mapping")
    return data


def _check_no_duplicate_keys(node):
    """Walk a yaml.compose() node tree; duplicate mapping keys are an
    error (SpecError). Only scalar keys are compared — fix files never
    use complex keys."""
    if node is None:
        return
    if isinstance(node, _pyyaml.MappingNode):
        seen = set()
        for key_node, value_node in node.value:
            if isinstance(key_node, _pyyaml.ScalarNode):
                if key_node.value in seen:
                    raise SpecError("yaml: duplicate key %r" % key_node.value)
                seen.add(key_node.value)
            _check_no_duplicate_keys(value_node)
    elif isinstance(node, _pyyaml.SequenceNode):
        for item in node.value:
            _check_no_duplicate_keys(item)


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


def _yaml_value_ok(v):
    """True if `yaml_dumps` can write `v` back. The emitter only handles
    scalars, mappings, and lists of scalars — a list containing a mapping
    would raise in `_yaml_fmt`, so such values are not round-trippable."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return True
    if isinstance(v, dict):
        return all(isinstance(k, str) and _yaml_value_ok(x)
                   for k, x in v.items())
    if isinstance(v, (list, tuple)):
        return all(_yaml_value_ok(x) and not isinstance(x, (dict, list, tuple))
                   for x in v)
    return False


# ---------------------------------------------------------------------------
# version constraints ("*", "1.2.0", "1.*", "^1.2", ">=1.2,<2")
# ---------------------------------------------------------------------------

def _vtup(v, allow_wildcard=False):
    """Strict (major, minor, patch) tuple; raises SpecError on garbage.

    Accepts one optional leading 'v' ('v1.2' == '1.2'). Every component
    must be digits (or an x/* wildcard, only when allow_wildcard is set,
    and only as trailing components, e.g. '1.*'), and there are at most
    three components — '1.2.3.4', '>=foo' and '1.2.3-rc1' are rejected
    loudly instead of silently collapsing to (1, 2, 3) or (0, 0, 0).
    Missing components pad with 0: '1.2' == (1, 2, 0).
    """
    s = str(v).strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    if not s:
        raise SpecError("bad version %r (empty after stripping 'v')" % (v,))
    parts = []
    for x in s.split("."):
        if x in ("x", "*"):
            if not allow_wildcard:
                raise SpecError(
                    "bad version %r: wildcards are only allowed in "
                    "constraints such as '1.*'" % (v,))
            parts.append(None)
        elif x.isdigit():
            if parts and parts[-1] is None:
                raise SpecError(
                    "bad version %r: wildcard components must be trailing "
                    "(e.g. '1.*', not '1.*.3')" % (v,))
            parts.append(int(x))
        else:
            raise SpecError(
                "bad version %r: component %r is not numeric — use a plain "
                "version like 1.2.3, optionally with a leading 'v' "
                "(prerelease suffixes such as '-rc1' are not supported)"
                % (v, x))
    if len(parts) > 3:
        raise SpecError(
            "bad version %r: at most three components (got %d)"
            % (v, len(parts)))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def _clause_base(raw):
    """The version operand of a ^ / ~ / comparison clause, validated.

    Returns (tuple, n_components_given) — the component count matters
    for '~': '~1' caps at 2.0.0, '~1.2' and '~1.2.3' cap at 1.3.0.
    """
    s = str(raw).strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    return _vtup(raw), (s.count(".") + 1 if s else 0)


def version_match(constraint, version):
    """True if `version` satisfies `constraint` (comma-separated clauses).

    Constraints: '*' (any), an exact version ('1.2.0', '1.*', 'v1.2'),
    '^1.2' (same major, 0.x rules), '~1.2' (same minor), comparisons
    '>=1', '<2', '=1.2.0', '==1.2.0'. Garbage raises SpecError — a typo
    like '>=foo' must never silently match everything or nothing.
    """
    c = str(constraint).strip()
    if c in ("*", "", "x", "any"):
        return True
    v = _vtup(version)
    for clause in [x.strip() for x in c.split(",") if x.strip()]:
        if clause in ("*", "x", "any"):
            continue
        if clause.startswith("^"):
            base, _n = _clause_base(clause[1:])
            if base[0] > 0:
                lo, hi = base, (base[0] + 1, 0, 0)
            elif base[1] > 0:
                lo, hi = base, (0, base[1] + 1, 0)
            else:
                lo, hi = base, (0, 0, base[2] + 1)
            if not (lo <= v < hi):
                return False
        elif clause.startswith("~"):
            base, ncomp = _clause_base(clause[1:])
            hi = (base[0] + 1, 0, 0) if ncomp <= 1 else (base[0], base[1] + 1, 0)
            if not (base <= v < hi):
                return False
        elif clause.startswith(">="):
            if not v >= _vtup(clause[2:]):
                return False
        elif clause.startswith("<="):
            if not v <= _vtup(clause[2:]):
                return False
        elif clause.startswith(">"):
            if not v > _vtup(clause[1:]):
                return False
        elif clause.startswith("<"):
            if not v < _vtup(clause[1:]):
                return False
        elif clause.startswith("=="):
            if v != _vtup(clause[2:]):
                return False
        elif clause.startswith("="):
            if v != _vtup(clause[1:]):
                return False
        else:
            # Exact version, optionally with trailing wildcards ('1.*').
            tup = _vtup(clause, allow_wildcard=True)
            depth = 0
            while depth < 3 and tup[depth] is not None:
                depth += 1
            if v[:depth] != tup[:depth]:
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
                 excludes, steps, env, spec_path, repo, source_desc=None):
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
        # The source descriptor feeds the derivation hash for non-local
        # sources (see derivation_hash). Local specs keep the default:
        # their hash must not change just because the descriptor exists.
        self.source_desc = source_desc or {"type": "local", "dir": "."}
        # Tarball sources are materialised lazily (see _materialize):
        # local sources are ready immediately.
        self._source_ready = self.source_desc.get("type") == "local"

    # -- parsing -----------------------------------------------------------

    @classmethod
    def from_file(cls, spec_path, repo):
        spec_path = Path(spec_path)
        data = yaml_load_file(spec_path)
        if not isinstance(data, dict):
            raise SpecError("%s: top level must be a YAML mapping"
                            % spec_path)
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
        try:
            _vtup(version)
        except SpecError as e:
            raise SpecError("%s: package.version %r is not a plain version: %s"
                            % (spec_path, version, e))
        depends = data.get("depends") or {}
        if not isinstance(depends, dict):
            raise SpecError("%s: 'depends:' must be a name: constraint mapping" % spec_path)
        for dep, con in depends.items():
            if not NAME_RE.match(str(dep)):
                raise SpecError("%s: bad dependency name %r" % (spec_path, dep))
        source = data.get("source") or {}
        if not isinstance(source, dict):
            raise SpecError("%s: 'source:' must be a mapping" % spec_path)
        stype = str(source.get("type", "local"))
        source_desc = {"type": "local",
                       "dir": str(source.get("dir", "."))}
        if stype == "local":
            pass  # validated below, as before
        elif stype == "tarball":
            # A tarball source is fetched, verified and extracted lazily
            # (see _materialize) — parsing must never touch the network.
            url = source.get("url")
            sha = source.get("sha256")
            if not url or not isinstance(url, str):
                raise SpecError("%s: tarball source needs 'url:'" % spec_path)
            if not sha or not isinstance(sha, str) or not SHA256_RE.match(sha):
                raise SpecError("%s: tarball source needs 'sha256:' with "
                                "64 lowercase hex characters (no checksum, "
                                "no fetch) — quote it in YAML if it is all "
                                "digits, or YAML reads it as a number"
                                % spec_path)
            strip = source.get("strip", 1)
            if isinstance(strip, bool) or not isinstance(strip, int) \
                    or strip < 0:
                raise SpecError("%s: tarball 'strip:' must be a "
                                "non-negative integer (got %r)"
                                % (spec_path, strip))
            sdir = str(source.get("dir", "."))
            if Path(sdir).is_absolute():
                raise SpecError("%s: tarball 'dir:' must be relative (got %r)"
                                % (spec_path, sdir))
            _validate_fetch_url(url, spec_path)
            source_desc = {"type": "tarball", "url": url, "sha256": sha,
                           "strip": strip, "dir": sdir}
        else:
            raise SpecError("%s: only source.type: local or tarball is "
                            "supported in v%s (git planned — see README "
                            "roadmap)" % (spec_path, __version__))
        src_dir = spec_path.parent / str(source.get("dir", "."))
        if stype == "local":
            if Path(str(source.get("dir", "."))).is_absolute():
                raise SpecError("%s: source.dir must be relative (got %r)"
                                % (spec_path, source.get("dir")))
            pkg_dir = spec_path.parent.resolve()
            resolved_src = src_dir.resolve()
            if resolved_src != pkg_dir and pkg_dir not in resolved_src.parents:
                raise SpecError("%s: source.dir %r escapes the package directory %s"
                                % (spec_path, source.get("dir"), pkg_dir))
            if not src_dir.is_dir():
                raise SpecError("%s: source dir %s does not exist" % (spec_path, src_dir))
        elif stype == "tarball":
            # The placeholder dir need not exist yet: materialisation
            # replaces source_dir with the extracted tree. `dir:` is
            # checked against the extracted tree at that point.
            pass
        excludes = list(source.get("exclude") or [])
        build = data.get("build") or {}
        steps = build.get("steps")
        if not steps or not isinstance(steps, list):
            raise SpecError("%s: build.steps is a required list of shell commands" % spec_path)
        env = data.get("env") or {}
        if not isinstance(env, dict):
            raise SpecError("%s: 'env:' must be a VAR: value mapping" % spec_path)
        validate_spec_env({str(k): str(v) for k, v in env.items()}, spec_path)
        return cls(str(name), version, str(pkg.get("description") or ""),
                   {str(k): str(v) for k, v in depends.items()},
                   src_dir.resolve(), DEFAULT_EXCLUDES + excludes,
                   [str(s) for s in steps], {str(k): str(v) for k, v in env.items()},
                   spec_path, repo, source_desc)

    # -- source hashing (also the exact set of files copied into builds) ----

    def _materialize(self, store):
        """Fetch + verify + extract a tarball source, once per instance.

        After this, self.source_dir is the extracted tree (or the `dir:`
        subdir inside it) and the normal v2 source rules apply to it.
        Local sources are already material; this is a no-op for them.
        A store is required: the download/extract cache lives under the
        fix home, and fetching without one would have nowhere to go.
        """
        if self._source_ready:
            return
        if store is None:
            raise BuildError("%s: tarball source for %s needs a store to "
                             "materialise into — build it with `fix build` "
                             "or `fix install`, not by hashing it directly"
                             % (self.spec_path, self.name))
        desc = self.source_desc
        tarball = download_tarball(desc["url"], desc["sha256"])
        root = extract_tarball(tarball, desc["sha256"], desc["strip"])
        if not any(root.iterdir()):
            raise SpecError("%s: tarball %s extracted nothing (strip=%d "
                            "may be deeper than the tarball's top level)"
                            % (self.spec_path, desc["url"], desc["strip"]))
        sdir = desc.get("dir", ".")
        sub = root if sdir in (".", "") else (root / sdir).resolve()
        if sub != root and root not in sub.parents:
            raise SpecError("%s: tarball 'dir:' %r escapes the extracted "
                            "tree %s" % (self.spec_path, sdir, root))
        if not sub.is_dir():
            raise SpecError("%s: tarball %s has no directory %r "
                            "(extracted tree: %s)"
                            % (self.spec_path, desc["url"], sdir, root))
        self.source_dir = sub
        self._source_ready = True

    def source_files(self, store=None):
        """{relative/path: entry} for every file that enters a build.

        A tarball source is materialised first (needs a store); local
        sources need no store and behave exactly as before.

        An entry is {"type": "file"|"symlink"|"dir", "sha256": str|None,
        "mode": "0o644" (lstat permission bits), "target": str|None}.
        Hashing covers content *and* mode *and* symlink target, and empty
        directories are recorded (they would otherwise vanish from the
        build). Symlinks are never followed out of the source dir: a
        symlink whose target escapes it is rejected, and an in-repo
        symlink is staged as a symlink, not as the file it points to.
        """
        if self.source_desc.get("type") == "tarball":
            self._materialize(store)
        out = {}
        root = self.source_dir.resolve()
        for path in sorted(self.source_dir.rglob("*")):
            rel = path.relative_to(self.source_dir).as_posix()
            if rel in ("fix.yml", "fix.yaml") and path.parent == self.source_dir:
                continue
            if any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(path.name, pat)
                   for pat in self.excludes):
                continue
            mode = oct(os.lstat(path).st_mode & 0o7777)
            if path.is_symlink():
                target = os.readlink(path)
                tgt = Path(target)
                resolved = tgt.resolve() if tgt.is_absolute() \
                    else (path.parent / target).resolve()
                if resolved != root and root not in resolved.parents:
                    raise SpecError(
                        "%s: symlink %s -> %s escapes the package source "
                        "directory %s" % (self.spec_path, rel, target, root))
                out[rel] = {"type": "symlink", "sha256": None,
                            "mode": mode, "target": target}
                continue
            if path.is_dir():
                if not any(path.iterdir()):  # empty directory: record it
                    out[rel] = {"type": "dir", "sha256": None,
                                "mode": mode, "target": None}
                continue
            if not path.is_file():
                continue
            # A regular file reached through a symlinked parent dir must
            # still resolve inside the source dir (see the symlink check).
            resolved = path.resolve()
            if resolved != root and root not in resolved.parents:
                raise SpecError(
                    "%s: %s resolves outside the package source directory "
                    "%s (symlinked parent?)" % (self.spec_path, rel, root))
            out[rel] = {"type": "file", "sha256": sha256_file(path),
                        "mode": mode, "target": None}
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


def _is_remote_repo_value(value):
    """True if a `repos:` value is a URL (remote repo), not a local path."""
    try:
        scheme = urllib.parse.urlparse(str(value)).scheme
    except ValueError:
        return False
    return scheme in ("http", "https", "file")


def open_repos(fixfile, fetch=True, force_refresh=False):
    """Ordered repo list from the fixfile, plus conventional fallbacks.

    A `repos:` value that is a URL is a remote repo: it is fetched,
    checksum-verified and extracted into ~/.fix/repos/<name>/ on first
    use (and reused from cache afterwards), then used exactly like a
    local repo. With fetch=False, remote repos are only included when
    already cached (used by `fix repo list`, which must not fetch).
    force_refresh re-fetches remote repos (used by `fix repo update`).
    Local entries behave exactly as before.
    """
    repos = []
    seen = set()

    def add(name, path):
        key = str(Path(path).expanduser().resolve() if Path(path).is_absolute()
                  else (fixfile.dir / path).resolve())
        if key not in seen and Path(key).is_dir():
            seen.add(key)
            repos.append(Repo(name, key))

    for name, value in fixfile.repos.items():
        if _is_remote_repo_value(value):
            url, sha = _parse_remote_repo_url(
                value, "%s (repos: %s)" % (fixfile.path, name))
            if fetch:
                path = ensure_remote_repo(name, url, sha,
                                          force_refresh=force_refresh)
                add(name, path)
            else:
                status, path = remote_repo_status(name, url, sha)
                if status == "cached":
                    add(name, path)
            continue
        add(name, value)
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
# https://github.com/gluedarwin/fixpkg · `fix help` for commands
#
# packages: software this environment needs (optionally name@version)
# shell:    name of the shell fix starts (`fix shell`)

packages:
%(packages)s
%(shell)s
# repos:                    # extra spec dirs (vendor/ is always searched)
#   extra: ../shared-pkgs
#   upstream: https://example.com/fix-repo.tar.gz#sha256=<64 hex>
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
        self.extra = {}             # unknown top-level keys, preserved on save

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
        # Unknown top-level keys are kept verbatim so a save (e.g. from
        # `fix install`) does not silently drop metadata fix doesn't know.
        # Comments are NOT preserved: fix rewrites the file from parsed
        # data, so any `# ...` lines are lost on save (see README).
        self.extra = {k: v for k, v in data.items()
                      if k not in ("shell", "repos", "packages")}
        return self

    def save(self):
        if self.path.exists():
            # with_name, not with_suffix: the backup of `fix.yaml` is
            # `fix.yaml.bak`, not `fix.yml.bak`.
            bak = self.path.with_name(self.path.name + ".bak")
            shutil.copy2(self.path, bak)
        data = {"packages": [_fmt_pkg_entry(n, c)
                             for n, c in self.packages.items()]}
        # {"vendor": "vendor"} is the default: open_repos() always searches
        # ./vendor/, so re-emitting it would be noise. Any other repos
        # mapping is written back.
        if self.repos and self.repos != {"vendor": "vendor"}:
            data["repos"] = dict(self.repos)
        if self.shell_name:
            data["shell"] = self.shell_name
        for k, v in self.extra.items():
            if _yaml_value_ok(v):
                data[k] = v
            else:
                warn("dropping %s key %r from %s: value cannot be written "
                     "back as YAML" % (self.path.name, k, self.path))
        write_atomic(self.path, yaml_dumps(data))

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
        write_atomic(ff.path, body)
        return ff


# ---------------------------------------------------------------------------
# resolver — dependency closure + topological order (deps first)
# ---------------------------------------------------------------------------

def resolve(fixfile, repos, wanted=None):
    """Returns (order, specs) where order lists dependencies before dependents."""
    wanted = dict(fixfile.packages if wanted is None else wanted)
    specs = {}

    def visit(name, chain, constraint="*"):
        if name in chain:
            # Check the cycle BEFORE the already-resolved check: specs[name]
            # is assigned before recursing into deps, so a back-edge would
            # otherwise return silently here and only be caught later by
            # the Kahn sort, with no cycle path in the message.
            cyc = chain[chain.index(name):] + [name]
            raise ResolveError("dependency cycle: %s" % " -> ".join(cyc))
        if name in specs:
            if not version_match(constraint, specs[name].version):
                raise ResolveError("dependency conflict: %s requires %s@%s but "
                                   "closure pins %s" % ("→".join(chain) or name,
                                                        name, constraint,
                                                        specs[name].version))
            return
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
    """Deterministic hash over the full build inputs (the 'derivation').

    Inputs, in order: schema tag, name, version, the source descriptor
    (tarball sources only: url, sha256, strip, dir), every source file
    (sorted paths, with content hash, permission mode and symlink
    target), empty directories (mode only), the exact build steps, the
    `env:` mapping, the derivation hashes of all dependencies, and the
    toolchain fingerprint. Source-file entries may be plain sha256
    strings (legacy callers) or the dicts `Spec.source_files` returns.
    """
    h = hashlib.sha256()

    def feed(s):
        h.update(s.encode("utf-8"))
        h.update(b"\x00")

    feed(DERIVATION_SCHEMA)
    feed("name=%s" % spec.name)
    feed("version=%s" % spec.version)
    # The source descriptor feeds the hash only for non-local sources:
    # a tarball's url/checksum/strip/dir are part of its inputs, so
    # changing them changes the store path. Local specs feed nothing
    # extra here — their inputs are already the source files below —
    # so v0.3 local store entries keep exactly the same hash.
    desc = getattr(spec, "source_desc", None) or {"type": "local"}
    if desc.get("type") != "local":
        feed("source=%s" % json.dumps(desc, sort_keys=True))
    for rel in sorted(source_files):
        entry = source_files[rel]
        if isinstance(entry, dict):
            feed("src:%s=%s:%s:%s:%s" % (rel, entry.get("type", "file"),
                                         entry.get("sha256") or "",
                                         entry.get("mode") or "",
                                         entry.get("target") or ""))
        else:  # plain sha256 string (legacy)
            feed("src:%s=%s" % (rel, entry))
    feed("build=%s" % json.dumps(spec.steps, sort_keys=True))
    feed("env=%s" % json.dumps(spec.env, sort_keys=True))
    for dep in sorted(dep_hashes):
        feed("dep:%s=%s" % (dep, dep_hashes[dep]))
    feed("toolchain=%s" % toolchain_fingerprint())
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# remote fetching — tarballs & remote repos (v0.4)
# ---------------------------------------------------------------------------
# Trust model: nothing remote is fetched without a pinned sha256, and
# nothing is used before its checksum verifies. Downloads are streamed
# through a content-addressed cache (~/.fix/cache/<sha256>), so a tarball
# whose checksum matches is never re-downloaded; extractions are staged
# per-sha256 under ~/.fix/cache/extract/ and reused the same way.
# Extraction is safe on every supported Python (the project claims
# Python >= 3.8, and tarfile's filter='data' only exists on 3.11.4+ /
# 3.12+), so every member is validated by hand before it is written:
# absolute paths, `..` components, device/fifo entries, hard links and
# symlinks escaping the extraction dir are all rejected. A remote repo
# is a tarball whose extracted tree is a repo directory; it lives under
# ~/.fix/repos/<name>/ with a .fix-repo.json recording the pinned url
# and sha256, so a failed re-fetch can never clobber the working copy.
# --offline (or FIX_OFFLINE=1) forbids network access outright: cached
# tarballs/repos are used, anything uncached fails loudly. file://
# URLs are local files, not network, so they still work offline.

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TARBALL_SUFFIXES = (".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar")
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024   # 512 MiB
DOWNLOAD_TIMEOUT = 60                    # seconds

_OFFLINE = False   # set by main() from --offline / FIX_OFFLINE=1


def is_offline():
    return _OFFLINE or os.environ.get("FIX_OFFLINE") == "1"


def _validate_fetch_url(url, where):
    """https:// or file:// only; http:// only for localhost (tests)."""
    try:
        parts = urllib.parse.urlparse(url)
    except ValueError as e:
        raise SpecError("%s: bad url %r (%s)" % (where, url, e))
    if parts.scheme == "https" or parts.scheme == "file":
        return
    if parts.scheme == "http" and parts.hostname in (
            "localhost", "127.0.0.1", "::1"):
        return
    raise SpecError("%s: url %r is not allowed — use https:// (or file://); "
                    "plain http:// is only allowed for localhost (tests)"
                    % (where, url))


def _check_tarball_suffix(url, where):
    path = urllib.parse.urlparse(url).path.lower()
    if not path.endswith(TARBALL_SUFFIXES):
        raise SpecError("%s: %r is not a supported tarball (need one of %s)"
                        % (where, url, ", ".join(TARBALL_SUFFIXES)))


def _cache_dir():
    return fix_home() / "cache"


def _repos_dir():
    return fix_home() / "repos"


def download_tarball(url, sha256, what="tarball"):
    """Fetch (or reuse from cache) the tarball for `sha256`.

    Returns the cache path. The checksum is verified BEFORE the file is
    trusted: a cached file whose hash does not match is discarded and
    re-downloaded. Network errors, oversize downloads and checksum
    mismatches raise FixError naming the URL — never a traceback.
    `file://` URLs are read from local disk (no network involved).
    """
    if not SHA256_RE.match(sha256 or ""):
        raise SpecError("%s: bad sha256 %r (need 64 lowercase hex)"
                        % (what, sha256))
    _validate_fetch_url(url, what)
    _check_tarball_suffix(url, what)
    cache = _cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    cached = cache / sha256
    if cached.is_file():
        try:
            if sha256_file(cached) == sha256:
                return cached
        except OSError:
            pass
        # Corrupt cache entry: drop it and re-fetch below.
        try:
            cached.unlink()
        except OSError:
            pass
    if is_offline() and urllib.parse.urlparse(url).scheme != "file":
        # file:// URLs are local files, not network: they still work
        # offline (and are still checksum-verified below).
        raise FixError("%s %s is not cached and --offline forbids network "
                       "access — run once without --offline (or with "
                       "FIX_OFFLINE unset) to fetch it, then retry"
                       % (what, url))
    tmp = None
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "fix/%s" % __version__})
        try:
            resp = urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT)
        except urllib.error.HTTPError as e:
            raise FixError("failed to download %s from %s: HTTP %s %s"
                           % (what, url, e.code, e.reason))
        except urllib.error.URLError as e:
            raise FixError("failed to download %s from %s: %s"
                           % (what, url, e.reason))
        except (OSError, ValueError) as e:
            raise FixError("failed to download %s from %s: %s"
                           % (what, url, e))
        with resp:
            # A redirect must not smuggle in plain http (or another host
            # scheme): the final URL has to pass the same checks.
            _validate_fetch_url(resp.geturl(), what)
            length = resp.headers.get("Content-Length")
            if length is not None:
                try:
                    if int(length) > MAX_DOWNLOAD_BYTES:
                        raise FixError("%s from %s is %s bytes, over the "
                                       "%s limit"
                                       % (what, url, length,
                                          human_size(MAX_DOWNLOAD_BYTES)))
                except ValueError:
                    pass
            fd, tmp_name = tempfile.mkstemp(prefix="fix-dl-",
                                            dir=str(cache))
            tmp = Path(tmp_name)
            h = hashlib.sha256()
            total = 0
            try:
                with os.fdopen(fd, "wb") as f:
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise FixError(
                                "%s from %s exceeds the %s download limit"
                                % (what, url,
                                   human_size(MAX_DOWNLOAD_BYTES)))
                        h.update(chunk)
                        f.write(chunk)
            except FixError:
                raise
            except OSError as e:
                raise FixError("failed to download %s from %s: %s"
                               % (what, url, e))
            if h.hexdigest() != sha256:
                raise FixError("checksum mismatch for %s from %s: "
                               "downloaded sha256 is %s, expected %s — "
                               "not cached, not extracted"
                               % (what, url, h.hexdigest(), sha256))
        os.replace(str(tmp), str(cached))
        tmp = None
        return cached
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def _member_rel(name, strip):
    """The path a tar member lands at, after stripping, or None to skip.

    Returns None for the stripped-away top-level entries themselves.
    Raises SpecError for anything that would escape the destination.
    An absolute member name is rejected outright — stripping must not
    be allowed to launder '/tmp/evil.txt' into 'evil.txt'.
    """
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise SpecError("tarball member %r is an absolute path"
                        % name)
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if len(parts) <= strip:
        return None
    rel_parts = parts[strip:]
    if any(p == ".." for p in rel_parts):
        raise SpecError("tarball member %r escapes the extraction directory"
                        % name)
    return "/".join(rel_parts)


def _safe_extract(tarball_path, dest, strip):
    """Extract a tarball into `dest` (must not exist yet), safely.

    Every member is validated before anything is written: no absolute
    paths, no `..`, no device/fifo entries, no hard links, and symlinks
    whose target escapes the extraction dir are rejected. After
    extraction, the whole tree is walked and any path whose real path
    escapes `dest` (e.g. via a symlinked parent) fails the extraction.
    On any failure `dest` is removed and a FixError is raised.
    """
    dest = Path(dest)
    try:
        try:
            tf = tarfile.open(str(tarball_path), "r:*")
        except (tarfile.TarError, OSError, EOFError) as e:
            raise FixError("cannot read tarball %s: %s"
                           % (relhome(tarball_path), e))
        with tf:
            members = tf.getmembers()
            # Pass 1 — validate every member before writing anything.
            planned = []
            for m in members:
                rel = _member_rel(m.name, strip)
                if rel is None:
                    continue
                if m.ischr() or m.isblk() or m.isfifo():
                    raise FixError("tarball %s: member %r is a device/fifo "
                                   "— refusing to extract"
                                   % (relhome(tarball_path), m.name))
                if m.islnk():
                    raise FixError("tarball %s: member %r is a hard link "
                                   "— refusing to extract"
                                   % (relhome(tarball_path), m.name))
                if m.issym():
                    # Resolve the target textually against the member's
                    # parent dir: it must stay inside the extraction dir.
                    parent = "/".join(rel.split("/")[:-1])
                    target = m.linkname
                    if target.startswith("/"):
                        raise FixError("tarball %s: symlink %r -> %r is "
                                       "absolute — refusing to extract"
                                       % (relhome(tarball_path), m.name,
                                          target))
                    norm = os.path.normpath(
                        os.path.join(parent, target)) if parent \
                        else os.path.normpath(target)
                    if norm == ".." or norm.startswith("../"):
                        raise FixError("tarball %s: symlink %r -> %r "
                                       "escapes the extraction directory"
                                       % (relhome(tarball_path), m.name,
                                          target))
                elif not (m.isfile() or m.isdir()):
                    raise FixError("tarball %s: member %r has unsupported "
                                   "type — refusing to extract"
                                   % (relhome(tarball_path), m.name))
                planned.append((m, rel))
            # Pass 2 — write.
            dest.mkdir(parents=True)
            root = dest.resolve()

            def _parent_inside(out):
                # A file/dir/symlink must not be created through a
                # symlinked parent that points outside `dest`: resolve
                # the parent on disk and require it to stay inside.
                # (os.path.realpath resolves the existing prefix, which
                # is exactly the part that could be a symlink.)
                parent = Path(os.path.realpath(out.parent))
                if parent != root and root not in parent.parents:
                    raise FixError("tarball %s: member %r would be "
                                   "written through a symlinked parent "
                                   "outside the extraction directory"
                                   % (relhome(tarball_path),
                                      out.relative_to(dest)))

            for m, rel in planned:
                out = dest / rel
                if m.isdir():
                    _parent_inside(out)
                    out.mkdir(parents=True, exist_ok=True)
                    try:
                        os.chmod(out, m.mode & 0o777)
                    except OSError:
                        pass
                    continue
                # Check the parent BEFORE creating anything through it:
                # mkdir/open through a symlinked parent could otherwise
                # write outside the extraction directory.
                _parent_inside(out)
                out.parent.mkdir(parents=True, exist_ok=True)
                if m.issym():
                    if out.is_symlink() or out.exists():
                        out.unlink()
                    os.symlink(m.linkname, out)
                    continue
                src = tf.extractfile(m)
                if src is None:
                    raise FixError("tarball %s: cannot read member %r"
                                   % (relhome(tarball_path), m.name))
                with open(out, "wb") as f:
                    shutil.copyfileobj(src, f, 1024 * 1024)
                try:
                    os.chmod(out, m.mode & 0o777)
                except OSError:
                    pass
        # Backstop: no path in the extracted tree may resolve outside
        # `dest` (a file reached through a symlinked parent, ...).
        root = dest.resolve()
        for path in sorted(dest.rglob("*")):
            resolved = path.resolve()
            if resolved != root and root not in resolved.parents:
                raise FixError("tarball %s: extracted path %s resolves "
                               "outside the extraction directory"
                               % (relhome(tarball_path),
                                  path.relative_to(dest)))
    except FixError:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    except (OSError, tarfile.TarError) as e:
        shutil.rmtree(dest, ignore_errors=True)
        raise FixError("failed to extract tarball %s: %s"
                       % (relhome(tarball_path), e))
    return dest


def extract_tarball(tarball_path, sha256, strip):
    """Extract a cached tarball into the per-sha256 staging dir.

    Reuses a previous extraction when its marker matches (same sha256
    and strip). The staging dir lives under ~/.fix/cache/extract/ and
    is keyed by sha256+strip, so the same tarball with a different
    strip extracts separately. A failed extraction leaves nothing
    behind and raises FixError.
    """
    base = _cache_dir() / "extract"
    base.mkdir(parents=True, exist_ok=True)
    dest = base / ("%s-strip%d" % (sha256, strip))
    marker = dest / ".fix-extracted"
    tree = dest / "tree"
    # The marker lives NEXT TO the tree, not inside it: fix's own
    # metadata must never leak into the hashed/built source files.
    if tree.is_dir() and marker.is_file():
        try:
            meta = json.loads(marker.read_text(encoding="utf-8"))
            if meta.get("sha256") == sha256 and meta.get("strip") == strip:
                return tree
        except (ValueError, OSError):
            pass
        shutil.rmtree(dest, ignore_errors=True)
    tmp = Path(tempfile.mkdtemp(prefix="fix-extract-%s-" % sha256[:12],
                                dir=str(base)))
    try:
        _safe_extract(tarball_path, tmp / "tree", strip)
        (tmp / ".fix-extracted").write_text(
            json.dumps({"sha256": sha256, "strip": strip}) + "\n",
            encoding="utf-8")
        try:
            os.rename(str(tmp), str(dest))
        except OSError:
            # Another process extracted the same tarball first: use
            # theirs if it is valid. If the dest is stale (no valid
            # marker), clear it and retry once instead of failing.
            if tree.is_dir() and marker.is_file():
                try:
                    meta = json.loads(marker.read_text(encoding="utf-8"))
                    if meta.get("sha256") == sha256 \
                            and meta.get("strip") == strip:
                        return tree
                except (ValueError, OSError):
                    pass
            shutil.rmtree(dest, ignore_errors=True)
            os.rename(str(tmp), str(dest))
        return tree
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _parse_remote_repo_url(value, where):
    """'https://host/repo.tar.gz#sha256=<hex>' -> (url, sha256).

    The sha256 fragment is required: a remote repo is never fetched
    without a pinned checksum.
    """
    parts = urllib.parse.urlparse(value)
    if parts.scheme not in ("http", "https", "file"):
        raise FixError("%s: remote repo %r must be an http(s):// or "
                       "file:// URL" % (where, value))
    frag = parts.fragment
    if not frag.startswith("sha256="):
        raise FixError("%s: remote repo %r needs a '#sha256=<64 hex>' "
                       "fragment — no checksum, no fetch"
                       % (where, value))
    sha = frag[len("sha256="):]
    if not SHA256_RE.match(sha):
        raise FixError("%s: remote repo %r has a bad sha256 fragment %r "
                       "(need 64 lowercase hex)" % (where, value, sha))
    url = urllib.parse.urlunparse(parts._replace(fragment=""))
    _validate_fetch_url(url, where)
    _check_tarball_suffix(url, where)
    return url, sha


def _remote_repo_name_ok(name, where):
    if not NAME_RE.match(str(name)):
        raise FixError("%s: bad remote repo name %r (must match %s)"
                       % (where, name, NAME_RE.pattern))


def ensure_remote_repo(name, url, sha256, force_refresh=False):
    """Fetch + verify + extract a remote repo into ~/.fix/repos/<name>/.

    Returns the repo directory. A cached copy whose .fix-repo.json
    matches the pinned url+sha256 is reused without a network round
    trip (unless force_refresh). The download is verified before
    extraction, and extraction completes fully before the cache is
    swapped in — a failed verification or extraction keeps the old
    copy untouched and raises FixError.
    """
    _remote_repo_name_ok(name, "remote repo")
    repos = _repos_dir()
    repos.mkdir(parents=True, exist_ok=True)
    dest = repos / name
    meta_path = dest / ".fix-repo.json"
    if not force_refresh and dest.is_dir() and meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("url") == url and meta.get("sha256") == sha256:
                return dest
        except (ValueError, OSError):
            pass
    tarball = download_tarball(url, sha256, what="remote repo %s" % name)
    tmp = Path(tempfile.mkdtemp(prefix="fix-repo-%s-" % name, dir=str(repos)))
    try:
        # Remote repos are extracted at their root (strip 0): the
        # tarball's tree IS the repo directory.
        _safe_extract(tarball, tmp / "tree", strip=0)
        (tmp / "tree" / ".fix-repo.json").write_text(
            json.dumps({"url": url, "sha256": sha256}) + "\n",
            encoding="utf-8")
        # Swap in atomically-ish: move the old copy aside first, so a
        # failure mid-swap can restore it instead of losing the repo.
        old = None
        if dest.is_dir():
            old = repos / (".old-%s" % name)
            if old.exists():
                shutil.rmtree(old, ignore_errors=True)
            os.rename(str(dest), str(old))
        try:
            os.rename(str(tmp / "tree"), str(dest))
        except OSError:
            if old is not None:
                os.rename(str(old), str(dest))
            raise
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
        return dest
    except FixError:
        raise
    except OSError as e:
        raise FixError("failed to install remote repo %s from %s: %s"
                       % (name, url, e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def remote_repo_status(name, url, sha256):
    """('cached'|'stale'|'not cached', Path) for a pinned remote repo."""
    dest = _repos_dir() / name
    meta_path = dest / ".fix-repo.json"
    if not dest.is_dir() or not meta_path.is_file():
        return "not cached", dest
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return "stale", dest
    if meta.get("url") == url and meta.get("sha256") == sha256:
        return "cached", dest
    return "stale", dest


# ---------------------------------------------------------------------------
# builder — staged build with best-effort isolation, then atomic install
# ---------------------------------------------------------------------------

class BuildResult:
    def __init__(self, spec, deriv_hash, path, cached, seconds, log):
        self.spec = spec
        self.hash = deriv_hash
        self.path = path
        self.cached = cached
        self.seconds = seconds
        self.log = log


# ---------------------------------------------------------------------------
# build sandbox — best-effort bwrap isolation, honest fallback
# ---------------------------------------------------------------------------
# The trust model (see README): builds are *not* a security boundary.
# When `bwrap` is available on Linux, build steps run in a user/pid/net
# namespace with the system read-only and only the build temp dir (plus
# dependency store paths, read-only) visible. When bwrap is missing or
# cannot start, fix warns once and builds unisolated — only build
# recipes you trust. `--no-sandbox` opts out explicitly. If mounting
# /proc is refused by the host, the sandbox runs without /proc rather
# than giving up on isolation entirely.

_SANDBOX_WARNED = False
_BWRAP_PROBE = None
_BWRAP_NO_PROC = False

# On usr-merged systems (Debian/Ubuntu: /lib, /lib64, /bin, /sbin are
# symlinks into /usr) the sandbox root starts empty, so those symlinks
# must be recreated inside it — otherwise no dynamically-linked binary
# can exec at all: the loader lives at /lib64/ld-linux, which resolves
# through the /lib64 symlink. Without them the probe fails and fix
# silently falls back to unisolated builds even where bwrap is installed.
# (Verified 2026-11-13: on Ubuntu 24.04 the probe failed until these were
# added.) The symlinks are harmless on systems without the matching
# /usr subdirs — they simply dangle.
_BWRAP_USR_MERGE_SYMLINKS = [
    "--symlink", "usr/lib", "/lib",
    "--symlink", "usr/lib64", "/lib64",
    "--symlink", "usr/lib32", "/lib32",
    "--symlink", "usr/libx32", "/libx32",
    "--symlink", "usr/bin", "/bin",
    "--symlink", "usr/sbin", "/sbin",
]


def _sandbox_available():
    """True only if bwrap exists on Linux and a probe namespace starts.

    Tries the full probe (with /proc) first; if mounting /proc is
    refused by the host (some locked-down VMs deny it), retries without
    /proc — a sandbox without /proc still isolates the filesystem and
    network, which beats building unisolated. _BWRAP_NO_PROC records
    which variant worked so real builds use the same argv.
    """
    global _BWRAP_PROBE, _BWRAP_NO_PROC
    if _BWRAP_PROBE is not None:
        return _BWRAP_PROBE
    _BWRAP_PROBE = False
    if not sys.platform.startswith("linux"):
        return _BWRAP_PROBE
    if shutil.which("bwrap") is None:
        return _BWRAP_PROBE
    base = ["bwrap", "--unshare-user", "--unshare-pid", "--unshare-net",
            "--unshare-ipc", "--unshare-uts", "--die-with-parent",
            "--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc",
            "--dev", "/dev", "--tmpfs", "/tmp"] + _BWRAP_USR_MERGE_SYMLINKS
    for with_proc in (True, False):
        argv = base + (["--proc", "/proc"] if with_proc else []) + ["true"]
        try:
            proc = subprocess.run(
                argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=15)
        except Exception:
            continue
        if proc.returncode == 0:
            _BWRAP_PROBE = True
            _BWRAP_NO_PROC = not with_proc
            return _BWRAP_PROBE
    return _BWRAP_PROBE


def _warn_no_sandbox_once():
    global _SANDBOX_WARNED
    if _SANDBOX_WARNED:
        return
    _SANDBOX_WARNED = True
    if not sys.platform.startswith("linux"):
        reason = "bwrap is only available on Linux"
    elif shutil.which("bwrap") is None:
        reason = "bwrap not found"
    else:
        reason = "bwrap could not start a namespace here"
    warn("no sandbox available (%s) — building unisolated; "
         "only build trusted recipes" % reason)


def _bwrap_argv(tmp, store_root):
    """bwrap invocation: read-only system, writable build tmp only.

    Includes the usr-merge symlinks (see _BWRAP_USR_MERGE_SYMLINKS) and
    omits --proc when the probe found /proc unmountable on this host.
    """
    argv = ["bwrap",
            "--unshare-user", "--unshare-pid", "--unshare-net",
            "--unshare-ipc", "--unshare-uts", "--die-with-parent",
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/etc", "/etc",
            "--dev", "/dev"]
    if not _BWRAP_NO_PROC:
        argv += ["--proc", "/proc"]
    argv += ["--tmpfs", "/tmp"] + _BWRAP_USR_MERGE_SYMLINKS + [
            "--bind", str(tmp), str(tmp),
            "--ro-bind", str(store_root), str(store_root)]
    return argv


def _run_build_step(cmd, cwd, env, tmp, store_root, no_sandbox):
    """Run one build step, sandboxed with bwrap when possible."""
    if no_sandbox:
        return subprocess.run(["bash", "-c", cmd], cwd=str(cwd), env=env,
                              text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
    if not _sandbox_available():
        _warn_no_sandbox_once()
        return subprocess.run(["bash", "-c", cmd], cwd=str(cwd), env=env,
                              text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
    argv = _bwrap_argv(tmp, store_root) + ["bash", "-c", cmd]
    return subprocess.run(argv, cwd=str(cwd), env=env,
                          text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT)


def _valid_store_entry(target, dhash, spec):
    """True if `target` is a complete store entry for this derivation:
    a directory whose .fix-meta.json records the same schema, name and
    hash. Used to tell a lost rename race (harmless) from real damage."""
    try:
        if not target.is_dir():
            return False
        meta = json.loads((target / ".fix-meta.json").read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (isinstance(meta, dict) and meta.get("schema") == DERIVATION_SCHEMA
            and meta.get("name") == spec.name and meta.get("hash") == dhash)


def build_package(store, spec, dep_results, force=False, verbose=False,
                  no_sandbox=False, _lockf=None):
    """Build one package (deps already built). Returns BuildResult."""
    dep_hashes = {n: r.hash for n, r in dep_results.items()}
    dep_paths = {n: r.path for n, r in dep_results.items()}
    source_files = spec.source_files(store)
    dhash = derivation_hash(spec, dep_hashes, source_files)
    target = store.entry(dhash, spec)

    if target.is_dir() and not force:
        return BuildResult(spec, dhash, target, cached=True, seconds=0.0, log=[])

    store.ensure()
    # The lock is taken here (or supplied by build_closure, which holds
    # one lock for the whole closure). Either way, the cache is re-checked
    # AFTER acquiring it: another process may have installed this exact
    # derivation while we were waiting.
    own_lock = _lockf is None
    lockf = store.lock() if own_lock else _lockf
    tmp = None
    try:
        if target.is_dir() and not force:
            return BuildResult(spec, dhash, target, cached=True, seconds=0.0,
                               log=[])
        tmp = Path(tempfile.mkdtemp(prefix="%s-" % spec.name, dir=store.build_dir))
        src = tmp / "src"
        out = tmp / "out"
        src.mkdir()
        for sub in ("bin", "lib", "share", "etc"):
            (out / sub).mkdir(parents=True, exist_ok=True)

        # copy exactly the hashed inputs — hash and build can never diverge.
        # Symlinks are staged as symlinks (preserving their target) and
        # empty directories are recreated, so staging matches the hash.
        for rel in sorted(source_files):
            entry = source_files[rel]
            dst = src / rel
            if isinstance(entry, dict) and entry.get("type") == "dir":
                dst.mkdir(parents=True, exist_ok=True)
                try:
                    os.chmod(dst, int(entry.get("mode") or "0o755", 8))
                except OSError:
                    pass
            elif isinstance(entry, dict) and entry.get("type") == "symlink":
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.is_symlink() or dst.exists():
                    dst.unlink()
                os.symlink(entry.get("target") or "", dst)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(spec.source_dir / rel, dst)

        # Scrubbed, deterministic build environment: nothing from the
        # caller's environment leaks in (no CC/CFLAGS/PATH/HOME/...).
        # PATH is dependency bin dirs, then the fixed system dirs only.
        path_pre = []
        for dep, dpath in sorted(dep_paths.items()):
            if (dpath / "bin").is_dir():
                path_pre.append(str(dpath / "bin"))
        env = {
            "PATH": ":".join(path_pre + ["/usr/local/bin", "/usr/bin", "/bin"]),
            "HOME": str(tmp),
            "SHELL": "/bin/bash",
            "TMPDIR": str(tmp),
            "SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH,
            "TZ": "UTC",
            "LC_ALL": "C",
            "LANG": "C",
            "FIX_OUT": str(out),
            "FIX_SRC": str(src),
            "FIX_ENTRY": str(target),  # final store path FIX_OUT becomes
            "FIX_STORE": str(store.root),
            "FIX_PKG": spec.name,
        }
        for dep, dpath in sorted(dep_paths.items()):
            env["FIX_DEP_%s" % re.sub(r"[^A-Za-z0-9]", "_", dep.upper())] = str(dpath)

        sandbox_used = (not no_sandbox) and _sandbox_available()
        log = []
        t0 = time.time()
        for i, raw_step in enumerate(spec.steps, 1):
            step = spec.interp(raw_step, out, dep_paths)
            cmd = "umask 022; set -e; set -o pipefail; %s" % step
            if verbose:
                print("  %s %s" % (C.dim("$"), C.dim(step)))
            proc = _run_build_step(cmd, src, env, tmp, store.root,
                                   no_sandbox)
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
                                    "  in build dir %s" % relhome(tmp)))

        # sanity: did the build actually install anything? Only real
        # files/symlinks count — out/{bin,lib,share,etc} are pre-created
        # empty dirs, so merely existing is not "produced". (.fix-meta.json
        # is written after this check, so it can never satisfy it.)
        produced = any(
            (p.is_file() or p.is_symlink()) and not p.name.startswith(".fix-")
            for p in out.rglob("*")
        )
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
            "toolchain": toolchain_fingerprint(),
            "sandbox": "bwrap" if sandbox_used else "none",
            "log_tail": log[-3:],
        }
        write_atomic(out / ".fix-meta.json", json.dumps(meta, indent=2))

        if target.is_dir() and force:
            shutil.rmtree(target)
        try:
            os.rename(str(out), str(target))
        except OSError as e:
            if _valid_store_entry(target, dhash, spec):
                # Lost the race: another process installed the same
                # derivation first. Our build is redundant, not a failure.
                return BuildResult(spec, dhash, target, cached=True,
                                   seconds=time.time() - t0, log=log)
            raise BuildError("could not install %s into the store (%s)"
                             % (target, e))
        seconds = time.time() - t0
        return BuildResult(spec, dhash, target, cached=False, seconds=seconds,
                           log=log)
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
        if own_lock:
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
        write_atomic(self.path, json.dumps(self.data, indent=2) + "\n")
        return self.data

    def store_roots(self):
        if not self.data:
            return []
        return [Path(p["store"]).name for p in self.data["packages"].values()]


class GlobalState:
    """~/.fix/state.json — registry of environments (the GC root set).

    A corrupt state file is never silently discarded (that used to reset
    the registry to empty, so `fix gc` could wipe live store entries).
    Instead: the last successfully saved state is kept in
    `state.json.bak`; a corrupt primary is restored from the backup with
    a loud warning, and if no usable backup exists the instance records
    `corrupt` so commands that delete based on GC roots (notably `gc`)
    can refuse outright. `save()` never overwrites a corrupt primary
    without first preserving a timestamped copy of it.
    """

    def __init__(self, store):
        self.path = store.root.parent / "state.json"
        self.bak_path = self.path.with_name(self.path.name + ".bak")
        self.data = {"schema": STATE_SCHEMA, "envs": {}}
        self.corrupt = None          # set to an explanation when unusable
        self.restored_from_backup = False
        if not self.path.is_file():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError as e:
            self._handle_corrupt("unparseable (%s)" % e)
            return
        if not isinstance(loaded, dict) or not isinstance(loaded.get("envs"), dict):
            self._handle_corrupt("invalid shape (expected a mapping with "
                                 "an 'envs' mapping)")
            return
        self.data = loaded

    def _handle_corrupt(self, why):
        """Try the last-good backup; otherwise mark the state corrupt."""
        if self.bak_path.is_file():
            try:
                loaded = json.loads(self.bak_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("envs"), dict):
                    self.data = loaded
                    self.restored_from_backup = True
                    warn("%s is corrupt (%s) — restored the last good state "
                         "from %s; the corrupt file was left in place"
                         % (self.path, why, self.bak_path.name))
                    return
            except ValueError:
                pass
        self.corrupt = why
        warn("%s is corrupt (%s) and no usable backup exists — treating "
             "the registry as empty. `fix gc` will refuse to run until "
             "you restore %s or run `fix purge` to start over"
             % (self.path, why, self.bak_path.name))

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
        text = json.dumps(self.data, indent=2) + "\n"
        if self.corrupt:
            # Never silently overwrite the corrupt bytes: preserve them
            # for forensics first, then record the fresh state.
            try:
                if self.path.is_file():
                    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime())
                    preserved = self.path.with_name(
                        "state.json.corrupt-" + stamp)
                    shutil.copy2(self.path, preserved)
                    warn("preserved corrupt %s as %s"
                         % (relhome(self.path), preserved.name))
            except OSError as e:
                warn("could not preserve corrupt state file: %s" % e)
            self.corrupt = None
            self.restored_from_backup = False
        write_atomic(self.path, text)
        try:
            write_atomic(self.bak_path, text)
        except OSError as e:
            warn("could not write state backup %s: %s" % (self.bak_path, e))


# ---------------------------------------------------------------------------
# environment composition + shell spawning
# ---------------------------------------------------------------------------

def compose_env(fixfile, order, results):
    """Environment dict for `fix shell`: deps' bins + per-package [env].

    Per-package `env:` keys are validated when the spec is loaded (see
    `Spec.from_file`); this also re-checks here, and warns once per key
    when two packages in the closure set the same key — last wins.
    """
    env = dict(os.environ)
    path_pre = []
    seen_env = {}
    warned_dup = set()
    for name in order:                      # order = dependencies first
        r = results[name]
        if (r.path / "bin").is_dir():
            path_pre.append(str(r.path / "bin"))
        env["FIX_PKG_%s" % re.sub(r"[^A-Za-z0-9]", "_", name.upper())] = str(r.path)
        deps = {d: results[d].path for d in r.spec.depends if d in results}
        for key, val in r.spec.env.items():
            if is_blocked_env_key(key):
                raise SpecError(
                    "%s: env key %r is not allowed in `fix shell`"
                    % (r.spec.spec_path, key))
            if key in seen_env and key not in warned_dup:
                warned_dup.add(key)
                warn("duplicate env key %s in %s (overrides %s); last wins"
                     % (key, name, seen_env[key]))
            seen_env[key] = name
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
        warn("already inside a fix shell — nesting anyway; set "
             "FIX_ALLOW_NEST=1 to nest intentionally")

    if cmd_args:
        # One argument is a shell snippet, passed through verbatim so
        # `fix shell -- 'echo $FIX_ENV'` still expands, splits and runs
        # as shell code. Several arguments are argv, joined with
        # shlex.join so quoting survives: `fix shell -- echo "a b"`
        # runs echo with one argument 'a b' (a plain " ".join would
        # re-split it into two). Either way the command runs through
        # the shell, so env vars and pipes behave as in an interactive
        # fix shell.
        cmd = cmd_args[0] if len(cmd_args) == 1 else shlex.join(cmd_args)
        rc = subprocess.run([shell, "-c", cmd], env=env).returncode
        return rc

    env["FIX_SHELL"] = "1"
    if sys.stdout.isatty():
        env["PS1"] = "(fix:%s) %s" % (fixfile.shell_name or fixfile.dir.name,
                                    env.get("PS1", ""))
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
                  targets=None, no_sandbox=False):
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

    # One store lock for the whole closure: the builds and the lock/state
    # recording are atomic with respect to other fix processes. The lock
    # is passed down to build_package — flock is not re-entrant across
    # file descriptions in a single process, so build_package must not
    # take a second lock while this one is held.
    lockf = store.lock()
    results = {}
    lock = Lock(fixfile)
    state = None
    try:
        for name in order:
            spec = specs[name]
            dep_results = {d: results[d] for d in spec.depends if d in results}
            r = build_package(store, spec, dep_results, force=force,
                              verbose=verbose, no_sandbox=no_sandbox,
                              _lockf=lockf)
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

        try:
            lock.load()
        except LockError:
            # A corrupt fix.lock is not fatal to a rebuild: the fresh
            # write below replaces it (Lock.write tolerates data=None).
            lock.data = None
        lock.write(order, results)

        state = GlobalState(store)
        state.touch(str(fixfile.dir), fixfile.shell_name or fixfile.dir.name,
                    lock.path, lock.store_roots())
        state.save()
    finally:
        try:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)
            lockf.close()
        except OSError:
            pass

    if not quiet:
        fresh = [n for n in order if not results[n].cached]
        cached = len(order) - len(fresh)
        info("environment ready — %d package(s) [%d built, %d cached], state "
             "recorded in %s" % (len(order), len(fresh), cached, LOCK_NAME))
    return results, lock, state


def ensure_built(fixfile, store, verbose=False, no_sandbox=False):
    """Return the built closure, rebuilding whenever the lock cannot be
    trusted. The lock is trusted only if, after re-resolving and
    re-deriving every derivation hash (v2 hashing: contents, modes,
    symlinks, toolchain), the closure, hashes and store paths all match
    and every recorded store entry still exists and agrees with its
    metadata. Any mismatch — edited recipe/source, changed mode, missing
    or tampered store entry, corrupt lock — falls back to build_closure.
    A resolve failure (e.g. a missing dependency) is a `fix:` error, not
    a silent rebuild: rebuilding would fail the same way. This costs one
    resolve + hash pass, the same work build_closure does anyway."""
    lockpath = fixfile.dir / LOCK_NAME
    if not lockpath.is_file():
        return build_closure(fixfile, store, verbose=verbose,
                             no_sandbox=no_sandbox)
    lock = Lock(fixfile)
    try:
        lock.load()
    except FixError:
        # Corrupt lock: rebuild (build_closure tolerates the corrupt
        # load and writes a fresh lock).
        return build_closure(fixfile, store, verbose=verbose,
                             no_sandbox=no_sandbox)
    if not isinstance(lock.data, dict):
        return build_closure(fixfile, store, verbose=verbose,
                             no_sandbox=no_sandbox)
    repos = open_repos(fixfile)
    order, specs = resolve(fixfile, repos)
    if list(order) != list(lock.data.get("closure") or []):
        return build_closure(fixfile, store, verbose=verbose,
                             no_sandbox=no_sandbox)
    packages = lock.data.get("packages") or {}
    results = {}
    for name in order:
        meta = packages.get(name)
        spec = specs.get(name)
        if not isinstance(meta, dict) or spec is None:
            return build_closure(fixfile, store, verbose=verbose,
                                 no_sandbox=no_sandbox)
        dep_results = {d: results[d] for d in spec.depends if d in results}
        dep_hashes = {n: r.hash for n, r in dep_results.items()}
        # A bad spec/source here raises FixError → surfaced as a `fix:` error.
        # Tarball sources are materialised (fetched) here, so hashing a
        # tarball package needs the network/cache like building it does.
        dhash = derivation_hash(spec, dep_hashes, spec.source_files(store))
        store_path = Path(meta.get("store") or "")
        if (dhash != meta.get("hash")
                or store_path != store.entry(dhash, spec)
                or not _valid_store_entry(store_path, dhash, spec)):
            return build_closure(fixfile, store, verbose=verbose,
                                 no_sandbox=no_sandbox)
        results[name] = BuildResult(spec, dhash, store_path, cached=True,
                                    seconds=0.0, log=[])
    if not results and order:
        return build_closure(fixfile, store, verbose=verbose,
                             no_sandbox=no_sandbox)
    return results, lock, None


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

HELP = """\
fix %s — a simple, source-based and declarative package manager

usage: fix [options] <command> [args]

commands:
  init [--shell NAME] [pkg...]
                        create a new environment (scaffold fix.yml + vendor/)
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
  repo list         list configured repos (local/remote, cache status)
  repo update [name]  re-fetch remote repos (or the named one)
  path [pkg]        print store path(s) (scripting friendly)
  gc [--dry-run]    delete store entries not referenced by any environment
  purge             remove the entire fix store & state (~/.fix)
  doctor            sanity-check toolchain, store & permissions

options:
  --file PATH       use this environment file (default: fix.yml)
  --no-color        disable colored output
  -v, --verbose     show build steps as they run
  -q, --quiet       only report errors
  --offline         no network access: use cached tarballs/repos only
                    (also: FIX_OFFLINE=1)
  --no-sandbox      build without bwrap isolation (build/install/shell)

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
    Fixfile.create(ffpath, args.shell, args.packages)
    vendor = ffpath.parent / "vendor"
    if not vendor.exists():
        vendor.mkdir(parents=True, exist_ok=True)
        (vendor / "README").write_text(
            "put package specs here as <name>/fix.yml or <name>.yml\n"
            "see `fix search .` and the project README for the spec format\n",
            encoding="utf-8")
    print("\nWrote %s" % relhome(ffpath))
    info("add packages with `fix install <name>`"
         + ("" if args.shell else ", name the shell with `fix init --shell NAME`")
         + ", then run `fix shell`")
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
                                     verbose=args.verbose, quiet=args.quiet,
                                     no_sandbox=args.no_sandbox)
    if not results and not args.quiet:
        info("nothing to do")


def cmd_shell(args):
    ff = _load_fixfile(args)
    store = Store()
    results, lock, _ = ensure_built(ff, store, verbose=args.verbose,
                                    no_sandbox=args.no_sandbox)
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
                  targets={n for n, _ in added}, no_sandbox=args.no_sandbox)
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
    packages = data.get("packages")
    if not isinstance(packages, dict):
        warn("fix.lock is inconsistent (packages is not a mapping) — "
             "run `fix build` to regenerate")
        packages = {}
    closure = data.get("closure") or []
    if not isinstance(closure, list):
        warn("fix.lock is inconsistent (closure is not a list) — "
             "run `fix build` to regenerate")
        closure = []
    for name in closure:
        p = packages.get(name)
        if not isinstance(p, dict):
            warn("fix.lock entry for %s is missing or incomplete — "
                 "run `fix build` to regenerate" % name)
            continue
        store_path = p.get("store") or ""
        size = human_size(dir_size(store_path)) if store_path \
            and Path(store_path).is_dir() else "-"
        deps = p.get("deps") or []
        print("  %s%-28s%s %s  %-7s %-8s %s"
              % (C.green("\u25cf"), "%s %s" % (name, p.get("version") or "?"),
                 C.dim(""), str(p.get("hash") or "")[:12], size,
                 ("needs " + " ".join(deps)) if deps else "-",
                 relhome(store_path)))[:100]
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
    if ff.path.is_file():
        # Load the file: without this, `repos:` entries in fix.yml are
        # ignored and search only sees ./vendor/ and ~/.fix/repo/.
        ff.load()
        repos = open_repos(ff)
    else:
        repos = [Repo("vendor", ff.dir / "vendor"),
                 Repo("user", fix_home() / "repo")]
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
            # NOTE: the gap is a padded empty string, not the name: %-12s
            # would count the colour escape codes in the green name.
            print("%s%-12s %-8s %s" % (C.green(name), "", spec.version,
                                       spec.description or C.dim("(no description)")))
    if not hits:
        info("no packages matched %r (repos: %s)"
             % (term, ", ".join(r.name for r in repos) or "none"))


def cmd_repo_list(args):
    ff = _load_fixfile(args)
    print("%-12s %-8s %s" % (C.bold("REPO"), C.bold("KIND"),
                             C.bold("LOCATION / STATUS")))
    for name, value in ff.repos.items():
        if _is_remote_repo_value(value):
            try:
                url, sha = _parse_remote_repo_url(
                    value, "%s (repos: %s)" % (ff.path, name))
            except FixError as e:
                print("%-12s %-8s %s" % (name, "remote", C.red(str(e))))
                continue
            status, path = remote_repo_status(name, url, sha)
            if status == "cached":
                note = "cached (sha256 %s…)" % sha[:12]
            elif status == "stale":
                note = ("cached copy is for a different sha256 "
                        "(pinned %s…) — run `fix repo update %s`"
                        % (sha[:12], name))
            else:
                note = ("not cached — fetched on first use "
                        "(pinned sha256 %s…)" % sha[:12])
            print("%-12s %-8s %s\n%-12s %-8s %s" % (
                name, "remote", url, "", "", C.dim("%s · %s"
                                                    % (relhome(path), note))))
        else:
            p = Path(value).expanduser()
            if not p.is_absolute():
                p = (ff.dir / p).resolve()
            print("%-12s %-8s %s" % (name, "local",
                                     relhome(p) + (" (missing)"
                                                  if not p.is_dir() else "")))
    for name, path in (("vendor", ff.dir / "vendor"),
                       ("user", Path.home() / ".fix" / "repo")):
        print("%-12s %-8s %s" % (name, "local",
                                 relhome(path) + (" (conventional, missing)"
                                                  if not path.is_dir()
                                                  else " (conventional)")))


def cmd_repo_update(args):
    ff = _load_fixfile(args)
    remote = {}
    for name, value in ff.repos.items():
        if _is_remote_repo_value(value):
            url, sha = _parse_remote_repo_url(
                value, "%s (repos: %s)" % (ff.path, name))
            remote[name] = (url, sha)
    if args.name:
        if args.name not in remote:
            die("no remote repo %r in %s (remote repos: %s)"
                % (args.name, ff.path.name,
                   ", ".join(sorted(remote)) or "none"))
        targets = [args.name]
    else:
        targets = sorted(remote)
    if not targets:
        info("no remote repos configured in %s" % ff.path.name)
        return
    failures = []
    for name in targets:
        url, sha = remote[name]
        try:
            ensure_remote_repo(name, url, sha, force_refresh=True)
            ok("updated %s from %s" % (name, url))
        except FixError as e:
            # The old cache is untouched (ensure_remote_repo only
            # swaps it in after a verified extraction) — report the
            # failure loudly and keep going with the other repos.
            failures.append((name, str(e)))
            warn("could not update %s: %s (kept the previous cached copy)"
                 % (name, e))
    if failures:
        die("failed to update %s — previous cached copies kept"
            % ", ".join(n for n, _ in failures))


def cmd_repo(args):
    if args.repo_cmd == "list":
        return cmd_repo_list(args)
    if args.repo_cmd == "update":
        return cmd_repo_update(args)
    die("fix repo: expected 'list' or 'update' "
        "(see `fix help`)")


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
    if state.corrupt:
        # The root set is unknown: deleting anything could wipe live
        # environments. Refuse outright — even for --dry-run, whose
        # output would wrongly report every entry as collectable.
        die("%s is corrupt (%s) and no usable backup exists — refusing "
            "to delete store entries because the GC roots are unknown. "
            "Restore %s, or run `fix purge` to start over"
            % (state.path, state.corrupt, state.bak_path.name))
    # gc serializes on the store lock for its whole run (including
    # --dry-run): a build recording roots in state.json while gc reads
    # them could otherwise lose a live entry. If the lock cannot be
    # taken, fail rather than proceeding unprotected.
    try:
        lockf = store.lock()
    except OSError as e:
        die("cannot lock the store for gc (%s) — another fix process "
            "may be running; try again" % e)
    try:
        if args.dry_run:
            # A dry run is side-effect-free: no pruning, no saving, no
            # deleting — it only reports what a real gc would remove.
            roots = state.all_roots()
            try:
                ff = Fixfile.locate(args.file)
                ff.load()
                lock = Lock(ff)
                if lock.load():
                    roots.update(lock.store_roots())
            except FixError:
                pass
            removed, freed = store.gc(roots, dry_run=True)
        else:
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
            removed, freed = store.gc(roots, dry_run=False)
            if removed:
                state.save()
    finally:
        lockf.close()
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


def cmd_purge(args):
    home = fix_home()
    if not home.exists():
        info("nothing to purge (%s does not exist)" % relhome(home))
        return
    if not args.yes:
        if not sys.stdin.isatty():
            # Non-interactive deletion without an explicit -y would be
            # silent data loss (the old code only prompted on a TTY and
            # otherwise deleted unconditionally).
            die("refusing to purge %s without -y in non-interactive mode "
                "— run `fix purge -y` to confirm" % relhome(home))
        try:
            answer = input("fix: remove %s entirely? [y/N] " % relhome(home))
        except EOFError:
            answer = "n"
        if answer.strip().lower() not in ("y", "yes"):
            die("aborted")
    shutil.rmtree(home, ignore_errors=True)
    ok("removed %s" % relhome(home))


def _dir_writable(p):
    """True if a file can actually be created and removed inside dir p.

    This is the writability test doctor needs: os.access() answers the
    wrong question on some setups, and creating the directory to test it
    would make a diagnostic change state. A missing directory is not
    tested here — the caller reports it as 'not created yet'.
    """
    try:
        fd, tmp = tempfile.mkstemp(dir=str(p))
    except OSError:
        return False
    try:
        os.close(fd)
        os.unlink(tmp)
    except OSError:
        return False
    return True


def cmd_doctor(args):
    checks = []
    home = fix_home()
    if home.is_dir():
        checks.append(("home %s writable" % relhome(home),
                       _dir_writable(home)))
    elif home.exists():
        checks.append(("home %s is not a directory" % relhome(home), False))
    else:
        # A diagnostic must not create anything: report it as missing.
        checks.append(("home %s (not created yet)" % relhome(home), True))
    store = Store()
    if store.root.is_dir():
        checks.append(("store %s writable" % relhome(store.root),
                       _dir_writable(store.root)))
    elif store.root.exists():
        checks.append(("store %s is not a directory" % relhome(store.root),
                       False))
    else:
        checks.append(("store %s (not created yet)" % relhome(store.root),
                       True))
    for tool, needed in (("bash", True), ("cc", True), ("make", False),
                         ("tar", False)):
        path = shutil.which(tool)
        checks.append(("%-5s %s" % (tool, path or C.red("NOT FOUND")),
                       path is not None if needed else True))
    # Informational only: sandbox availability never fails doctor.
    bwrap = shutil.which("bwrap")
    if sys.platform.startswith("linux"):
        checks.append(("sandbox bwrap %s"
                       % (bwrap or "NOT FOUND — builds run unisolated"), True))
    else:
        checks.append(("sandbox bwrap n/a on %s" % sys.platform, True))
    # Informational only: a corrupt state file never fails doctor, but
    # it is reported here (gc refuses while it stays corrupt).
    state = GlobalState(store)
    if state.corrupt:
        checks.append(("state %s corrupt (%s) — no usable backup"
                       % (relhome(state.path), state.corrupt), True))
    elif state.restored_from_backup:
        checks.append(("state %s corrupt — restored from %s"
                       % (relhome(state.path), state.bak_path.name), True))
    elif state.path.is_file():
        checks.append(("state %s parses" % relhome(state.path), True))
    else:
        checks.append(("state %s (none recorded yet)" % relhome(state.path),
                       True))
    # The current project's lock, if we are inside a project: a corrupt
    # lock is a real problem (status fails on it), so it fails doctor.
    # No fixfile here means no project — informational, not a failure.
    try:
        ff = Fixfile.locate("fix.yml")
        ff.load()
        lock = Lock(ff)
        if lock.path.is_file():
            try:
                lock.load()
                checks.append(("lock %s parses" % relhome(lock.path), True))
            except FixError:
                checks.append(
                    ("lock %s is corrupt — run `fix build` to regenerate"
                     % relhome(lock.path), False))
        else:
            checks.append(("lock %s (none recorded yet)" % relhome(lock.path),
                           True))
    except FixError:
        checks.append(("lock (no fix environment in this directory)", True))
    # Orphaned build sandboxes (left by a killed build): informational
    # only — they are safe to delete by hand, never a doctor failure.
    build_dir = store.build_dir
    if build_dir.is_dir():
        orphans = [d for d in build_dir.iterdir() if d.is_dir()]
        if orphans:
            checks.append(("build %s: %d orphaned build dir(s) — safe to "
                           "remove by hand" % (relhome(build_dir),
                                               len(orphans)), True))
    if not all(r for _, r in checks):
        warn("failed checks above need attention; missing tools are only "
             "needed if packages you build require them")
    failed = [name for name, r in checks if not r]
    for name, r in checks:
        print("  %s %s" % (C.green("✔") if r else C.red("✘"), name))
    if failed:
        die("doctor: %d check(s) failed" % len(failed))
    ok("all critical checks passed")


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
    ap.add_argument("--offline", action="store_true",
                    help="no network access: use cached tarballs/repos only")
    ap.add_argument("-h", "--help", action="store_true")
    ap.add_argument("--version", action="store_true")
    sub = ap.add_subparsers(dest="cmd", metavar="<command>")

    p = sub.add_parser("init")
    p.add_argument("packages", nargs="*")
    p.add_argument("--force", action="store_true")
    p.add_argument("--shell", default=None, metavar="NAME",
                   help="name for the shell (`shell:` in fix.yml)")

    p = sub.add_parser("build")
    p.add_argument("-f", "--force", action="store_true")
    p.add_argument("--no-sandbox", action="store_true")

    p = sub.add_parser("shell")
    p.add_argument("cmd_args", nargs="*")
    p.add_argument("--no-sandbox", action="store_true")

    p = sub.add_parser("install", aliases=["add"])
    p.add_argument("packages", nargs="+")
    p.add_argument("--no-sandbox", action="store_true")

    p = sub.add_parser("remove", aliases=["rm"])
    p.add_argument("packages", nargs="+")

    sub.add_parser("upgrade")
    sub.add_parser("list")
    sub.add_parser("status")
    sub.add_parser("tree")

    p = sub.add_parser("search")
    p.add_argument("term", nargs="?")

    p = sub.add_parser("repo")
    rs = p.add_subparsers(dest="repo_cmd", metavar="<repo-command>")
    rs.add_parser("list")
    pu = rs.add_parser("update")
    pu.add_argument("name", nargs="?", help="remote repo to update")

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
        "search": cmd_search, "repo": cmd_repo, "path": cmd_path, "gc": cmd_gc,
        "purge": cmd_purge, "doctor": cmd_doctor,
    }
    try:
        # --offline (or FIX_OFFLINE=1) is honoured from here on: no
        # command below may touch the network unless it is cached.
        global _OFFLINE
        _OFFLINE = bool(args.offline)
        rc = dispatch[args.cmd](args)
        return rc if isinstance(rc, int) else 0
    except FixError as e:
        die(str(e))
    except KeyboardInterrupt:
        die("interrupted", 130)
    except (OSError, KeyError, AttributeError, ValueError) as e:
        # No user-facing command may ever print a raw traceback for
        # these: they almost always mean a corrupt or inconsistent
        # lock/state file, which has a concrete remedy.
        die("unexpected error: %s — check fix.lock and ~/.fix/state.json "
            "for corruption, then run `fix build` to regenerate state "
            "(or `fix doctor` for a diagnosis)" % e)


if __name__ == "__main__":
    sys.exit(main())
