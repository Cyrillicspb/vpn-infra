# Deploy State Contract

`deploy.sh`, watchdog API и Telegram-бот должны опираться на один и тот же contract в `/opt/vpn/.deploy-state/`.

## Files

- `current.json` — последний committed release.
- `pending.json` — текущая выполняющаяся операция deploy/rollback.
- `last-attempt.json` — итог последней попытки apply/rollback.

Такая же структура должна зеркалироваться и на VPS в `/opt/vpn/.deploy-state/` для parity check и операторской диагностики.

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
  "message": "release applied"
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
  "phase": "apply-vps",
  "status": "running",
  "message": "applying VPS release fedcba987654"
}
```

Допустимые `phase`:
- `prepare`
- `apply-home`
- `apply-vps`
- `verify`
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
  "message": "release fedcba987654 applied"
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
- `apply-vps`
- `verify`
- `commit`
- `rollback`

## Behavioral Rules

- `current.json` обновляется только после успешного health gate и commit release.
- `pending.json` создаётся до начала apply и очищается только после successful commit или successful rollback.
- Любой unsafe state после snapshot переводит deploy в `failed`, затем запускает rollback.
- Если rollback сам не проходит verification, `pending.json` остаётся с `phase=rollback` и `status=failed`, а `last-attempt.json` получает `rollback-failed`.
- Watchdog и бот не должны парсить stdout `deploy.sh` для определения результата; stdout допустим только как вспомогательная диагностика.
- Home и VPS должны показывать один и тот же committed release после успешного deploy и после успешного rollback.
