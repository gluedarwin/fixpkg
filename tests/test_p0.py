#!/usr/bin/env python3
"""Unit tests for the P0 fixes (stdlib only).

Run:  python3 tests/test_p0.py
Plain asserts, no pytest. Each test is a function; `main` runs them all
and prints a pass/fail summary. Covers:
  P0-1  corrupt state.json: backup/restore, gc refusal, preservation
  P0-2  atomic writes, cache re-check under the lock, rename race
  P0-3  ensure_built re-derives hashes instead of trusting mtime/lock
"""
import errno
import io
import json
import os
import shutil
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


def test_write_atomic_basic():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "f.txt"
        fix.write_atomic(p, "one\n")
        check("write_atomic creates file with exact content",
              p.read_text() == "one\n")
        fix.write_atomic(p, "two\n")
        check("write_atomic replaces content",
              p.read_text() == "two\n")
        leftovers = list(Path(td).glob("*.tmp"))
        check("write_atomic leaves no temp files", leftovers == [],
              "(%r)" % leftovers)


def test_write_atomic_failed_replace_keeps_old():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "f.txt"
        p.write_text("old\n", encoding="utf-8")
        orig_replace = fix.os.replace

        def boom(src, dst):
            raise OSError(errno.EIO, "simulated disk failure")

        fix.os.replace = boom
        try:
            err = expect_raises("write_atomic: failed replace raises",
                                OSError, fix.write_atomic, p, "new\n")
        finally:
            fix.os.replace = orig_replace
        check("write_atomic: old content intact after failed replace",
              p.read_text() == "old\n", "(%r)" % p.read_text())
        leftovers = list(Path(td).glob("*.tmp"))
        check("write_atomic: temp file cleaned up after failure",
              leftovers == [], "(%r)" % leftovers)


def test_write_atomic_failed_fsync_keeps_old():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "f.txt"
        p.write_text("old\n", encoding="utf-8")
        orig_fsync = fix.os.fsync

        def boom(fd):
            raise OSError(errno.EIO, "simulated fsync failure")

        fix.os.fsync = boom
        try:
            expect_raises("write_atomic: failed fsync raises",
                          OSError, fix.write_atomic, p, "new\n")
        finally:
            fix.os.fsync = orig_fsync
        check("write_atomic: old content intact after failed fsync",
              p.read_text() == "old\n", "(%r)" % p.read_text())
        leftovers = list(Path(td).glob("*.tmp"))
        check("write_atomic: temp file cleaned up after fsync failure",
              leftovers == [], "(%r)" % leftovers)


def test_state_backup_and_restore():
    with tempfile.TemporaryDirectory() as td:
        home = _set_home(td)
        try:
            proj = Path(td) / "proj"
            proj.mkdir()
            store = fix.Store()
            state = fix.GlobalState(store)
            state.touch(str(proj), "test", proj / "fix.lock", ["abc-hello-1.0.0"])
            state.save()
            saved = json.loads((home / "state.json").read_text())
            check("state save writes a .bak",
                  (home / "state.json.bak").is_file())
            check("state .bak matches the primary",
                  json.loads((home / "state.json.bak").read_text()) == saved)
            # Corrupt the primary: the backup must be restored, loudly.
            (home / "state.json").write_text("{garbage", encoding="utf-8")
            buf = io.StringIO()
            with redirect_stderr(buf):
                restored = fix.GlobalState(store)
            check("corrupt state restores from backup",
                  restored.restored_from_backup and restored.corrupt is None)
            check("restored state has the recorded env",
                  restored.data.get("envs", {}).get(str(proj), {}).get("name") == "test",
                  "(%r)" % restored.data)
            check("restore warns loudly",
                  "restored" in buf.getvalue() and "corrupt" in buf.getvalue(),
                  "(%r)" % buf.getvalue())
            # Corrupt both primary and backup: marked corrupt, empty.
            (home / "state.json").write_text("{garbage", encoding="utf-8")
            (home / "state.json.bak").write_text("{also garbage", encoding="utf-8")
            buf = io.StringIO()
            with redirect_stderr(buf):
                broken = fix.GlobalState(store)
            check("corrupt state with corrupt backup is flagged",
                  broken.corrupt is not None and not broken.restored_from_backup)
            check("corrupt state starts from an empty registry",
                  broken.data == {"schema": fix.STATE_SCHEMA, "envs": {}})
            check("unrecoverable corruption warns loudly",
                  "corrupt" in buf.getvalue() and "purge" in buf.getvalue(),
                  "(%r)" % buf.getvalue())
            # A bad shape (valid JSON, wrong structure) is corruption too.
            (home / "state.json").write_text('{"envs": "nope"}', encoding="utf-8")
            (home / "state.json.bak").unlink()
            broken2 = fix.GlobalState(store)
            check("state with wrong shape is flagged corrupt",
                  broken2.corrupt is not None)
        finally:
            os.environ.pop("FIX_TEST_HOME", None)


