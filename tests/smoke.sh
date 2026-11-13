#!/usr/bin/env bash
# smoke.sh — end-to-end test of fix against the website spec
# (https://gluedarwin.github.io/fixpkg/). Usage: [FIX_BIN="python3 fix.py"]
# bash tests/smoke.sh
set -u
cd "$(dirname "$0")/.."
PROJ="$(pwd)"
case "${FIX_BIN:-}" in ./*|../*) FIX_BIN="$PROJ/$FIX_BIN" ;; esac
FIX="${FIX_BIN:-python3 $PROJ/fix.py}"
WORK="$(mktemp -d)"
export FIX_TEST_HOME="$WORK/fixhome"
PASS=0; FAIL=0

say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
good() { PASS=$((PASS+1)); printf '  \033[32mok\033[0m  %s\n' "$*"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$*"; }
run()  { $FIX --no-color "$@"; }
check() { # check <desc> <expected-substring> <actual-output>
  if printf '%s' "$3" | grep -qF "$2"; then good "$1"; else
    bad "$1 — expected '$2' in output:"; printf '%s\n' "$3" | sed 's/^/      | /'; fi
}

say "version & help"
check "version"         "fix 0.4.0"        "$($FIX --version)"
check "help lists install" "install <pkg...>" "$($FIX help)"
check "help lists shell"   "fix shell"     "$($FIX help)"
good   "doctor: $(run doctor 2>&1 | tail -1)"

say "fix init (website: 'Creating environment...')"
mkdir -p "$WORK/proj" && cd "$WORK/proj"
cp -r "$PROJ/vendor" .
out="$(run init 2>&1)"
check "prints Creating environment..." "Creating environment..." "$out"
check "writes fix.yml"    "Wrote"        "$out"
[ -f fix.yml ] && good "fix.yml exists" || bad "fix.yml missing"
grep -q "^packages:" fix.yml && good "fix.yml has packages: list" || bad "no packages: key"

say "fix install (website: 'Building gcc from source...')"
out="$(run install hello banner 2>&1)"
check "hello from source"  "Building hello from source..."  "$out"
check "banner from source" "Building banner from source..." "$out"
check "installed summary"  "installed hello 1.0.0, banner 1.0.0" "$out"
grep -q "  - hello" fix.yml && good "fix.yml lists hello" || bad "hello not in fix.yml"

say "build messages (website: 'Building dependencies...')"
out="$(run build --force 2>&1)"
check "dependencies banner" "Building dependencies..." "$out"
check "building hello"      "Building hello..."         "$out"
[ -f fix.lock ] && good "fix.lock exists" || bad "fix.lock missing"
STORE1="$(run path hello)"
[ -x "$STORE1/bin/hello" ] && good "store binary exists: ${STORE1##*/}" || bad "no binary in store"

say "cached rebuild (content-addressed memoization)"
out="$(run build 2>&1)"
check "hello cached"  "Building hello... (cached)"  "$out"
check "banner cached" "Building banner... (cached)" "$out"
H1="$(run path hello)"

say "fix shell -- cmd"
out="$(run shell -- hello world 2>&1)"
check "hello runs in env" "hello, world! (built from source by fix)" "$out"
out="$(run shell -- banner OK 2>&1)"
check "banner runs in env" "###" "$out"
out="$(run shell -- 'echo $FIX_ENV' 2>&1)"
check "FIX_ENV exported" "proj" "$out"
out="$(run shell -- printf '%s|' "a b" 2>&1)"
check "shell preserves quoting (one 'a b' argument)" "a b|" "$out"

say "fix shell banners (website transcripts)"
printf 'packages:\n  - hello\n  - banner\n\nshell: development\n' > fix.yml
out="$(run shell </dev/null 2>&1)"
check "named banner (Starting development shell...)" "Starting development shell..." "$out"
printf 'packages:\n  - hello\n  - banner\n' > fix.yml
out="$(run shell </dev/null 2>&1)"
check "unnamed banner (Entering fix environment...)" "Entering fix environment..." "$out"
printf 'packages:\n  - hello\n  - banner\n\nshell: development\n' > fix.yml

