# Changelog

All notable changes to this project are documented here.

This project uses strict Semantic Versioning: `MAJOR.MINOR.PATCH`.

## [Unreleased]

### Added
- Deploy: populate OSS deploy template (`de2dcf2`)
- Infra: OOM mitigation — `user-1000.slice` MemoryHigh=20G / MemoryMax=24G drop-in + idempotent installer (`deploy/user-1000.slice.d/override.conf`, `core/scripts/install-oom-mitigation.sh`). Caps Claude/Codex CLI sessions to prevent the cascade OOM kill that took down sshd + pir-api + tmux for ~25h on 2026-05-18/19 (learning `f47b9d0a`).

### Fixed
- Hooks: point safety bridge to core scripts (`3acab45`)
- License: replace AGPL PDF parser fallbacks (`a7702ea`)

## [v2.0.0] - 2026-05-18

### Changed
- Materialized the final core layout for the Plan 4 switch path.
- Preserved the existing annotated unsigned `v2.0.0` tag for backward compatibility.

### Fixed
- Added frontmatter to the final audit report.

## [v1.0.0] - 2026-05-11

### Changed
- Historical baseline before the Plan 4 release train. Detailed entries were not backfilled.