def test_state_corrupt_preserved_on_save():
    with tempfile.TemporaryDirectory() as td:
        home = _set_home(td)
        try:
            proj = Path(td) / "proj"
            proj.mkdir()
            store = fix.Store()
            (home).mkdir(parents=True, exist_ok=True)
            (home / "state.json").write_text("{garbage", encoding="utf-8")
            state = fix.GlobalState(store)
            check("state is corrupt before save", state.corrupt is not None)
            state.touch(str(proj), "test", proj / "fix.lock", ["abc-hello-1.0.0"])
            state.save()
            preserved = sorted((home).glob("state.json.corrupt-*"))
            check("save preserves the corrupt bytes first",
                  len(preserved) == 1
                  and preserved[0].read_text() == "{garbage",
                  "(%r)" % preserved)
            check("save writes a fresh parseable primary",
                  json.loads((home / "state.json").read_text())
                  ["envs"][str(proj)]["name"] == "test")
            check("corrupt flag clears after a successful save",
                  state.corrupt is None)
        finally:
            os.environ.pop("FIX_TEST_HOME", None)


def _corrupt_state_files(home):
    (home / "state.json").write_text("{garbage", encoding="utf-8")
    (home / "state.json.bak").write_text("{also garbage", encoding="utf-8")


def test_gc_refuses_on_corrupt_state():
    with tempfile.TemporaryDirectory() as td:
        home = _set_home(td)
        try:
            store = fix.Store()
            store.ensure()
            entry = store.root / "deadbeef-hello-1.0.0"
            entry.mkdir(parents=True)
            (entry / "bin").mkdir()
            (entry / "bin" / "hello").write_text("x", encoding="utf-8")
            _corrupt_state_files(home)
            for dry in (False, True):
                args = SimpleNamespace(file="fix.yml", dry_run=dry)
                try:
                    fix.cmd_gc(args)
                    check("gc refuses with corrupt state (dry_run=%s)" % dry,
                          False, "(did not die)")
                except SystemExit as e:
                    check("gc refuses with corrupt state (dry_run=%s)" % dry,
                          e.code != 0, "(exit %r)" % e.code)
                check("gc with corrupt state deletes nothing (dry_run=%s)" % dry,
                      entry.is_dir())
        finally:
            os.environ.pop("FIX_TEST_HOME", None)