say "fix install greet (build-time dep on banner)"
out="$(run install greet 2>&1)"
check "greet from source" "Building greet from source..." "$out"
check "banner cached"     "Building banner... (cached)"   "$out"
out="$(run shell -- greet you 2>&1)"
check "greet runs (baked banner from dep)" "greetings, you!" "$out"
check "greet banner baked" "###"            "$out"
grep -q '"deps": \[' fix.lock && good "fix.lock records deps" || bad "fix.lock missing deps"

say "tree / status / list / path"
check "tree shows dep edge"   "banner"      "$(run tree 2>&1)"
check "tree shows shell name" "development" "$(run tree 2>&1)"
check "status shows packages" "packages"    "$(run status 2>&1)"
check "list shows env"        "proj"        "$(run list 2>&1)"
check "path is stable"        "$H1"         "$(run path hello)"

say "error paths"
out="$(run install nosuchpkg 2>&1)"; rc=$?
check "unknown pkg suggests" "not found in any repo" "$out"
[ "$rc" -ne 0 ] && good "unknown pkg exits nonzero" || bad "unknown pkg rc=0"
out="$(run remove banner 2>&1)"; rc=$?
check "remove refused (dependent)" "greet depends on banner" "$out"
[ "$rc" -ne 0 ] && good "refused remove exits nonzero" || bad "refused remove rc=0"
out="$(run remove greet banner 2>&1)"
check "remove both ok" "removed from fix.yml" "$out"

say "gc (roots: this env keeps hello; greet/banner entries collectable)"
cp "$FIX_TEST_HOME/state.json" "$WORK/state.before"
out="$(run gc --dry-run 2>&1)"
check "dry-run would free" "would free" "$out"
cmp -s "$FIX_TEST_HOME/state.json" "$WORK/state.before" \
  && good "gc --dry-run leaves state.json byte-identical" \
  || bad "gc --dry-run mutated state.json"
N_BEFORE="$(ls "$FIX_TEST_HOME/store" | wc -l)"
run gc >/dev/null 2>&1
N_AFTER="$(ls "$FIX_TEST_HOME/store" | wc -l)"
[ "$N_AFTER" -lt "$N_BEFORE" ] && good "gc removed entries ($N_BEFORE→$N_AFTER)" \
  || bad "gc removed nothing"
check "hello still present after gc" "$H1" "$(run path hello)"
out="$(run shell -- hello 2>&1)"
check "env still works after gc" "hello, fix!" "$out"

say "reproducibility: identical inputs → identical hash"
run upgrade >/dev/null 2>&1
check "hash stable after upgrade" "$H1" "$(run path hello)"

say "v0.3: file mode changes the hash, unsafe specs are rejected"
chmod -x vendor/hello/hello.c
run build >/dev/null 2>&1
H2="$(run path hello)"
[ "$H2" != "$H1" ] && good "chmod -x changes store path ($H1 → $H2)" \
  || bad "chmod -x did not change store path"
chmod +x vendor/hello/hello.c
run build >/dev/null 2>&1
check "restoring mode restores store path" "$H1" "$(run path hello)"

# (b) a symlink escaping the package is rejected
mkdir -p "$WORK/bad-symlink/vendor" && cp -r "$PROJ/vendor" "$WORK/bad-symlink/"
printf 'packages:\n  - hello\n\nshell: bad\n' > "$WORK/bad-symlink/fix.yml"
mkdir -p "$WORK/bad-symlink/vendor/evil"
printf 'package:\n  name: evil\n  version: 1.0.0\n\nbuild:\n  steps:\n    - "true"\n' > "$WORK/bad-symlink/vendor/evil/fix.yml"
ln -s /etc/passwd "$WORK/bad-symlink/vendor/evil/evil.txt"
out="$(run --file "$WORK/bad-symlink/fix.yml" install evil 2>&1)"; rc=$?
check "symlink escaping package rejected" "escapes the package" "$out"
[ "$rc" -ne 0 ] && good "escaping symlink exits nonzero" || bad "escaping symlink rc=0"

