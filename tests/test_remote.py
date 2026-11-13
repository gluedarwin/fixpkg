#!/usr/bin/env python3
"""Unit tests for remote repos and tarball sources (stdlib only).

Run:  python3 tests/test_remote.py
Plain asserts, no pytest. Each test is a function; `main` runs them all
and prints a pass/fail summary. No external network is used: fixtures
are served by a local http.server in a thread (127.0.0.1) or via
file:// URLs. Covers:
  tarball sources end to end (build, stable hash, hash changes on
    url/sha256 change), missing sha256, checksum mismatch, unsafe
    tarball members, strip components, tarball formats
  remote repos (fetch, cache, search, install, repo list/update,
    bad-checksum update keeps the old cache)
  --offline / FIX_OFFLINE=1 (cache used, uncached fails loudly)
  http:// (non-localhost) rejected before any fetch
"""
import functools
import hashlib
import http.server
import io
import json
import os
import shutil
import socketserver
import subprocess
import sys
import tarfile
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


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_tarball(path, members, mode="w:gz"):
    """Write a tarball. members: {name: (bytes, mode)} or
    {name: ("symlink", target)} or {name: ("hardlink", target)} or
    {name: ("dir", None)} — names are stored verbatim (top dir included
    by the caller, e.g. 'pkg-1.0/a.txt')."""
    with tarfile.open(str(path), mode) as tf:
        for name, spec in members.items():
            if spec[0] == "dir":
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.DIRTYPE
                ti.mode = spec[1] if len(spec) > 1 else 0o755
                tf.addfile(ti)
            elif spec[0] == "symlink":
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.SYMTYPE
                ti.linkname = spec[1]
                ti.mode = 0o777
                tf.addfile(ti)
            elif spec[0] == "hardlink":
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.LNKTYPE
                ti.linkname = spec[1]
                tf.addfile(ti)
            else:
                data, fmode = spec
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                ti.mode = fmode
                tf.addfile(ti, io.BytesIO(data))


def write_tarball_spec(pkgdir, url, sha, strip=1, sdir=".", name="tb",
                       version="1.0.0", extra=""):
    """A package spec whose source is a tarball. The tarball is expected
    to contain a.txt and run.sh (0755) under the (stripped) source dir."""
    pkgdir.mkdir(parents=True, exist_ok=True)
    (pkgdir / "fix.yml").write_text(
        "package:\n  name: %s\n  version: %s\n\nsource:\n"
        "  type: tarball\n  url: %s\n  sha256: %s\n  strip: %d\n"
        "  dir: %s\n%s\nbuild:\n  steps:\n"
        '    - "cp a.txt %%(out)s/share/a.txt"\n'
        '    - "install -m 755 run.sh %%(out)s/bin/%s"\n'
        % (name, version, url, sha, strip, sdir, extra, name),
        encoding="utf-8")
    return fix.Spec.from_file(pkgdir / "fix.yml", "test")


def make_tb_tarball(path, content=b"hello tarball\n", top="pkg-1.0"):
    make_tarball(path, {
        "%s/a.txt" % top: (content, 0o644),
        "%s/run.sh" % top: (b"#!/bin/sh\necho tb-runs\n", 0o755),
    })
    return _sha256(path)


def make_project(td, packages, repos=None, shell="test", name="proj"):
    """A project fix.yml (packages: list of names) + empty vendor/.
    `name` is the project directory — use a distinct one per fixfile
    in a test, because each call rewrites <dir>/fix.yml."""
    proj = Path(td) / name
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "vendor").mkdir(exist_ok=True)
    body = ""
    if repos:
        body += "repos:\n"
        for k, v in repos.items():
            body += "  %s: %s\n" % (k, v)
        body += "\n"
    body += "packages:\n"
    for p in packages:
        body += "  - %s\n" % p
    body += "\nshell: %s\n" % shell
    (proj / "fix.yml").write_text(body, encoding="utf-8")
    ff = fix.Fixfile(proj / "fix.yml")
    ff.load()
    return ff


class _CountingHandler(http.server.SimpleHTTPRequestHandler):
    requests = 0

    def log_message(self, *args):  # silence the server
        pass

    def do_GET(self):
        type(self).requests += 1
        super().do_GET()


def start_server(root):
    """Serve `root` on 127.0.0.1:0. Returns (server, port)."""
    handler = functools.partial(_CountingHandler, directory=str(root))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    httpd.allow_reuse_address = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, httpd.server_address[1]


