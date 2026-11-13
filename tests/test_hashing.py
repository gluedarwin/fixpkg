#!/usr/bin/env python3
"""Unit tests for the v0.3 sandbox/hashing changes (stdlib only).

Run:  python3 tests/test_hashing.py
Plain asserts, no pytest. Each test is a function; `main` runs them all
and prints a pass/fail summary.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

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


def write_spec(pkgdir, name="tiny", version="1.0.0", extra="", env=""):
    pkgdir.mkdir(parents=True, exist_ok=True)
    (pkgdir / "fix.yml").write_text(
        "package:\n  name: %s\n  version: %s\n\n%sbuild:\n  steps:\n"
        '    - "cp a.txt %%(out)s/share/a.txt"\n%s'
        % (name, version, extra, env), encoding="utf-8")
    (pkgdir / "a.txt").write_text("hello\n", encoding="utf-8")
    return fix.Spec.from_file(pkgdir / "fix.yml", "test")


def test_hash_mode_sensitivity():
    with tempfile.TemporaryDirectory() as td:
        pkg = Path(td) / "tiny"
        spec = write_spec(pkg)
        orig_mode = os.lstat(pkg / "a.txt").st_mode & 0o7777
        h1 = fix.derivation_hash(spec, {}, spec.source_files())
        os.chmod(pkg / "a.txt", 0o755)
        h2 = fix.derivation_hash(spec, {}, spec.source_files())
        check("chmod +x changes derivation hash", h1 != h2, "(%s == %s)" % (h1, h2))
        os.chmod(pkg / "a.txt", orig_mode)
        h3 = fix.derivation_hash(spec, {}, spec.source_files())
        check("restoring mode restores hash", h1 == h3)


def test_hash_symlink_target():
    with tempfile.TemporaryDirectory() as td:
        pkg = Path(td) / "tiny"
        spec = write_spec(pkg)
        (pkg / "b.txt").write_text("b\n", encoding="utf-8")
        os.symlink("a.txt", pkg / "link.txt")
        h1 = fix.derivation_hash(spec, {}, spec.source_files())
        os.unlink(pkg / "link.txt")
        os.symlink("b.txt", pkg / "link.txt")
        h2 = fix.derivation_hash(spec, {}, spec.source_files())
        check("retargeting an in-repo symlink changes hash", h1 != h2)
        os.unlink(pkg / "link.txt")
        os.symlink("a.txt", pkg / "link.txt")
        h3 = fix.derivation_hash(spec, {}, spec.source_files())
        check("in-repo symlink is staged as a symlink, hash stable",
              h1 == h3)


def test_hash_empty_dir_and_stability():
    with tempfile.TemporaryDirectory() as td:
        pkg = Path(td) / "tiny"
        spec = write_spec(pkg)
        h1 = fix.derivation_hash(spec, {}, spec.source_files())
        (pkg / "empty").mkdir()
        h2 = fix.derivation_hash(spec, {}, spec.source_files())
        check("adding an empty dir changes hash", h1 != h2)
        h3 = fix.derivation_hash(spec, {}, spec.source_files())
        check("same inputs give same hash", h2 == h3)
        entries = spec.source_files()
        check("empty dir is recorded in source_files",
              "empty" in entries and entries["empty"]["type"] == "dir")


def test_hash_toolchain():
    with tempfile.TemporaryDirectory() as td:
        pkg = Path(td) / "tiny"
        spec = write_spec(pkg)
        fp = fix.toolchain_fingerprint()
        check("toolchain fingerprint is non-empty", bool(fp), "(%r)" % fp)
        check("fingerprint names cc", "cc:" in fp, "(%r)" % fp)
        h1 = fix.derivation_hash(spec, {}, spec.source_files())
        # Monkeypatch the cached fingerprint and confirm the hash follows it.
        old = fix._TOOLCHAIN_FP
        fix._TOOLCHAIN_FP = "cc:fake-1.0;bash:fake-1.0"
        try:
            h2 = fix.derivation_hash(spec, {}, spec.source_files())
        finally:
            fix._TOOLCHAIN_FP = old
        check("changing toolchain fingerprint changes hash", h1 != h2)


def test_env_allowlist():
    for key in ("PATH", "HOME", "SHELL", "LD_PRELOAD", "LD_LIBRARY_PATH",
                "DYLD_INSERT_LIBRARIES", "FIX_OUT", "IFS", "PYTHONPATH",
                "PYTHONHOME", "BASH_ENV", "ENV"):
        check("env key %s is blocked" % key, fix.is_blocked_env_key(key))
    for key in ("GREET_BANNER", "HELLO_DOC", "MY_PATH", "CC"):
        check("env key %s is allowed" % key, not fix.is_blocked_env_key(key))
    expect_raises("validate_spec_env rejects PATH", fix.SpecError,
                  fix.validate_spec_env, {"PATH": "/tmp"}, "spec.yml")
    expect_raises("validate_spec_env rejects FIX_ prefix", fix.SpecError,
                  fix.validate_spec_env, {"FIX_EVIL": "1"}, "spec.yml")
    try:
        fix.validate_spec_env({"GREET_BANNER": "x"}, "spec.yml")
        check("validate_spec_env allows safe keys", True)
    except Exception as e:
        check("validate_spec_env allows safe keys", False, "(%s)" % e)


def test_source_dir_escape():
    with tempfile.TemporaryDirectory() as td:
        pkg = Path(td) / "tiny"
        spec = write_spec(pkg)
        # absolute source.dir
        (pkg / "fix.yml").write_text(
            "package:\n  name: tiny\n  version: 1.0.0\n\n"
            "source:\n  dir: /etc\n\nbuild:\n  steps:\n"
            '    - "true"\n', encoding="utf-8")
        expect_raises("absolute source.dir is rejected", fix.SpecError,
                      fix.Spec.from_file, pkg / "fix.yml", "test")
        # relative escape
        (pkg / "fix.yml").write_text(
            "package:\n  name: tiny\n  version: 1.0.0\n\n"
            "source:\n  dir: ../..\n\nbuild:\n  steps:\n"
            '    - "true"\n', encoding="utf-8")
        expect_raises("escaping source.dir is rejected", fix.SpecError,
                      fix.Spec.from_file, pkg / "fix.yml", "test")
        # a subdir inside the package is fine
        (pkg / "sub").mkdir(exist_ok=True)
        (pkg / "sub" / "a.txt").write_text("x\n", encoding="utf-8")
        (pkg / "fix.yml").write_text(
            "package:\n  name: tiny\n  version: 1.0.0\n\n"
            "source:\n  dir: sub\n\nbuild:\n  steps:\n"
            '    - "cp a.txt %(out)s/share/a.txt"\n', encoding="utf-8")
        try:
            fix.Spec.from_file(pkg / "fix.yml", "test")
            check("source.dir inside the package is allowed", True)
        except Exception as e:
            check("source.dir inside the package is allowed", False, "(%s)" % e)


def test_symlink_escape_rejected():
    with tempfile.TemporaryDirectory() as td:
        pkg = Path(td) / "tiny"
        spec = write_spec(pkg)
        os.symlink("/etc/passwd", pkg / "evil.txt")
        expect_raises("symlink escaping the package is rejected",
                      fix.SpecError, spec.source_files)
        os.unlink(pkg / "evil.txt")
        os.symlink("../outside.txt", pkg / "evil2.txt")
        expect_raises("symlink to a sibling outside the package is rejected",
                      fix.SpecError, spec.source_files)


def test_scrubbed_build_env():
    # Build a package whose step dumps its environment; the caller's
    # CC/CFLAGS/PATH must not leak in, and PATH must be the fixed one.
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "fixhome"
        os.environ["FIX_TEST_HOME"] = str(home)
        os.environ["CC"] = "bogus-cc-from-caller"
        os.environ["CFLAGS"] = "-Dbogus"
        try:
            pkg = Path(td) / "tiny"
            pkg.mkdir(parents=True)
            (pkg / "fix.yml").write_text(
                "package:\n  name: tiny\n  version: 1.0.0\n\nbuild:\n  steps:\n"
                '    - "env > %(out)s/share/env.txt"\n'
                '    - "mkdir -p %(out)s/bin && printf \\"hi\\" > %(out)s/bin/tiny"\n',
                encoding="utf-8")
            (pkg / "a.txt").write_text("hello\n", encoding="utf-8")
            spec = fix.Spec.from_file(pkg / "fix.yml", "test")
            store = fix.Store()
            result = fix.build_package(store, spec, {}, no_sandbox=True)
            env_txt = (result.path / "share" / "env.txt").read_text()
            check("build env does not inherit caller's CC",
                  "bogus-cc-from-caller" not in env_txt)
            check("build env does not inherit caller's CFLAGS",
                  "bogus" not in env_txt)
            path_line = [l for l in env_txt.splitlines()
                         if l.startswith("PATH=")][0]
            check("build PATH is the fixed scrubbed PATH",
                  path_line == "PATH=/usr/local/bin:/usr/bin:/bin",
                  "(%s)" % path_line)
            check("build env pins SOURCE_DATE_EPOCH",
                  "SOURCE_DATE_EPOCH=946684800" in env_txt)
            check("build env pins TZ=UTC", "TZ=UTC" in env_txt)
            check("build meta records sandbox=none for --no-sandbox",
                  (result.path / ".fix-meta.json").read_text().find('"sandbox": "none"') > 0)
        finally:
            os.environ.pop("FIX_TEST_HOME", None)
            os.environ.pop("CC", None)
            os.environ.pop("CFLAGS", None)


def test_duplicate_env_warning():
    # Two packages setting the same env key: last wins, with a warning.
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "fixhome"
        os.environ["FIX_TEST_HOME"] = str(home)
        try:
            import io
            from contextlib import redirect_stderr
            pkg_a = Path(td) / "a"
            pkg_b = Path(td) / "b"
            for pkg, val in ((pkg_a, "A"), (pkg_b, "B")):
                pkg.mkdir(parents=True)
                (pkg / "fix.yml").write_text(
                    "package:\n  name: %s\n  version: 1.0.0\n\nbuild:\n  steps:\n"
                    '    - "mkdir -p %%(out)s/bin"\n'
                    '    - "printf \'x\' > %%(out)s/bin/%s"\n\nenv:\n  SHARED: "%s"\n'
                    % (pkg.name, pkg.name, val), encoding="utf-8")
                (pkg / "a.txt").write_text("x\n", encoding="utf-8")
            store = fix.Store()
            results = {}
            for pkg in (pkg_a, pkg_b):
                spec = fix.Spec.from_file(pkg / "fix.yml", "test")
                results[spec.name] = fix.build_package(
                    store, spec, {}, no_sandbox=True)
            ff = fix.Fixfile.__new__(fix.Fixfile)
            ff.shell_name = None
            ff.dir = Path(td)
            buf = io.StringIO()
            with redirect_stderr(buf):
                env = fix.compose_env(ff, ["a", "b"], results)
            check("duplicate env key: last wins", env.get("SHARED") == "B",
                  "(%r)" % env.get("SHARED"))
            check("duplicate env key warns once",
                  buf.getvalue().count("duplicate env key SHARED") == 1,
                  "(%r)" % buf.getvalue())
        finally:
            os.environ.pop("FIX_TEST_HOME", None)


def main():
    print("test_hashing.py — v0.3 sandbox/hashing unit tests")
    test_hash_mode_sensitivity()
    test_hash_symlink_target()
    test_hash_empty_dir_and_stability()
    test_hash_toolchain()
    test_env_allowlist()
    test_source_dir_escape()
    test_symlink_escape_rejected()
    test_scrubbed_build_env()
    test_duplicate_env_warning()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