# (c) a source.dir escaping the package is rejected
mkdir -p "$WORK/bad-srcdir/vendor" && cp -r "$PROJ/vendor" "$WORK/bad-srcdir/"
printf 'packages:\n  - hello\n\nshell: bad\n' > "$WORK/bad-srcdir/fix.yml"
mkdir -p "$WORK/bad-srcdir/vendor/esc"
printf 'package:\n  name: esc\n  version: 1.0.0\n\nsource:\n  dir: ../..\n\nbuild:\n  steps:\n    - "true"\n' > "$WORK/bad-srcdir/vendor/esc/fix.yml"
out="$(run --file "$WORK/bad-srcdir/fix.yml" install esc 2>&1)"; rc=$?
check "source.dir escaping package rejected" "escapes the package directory" "$out"
[ "$rc" -ne 0 ] && good "escaping source.dir exits nonzero" || bad "escaping source.dir rc=0"

# (d) a spec env: setting PATH is rejected
mkdir -p "$WORK/bad-env/vendor" && cp -r "$PROJ/vendor" "$WORK/bad-env/"
printf 'packages:\n  - hello\n\nshell: bad\n' > "$WORK/bad-env/fix.yml"
mkdir -p "$WORK/bad-env/vendor/badenv"
printf 'package:\n  name: badenv\n  version: 1.0.0\n\nbuild:\n  steps:\n    - "true"\n\nenv:\n  PATH: "/tmp"\n' > "$WORK/bad-env/vendor/badenv/fix.yml"
out="$(run --file "$WORK/bad-env/fix.yml" install badenv 2>&1)"; rc=$?
check "spec env PATH rejected" "env key 'PATH' is not allowed" "$out"
[ "$rc" -ne 0 ] && good "blocked env key exits nonzero" || bad "blocked env key rc=0"

say "P2: search, path, -v/-q, interactive shell, init --shell, examples/"
check "search finds hello" "hello" "$(run search hello 2>&1)"
check "search . lists all repos" "greet" "$(run search . 2>&1)"
check "search with no match" "no packages matched" "$(run search nosuchpkg 2>&1)"
check "path hello prints the store path" "$H1" "$(run path hello)"
check "path prints all store paths" "$H1" "$(run path 2>&1)"
mkdir -p "$WORK/nolock"
printf 'packages:\n  - hello\n' > "$WORK/nolock/fix.yml"
out="$(run --file "$WORK/nolock/fix.yml" path 2>&1)"; rc=$?
check "path without a lock explains itself" "no fix.lock yet" "$out"
[ "$rc" -ne 0 ] && good "path without a lock exits nonzero" || bad "path without a lock rc=0"
out="$(run -v build --force 2>&1)"
check "verbose shows build steps" "\$ cc" "$out"
out="$(run -q build --force 2>&1)"
printf '%s' "$out" | grep -q "Building dependencies" \
  && bad "quiet build still printed info" \
  || good "quiet build suppresses info"
out="$(printf 'hello world\nexit\n' | run shell 2>&1)"
check "interactive shell runs piped commands" "hello, world! (built from source by fix)" "$out"
check "interactive shell prints the banner" "Starting development shell..." "$out"
mkdir -p "$WORK/init-shell" && cp -r "$PROJ/vendor" "$WORK/init-shell/vendor"
cd "$WORK/init-shell"
out="$(run init --shell myshell hello 2>&1)"
grep -q "^shell: myshell" fix.yml && good "init --shell writes shell: into fix.yml" \
  || bad "init --shell did not write shell:"
