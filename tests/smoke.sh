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
check "version"         "fix 0.2.0"        "$($FIX --version)"
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
out="$(run gc --dry-run 2>&1)"
check "dry-run would free" "would free" "$out"
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

say "purge"
run purge >/dev/null 2>&1
[ ! -d "$FIX_TEST_HOME/store" ] && good "store removed" || bad "store survived purge"

printf '\n\033[1m%d passed, %d failed\033[0m\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || { rm -rf "$WORK"; exit 1; }
rm -rf "$WORK"
echo "ALL SMOKE TESTS PASSED"