def test_build_package_concurrent_same_target():
    with tempfile.TemporaryDirectory() as td:
        _set_home(td)
        try:
            pkg = Path(td) / "tiny"
            pkg.mkdir(parents=True)
            (pkg / "fix.yml").write_text(
                "package:\n  name: tiny\n  version: 1.0.0\n\nbuild:\n  steps:\n"
                '    - "cp a.txt %(out)s/share/a.txt"\n'
                '    - "mkdir -p %(out)s/bin && printf \'hi\' > %(out)s/bin/tiny"\n',
                encoding="utf-8")
            (pkg / "a.txt").write_text("hello\n", encoding="utf-8")
            spec = fix.Spec.from_file(pkg / "fix.yml", "test")
            store = fix.Store()
            outcomes, errors = [], []

            def build_one():
                try:
                    outcomes.append(fix.build_package(
                        store, spec, {}, no_sandbox=True))
                except Exception as e:  # noqa: BLE001 — must not escape
                    errors.append(e)

            threads = [threading.Thread(target=build_one) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            check("concurrent builds of one package all succeed",
                  len(outcomes) == 4 and not errors,
                  "(outcomes=%d errors=%r)" % (len(outcomes), errors))
            check("concurrent builds agree on one store path",
                  len({str(r.path) for r in outcomes}) == 1)
            check("concurrent builds were not all full rebuilds",
                  sum(1 for r in outcomes if r.cached) >= 3,
                  "(cached=%d)" % sum(1 for r in outcomes if r.cached))
        finally:
            os.environ.pop("FIX_TEST_HOME", None)


def test_build_package_rename_race_is_cached():
    with tempfile.TemporaryDirectory() as td:
        _set_home(td)
        try:
            pkg = Path(td) / "tiny"
            pkg.mkdir(parents=True)
            (pkg / "fix.yml").write_text(
                "package:\n  name: tiny\n  version: 1.0.0\n\nbuild:\n  steps:\n"
                '    - "cp a.txt %(out)s/share/a.txt"\n'
                '    - "mkdir -p %(out)s/bin && printf \'hi\' > %(out)s/bin/tiny"\n',
                encoding="utf-8")
            (pkg / "a.txt").write_text("hello\n", encoding="utf-8")
            spec = fix.Spec.from_file(pkg / "fix.yml", "test")
            store = fix.Store()
            dhash = fix.derivation_hash(spec, {}, spec.source_files())
            target = store.entry(dhash, spec)
            orig_rename = fix.os.rename

            def flaky_rename(src, dst):
                # Simulate another process installing the same derivation
                # between our build and our rename.
                tgt = Path(dst)
                tgt.mkdir(parents=True, exist_ok=True)
                (tgt / ".fix-meta.json").write_text(json.dumps({
                    "schema": fix.DERIVATION_SCHEMA, "name": spec.name,
                    "hash": dhash}), encoding="utf-8")
                raise OSError(errno.EEXIST, "File exists", dst)

            fix.os.rename = flaky_rename
            try:
                result = fix.build_package(store, spec, {}, no_sandbox=True)
            finally:
                fix.os.rename = orig_rename
            check("rename race against a valid entry is treated as cached",
                  result.cached and result.path == target
                  and result.hash == dhash,
                  "(cached=%r path=%r)" % (result.cached, result.path))
            check("rename race leaves the winner's entry in place",
                  target.is_dir())
            # A rename failure with no valid entry is a `fix:` error,
            # not a traceback.
            shutil.rmtree(store.root, ignore_errors=True)
            orig_rename = fix.os.rename

            def failing_rename(src, dst):
                raise OSError(errno.ENOSPC, "No space left on device", dst)

            fix.os.rename = failing_rename
            try:
                err = expect_raises(
                    "rename failure without a valid entry is a BuildError",
                    fix.BuildError, fix.build_package,
                    store, spec, {}, no_sandbox=True)
            finally:
                fix.os.rename = orig_rename
            check("rename failure message names the store entry",
                  "could not install" in err, "(%r)" % err)
        finally:
            os.environ.pop("FIX_TEST_HOME", None)


def test_lock_and_state_writes_are_atomic():
    with tempfile.TemporaryDirectory() as td:
        home = _set_home(td)
        try:
            ff, _pkg = make_project(td)
            store = fix.Store()
            results, lock, _state = fix.build_closure(
                ff, store, quiet=True, no_sandbox=True)
            check("fix.lock parses after build",
                  json.loads((ff.dir / "fix.lock").read_text())["schema"]
                  == fix.LOCK_SCHEMA)
            primary = json.loads((home / "state.json").read_text())
            backup = json.loads((home / "state.json.bak").read_text())
            check("state.json and state.json.bak agree after build",
                  primary == backup)
            check("state records the project",
                  str(ff.dir) in primary["envs"])
            check("store entry has an atomic .fix-meta.json",
                  json.loads((results["tiny"].path / ".fix-meta.json")
                             .read_text())["hash"] == results["tiny"].hash)
        finally:
            os.environ.pop("FIX_TEST_HOME", None)


def test_ensure_built_rebuilds_after_source_edit():
    with tempfile.TemporaryDirectory() as td:
        home = _set_home(td)
        try:
            ff, pkg = make_project(td)
            store = fix.Store()
            results, _lock, _state = fix.build_closure(
                ff, store, quiet=True, no_sandbox=True)
            path1 = results["tiny"].path
            check("initial build stores the source content",
                  (path1 / "share" / "a.txt").read_text() == "hello\n")
            # Re-check with nothing changed: fast path, same path, cached.
            results2, _l, _s = fix.ensure_built(ff, store, no_sandbox=True)
            check("ensure_built with no changes returns the same path",
                  results2["tiny"].path == path1)
            check("ensure_built with no changes is cached",
                  results2["tiny"].cached)
            # Edit a source file WITHOUT touching fix.yml (whose mtime is
            # older than fix.lock, so the old mtime check would serve the
            # stale binary). The shell must now rebuild.
            (pkg / "a.txt").write_text("changed\n", encoding="utf-8")
            results3, _l, _s = fix.ensure_built(ff, store, no_sandbox=True)
            check("ensure_built rebuilds after a source edit",
                  results3["tiny"].path != path1,
                  "(path %s, was %s)" % (results3["tiny"].path, path1))
            check("rebuilt entry has the new source content",
                  (results3["tiny"].path / "share" / "a.txt").read_text()
                  == "changed\n")
            # Mode-only change also forces a rebuild (v2 hashing).
            orig_mode = os.lstat(pkg / "a.txt").st_mode & 0o7777
            os.chmod(pkg / "a.txt", 0o755 if orig_mode != 0o755 else 0o644)
            results4, _l, _s = fix.ensure_built(ff, store, no_sandbox=True)
            check("ensure_built rebuilds after a mode-only change",
                  results4["tiny"].path != results3["tiny"].path)
            os.chmod(pkg / "a.txt", orig_mode)
            results5, _l, _s = fix.ensure_built(ff, store, no_sandbox=True)
            check("restoring the mode restores the earlier store path",
                  results5["tiny"].path == results3["tiny"].path)
        finally:
            os.environ.pop("FIX_TEST_HOME", None)


def test_ensure_built_rebuilds_missing_store_entry():
    with tempfile.TemporaryDirectory() as td:
        _set_home(td)
        try:
            ff, _pkg = make_project(td)
            store = fix.Store()
            results, _lock, _state = fix.build_closure(
                ff, store, quiet=True, no_sandbox=True)
            path1 = results["tiny"].path
            shutil.rmtree(path1)
            check("store entry removed to simulate damage",
                  not path1.exists())
            results2, _l, _s = fix.ensure_built(ff, store, no_sandbox=True)
            check("ensure_built rebuilds a missing store entry",
                  results2["tiny"].path == path1 and path1.is_dir()
                  and (path1 / "share" / "a.txt").read_text() == "hello\n")
        finally:
            os.environ.pop("FIX_TEST_HOME", None)


def test_ensure_built_recovers_corrupt_lock():
    with tempfile.TemporaryDirectory() as td:
        _set_home(td)
        try:
            ff, _pkg = make_project(td)
            store = fix.Store()
            results, _lock, _state = fix.build_closure(
                ff, store, quiet=True, no_sandbox=True)
            path1 = results["tiny"].path
            (ff.dir / "fix.lock").write_text("{corrupt", encoding="utf-8")
            results2, _l, _s = fix.ensure_built(ff, store, no_sandbox=True)
            check("ensure_built rebuilds after a corrupt fix.lock",
                  results2["tiny"].path == path1 and path1.is_dir())
            check("fix.lock is parseable again after the rebuild",
                  isinstance(json.loads((ff.dir / "fix.lock").read_text()),
                             dict))
        finally:
            os.environ.pop("FIX_TEST_HOME", None)


def test_doctor_reports_corrupt_state():
    with tempfile.TemporaryDirectory() as td:
        home = _set_home(td)
        try:
            store = fix.Store()
            store.ensure()
            # No state file at all: doctor passes, mentions no state yet.
            buf = io.StringIO()
            with redirect_stdout(buf):
                fix.cmd_doctor(SimpleNamespace())
            check("doctor passes with no state file",
                  "all critical checks passed" in buf.getvalue())
            # Corrupt primary, valid backup: doctor reports the restore.
            state = fix.GlobalState(store)
            state.touch(str(Path(td)), "test", Path(td) / "fix.lock", [])
            state.save()
            (home / "state.json").write_text("{garbage", encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                fix.cmd_doctor(SimpleNamespace())
            check("doctor passes with corrupt state (informational)",
                  "all critical checks passed" in buf.getvalue())
            check("doctor reports the state restore",
                  "restored from" in buf.getvalue(),
                  "(%r)" % buf.getvalue())
            # Corrupt primary and backup: doctor still passes but says so.
            _corrupt_state_files(home)
            buf = io.StringIO()
            with redirect_stdout(buf):
                fix.cmd_doctor(SimpleNamespace())
            check("doctor passes with unrecoverable state (informational)",
                  "all critical checks passed" in buf.getvalue())
            check("doctor reports the unrecoverable corruption",
                  "corrupt" in buf.getvalue(), "(%r)" % buf.getvalue())
        finally:
            os.environ.pop("FIX_TEST_HOME", None)


def main():
    print("test_p0.py — P0 trio unit tests (corrupt state, atomic writes, "
          "stale shell)")
    test_write_atomic_basic()
    test_write_atomic_failed_replace_keeps_old()
    test_write_atomic_failed_fsync_keeps_old()
    test_state_backup_and_restore()
    test_state_corrupt_preserved_on_save()
    test_gc_refuses_on_corrupt_state()
    test_build_package_concurrent_same_target()
    test_build_package_rename_race_is_cached()
    test_lock_and_state_writes_are_atomic()
    test_ensure_built_rebuilds_after_source_edit()
    test_ensure_built_rebuilds_missing_store_entry()
    test_ensure_built_recovers_corrupt_lock()
    test_doctor_reports_corrupt_state()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