out="$(run shell </dev/null 2>&1)"
check "init --shell names the shell banner" "Starting myshell shell..." "$out"
cd "$WORK/proj"
# examples/fix.yml works from examples/ via repos: {shared: ../vendor}
# (tree only: a build here would write examples/fix.lock into the repo)
check "examples/fix.yml resolves" "greet" \
  "$(run --file "$PROJ/examples/fix.yml" tree 2>&1)"

say "P0: corrupt state.json must not wipe the store; fix shell must not serve stale binaries"
# corrupt state.json AND its backup: gc must refuse and delete nothing
N_BEFORE="$(ls "$FIX_TEST_HOME/store" | wc -l)"
printf '{garbage' > "$FIX_TEST_HOME/state.json"
printf '{also garbage' > "$FIX_TEST_HOME/state.json.bak"
out="$(run gc 2>&1)"; rc=$?
check "gc refuses with corrupt state" "refusing to delete store entries" "$out"
[ "$rc" -ne 0 ] && good "corrupt-state gc exits nonzero" || bad "corrupt-state gc rc=0"
N_AFTER="$(ls "$FIX_TEST_HOME/store" | wc -l)"
[ "$N_AFTER" -eq "$N_BEFORE" ] && good "corrupt-state gc deleted nothing ($N_BEFORE entries kept)" \
  || bad "corrupt-state gc deleted entries ($N_BEFORE→$N_AFTER)"
# a later build recovers: it preserves the corrupt bytes and records fresh state
out="$(run build 2>&1)"
check "build recovers from corrupt state" "environment ready" "$out"
ls "$FIX_TEST_HOME"/state.json.corrupt-* >/dev/null 2>&1 \
  && good "corrupt state.json preserved" || bad "corrupt state.json not preserved"
python3 -c "import json; json.load(open('$FIX_TEST_HOME/state.json'))" \
  && good "state.json parseable after recovery" || bad "state.json still corrupt"

# stale shell: edit a source file without touching fix.yml — the shell
# must serve the NEW binary, not the locked one
sed -i 's/built from source by fix/built from source by fix CHANGED/' vendor/hello/hello.c
out="$(run shell -- hello world 2>&1)"
check "shell serves new binary after source edit" "CHANGED" "$out"
sed -i 's/built from source by fix CHANGED/built from source by fix/' vendor/hello/hello.c
out="$(run shell -- hello world 2>&1)"
check "shell serves restored binary" "hello, world! (built from source by fix)" "$out"
run build >/dev/null 2>&1
check "restoring source restores store path" "$H1" "$(run path hello)"

say "P1: corrupt fix.lock gives a fix: error, not a traceback"
printf 'not json{' > fix.lock
out="$(run status 2>&1)"; rc=$?
check "corrupt lock status is a fix: error" "is corrupt" "$out"
[ "$rc" -ne 0 ] && good "corrupt-lock status exits nonzero" || bad "corrupt-lock status rc=0"
printf '%s' "$out" | grep -q "Traceback" \
  && bad "corrupt-lock status printed a traceback" \
  || good "corrupt-lock status printed no traceback"
run build >/dev/null 2>&1
check "build regenerates a corrupt lock" "$H1" "$(run path hello)"

say "v0.4: tarball source (file://) — install + shell"
mkdir -p "$WORK/tb-pkg/tb-1.0" "$WORK/proj/vendor/tb"
printf 'hello tarball\n' > "$WORK/tb-pkg/tb-1.0/a.txt"
printf '#!/bin/sh\necho tb-runs\n' > "$WORK/tb-pkg/tb-1.0/run.sh"
chmod 755 "$WORK/tb-pkg/tb-1.0/run.sh"
tar -czf "$WORK/tb.tar.gz" -C "$WORK/tb-pkg" tb-1.0
TB_SHA="$(python3 -c "import hashlib;print(hashlib.sha256(open('$WORK/tb.tar.gz','rb').read()).hexdigest())")"
cat > "$WORK/proj/vendor/tb/fix.yml" <<EOF
package:
  name: tb
  version: 1.0.0

