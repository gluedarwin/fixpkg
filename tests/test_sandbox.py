#!/usr/bin/env python3
"""Unit tests for the bwrap sandbox path (stdlib only).

Run:  python3 tests/test_sandbox.py
Plain asserts, no pytest. Each test is a function; `main` runs them all
and prints a pass/fail summary. Covers:
  - the sandbox argv recreates the usr-merge symlinks (/lib, /lib64,
    /bin, /sbin) — without them no dynamically-linked binary can exec
    inside the sandbox on Debian/Ubuntu, so the probe failed and fix
    silently fell back to unisolated builds even with bwrap installed
  - the probe succeeds where bwrap exists, falling back to a no-/proc
    sandbox when mounting /proc is refused (locked-down hosts/VMs)
  - a real sandboxed build step runs, cannot write to the read-only
    system, and cannot read a file outside the build tmp dir
  - a real sandboxed build_package records "sandbox": "bwrap" in meta

The whole file is skipped when bwrap is not installed, so it passes
on macOS and on Linux hosts without bubblewrap.
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


def test_bwrap_argv_has_usr_merge_symlinks():
    with tempfile.TemporaryDirectory() as td:
        argv = fix._bwrap_argv(Path(td), Path(td))
        pairs = set()
        for i, a in enumerate(argv):
            if a == "--symlink" and i + 2 < len(argv):
                pairs.add((argv[i + 1], argv[i + 2]))
        for target, dest in (("usr/lib", "/lib"), ("usr/lib64", "/lib64"),
                             ("usr/bin", "/bin"), ("usr/sbin", "/sbin")):
            check("bwrap argv recreates %s -> %s" % (dest, target),
                  (target, dest) in pairs, "(%r)" % (pairs,))


def test_probe_succeeds_where_bwrap_exists():
    check("sandbox probe succeeds with bwrap installed",
          fix._sandbox_available() is True)
    # The argv used for real builds must match the probe's variant:
    # --proc is present exactly when the probe managed to mount it.
    with tempfile.TemporaryDirectory() as td:
        argv = fix._bwrap_argv(Path(td), Path(td))
        check("build argv --proc matches probe outcome",
              ("--proc" in argv) == (not fix._BWRAP_NO_PROC),
              "(no_proc=%r argv=%r)" % (fix._BWRAP_NO_PROC, argv))


def test_sandboxed_step_runs_and_is_isolated():
    with tempfile.TemporaryDirectory() as td:
        # The canary lives OUTSIDE the build tmp dir: only tmp itself
        # (plus /usr and /etc, read-only) is visible inside the sandbox.
        tmp = Path(td) / "buildtmp"
        tmp.mkdir()
        store_root = Path(td) / "store"
        store_root.mkdir()
        canary = Path(td) / "outside" / "canary.txt"
        canary.parent.mkdir()
        canary.write_text("secret\n", encoding="utf-8")
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp),
               "TMPDIR": str(tmp)}
        proc = fix._run_build_step("echo sandboxed-ok", tmp, env, tmp,
                                   store_root, no_sandbox=False)
        check("sandboxed step runs and prints",
              proc.returncode == 0 and "sandboxed-ok" in (proc.stdout or ""),
              "(rc=%r out=%r)" % (proc.returncode, proc.stdout))
        proc = fix._run_build_step(
            "test -x /bin/bash && echo bash-found", tmp, env, tmp,
            store_root, no_sandbox=False)
        check("sandboxed step can exec /bin/bash (usr-merge symlink)",
              proc.returncode == 0 and "bash-found" in (proc.stdout or ""),
              "(rc=%r out=%r)" % (proc.returncode, proc.stdout))
        proc = fix._run_build_step("touch /etc/fix-sandbox-probe", tmp, env,
                                   tmp, store_root, no_sandbox=False)
        check("sandboxed step cannot write to read-only /etc",
              proc.returncode != 0 and not Path("/etc/fix-sandbox-probe").exists(),
              "(rc=%r)" % proc.returncode)
        proc = fix._run_build_step("cat %s" % canary, tmp, env, tmp,
                                   store_root, no_sandbox=False)
        check("sandboxed step cannot read a file outside the build dir",
              proc.returncode != 0 and "secret" not in (proc.stdout or ""),
              "(rc=%r out=%r)" % (proc.returncode, proc.stdout))


def test_sandboxed_build_records_bwrap_meta():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "fixhome"
        os.environ["FIX_TEST_HOME"] = str(home)
        try:
            pkg = Path(td) / "tiny"
            pkg.mkdir(parents=True)
            (pkg / "fix.yml").write_text(
                "package:\n  name: tiny\n  version: 1.0.0\n\nbuild:\n  steps:\n"
                '    - "mkdir -p %(out)s/bin && printf \\"hi\\" > %(out)s/bin/tiny"\n',
                encoding="utf-8")
            (pkg / "a.txt").write_text("hello\n", encoding="utf-8")
            spec = fix.Spec.from_file(pkg / "fix.yml", "test")
            store = fix.Store()
            result = fix.build_package(store, spec, {}, no_sandbox=False)
            meta = (result.path / ".fix-meta.json").read_text()
            check("sandboxed build meta records sandbox=bwrap",
                  '"sandbox": "bwrap"' in meta, "(%s)" % meta[:200])
            check("sandboxed build actually produced the binary",
                  (result.path / "bin" / "tiny").is_file())
        finally:
            os.environ.pop("FIX_TEST_HOME", None)


def main():
    print("test_sandbox.py — bwrap sandbox unit tests")
    if shutil.which("bwrap") is None:
        print("  skip (bwrap not installed)")
        return
    test_bwrap_argv_has_usr_merge_symlinks()
    test_probe_succeeds_where_bwrap_exists()
    test_sandboxed_step_runs_and_is_isolated()
    test_sandboxed_build_records_bwrap_meta()
    print("test_sandbox.py: %d passed, %d failed" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