def build_pkg(ff, store=None):
    store = store or fix.Store()
    results, _lock, _state = fix.build_closure(ff, store)
    return results


# ---------------------------------------------------------------------------
# tarball sources
# ---------------------------------------------------------------------------

def test_tarball_build_end_to_end():
    with tempfile.TemporaryDirectory() as td:
        home = _set_home(td)
        tb = Path(td) / "tb.tar.gz"
        sha = make_tb_tarball(tb)
        pkg = Path(td) / "vendor" / "tb"
        spec = write_tarball_spec(pkg, "file://%s" % tb, sha)
        ff = make_project(td, ["tb"])
        # the spec lives in the project's vendor/ for resolution
        shutil.copytree(pkg, Path(td) / "proj" / "vendor" / "tb",
                        dirs_exist_ok=True)
        results = build_pkg(ff)
        r = results["tb"]
        check("tarball package builds", r.path.is_dir())
        check("tarball a.txt staged", (r.path / "share" / "a.txt").read_text()
              == "hello tarball\n")
        out = subprocess.run([str(r.path / "bin" / "tb")], capture_output=True,
                             text=True)
        check("tarball bin runs", out.stdout.strip() == "tb-runs",
              "(%r %r)" % (out.stdout, out.stderr))
        check("tarball cached in ~/.fix/cache",
              (home / "cache" / sha).is_file())
        check("tarball extraction staged per-sha",
              (home / "cache" / "extract" / ("%s-strip1" % sha)).is_dir())
        # Rebuild: same store path, cached.
        results2 = build_pkg(ff)
        check("tarball rebuild is cached and stable",
              results2["tb"].path == r.path and results2["tb"].cached)
        # Same content, different URL -> different hash (url is an input).
        # Compare with the SAME source files, so only the descriptor differs.
        tb2 = Path(td) / "tb-copy.tar.gz"
        shutil.copyfile(tb, tb2)
        pkg2 = Path(td) / "vendor2" / "tb"
        spec2 = write_tarball_spec(pkg2, "file://%s" % tb2, sha)
        files = spec.source_files(fix.Store())
        h1 = fix.derivation_hash(spec, {}, files)
        h2 = fix.derivation_hash(spec2, {}, files)
        check("changing the tarball url changes the hash", h1 != h2)
        # Same URL, different sha256 -> different hash.
        spec3 = write_tarball_spec(Path(td) / "vendor3" / "tb",
                                   "file://%s" % tb, "f" * 64)
        h3 = fix.derivation_hash(spec3, {}, files)
        check("changing the tarball sha256 changes the hash", h1 != h3)
        # A local package keeps its old hash: the descriptor feed is
        # tarball-only (no store orphaning for local packages).
        local = Path(td) / "proj" / "vendor" / "hello"
        local.mkdir(parents=True, exist_ok=True)
        (local / "fix.yml").write_text(
            "package:\n  name: hello\n  version: 1.0.0\n\nbuild:\n"
            '  steps:\n    - "mkdir -p %(out)s/bin && printf x > %(out)s/bin/hello"\n',
            encoding="utf-8")
        lspec = fix.Spec.from_file(local / "fix.yml", "test")
        check("local spec descriptor is local",
              lspec.source_desc.get("type") == "local")


def test_missing_sha256_rejected_before_fetch():
    with tempfile.TemporaryDirectory() as td:
        _set_home(td)
        pkg = Path(td) / "tb"
        pkg.mkdir(parents=True, exist_ok=True)
        # An unreachable URL: if fix tried to fetch it, the error would
        # be a network error, not a sha256 error.
        (pkg / "fix.yml").write_text(
            "package:\n  name: tb\n  version: 1.0.0\n\nsource:\n"
            "  type: tarball\n  url: http://127.0.0.1:1/nope.tar.gz\n\n"
            "build:\n  steps:\n    - \"true\"\n", encoding="utf-8")
        err = expect_raises("tarball without sha256 rejected at parse time",
                            fix.SpecError, fix.Spec.from_file,
                            pkg / "fix.yml", "test")
        check("missing-sha256 error names the requirement",
              "sha256" in err)
        check("no cache file created for the rejected spec",
              not (Path(td) / "fixhome" / "cache").exists())