source:
  type: tarball
  url: file://$WORK/tb.tar.gz
  sha256: $TB_SHA
  strip: 1

build:
  steps:
    - "cp a.txt %(out)s/share/a.txt"
    - "install -m 755 run.sh %(out)s/bin/tb"
EOF
out="$(run install tb 2>&1)"
check "tarball install builds" "installed tb 1.0.0" "$out"
out="$(run shell -- tb 2>&1)"
check "tarball bin runs in env" "tb-runs" "$out"
check "tarball a.txt installed" "hello tarball" "$(cat "$(run path tb)/share/a.txt")"
[ -f "$FIX_TEST_HOME/cache/$TB_SHA" ] \
  && good "tarball cached by sha256" || bad "tarball not cached"

say "v0.4: remote repo (file://) — install + repo list/update"
mkdir -p "$WORK/remoterepo/rp" "$WORK/proj2"
printf 'package:\n  name: rp\n  version: 1.0.0\n\nbuild:\n  steps:\n    - "cp a.txt %%(out)s/share/a.txt"\n    - "mkdir -p %%(out)s/bin && install -m 755 run.sh %%(out)s/bin/rp"\n' \
  > "$WORK/remoterepo/rp/fix.yml"
printf 'from remote repo\n' > "$WORK/remoterepo/rp/a.txt"
printf '#!/bin/sh\necho rp-runs\n' > "$WORK/remoterepo/rp/run.sh"
chmod 755 "$WORK/remoterepo/rp/run.sh"
tar -czf "$WORK/repo.tar.gz" -C "$WORK/remoterepo" .
REPO_SHA="$(python3 -c "import hashlib;print(hashlib.sha256(open('$WORK/repo.tar.gz','rb').read()).hexdigest())")"
printf 'repos:\n  upstream: file://%s/repo.tar.gz#sha256=%s\n\npackages:\n  - rp\n\nshell: remote\n' \
  "$WORK" "$REPO_SHA" > "$WORK/proj2/fix.yml"
out="$(run --file "$WORK/proj2/fix.yml" install rp 2>&1)"
check "remote repo install builds" "installed rp 1.0.0" "$out"
out="$(run --file "$WORK/proj2/fix.yml" shell -- rp 2>&1)"
check "remote repo bin runs in env" "rp-runs" "$out"
out="$(run --file "$WORK/proj2/fix.yml" repo list 2>&1)"
check "repo list shows the remote repo" "upstream" "$out"
check "repo list shows it cached" "cached" "$out"
out="$(run --file "$WORK/proj2/fix.yml" repo update upstream 2>&1)"
check "repo update refreshes the remote repo" "updated upstream" "$out"
out="$(run --file "$WORK/proj2/fix.yml" search . 2>&1)"
check "search finds the remote repo package" "rp" "$out"

say "P1: purge refuses without -y when stdin is not a TTY"
out="$(run purge </dev/null 2>&1)"; rc=$?
check "purge refuses without -y (non-TTY)" "without -y" "$out"
[ "$rc" -ne 0 ] && good "non-TTY purge exits nonzero" || bad "non-TTY purge rc=0"
[ -d "$FIX_TEST_HOME/store" ] && good "non-TTY purge left the store intact" \
  || bad "non-TTY purge deleted the store"

say "purge"
run purge -y >/dev/null 2>&1
[ ! -d "$FIX_TEST_HOME/store" ] && good "store removed" || bad "store survived purge"

printf '\n\033[1m%d passed, %d failed\033[0m\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || { rm -rf "$WORK"; exit 1; }
rm -rf "$WORK"
echo "ALL SMOKE TESTS PASSED"
