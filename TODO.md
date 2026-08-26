# Roadmap / TODO

This project is usable, but it is still under active development. The items
below are planned work rather than guarantees for a particular release.

## Stability and bug fixing

- [ ] Fix known gameplay, lobby, matchmaking, and reconnect edge cases.
- [ ] Add regression tests for every confirmed protocol bug.
- [ ] Improve recovery from dropped TCP and UDP sessions.
- [ ] Validate malformed, duplicated, delayed, and out-of-order packets.
- [ ] Reduce noisy logs and make warnings and failures actionable.
- [ ] Test long-running servers, concurrent races, and clean shutdown/restart.

## Protocol completeness

- [ ] Audit responses against the original clients for all three supported games.
- [ ] Identify and implement missing protocol commands and state transitions.
- [ ] Map protocol error codes to the correct client-visible behavior.
- [ ] Replace generic or placeholder errors with game-specific error codes.
- [ ] Document understood and unknown error codes, packet fields, and quirks.
- [ ] Add packet captures and compatibility cases to the test suite where legally
      distributable.

## Rankings and progression

- [ ] Fully implement race-result validation and ranking updates.
- [ ] Complete leaderboards for Underground 2, Most Wanted, and Carbon.
- [ ] Support ties, disconnects, invalid results, and abandoned races correctly.
- [ ] Finish persistent player statistics and progression data.
- [ ] Add ranking administration, repair, import, export, and reset tools.
- [ ] Add tests for multiplayer ranking calculations and protocol responses.

## Configuration and options

- [ ] Expose remaining safe runtime options through `config/server.toml`.
- [ ] Validate every option and report invalid combinations clearly.
- [ ] Add per-game feature flags and compatibility settings.
- [ ] Improve CLI commands for accounts, moderation, statistics, and maintenance.
- [ ] Support configuration migration between releases.
- [ ] Provide documented production-ready configuration examples.

## Structure and maintainability

- [ ] Reduce duplicated Classic and Carbon account, social, and admin logic.
- [ ] Clarify service boundaries and shared protocol abstractions.
- [ ] Split remaining large modules into focused components.
- [ ] Add type checking, linting, and formatting to CI.
- [ ] Improve database migrations, backups, and integrity checks.
- [ ] Keep client patches isolated, documented, and independently testable.

## Documentation

- [ ] Write a complete installation and Internet deployment guide.
- [ ] Document router, firewall, NAT, and port-forwarding requirements.
- [ ] Add client installation and troubleshooting guides for each game.
- [ ] Document the server architecture, protocols, and database layout.
- [ ] Publish an administrator command and configuration reference.
- [ ] Add a compatibility matrix and a list of known limitations.

## Polish and release quality

- [ ] Improve first-run setup and diagnostics.
- [ ] Standardize messages, help text, logs, and exit codes.
- [ ] Add release packaging and upgrade checks for server and client plugins.
- [ ] Test clean installations on supported Linux and Termux environments.
- [ ] Add performance and resource-usage benchmarks.
- [ ] Triage community reports and keep this roadmap current.