def test_checksum_mismatch_rejected():
    with tempfile.TemporaryDirectory() as td:
        home = _set_home(td)
        tb = Path(td) / "tb.tar.gz"
        make_tb_tarball(tb)
        pkg = Path(td) / "vendor" / "tb"
        spec = write_tarball_spec(pkg, "file://%s" % tb, "f" * 64)
        ff = make_project(td, ["tb"])
        shutil.copytree(pkg, Path(td) / "proj" / "vendor" / "tb",
                        dirs_exist_ok=True)
        err = expect_raises("checksum mismatch rejected",
                            fix.FixError, build_pkg, ff)
        check("checksum error names the mismatch", "mismatch" in err.lower(),
              "(%r)" % err)
        check("mismatched tarball not cached",
              not (home / "cache" / ("f" * 64)).exists())
        check("mismatched tarball not extracted",
              not (home / "cache" / "extract").exists())


def test_unsafe_tarball_members():
    with tempfile.TemporaryDirectory() as td:
        home = _set_home(td)
        bad = {
            "absolute path": {"/tmp/evil.txt": (b"x", 0o644)},
            ".. escape": {"pkg/../evil.txt": (b"x", 0o644)},
            "symlink escape": {"pkg/link": ("symlink", "/etc/passwd")},
            "symlink parent escape": {
                "pkg/link": ("symlink", "/tmp"),
                "pkg/link/evil.txt": (b"x", 0o644),
            },
            "hard link": {"pkg/hard": ("hardlink", "a.txt"),
                          "pkg/a.txt": (b"x", 0o644)},
        }
        for name, members in bad.items():
            tb = Path(td) / ("bad-%s.tar.gz" % name.split()[0])
            make_tarball(tb, members)
            sha = _sha256(tb)
            err = expect_raises("unsafe tarball rejected: %s" % name,
                                fix.FixError, fix.extract_tarball, tb, sha, 1)
            check("unsafe tarball %s: no extract dir left behind" % name,
                  not (home / "cache" / "extract" / ("%s-strip1" % sha)).exists())
        # A symlink that stays inside the tree is fine.
        tb = Path(td) / "ok-link.tar.gz"
        make_tarball(tb, {
            "pkg/a.txt": (b"hi\n", 0o644),
            "pkg/link": ("symlink", "a.txt"),
        })
        sha = _sha256(tb)
        dest = fix.extract_tarball(tb, sha, 1)
        check("in-tree symlink extracts", (dest / "link").is_symlink())
        check("in-tree symlink resolves inside",
              (dest / "link").read_text() == "hi\n")


def test_strip_components():
    with tempfile.TemporaryDirectory() as td:
        home = _set_home(td)
        tb = Path(td) / "tb.tar.gz"
        make_tb_tarball(tb, top="pkg-1.0")
        sha = _sha256(tb)
        d1 = fix.extract_tarball(tb, sha, 1)
        check("strip=1 puts a.txt at the root", (d1 / "a.txt").is_file())
        d0 = fix.extract_tarball(tb, sha, 0)
        check("strip=0 keeps the top dir",
              (d0 / "pkg-1.0" / "a.txt").is_file())
        check("strip changes the staging dir",
              d0 != d1)
        # strip deeper than the tree: the source dir is empty/missing.
        pkg = Path(td) / "vendor" / "tb"
        spec = write_tarball_spec(pkg, "file://%s" % tb, sha, strip=5)
        ff = make_project(td, ["tb"])
        shutil.copytree(pkg, Path(td) / "proj" / "vendor" / "tb",
                        dirs_exist_ok=True)
        expect_raises("strip deeper than the tree is an error",
                      fix.FixError, build_pkg, ff)


def test_tarball_formats():
    with tempfile.TemporaryDirectory() as td:
        _set_home(td)
        formats = [("w", ".tar"), ("w:gz", ".tar.gz"), ("w:gz", ".tgz"),
                   ("w:bz2", ".tar.bz2")]
        try:
            import lzma  # noqa: F401
            formats.append(("w:xz", ".tar.xz"))
        except ImportError:
            print("  (lzma unavailable: skipping .tar.xz)")
        for mode, suffix in formats:
            tb = Path(td) / ("tb%s" % suffix)
            make_tarball(tb, {
                "pkg-1.0/a.txt": (b"hi\n", 0o644),
                "pkg-1.0/run.sh": (b"#!/bin/sh\necho ok\n", 0o755),
            }, mode=mode)
            sha = _sha256(tb)
            dest = fix.extract_tarball(tb, sha, 1)
            check("format %s extracts" % suffix, (dest / "a.txt").is_file())


