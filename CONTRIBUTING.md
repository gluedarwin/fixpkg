# Contributing to fix

`fix` is deliberately small: the whole manager is `fix.py` (stdlib only),
plus the bundled example recipes in `vendor/` and the website in `docs/`.

## Running the tests

```sh
make test         # smoke suite + all unit tests, against the Python source
make test-mini    # same smoke suite with the fallback mini-YAML parser
                  # (FIX_MINI_YAML=1)
make test-binary  # smoke suite against the compiled ./fix binary
                  # (build it first: bash scripts/build_binary.sh)
```

The unit tests are plain stdlib scripts — no pytest needed:

```sh
python3 tests/test_hashing.py   # derivation hashing, symlinks, env allowlist
python3 tests/test_p0.py        # corrupt state, atomic writes, stale shell
python3 tests/test_p1.py        # YAML parity, versions, cycles, gc, doctor,
                                # purge, shell quoting, save fidelity
```

Tests run in an isolated home (`FIX_TEST_HOME`); the smoke suite uses a
fresh temp directory per run and cleans up after itself.

## Making changes

- **Add or extend tests with every behaviour change.** A fix without a
  test that fails before it and passes after it is not done.
- Keep `fix.py` stdlib-only. If a feature needs a new dependency, say so
  in the PR — it is probably out of scope.
- Keep the README and `docs/index.html` honest: if a claim changes
  (sandboxing, reproducibility, supported platforms), update the docs in
  the same change.
- Record user-visible changes in `CHANGELOG.md` under the unreleased
  `0.3.0` entry.
- Run `make test` and `make test-mini` before submitting; both must pass.

## Releases

Bump `__version__` in `fix.py` and add a `CHANGELOG.md` entry. A change
to the derivation hash is breaking: bump `DERIVATION_SCHEMA` (e.g.
`fix-derivation-v3`) and say so in the changelog — old store entries
become unreachable and are collected by `fix gc`.
