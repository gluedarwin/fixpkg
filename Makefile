.PHONY: all build test test-binary test-mini install clean

all: build

build: ## compile the standalone `fix` binary with PyInstaller
	python3 -m PyInstaller --onefile --clean --strip --name fix fix.py
	cp dist/fix fix
	@ls -lh fix

test: ## run the smoke suite + unit tests against the python source
	bash tests/smoke.sh
	python3 tests/test_hashing.py
	python3 tests/test_p0.py
	python3 tests/test_p1.py
	python3 tests/test_remote.py
	python3 tests/test_sandbox.py

test-binary: ## run the smoke suite against the compiled binary
	FIX_BIN=./fix bash tests/smoke.sh

test-mini: ## force the fallback mini-YAML parser
	FIX_MINI_YAML=1 bash tests/smoke.sh

install: ## install the binary to /usr/local/bin (may need sudo)
	mkdir -p /usr/local/bin
	install -m 755 fix /usr/local/bin/fix

clean:
	rm -rf build dist fix.spec __pycache__ fix