def test_http_url_validation():
    with tempfile.TemporaryDirectory() as td:
        _set_home(td)
        pkg = Path(td) / "tb"
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "fix.yml").write_text(
            "package:\n  name: tb\n  version: 1.0.0\n\nsource:\n"
            "  type: tarball\n  url: http://example.com/tb.tar.gz\n"
            "  sha256: %s\n\nbuild:\n  steps:\n    - \"true\"\n"
            % ("a" * 64), encoding="utf-8")
        err = expect_raises("http:// (non-localhost) rejected at parse time",
                            fix.SpecError, fix.Spec.from_file,
                            pkg / "fix.yml", "test")
        check("http rejection explains the rule", "https" in err.lower(),
              "(%r)" % err)
        # Bad sha256 shape is rejected too.
        (pkg / "fix.yml").write_text(
            "package:\n  name: tb\n  version: 1.0.0\n\nsource:\n"
            "  type: tarball\n  url: https://example.com/tb.tar.gz\n"
            "  sha256: xyz\n\nbuild:\n  steps:\n    - \"true\"\n",
            encoding="utf-8")
        expect_raises("non-hex sha256 rejected",
                      fix.SpecError, fix.Spec.from_file, pkg / "fix.yml",
                      "test")


# ---------------------------------------------------------------------------
# remote repos
# ---------------------------------------------------------------------------

def _write_repo_pkg(repodir, name, version="1.0.0", extra=""):
    pkg = Path(repodir) / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "fix.yml").write_text(
        "package:\n  name: %s\n  version: %s\n%s\nbuild:\n  steps:\n"
        '    - "cp a.txt %%(out)s/share/a.txt"\n'
        '    - "mkdir -p %%(out)s/bin && install -m 755 run.sh %%(out)s/bin/%s"\n'
        % (name, version, extra, name), encoding="utf-8")
    (pkg / "a.txt").write_text("from repo %s\n" % name, encoding="utf-8")
    (pkg / "run.sh").write_text("#!/bin/sh\necho %s-runs\n" % name,
                                encoding="utf-8")
    os.chmod(pkg / "run.sh", 0o755)
    return pkg


def _tar_repo(repodir, out_path):
    with tarfile.open(str(out_path), "w:gz") as tf:
        tf.add(str(repodir), arcname=".", recursive=True)


def test_remote_repo_http():
    with tempfile.TemporaryDirectory() as td:
        home = _set_home(td)
        repodir = Path(td) / "repo"
        _write_repo_pkg(repodir, "rp")
        repo_tar = Path(td) / "repo.tar.gz"
        _tar_repo(repodir, repo_tar)
        sha = _sha256(repo_tar)
        _CountingHandler.requests = 0
        httpd, port = start_server(td)
        try:
            url = "http://127.0.0.1:%d/repo.tar.gz#sha256=%s" % (port, sha)
            ff = make_project(td, ["rp"],
                              repos={"upstream": url})
            results = build_pkg(ff)
            check("remote repo package builds", results["rp"].path.is_dir())
            check("remote repo package content",
                  (results["rp"].path / "share" / "a.txt").read_text()
                  == "from repo rp\n")
            check("remote repo cached under ~/.fix/repos",
                  (home / "repos" / "upstream" / "rp" / "fix.yml").is_file())
            check("remote repo meta records the pinned sha",
                  json.loads((home / "repos" / "upstream"
                              / ".fix-repo.json").read_text())["sha256"] == sha)
            # Search finds the remote package.
            buf = io.StringIO()
            with redirect_stdout(buf):
                fix.cmd_search(SimpleNamespace(file=str(ff.path),
                                               term="rp"))
            check("search finds remote repo package", "rp" in buf.getvalue(),
                  "(%r)" % buf.getvalue())
            # Cache reuse: stop the server, the repo must still work.
            httpd.shutdown()
            httpd.server_close()
            results2 = build_pkg(ff)
            check("remote repo works from cache with server down",
                  results2["rp"].path == results["rp"].path)
        finally:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                pass


