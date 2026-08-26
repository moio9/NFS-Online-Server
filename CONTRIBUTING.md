# Contributing

## Contribution licensing

By submitting code, documentation, tests, or other material to this repository, you certify that you have the right to submit it and agree that it is licensed under `AGPL-3.0-or-later`, unless the maintainers explicitly agree to different terms in writing before accepting the contribution. Do not submit third-party material without preserving its compatible license and attribution.

## Before opening a change

- Keep `common` independent from game-specific packages.
- `classic` and `carbon` may import `common`, but must not import each other.
- Do not add original game executables, proprietary SDK files, packet captures containing real identities, production databases, logs, or player data.
- Keep protocol claims tied to captures, decompilation evidence, or reproducible tests. Clearly label local abstractions as local abstractions.
- Do not commit built ASI files. CI and Releases handle client artifacts.

## Development setup

Python 3.10+ is sufficient for the server tests. No third-party Python package is required.

```bash
python nfs_online.py check
python tools/check_public_tree.py
```

Run all suites before submitting a pull request:

```bash
python -m unittest discover -s tests -q
(cd server/common && PYTHONPATH=.. python -m unittest discover -s tests -q)
(cd server/classic && PYTHONPATH=.. python -m unittest discover -s tests -q)
(cd server/carbon && PYTHONPATH=.. python -m unittest discover -s tests -q)
```

Build and audit the clients when changing Zig code:

```bash
cd source/client-zig
./build_linux.sh
```

## Pull requests

A pull request should state:

- affected game and component;
- the observed problem and expected behavior;
- the protocol evidence or reasoning behind the change;
- tests added or updated;
- compatibility or migration impact;
- whether client and server must be upgraded together.

Use synthetic identities and RFC 5737 documentation addresses in tests and examples. Sanitize logs before attaching them.

## Coding conventions

- Python: four spaces, type hints for new public interfaces, focused modules, explicit error handling.
- Zig: run `zig fmt`; keep patch addresses and displaced instructions documented near the hook.
- Configuration: server settings belong in `config/server.toml`; client settings remain in their matching INI files.
- Persistent writes should be atomic where practical and must not silently discard existing player state.
