#!/usr/bin/env python3
"""Unit tests for the P1 fixes (stdlib only).

Run:  python3 tests/test_p1.py
Plain asserts, no pytest. Each test is a function; `main` runs them all
and prints a pass/fail summary. Covers:
  P1-5  mini-YAML parity with PyYAML, flow values, lists of mappings,
        duplicate keys, unexpected indentation, non-dict top level
  P1-6  strict version constraints (v prefix, ~, wildcards, garbage)
  P1-7  resolver cycle detection names the cycle path
  P1-8  gc --dry-run is side-effect-free; gc takes the store lock
  P1-9  doctor tests writability for real and never creates dirs
  P1-10 purge refuses without -y when stdin is not a TTY
  P1-11 corrupt lock -> fix: errors, never tracebacks (status/tree/shell)
  P1-12 shell -- cmd preserves quoting; nesting warning names FIX_ALLOW_NEST
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fix  # noqa: E402

PASS = 0
FAIL = 0
FIXPY = Path(__file__).resolve().parent.parent / "fix.py"


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s %s" % (name, detail))


def expect_raises(name, exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc as e:
        check(name, True)
        return str(e)
    except Exception as e:  # wrong exception type
        check(name, False, "(raised %s: %s)" % (type(e).__name__, e))
        return ""
    check(name, False, "(no exception raised)")
    return ""


def _set_home(td):
    home = Path(td) / "fixhome"
    os.environ["FIX_TEST_HOME"] = str(home)
    return home


def make_project(td, name="tiny"):
    """A minimal project: fix.yml + vendor/<name>/ with a source file
    whose contents land in the store, so edits are observable."""
    proj = Path(td) / "proj"
    pkg = proj / "vendor" / name
    pkg.mkdir(parents=True, exist_ok=True)
    (proj / "fix.yml").write_text(
        "packages:\n  - %s\n\nshell: test\n" % name, encoding="utf-8")
    (pkg / "fix.yml").write_text(
        "package:\n  name: %s\n  version: 1.0.0\n\nbuild:\n  steps:\n"
        '    - "cp a.txt %%(out)s/share/a.txt"\n'
        '    - "mkdir -p %%(out)s/bin && printf \'hi\' > %%(out)s/bin/%s"\n'
        % (name, name), encoding="utf-8")
    (pkg / "a.txt").write_text("hello\n", encoding="utf-8")
    ff = fix.Fixfile(proj / "fix.yml")
    ff.load()
    return ff, pkg


def _write_spec(pkgdir, name, version="1.0.0", depends=None):
    pkgdir.mkdir(parents=True, exist_ok=True)
    text = "package:\n  name: %s\n  version: %s\n\n" % (name, version)
    if depends:
        text += "depends:\n"
        for dep, con in depends.items():
            text += '  %s: "%s"\n' % (dep, con)
        text += "\n"
    text += "build:\n  steps:\n    - \"true\"\n"
    (pkgdir / "fix.yml").write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# P1-5: mini-YAML parity
# ---------------------------------------------------------------------------

PARITY_FIXTURES = [
    ("flow list",
     "packages: [hello, banner]\n\nshell: dev\n"),
    ("flow list, quoted items",
     "packages: [hello, 'ban ner', \"quo:ted\"]\n"),
    ("flow map",
     "depends: {banner: \"*\", greet: \"^1.2\"}\n"),
    ("nested flow",
     "a: {b: [1, 2], c: {d: x}}\n"),
    ("list of mappings",
     "items:\n  - name: a\n    version: 1.0\n  - name: b\n    version: 2.0\n"),
    ("list of mappings with nested list",
     "items:\n  - name: x\n    steps:\n      - one\n      - two\n  - name: y\n"),
    ("4-space indent",
     "a:\n    b:\n        c: 1\n    d: 2\n"),
    ("quoted keys",
     "\"my key\": v\n'other': 2\n"),
    ("quoted list items stay scalars, not keys",
     "steps:\n  - \"cc -O2 -o %(out)s/bin/x x.c\"\n  - 'single quoted'\n"),
    ("comments and empty list",
     "# a comment\npackages: []\nempty:\n"),
    ("scalar types",
     "a: 1\nb: 1.5\nc: true\nd: null\ne: on\nf: off\n"),
    ("url scalar (colon, no space)",
     "url: http://example.com/x\n"),
    ("single-quote escape",
     "a: 'it''s'\n"),
    ("double-quote escape",
     'a: "x\\ny"\n'),
]


def test_yaml_parity():
    try:
        import yaml  # noqa
    except ImportError:
        print("  skip yaml parity — PyYAML not installed")
        return
    import yaml
    for name, text in PARITY_FIXTURES:
        mini = fix._mini_yaml_loads(text)
        py = yaml.safe_load(text)
        check("yaml parity: %s" % name, mini == py,
              "(mini=%r pyyaml=%r)" % (mini, py))
    # yaml_loads must agree too, on both backends.
    for name, text in PARITY_FIXTURES:
        os.environ["FIX_MINI_YAML"] = "1"
        try:
            mini = fix.yaml_loads(text)
        finally:
            os.environ.pop("FIX_MINI_YAML", None)
        check("yaml_loads agrees (mini): %s" % name,
              mini == yaml.safe_load(text))
    check("yaml_loads empty text is {}", fix.yaml_loads("") == {})


def test_yaml_errors():
    # Duplicate keys: both fix backends must reject; neither silently wins.
    # (Raw PyYAML itself silently takes the last value — fix checks the
    # node tree first so the PyYAML backend agrees with the mini parser.)
    dup = "a: 1\na: 2\n"
    expect_raises("mini yaml: duplicate key rejected",
                  fix.SpecError, fix._mini_yaml_loads, dup)
    expect_raises("yaml_loads (PyYAML backend): duplicate key rejected",
                  fix.SpecError, fix.yaml_loads, dup)
    os.environ["FIX_MINI_YAML"] = "1"
    try:
        expect_raises("yaml_loads (mini backend): duplicate key rejected",
                      fix.SpecError, fix.yaml_loads, dup)
    finally:
        os.environ.pop("FIX_MINI_YAML", None)
    expect_raises("mini yaml: duplicate key in flow map rejected",
                  fix.SpecError, fix._mini_yaml_loads, "a: {x: 1, x: 2}\n")
    # Unexpected indentation is an error, not a silent drop.
    expect_raises("mini yaml: unexpected indentation rejected",
                  fix.SpecError, fix._mini_yaml_loads,
                  "a:\n  b: 1\n   c: 2\n")
    # Top-level list is not a mapping.
    expect_raises("mini yaml: top-level list rejected",
                  fix.SpecError, fix._mini_yaml_loads, "- a\n- b\n")
    expect_raises("yaml_loads: top-level list rejected",
                  fix.SpecError, fix.yaml_loads, "- a\n- b\n")
    # Malformed flow values are errors, not literal strings.
    for bad in ("a: [1, 2\n", "a: [1, 2] junk\n", "a: {x: 1\n",
                "a: [\"unterminated]\n"):
        expect_raises("mini yaml: malformed flow rejected %r" % bad,
                      fix.SpecError, fix._mini_yaml_loads, bad)
    # A spec file with a non-dict top level is a SpecError, not a crash.
    with tempfile.TemporaryDirectory() as td:
        spec = Path(td) / "fix.yml"
        spec.write_text("- a\n- b\n", encoding="utf-8")
        expect_raises("spec with list top level -> SpecError",
                      fix.SpecError, fix.Spec.from_file, spec, None)


# ---------------------------------------------------------------------------
# P1-6: version constraints
# ---------------------------------------------------------------------------

VERSION_CASES = [
    ("*", "1.2.3", True), ("", "1.2.3", True),
    ("1.2.0", "1.2.0", True), ("1.2.0", "1.2.1", False),
    ("1.2", "1.2.0", True), ("1", "1.0.0", True),
    ("v1.2", "1.2.0", True), ("v1.2.0", "v1.2.0", True),
    ("^1.2", "1.2.3", True), ("^1.2", "2.0.0", False),
    ("^1.2", "1.9.9", True), ("^0.2", "0.2.5", True),
    ("^0.2", "0.3.0", False), ("^0.0.3", "0.0.3", True),
    ("^0.0.3", "0.0.4", False), ("^v1.2", "1.2.3", True),
    ("~1.2", "1.2.9", True), ("~1.2", "1.3.0", False),
    ("~1.2.3", "1.2.9", True), ("~1.2.3", "1.3.0", False),
    ("~1", "1.9.0", True), ("~1", "2.0.0", False),
    (">=1,<2", "1.5.0", True), (">=1,<2", "2.0.0", False),
    (">=1,<2", "0.9.9", False), (">=1.2.3", "1.2.3", True),
    (">1.2.3", "1.2.3", False), ("<2", "1.9.9", True),
    ("<=2.0.0", "2.0.0", True),
    ("=1.2.0", "1.2.0", True), ("==1.2.0", "1.2.0", True),
    ("=1.2.0", "1.2.1", False),
    ("1.*", "1.9.9", True), ("1.*", "2.0.0", False),
    ("1.2.*", "1.2.9", True), ("1.2.*", "1.3.0", False),
    ("1.2.x", "1.2.0", True), ("*.*", "9.9.9", True),
]

VERSION_ERRORS = [
    (">=foo", "1.0.0"), ("^foo", "1.0.0"), ("~foo", "1.0.0"),
    ("^v1.x", "1.0.0"), ("1.2.3.4", "1.2.3.4"),
    ("1.2.3", "1.2.3.4"), ("1.2.3", "1.2.3-rc1"),
    ("1.2.*.3", "1.2.3"), ("1.2.3", "v1.2.x"),
    (">=", "1.0.0"), ("^", "1.0.0"), ("~", "1.0.0"),
    ("1..2", "1.0.0"), ("foo", "1.0.0"),
]


def test_version_table():
    for constraint, version, expected in VERSION_CASES:
        got = fix.version_match(constraint, version)
        check("version %r ~ %r == %s" % (constraint, version, expected),
              got == expected, "(got %s)" % got)
    for constraint, version in VERSION_ERRORS:
        expect_raises("version %r ~ %r rejected" % (constraint, version),
                      fix.SpecError, fix.version_match, constraint, version)
    # A spec with a non-plain version is rejected at load time.
    with tempfile.TemporaryDirectory() as td:
        pkg = Path(td) / "pkg"
        _write_spec(pkg, "pkg", version="1.2.3-rc1")
        expect_raises("spec version 1.2.3-rc1 rejected at load",
                      fix.SpecError, fix.Spec.from_file,
                      pkg / "fix.yml", None)
        _write_spec(pkg, "pkg", version="v1.2.0")
        spec = fix.Spec.from_file(pkg / "fix.yml", None)
        check("spec version v1.2.0 loads", spec.version == "v1.2.0")


# ---------------------------------------------------------------------------
# P1-7: resolver cycle path
# ---------------------------------------------------------------------------

def test_cycle_names_path():
    with tempfile.TemporaryDirectory() as td:
        _set_home(td)
        try:
            proj = Path(td) / "proj"
            _write_spec(proj / "vendor" / "a", "a", depends={"b": "*"})
            _write_spec(proj / "vendor" / "b", "b", depends={"a": "*"})
            (proj / "fix.yml").write_text(
                "packages:\n  - a\n", encoding="utf-8")
            ff = fix.Fixfile(proj / "fix.yml")
            ff.load()
            msg = expect_raises("cyclic specs raise ResolveError",
                                fix.ResolveError,
                                fix.resolve, ff, fix.open_repos(ff))
            check("cycle error names the path a -> b -> a",
                  "a -> b -> a" in msg, "(%r)" % msg)
            # A diamond (a -> b, a -> c, b -> c) is not a cycle.
            _write_spec(proj / "vendor" / "a", "a",
                        depends={"b": "*", "c": "*"})
            _write_spec(proj / "vendor" / "b", "b", depends={"c": "*"})
            _write_spec(proj / "vendor" / "c", "c")
            order, _specs = fix.resolve(ff, fix.open_repos(ff))
            check("diamond resolves (order deps-first)",
                  order.index("c") < order.index("a"), "(%r)" % (order,))
        finally:
            os.environ.pop("FIX_TEST_HOME", None)


# ---------------------------------------------------------------------------
# P1-8: gc --dry-run is side-effect-free; gc takes the lock
# ---------------------------------------------------------------------------

def test_gc_dry_run_no_mutation():
    with tempfile.TemporaryDirectory() as td:
        home = _set_home(td)
        try:
            ff, _pkg = make_project(td)
            store = fix.Store()
            fix.build_closure(ff, store, quiet=True, no_sandbox=True)
            orphan = store.root / "deadbeef-orphan-9.9.9"
            (orphan / "bin").mkdir(parents=True)
            (orphan / "bin" / "x").write_text("x", encoding="utf-8")
            # A stale env entry (project dir long gone) must survive dry-run.
            state = fix.GlobalState(store)
            state.data["envs"]["/nonexistent/proj"] = {
                "name": "gone", "lockfile": "/nonexistent/proj/fix.lock",
                "roots": ["deadbeef-gone-1.0.0"], "updated": "x"}
            state.save()
            before_state = (home / "state.json").read_bytes()
            before_entries = sorted(p.name for p in store.root.iterdir())
            buf = io.StringIO()
            with redirect_stdout(buf):
                fix.cmd_gc(SimpleNamespace(file=str(ff.path), dry_run=True))
            check("gc --dry-run reports what it would remove",
                  "would remove" in buf.getvalue()
                  or "would free" in buf.getvalue(), "(%r)" % buf.getvalue())
            check("gc --dry-run leaves state.json byte-identical",
                  (home / "state.json").read_bytes() == before_state)
            check("gc --dry-run deletes nothing",
                  sorted(p.name for p in store.root.iterdir()) == before_entries)
            check("gc --dry-run keeps the stale env entry",
                  "/nonexistent/proj" in json.loads(
                      (home / "state.json").read_text())["envs"])
            # The real gc removes the orphan and prunes the stale entry.
            buf = io.StringIO()
            with redirect_stdout(buf):
                fix.cmd_gc(SimpleNamespace(file=str(ff.path), dry_run=False))
            check("real gc removes the orphan", not orphan.exists())
            check("real gc prunes the stale env entry",
                  "/nonexistent/proj" not in json.loads(
                      (home / "state.json").read_text())["envs"])
            check("real gc keeps the live entry",
                  any(p.name.endswith("-tiny-1.0.0")
                      for p in store.root.iterdir()))
        finally:
            os.environ.pop("FIX_TEST_HOME", None)


def test_gc_waits_for_store_lock():
    with tempfile.TemporaryDirectory() as td:
        home = _set_home(td)
        try:
            ff, _pkg = make_project(td)
            store = fix.Store()
            fix.build_closure(ff, store, quiet=True, no_sandbox=True)
            lockf = store.lock()  # held by this thread: gc must wait
            done = []

            def run_gc():
                try:
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        fix.cmd_gc(SimpleNamespace(file=str(ff.path),
                                                   dry_run=True))
                    done.append(True)
                except BaseException as e:  # never let a thread die silently
                    done.append(e)

            t = threading.Thread(target=run_gc, daemon=True)
            t.start()
            t.join(timeout=1.0)
            check("gc waits for the store lock instead of racing",
                  t.is_alive(), "(gc finished while the lock was held)")
            lockf.close()
            t.join(timeout=5)
            check("gc proceeds once the lock is released",
                  not t.is_alive() and done == [True], "(%r)" % (done,))
        finally:
            os.environ.pop("FIX_TEST_HOME", None)


# ---------------------------------------------------------------------------
# P1-9: doctor writability / lock / orphans
# ---------------------------------------------------------------------------

def _run_doctor():
    buf_out, buf_err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            fix.cmd_doctor(SimpleNamespace())
        return 0, buf_out.getvalue() + buf_err.getvalue()
    except SystemExit as e:
        return e.code, buf_out.getvalue() + buf_err.getvalue()


def test_doctor_writability():
    check("_dir_writable: real dir is writable",
          fix._dir_writable(Path(tempfile.gettempdir())))
    with tempfile.TemporaryDirectory() as td:
        victim = Path(td) / "afile"
        victim.write_text("x", encoding="utf-8")
        check("_dir_writable: a regular file is not a writable dir",
              not fix._dir_writable(victim))
    old_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as td:
        empty = Path(td) / "empty"
        empty.mkdir()
        os.chdir(empty)
        try:
            home = _set_home(td) / "no-home"
            rc, out = _run_doctor()
            check("doctor passes with no ~/.fix yet", rc == 0, "(rc=%r)" % rc)
            check("doctor does not create ~/.fix",
                  not home.exists(), "(it was created)")
            check("doctor reports the missing home",
                  "not created yet" in out, "(%r)" % out)
            # ~/.fix exists but is a file: doctor must fail, not pass.
            homefile = Path(td) / "homefile"
            homefile.write_text("x", encoding="utf-8")
            os.environ["FIX_TEST_HOME"] = str(homefile)
            rc, out = _run_doctor()
            check("doctor fails when ~/.fix is not a directory",
                  rc != 0, "(rc=%r)" % rc)
            check("doctor says why", "not a directory" in out, "(%r)" % out)
            # Orphaned build dirs are reported, never fatal.
            home2 = _set_home(td)
            (home2 / "build" / "leftover-1").mkdir(parents=True)
            rc, out = _run_doctor()
            check("doctor passes with orphaned build dirs",
                  rc == 0, "(rc=%r)" % rc)
            check("doctor reports the orphaned build dir",
                  "orphaned" in out, "(%r)" % out)
        finally:
            os.chdir(old_cwd)
            os.environ.pop("FIX_TEST_HOME", None)


def test_doctor_corrupt_lock():
    old_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as td:
        home = _set_home(td)
        try:
            ff, _pkg = make_project(td)
            store = fix.Store()
            fix.build_closure(ff, store, quiet=True, no_sandbox=True)
            os.chdir(ff.dir)
            rc, out = _run_doctor()
            check("doctor passes with a valid lock", rc == 0, "(rc=%r)" % rc)
            check("doctor reports the lock parses",
                  "lock" in out and "parses" in out, "(%r)" % out)
            (ff.dir / "fix.lock").write_text("{garbage", encoding="utf-8")
            rc, out = _run_doctor()
            check("doctor fails on a corrupt lock", rc != 0, "(rc=%r)" % rc)
            check("doctor names the corrupt lock",
                  "corrupt" in out and "fix.lock" in out, "(%r)" % out)
        finally:
            os.chdir(old_cwd)
            os.environ.pop("FIX_TEST_HOME", None)


# ---------------------------------------------------------------------------
# P1-10: purge refuses without -y when non-interactive
# ---------------------------------------------------------------------------

def test_purge_non_tty():
    with tempfile.TemporaryDirectory() as td:
        home = _set_home(td)
        try:
            home.mkdir(parents=True, exist_ok=True)
            (home / "keep.txt").write_text("x", encoding="utf-8")
            old_stdin = sys.stdin
            sys.stdin = io.StringIO("")  # not a TTY
            buf = io.StringIO()
            try:
                with redirect_stderr(buf):
                    try:
                        fix.cmd_purge(SimpleNamespace(yes=False))
                        check("purge without -y refuses when stdin is "
                              "not a TTY", False, "(did not die)")
                    except SystemExit as e:
                        check("purge without -y refuses when stdin is "
                              "not a TTY", e.code != 0,
                              "(exit %r)" % e.code)
            finally:
                sys.stdin = old_stdin
            check("purge refusal names the -y flag",
                  "-y" in buf.getvalue(), "(%r)" % buf.getvalue())
            check("refused purge leaves ~/.fix intact",
                  (home / "keep.txt").is_file())
            fix.cmd_purge(SimpleNamespace(yes=True))
            check("purge -y removes ~/.fix", not home.exists())
        finally:
            os.environ.pop("FIX_TEST_HOME", None)


# ---------------------------------------------------------------------------
# P1-11: corrupt lock -> fix: errors, never tracebacks
# ---------------------------------------------------------------------------

def test_corrupt_lock_commands():
    with tempfile.TemporaryDirectory() as td:
        home = _set_home(td)
        try:
            ff, _pkg = make_project(td)
            store = fix.Store()
            fix.build_closure(ff, store, quiet=True, no_sandbox=True)
            (ff.dir / "fix.lock").write_text("{garbage", encoding="utf-8")
            # status: the corrupt lock is a FixError, not a traceback.
            expect_raises("status on corrupt lock raises LockError",
                          fix.LockError, fix.cmd_status,
                          SimpleNamespace(file=str(ff.path)))
            # tree does not read the lock: it still works.
            buf = io.StringIO()
            with redirect_stdout(buf):
                fix.cmd_tree(SimpleNamespace(file=str(ff.path)))
            check("tree works despite a corrupt lock",
                  "test" in buf.getvalue(), "(%r)" % buf.getvalue())
            # shell rebuilds from the corrupt lock instead of crashing.
            results, _lock, _s = fix.ensure_built(ff, store, no_sandbox=True)
            check("shell rebuilds after a corrupt lock",
                  "tiny" in results)
            check("rebuild regenerates a parseable lock",
                  json.loads((ff.dir / "fix.lock").read_text())
                  ["closure"] == ["tiny"])
            # status with an inconsistent (not corrupt) lock: a package
            # entry missing from the lock data is a warning, not a crash.
            (ff.dir / "fix.lock").write_text(
                json.dumps({"schema": fix.LOCK_SCHEMA, "closure": ["tiny"],
                            "packages": {}, "updated": "x"}),
                encoding="utf-8")
            buf_out, buf_err = io.StringIO(), io.StringIO()
            with redirect_stdout(buf_out), redirect_stderr(buf_err):
                fix.cmd_status(SimpleNamespace(file=str(ff.path)))
            check("status warns on a missing lock entry instead of crashing",
                  "missing or incomplete" in buf_out.getvalue()
                  + buf_err.getvalue())
            # The real CLI path: status via main() exits non-zero, prints
            # a fix: error, and never a traceback.
            (ff.dir / "fix.lock").write_text("{garbage", encoding="utf-8")
            env = dict(os.environ)
            env["FIX_TEST_HOME"] = str(home)
            r = subprocess.run(
                [sys.executable, str(FIXPY), "status"],
                cwd=str(ff.dir), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            check("CLI status on corrupt lock exits nonzero", r.returncode != 0)
            check("CLI status prints no traceback",
                  "Traceback" not in r.stdout + r.stderr)
            check("CLI status prints a fix: error",
                  "corrupt" in r.stdout + r.stderr)
            # The command boundary converts stray exceptions too.
            orig = fix.cmd_status
            fix.cmd_status = lambda args: (_ for _ in ()).throw(
                KeyError("boom"))

            def run_main():
                fix.main(["status"])

            buf_err = io.StringIO()
            try:
                with redirect_stderr(buf_err):
                    run_main()
                check("main() converts KeyError to a fix: error", False,
                      "(did not die)")
            except SystemExit as e:
                check("main() converts KeyError to a fix: error",
                      e.code != 0
                      and "unexpected error" in buf_err.getvalue()
                      and "Traceback" not in buf_err.getvalue(),
                      "(rc=%r out=%r)" % (e.code, buf_err.getvalue()))
            finally:
                fix.cmd_status = orig
        finally:
            os.environ.pop("FIX_TEST_HOME", None)


# ---------------------------------------------------------------------------
# P1-12: shell quoting and nesting
# ---------------------------------------------------------------------------

def test_shell_quoting():
    with tempfile.TemporaryDirectory() as td:
        home = _set_home(td)
        try:
            ff, _pkg = make_project(td)
            store = fix.Store()
            fix.build_closure(ff, store, quiet=True, no_sandbox=True)
            env = dict(os.environ)
            env["FIX_TEST_HOME"] = str(home)
            r = subprocess.run(
                [sys.executable, str(FIXPY), "shell", "--",
                 "python3", "-c", "import sys; print(len(sys.argv))", "a b"],
                cwd=str(ff.dir), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            check("shell -- passes 'a b' as one argument (argv == 2)",
                  "\n2\n" in "\n" + r.stdout, "(rc=%d out=%r err=%r)"
                  % (r.returncode, r.stdout, r.stderr))
            r = subprocess.run(
                [sys.executable, str(FIXPY), "shell", "--",
                 "printf", "%s|", "a b"],
                cwd=str(ff.dir), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            check("shell -- printf sees one 'a b' argument",
                  "a b|" in r.stdout, "(out=%r)" % r.stdout)
            # A single argument is a shell snippet, passed through
            # verbatim: 'echo $FIX_ENV' still expands and splits.
            r = subprocess.run(
                [sys.executable, str(FIXPY), "shell", "--",
                 "echo $FIX_ENV"],
                cwd=str(ff.dir), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            check("shell -- 'echo $FIX_ENV' still expands (snippet)",
                  "test" in r.stdout, "(out=%r)" % r.stdout)
            # Nesting: warn names FIX_ALLOW_NEST, and never warns when set.
            env2 = dict(os.environ)
            env2["FIX_SHELL"] = "1"
            buf = io.StringIO()
            with redirect_stderr(buf):
                rc = fix.spawn_shell(ff, env2, cmd_args=["true"])
            check("nesting warning names FIX_ALLOW_NEST",
                  "FIX_ALLOW_NEST" in buf.getvalue()
                  and "FIX_SHELL by hand" not in buf.getvalue(),
                  "(%r)" % buf.getvalue())
            check("nesting still runs the command", rc == 0)
            env2["FIX_ALLOW_NEST"] = "1"
            buf = io.StringIO()
            with redirect_stderr(buf):
                fix.spawn_shell(ff, env2, cmd_args=["true"])
            check("no nesting warning when FIX_ALLOW_NEST is set",
                  "already inside" not in buf.getvalue(),
                  "(%r)" % buf.getvalue())
        finally:
            os.environ.pop("FIX_TEST_HOME", None)


# ---------------------------------------------------------------------------
# P2: Fixfile.save fidelity (unknown keys, .bak naming), fix init --shell,
# empty builds, stale build dirs, global flags
# ---------------------------------------------------------------------------

def test_fixfile_save_preserves_unknown_keys():
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td) / "proj"
        proj.mkdir()
        (proj / "fix.yml").write_text(
            "# a comment that will be lost on save\n"
            "name: demo-project\n"
            "meta:\n"
            "  owner: donald\n"
            "  retries: 3\n"
            "tags: [a, b]\n"
            "packages:\n"
            "  - hello\n"
            "\nshell: test\n", encoding="utf-8")
        ff = fix.Fixfile(proj / "fix.yml")
        ff.load()
        check("unknown scalar key loaded", ff.extra.get("name") == "demo-project")
        check("unknown mapping key loaded",
              ff.extra.get("meta") == {"owner": "donald", "retries": 3})
        check("unknown list key loaded", ff.extra.get("tags") == ["a", "b"])
        ff.packages["banner"] = "*"
        ff.save()
        saved = (proj / "fix.yml").read_text(encoding="utf-8")
        check("comment is not preserved on save (documented)",
              "#" not in saved)
        ff2 = fix.Fixfile(proj / "fix.yml")
        ff2.load()
        check("unknown keys survive a save round-trip",
              ff2.extra.get("name") == "demo-project"
              and ff2.extra.get("meta") == {"owner": "donald", "retries": 3}
              and ff2.extra.get("tags") == ["a", "b"],
              "(%r)" % ff2.extra)
        check("packages survive alongside extras",
              ff2.packages == {"hello": "*", "banner": "*"})
        # A value the YAML emitter cannot represent (list of mappings) is
        # dropped with a warning instead of crashing the save.
        (proj / "fix.yml").write_text(
            "packages:\n  - hello\n\nweird:\n  - name: a\n    v: 1\n",
            encoding="utf-8")
        ff3 = fix.Fixfile(proj / "fix.yml")
        ff3.load()
        buf = io.StringIO()
        with redirect_stderr(buf):
            ff3.save()
        check("unrepresentable extra key is dropped, not fatal",
              "dropping" in buf.getvalue() and "weird" in buf.getvalue(),
              "(%r)" % buf.getvalue())
        ff4 = fix.Fixfile(proj / "fix.yml")
        ff4.load()
        check("dropped key is gone after reload", "weird" not in ff4.extra)


def test_fixfile_save_bak_naming():
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td) / "proj"
        proj.mkdir()
        # fix.yaml must back up to fix.yaml.bak, not fix.yml.bak.
        (proj / "fix.yaml").write_text(
            "packages:\n  - hello\n", encoding="utf-8")
        ff = fix.Fixfile(proj / "fix.yaml")
        ff.load()
        ff.packages["banner"] = "*"
        ff.save()
        check("fix.yaml backs up to fix.yaml.bak",
              (proj / "fix.yaml.bak").is_file())
        check("fix.yaml does not create a fix.yml.bak",
              not (proj / "fix.yml.bak").exists())
        # And the plain fix.yml case is unchanged.
        (proj / "fix.yml").write_text(
            "packages:\n  - hello\n", encoding="utf-8")
        ff = fix.Fixfile(proj / "fix.yml")
        ff.load()
        ff.save()
        check("fix.yml backs up to fix.yml.bak",
              (proj / "fix.yml.bak").is_file())


def test_init_shell_option():
    ap = fix.build_parser()
    args = ap.parse_args(["init", "--shell", "dev", "hello"])
    check("init --shell parses", args.shell == "dev" and args.packages == ["hello"])
    args = ap.parse_args(["init"])
    check("init --shell defaults to None", args.shell is None)
    with tempfile.TemporaryDirectory() as td:
        old = os.getcwd()
        os.chdir(td)
        try:
            ns = SimpleNamespace(file="fix.yml", packages=["hello"],
                                 force=False, shell="dev")
            fix.cmd_init(ns)
            text = (Path(td) / "fix.yml").read_text(encoding="utf-8")
            check("init --shell writes shell: into fix.yml",
                  "shell: dev" in text, "(%r)" % text)
            check("init creates vendor/", (Path(td) / "vendor").is_dir())
        finally:
            os.chdir(old)


def test_global_flags_parsed():
    ap = fix.build_parser()
    args = ap.parse_args(["--file", "other.yml", "--no-color", "-v", "-q",
                          "build"])
    check("--file parses", args.file == "other.yml")
    check("--no-color parses", args.no_color is True)
    check("-v parses", args.verbose is True)
    check("-q parses", args.quiet is True)
    args = ap.parse_args(["build"])
    check("flags default off",
          args.file == "fix.yml" and not args.no_color
          and not args.verbose and not args.quiet)


def test_empty_build_produces_error():
    with tempfile.TemporaryDirectory() as td:
        home = _set_home(td)
        try:
            proj = Path(td) / "proj"
            pkg = proj / "vendor" / "empty"
            pkg.mkdir(parents=True, exist_ok=True)
            (proj / "fix.yml").write_text(
                "packages:\n  - empty\n\nshell: test\n", encoding="utf-8")
            (pkg / "fix.yml").write_text(
                "package:\n  name: empty\n  version: 1.0.0\n\n"
                "build:\n  steps:\n    - \"true\"\n", encoding="utf-8")
            ff = fix.Fixfile(proj / "fix.yml")
            ff.load()
            err = expect_raises("empty build raises BuildError",
                                fix.BuildError, fix.build_closure,
                                ff, fix.Store())
            check("empty build error names the cause",
                  "produced no output" in err, "(%r)" % err)
            check("empty build installs nothing",
                  not list((home / "store").iterdir())
                  if (home / "store").is_dir() else True)
        finally:
            os.environ.pop("FIX_TEST_HOME", None)


def test_stale_build_dir_tolerated():
    with tempfile.TemporaryDirectory() as td:
        home = _set_home(td)
        try:
            # An interrupted build leaves an orphaned dir under ~/.fix/build.
            stale = home / "build" / "hello-interrupted-123"
            stale.mkdir(parents=True, exist_ok=True)
            (stale / "junk.txt").write_text("leftover\n", encoding="utf-8")
            ff, _pkg = make_project(td, "tiny")
            results, _lock, _ = fix.build_closure(ff, fix.Store())
            check("build succeeds despite a stale build dir",
                  "tiny" in results)
            check("stale build dir is left alone by the build",
                  stale.is_dir())
            # And doctor reports the orphan (informationally).
            buf = io.StringIO()
            with redirect_stdout(buf):
                fix.cmd_doctor(SimpleNamespace())
            check("doctor reports the orphaned build dir",
                  "orphan" in buf.getvalue(), "(%r)" % buf.getvalue())
        finally:
            os.environ.pop("FIX_TEST_HOME", None)


def main():
    print("test_p1.py — P1 unit tests (yaml, versions, cycles, gc, "
          "doctor, purge, corrupt lock, shell quoting)")
    test_yaml_parity()
    test_yaml_errors()
    test_version_table()
    test_cycle_names_path()
    test_gc_dry_run_no_mutation()
    test_gc_waits_for_store_lock()
    test_doctor_writability()
    test_doctor_corrupt_lock()
    test_purge_non_tty()
    test_corrupt_lock_commands()
    test_shell_quoting()
    test_fixfile_save_preserves_unknown_keys()
    test_fixfile_save_bak_naming()
    test_init_shell_option()
    test_global_flags_parsed()
    test_empty_build_produces_error()
    test_stale_build_dir_tolerated()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