def test_repo_list_and_update():
    with tempfile.TemporaryDirectory() as td:
        home = _set_home(td)
        repodir = Path(td) / "repo"
        _write_repo_pkg(repodir, "rp", version="1.0.0")
        repo_tar = Path(td) / "repo.tar.gz"
        _tar_repo(repodir, repo_tar)
        sha1 = _sha256(repo_tar)
        httpd, port = start_server(td)
        try:
            base = "http://127.0.0.1:%d/repo.tar.gz" % port
            ff = make_project(td, ["rp"],
                              repos={"upstream": "%s#sha256=%s" % (base, sha1)})
            # list before fetching: not cached.
            buf = io.StringIO()
            with redirect_stdout(buf):
                fix.cmd_repo_list(SimpleNamespace(file=str(ff.path)))
            check("repo list shows remote repo before fetch",
                  "upstream" in buf.getvalue() and "remote" in buf.getvalue()
                  and "not cached" in buf.getvalue(), "(%r)" % buf.getvalue())
            # update fetches it.
            buf = io.StringIO()
            with redirect_stdout(buf):
                fix.cmd_repo_update(SimpleNamespace(file=str(ff.path),
                                                    name="upstream"))
            check("repo update fetches the remote repo",
                  "updated upstream" in buf.getvalue(),
                  "(%r)" % buf.getvalue())
            check("repo update caches the repo",
                  (home / "repos" / "upstream" / "rp" / "fix.yml").is_file())
            # Publish v1.0.1 on the server and update fix.yml's sha.
            _write_repo_pkg(repodir, "rp", version="1.0.1")
            _tar_repo(repodir, repo_tar)
            sha2 = _sha256(repo_tar)
            check("new repo tarball has a new sha", sha2 != sha1)
            ff2 = make_project(td, ["rp"],
                               repos={"upstream": "%s#sha256=%s" % (base, sha2)},
                               name="proj2")
            fix.cmd_repo_update(SimpleNamespace(file=str(ff2.path),
                                                name="upstream"))
            spec = fix.find_spec(fix.open_repos(ff2), "rp")
            check("repo update picks up the new version",
                  spec.version == "1.0.1", "(got %s)" % spec.version)
            # Bad checksum on update: the server now serves v9.9.9, but
            # fix.yml pins a sha that matches nothing on the server.
            # (Pinning the old sha2 would NOT fail: the download cache
            # is content-addressed, so verified sha2 bytes are the right
            # bytes by definition. The failure mode that matters is a
            # wrong pinned sha.) The old cache must survive.
            _write_repo_pkg(repodir, "rp", version="9.9.9")
            _tar_repo(repodir, repo_tar)
            ff_bad = make_project(td, ["rp"], repos={
                "upstream": "%s#sha256=%s" % (base, "a" * 64)},
                name="projbad")
            buf_out, buf_err = io.StringIO(), io.StringIO()
            try:
                with redirect_stdout(buf_out), redirect_stderr(buf_err):
                    fix.cmd_repo_update(SimpleNamespace(file=str(ff_bad.path),
                                                        name="upstream"))
                check("bad-checksum repo update fails loudly", False,
                      "(update unexpectedly succeeded)")
            except SystemExit as e:
                check("bad-checksum repo update exits non-zero",
                      e.code != 0)
            spec = fix.find_spec(fix.open_repos(ff2), "rp")
            check("bad-checksum update keeps the old cache",
                  spec.version == "1.0.1", "(got %s)" % spec.version)
            # Updating an unknown repo name fails.
            try:
                fix.cmd_repo_update(SimpleNamespace(file=str(ff2.path),
                                                    name="nope"))
                check("repo update of an unknown repo fails", False)
            except SystemExit as e:
                check("repo update of an unknown repo fails", e.code != 0)
        finally:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                pass


def test_remote_repo_needs_sha256():
    with tempfile.TemporaryDirectory() as td:
        _set_home(td)
        ff = make_project(td, ["rp"], repos={
            "upstream": "https://example.com/repo.tar.gz"})
        err = expect_raises("remote repo without #sha256 rejected",
                            fix.FixError, fix.open_repos, ff)
        check("missing-sha error names the fragment", "sha256" in err)


# ---------------------------------------------------------------------------
# offline
# ---------------------------------------------------------------------------

