# Deploy State Contract

`deploy.sh`, watchdog API и Telegram-бот должны опираться на один и тот же contract в `/opt/vpn/.deploy-state/`.

## Files

- `current.json` — последний committed release.
- `pending.json` — текущая выполняющаяся операция deploy/rollback.
- `last-attempt.json` — итог последней попытки apply/rollback.

Такая же структура должна зеркалироваться на каждом backend node в `/opt/vpn/.deploy-state/` для parity check и операторской диагностики.

## current.json

```json
{
  "current_release": {
    "id": "fedcba987654",
    "sha": "fedcba9876543210fedcba9876543210fedcba98",
    "version": "v1.2.3"
  },
  "previous_release": {
    "id": "abc123def456",
    "sha": "abc123def4567890abc123def4567890abc12345",
    "version": "v1.2.2"
  },
  "status": "ready",
  "message": "release applied",
  "target_source": "origin",
  "origin_sha": "fedcba9876543210fedcba9876543210fedcba98",
  "mirror_sha": "fedcba9876543210fedcba9876543210fedcba98",
  "mirror_parity": "ok",
  "backend_targets": [
    {"id": "backend-a", "ip": "198.51.100.10", "ssh_port": 22, "tunnel_ip": "10.177.2.2", "ordinal": 0},
    {"id": "backend-b", "ip": "203.0.113.20", "ssh_port": 22, "tunnel_ip": "10.177.2.6", "ordinal": 1}
  ]
}
```

Допустимые `status`:
- `ready`

Допустимые `message`:
- `release applied`
- `rollback completed`

## pending.json

```json
{
  "pending_release": {
    "id": "fedcba987654",
    "sha": "fedcba9876543210fedcba9876543210fedcba98",
    "version": "v1.2.3"
  },
  "base_release": {
    "id": "abc123def456",
    "sha": "abc123def4567890abc123def4567890abc12345",
    "version": "v1.2.2"
  },
  "phase": "apply-backends",
  "status": "running",
  "message": "applying backend release fedcba987654",
  "target_source": "origin",
  "origin_sha": "fedcba9876543210fedcba9876543210fedcba98",
  "mirror_sha": "fedcba9876543210fedcba9876543210fedcba98",
  "mirror_parity": "ok",
  "backend_targets": [
    {"id": "backend-a", "ip": "198.51.100.10", "ssh_port": 22, "tunnel_ip": "10.177.2.2", "ordinal": 0},
    {"id": "backend-b", "ip": "203.0.113.20", "ssh_port": 22, "tunnel_ip": "10.177.2.6", "ordinal": 1}
  ]
}
```

Допустимые `phase`:
- `prepare`
- `apply-home`
- `apply-backends`
- `verify-backends`
- `rollback`

Допустимые `status`:
- `running`
- `failed`

`pending.json` существует только во время активного deploy/rollback или если automation остановилась в аварийном состоянии.

## last-attempt.json

```json
{
  "status": "success",
  "phase": "commit",
  "message": "release fedcba987654 applied",
  "target_source": "origin",
  "origin_sha": "fedcba9876543210fedcba9876543210fedcba98",
  "mirror_sha": "fedcba9876543210fedcba9876543210fedcba98",
  "mirror_parity": "ok",
  "backend_targets": [
    {"id": "backend-a", "ip": "198.51.100.10", "ssh_port": 22, "tunnel_ip": "10.177.2.2", "ordinal": 0},
    {"id": "backend-b", "ip": "203.0.113.20", "ssh_port": 22, "tunnel_ip": "10.177.2.6", "ordinal": 1}
  ]
}
```

Допустимые `status`:
- `success`
- `noop`
- `failed`
- `rollback-completed`
- `rollback-failed`
- `running`

Допустимые `phase`:
- `check`
- `prepare`
- `apply-home`
- `apply-backends`
- `verify-backends`
- `commit`
- `rollback`

## Behavioral Rules

- latest release tag из `origin` является единственным authoritative source для выбора `target release`.
- `vps-mirror` не выбирает target release; он используется только как parity gate перед backend rollout.
- `mirror_parity` должен быть `ok` для strict deploy. Состояния `stale`, `unreachable`, `missing-ref` и `not-configured` считаются blocker для apply.
- `current.json` обновляется только после успешного health gate и commit release.
- `pending.json` создаётся до начала apply и очищается только после successful commit или successful rollback.
- Любой unsafe state после snapshot переводит deploy в `failed`, затем запускает rollback.
- Если rollback сам не проходит verification, `pending.json` остаётся с `phase=rollback` и `status=failed`, а `last-attempt.json` получает `rollback-failed`.
- Watchdog и бот не должны парсить stdout `deploy.sh` для определения результата; stdout допустим только как вспомогательная диагностика.
- Home и все backend nodes должны показывать один и тот же committed release после успешного deploy и после успешного rollback.
- Rollout policy для multi-VPS strict: если хотя бы один backend не проходит apply или verify, весь release не коммитится и запускается cluster-wide rollback.
- `target_source`, `origin_sha`, `mirror_sha` и `mirror_parity` должны отражать фактический source/parity контекст последней операции deploy или rollback.
