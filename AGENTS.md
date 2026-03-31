# Repository Guidelines

## Project Structure & Module Organization
Root scripts are the primary entrypoints: `setup.sh` orchestrates full installation, while `install-home.sh`, `install-vps.sh`, `deploy.sh`, and `restore.sh` handle specific lifecycle stages. Runtime assets live under `home/` (home server services, Docker, watchdog, Telegram bot, scripts) and `vps/` (VPS Docker stack, nginx, 3x-ui, cloudflared). Tests are in `tests/`, migrations in `migrations/`, and operator docs in `docs/`.

## Build, Test, and Development Commands
- `sudo bash setup.sh`: full end-to-end install on the home server.
- `sudo bash install-home.sh`: run or rerun only the home-server phase.
- `sudo bash install-vps.sh`: run or rerun only the VPS phase.
- `bash home/scripts/post-install-check.sh`: post-install verification and report.
- `bash home/scripts/docker-phase2.sh`: retry Telegram bot build and install monitoring after VPN is up.
- `bash -n setup.sh` or `bash -n home/scripts/post-install-check.sh`: quick shell syntax check.
- `python3 -m py_compile home/watchdog/watchdog.py`: validate Python syntax after watchdog changes.

## Coding Style & Naming Conventions
Shell is the dominant language. Use `bash`, 4-space indentation, lowercase snake_case variables/functions, and keep logic idempotent. Reuse `common.sh` helpers and `/opt/vpn/.env` as the single source of truth. Do not patch live server configs manually; fix the generating script or template instead. Python modules should stay simple, typed where practical, and consistent with existing watchdog and bot patterns.

## Testing Guidelines
Every behavioral fix should include a verification path. Prefer rerunnable checks over manual inspection: syntax checks, `post-install-check.sh`, and targeted smoke tests in `tests/smoke/`. When adding a new failure mode, add or update a test or a post-install check. Name shell tests descriptively, for example `tests/smoke/test_ssh_proxy.sh`.

## Commit & Pull Request Guidelines
Follow the existing git history: concise imperative subjects such as `fix: restart 3x-ui...`, `feat: verify VPS root password...`, or `Bundle telegram bot wheels...`. Keep commits scoped to one problem. PRs should explain operational impact, affected install phase (`home`, `vps`, `watchdog`, `post-install`), rollback considerations, and include relevant logs or screenshots for installer and routing changes.

## Security & Operations Notes
Never commit real secrets. Assume production is active. Preserve `.env` ownership and permissions, avoid ad-hoc server edits, and prefer safe reruns by clearing the relevant `.setup-state` marker and re-executing the script.

## Local Operator Notes
Before any SSH/server diagnostics or live changes, check the local-only file `.codex-local/ssh-access.md` if it exists. This file is excluded from git and may contain current hostnames, users, passwords, jump-host notes, and other session-critical operational access details.