def test_offline():
    with tempfile.TemporaryDirectory() as td:
        home = _set_home(td)
        tb = Path(td) / "tb.tar.gz"
        sha = make_tb_tarball(tb)
        pkg = Path(td) / "vendor" / "tb"
        spec = write_tarball_spec(pkg, "file://%s" % tb, sha)
        ff = make_project(td, ["tb"])
        shutil.copytree(pkg, Path(td) / "proj" / "vendor" / "tb",
                        dirs_exist_ok=True)
        # Prime the cache, then go offline: the cached build works.
        build_pkg(ff)
        fix._OFFLINE = True
        try:
            results = build_pkg(ff)
            check("offline build uses the cached tarball",
                  results["tb"].cached)
            # An uncached file:// tarball still works offline: it is a
            # local file, not network access (still checksum-verified).
            tb2 = Path(td) / "tb2.tar.gz"
            sha2 = make_tb_tarball(tb2, content=b"other\n")
            pkg2 = Path(td) / "vendor" / "tb2"
            write_tarball_spec(pkg2, "file://%s" % tb2, sha2, name="tb2")
            ff2 = make_project(td, ["tb2"], name="proj2")
            shutil.copytree(pkg2, Path(td) / "proj2" / "vendor" / "tb2",
                            dirs_exist_ok=True)
            results2 = build_pkg(ff2)
            check("offline file:// tarball builds (local, not network)",
                  results2["tb2"].path.is_dir())
            # An uncached http tarball fails loudly offline.
            pkg3 = Path(td) / "vendor" / "tb3"
            write_tarball_spec(pkg3, "http://127.0.0.1:1/tb3.tar.gz",
                               "e" * 64, name="tb3")
            ff3 = make_project(td, ["tb3"], name="proj3")
            shutil.copytree(pkg3, Path(td) / "proj3" / "vendor" / "tb3",
                            dirs_exist_ok=True)
            err = expect_raises("offline uncached http tarball fails",
                                fix.FixError, build_pkg, ff3)
            check("offline error says it is unavailable offline",
                  "offline" in err.lower(), "(%r)" % err)
            # The --offline flag reaches main() too.
            r = subprocess.run(
                [sys.executable, str(FIXPY), "--offline", "--no-color",
                 "--file", str(ff.path), "build"],
                capture_output=True, text=True,
                env={**os.environ, "FIX_TEST_HOME": str(home)})
            check("--offline flag: cached build succeeds via the CLI",
                  r.returncode == 0, "(%s)" % r.stderr[-300:])
            # FIX_OFFLINE=1 works the same way for an uncached package.
            env = {**os.environ, "FIX_TEST_HOME": str(home),
                   "FIX_OFFLINE": "1"}
            r = subprocess.run(
                [sys.executable, str(FIXPY), "--no-color",
                 "--file", str(ff3.path), "build"],
                capture_output=True, text=True, env=env)
            check("FIX_OFFLINE=1: uncached build fails via the CLI",
                  r.returncode != 0 and "offline" in (r.stdout + r.stderr).lower(),
                  "(%s)" % (r.stdout + r.stderr)[-300:])
        finally:
            fix._OFFLINE = False
            os.environ.pop("FIX_OFFLINE", None)


def test_download_cache_reuse_without_network():
    with tempfile.TemporaryDirectory() as td:
        home = _set_home(td)
        tb = Path(td) / "tb.tar.gz"
        sha = make_tb_tarball(tb)
        httpd, port = start_server(td)
        try:
            url = "http://127.0.0.1:%d/tb.tar.gz" % port
            p1 = fix.download_tarball(url, sha)
            check("first download lands in the cache",
                  p1 == home / "cache" / sha)
            reqs_after_first = _CountingHandler.requests
        finally:
            httpd.shutdown()
            httpd.server_close()
        # Server is down: the second call must not touch the network.
        p2 = fix.download_tarball(url, sha)
        check("second download reuses the cache with server down",
              p2 == p1)
        check("no extra request was made for the cached tarball",
              _CountingHandler.requests == reqs_after_first)


def main():
    tests = [test_tarball_build_end_to_end,
             test_missing_sha256_rejected_before_fetch,
             test_checksum_mismatch_rejected,
             test_unsafe_tarball_members,
             test_strip_components,
             test_tarball_formats,
             test_http_url_validation,
             test_remote_repo_http,
             test_repo_list_and_update,
             test_remote_repo_needs_sha256,
             test_offline,
             test_download_cache_reuse_without_network]
    for t in tests:
        try:
            t()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check("%s (unexpected exception: %s)" % (t.__name__, e), False)
        finally:
            fix._OFFLINE = False
            os.environ.pop("FIX_OFFLINE", None)
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
