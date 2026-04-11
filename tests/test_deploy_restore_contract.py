import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy.sh"
RESTORE = ROOT / "restore.sh"


class DeployRestoreContractTests(unittest.TestCase):
    def run_cmd(self, cmd, env=None):
        merged_env = os.environ.copy()
        tmpdir = tempfile.mkdtemp(prefix="deploy-restore-test-")
        merged_env.update(
            {
                "ALLOW_NON_ROOT": "1",
                "LOG_FILE": str(Path(tmpdir) / "script.log"),
                "LOCK_FILE": str(Path(tmpdir) / "script.lock"),
            }
        )
        if env:
            merged_env.update(env)
        return subprocess.run(
            cmd,
            cwd=ROOT,
            env=merged_env,
            text=True,
            capture_output=True,
            check=False,
        )

    def make_repo_with_state(self, root: Path, current_id="abc123def456", current_sha="abc123def4567890", version="v0.test"):
        repo = root / "repo"
        state_dir = repo / ".deploy-state"
        snapshot_dir = repo / ".deploy-snapshot"
        vps_state_dir = root / "vps-state"
        repo.mkdir()
        state_dir.mkdir()
        snapshot_dir.mkdir()
        vps_state_dir.mkdir()
        (repo / "version").write_text(f"{version}\n", encoding="utf-8")
        (repo / ".env").write_text("WATCHDOG_API_TOKEN=test-token\nVPS_IP=127.0.0.1\n", encoding="utf-8")
        (state_dir / "current.json").write_text(
            json.dumps(
                {
                    "current_release": {"id": current_id, "sha": current_sha, "version": version},
                    "previous_release": {"id": "", "sha": "", "version": ""},
                    "status": "ready",
                    "message": "release applied",
                }
            ),
            encoding="utf-8",
        )
        (state_dir / "last-attempt.json").write_text(
            json.dumps({"status": "success", "phase": "commit", "message": f"release {current_id} applied"}),
            encoding="utf-8",
        )
        return repo, state_dir, snapshot_dir, vps_state_dir

    def deploy_env(self, tmp: str, repo: Path, state_dir: Path, snapshot_dir: Path, vps_state_dir: Path, **extra):
        env = {
            "ALLOW_NON_ROOT": "1",
            "REPO_DIR": str(repo),
            "STATE_DIR": str(state_dir),
            "SNAPSHOT_DIR": str(snapshot_dir),
            "LOG_FILE": str(Path(tmp) / "deploy.log"),
            "LOCK_FILE": str(Path(tmp) / "deploy.lock"),
            "DEPLOY_TEST_MODE": "1",
            "VPS_IP": "127.0.0.1",
            "VPS_STATE_DIR": str(vps_state_dir),
        }
        env.update(extra)
        return env

    def test_deploy_help_lists_release_rollback_contract(self):
        result = self.run_cmd(["bash", str(DEPLOY), "--help"], env={"ALLOW_NON_ROOT": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("bash deploy.sh --rollback", result.stdout)
        self.assertIn("bash deploy.sh --ref <tag|sha>", result.stdout)
        self.assertNotIn("--to", result.stdout)
        self.assertIn("--status", result.stdout)

    def test_restore_help_declares_dr_only_role(self):
        result = self.run_cmd(["bash", str(RESTORE), "--help"], env={"ALLOW_NON_ROOT": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("restore.sh используется только для DR/backup restore", result.stdout)
        self.assertIn("deploy.sh --rollback", result.stdout)

    def test_restore_rejects_deprecated_component_flag(self):
        result = self.run_cmd(["bash", str(RESTORE), "--component"], env={"ALLOW_NON_ROOT": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("не поддерживается", result.stdout + result.stderr)

    def test_deploy_state_contract_doc_exists(self):
        contract_doc = ROOT / "docs" / "DEPLOY-STATE.md"
        self.assertTrue(contract_doc.exists())
        content = contract_doc.read_text(encoding="utf-8")
        self.assertIn("current.json", content)
        self.assertIn("pending.json", content)
        self.assertIn("last-attempt.json", content)
        self.assertIn("rollback-failed", content)
        self.assertIn("backend_targets", content)
        self.assertIn("apply-backends", content)
        self.assertIn("verify-backends", content)
        self.assertIn("target_source", content)
        self.assertIn("origin_sha", content)
        self.assertIn("mirror_sha", content)
        self.assertIn("mirror_parity", content)

    def test_deploy_script_guards_tg_send_and_uses_sudo_for_vps_apply(self):
        deploy_script = DEPLOY.read_text(encoding="utf-8")
        self.assertIn('if [[ ! -x "$tg_send" ]]; then', deploy_script)
        self.assertIn('sudo -n bash -lc', deploy_script)
        self.assertIn('backend_exec "$backend_ip" "$backend_port" "$cmd" || die "Backend ${backend_id} deploy завершился с ошибкой"', deploy_script)
        self.assertNotIn('vps_tmux_exec "$cmd" 300', deploy_script)
        self.assertIn('collect_baseline_smoke_failures', deploy_script)
        self.assertIn('Smoke suite introduced new failures', deploy_script)
        self.assertIn('backend_copy_stdin_to_file "$backend_ip" "$backend_port" "$remote_file" < "$STATE_DIR/$file"', deploy_script)
        self.assertIn('raw="$(backend_read_file "$backend_ip" "$backend_port" "$REMOTE_STATE_DIR/$file" || true)"', deploy_script)
        self.assertIn('DEPLOY_USE_SSH_PROXY="${DEPLOY_USE_SSH_PROXY:-0}"', deploy_script)
        self.assertIn('raw="$(backend_read_json_key "$backend_ip" "$backend_port" "$REMOTE_STATE_DIR/$file" "$key" 2>/dev/null | tr -d', deploy_script)
        self.assertIn('load_backend_targets()', deploy_script)
        self.assertIn('backend_targets_tsv()', deploy_script)
        self.assertIn('rsync -a "$REPO_DIR/home/scripts/" "$REPO_DIR/scripts/"', deploy_script)
        self.assertNotIn('rsync -a --delete "$REPO_DIR/home/scripts/" "$REPO_DIR/scripts/"', deploy_script)
        self.assertIn('export XRAY_VISION_PUBLIC_KEY="${XRAY_VISION_PUBLIC_KEY:-${XRAY_XHTTP_PUBLIC_KEY:-}}"', deploy_script)
        self.assertIn("grep -oE '\\$\\{[^}]+\\}'", deploy_script)
        self.assertIn('TARGET_SOURCE_REMOTE="origin"', deploy_script)
        self.assertNotIn('TARGET_SOURCE_REMOTE="vps-mirror"', deploy_script)
        self.assertIn('MIRROR_PARITY_STATUS="stale"', deploy_script)
        self.assertIn('record_preflight_blocker "mirror-stale" "origin and vps-mirror differ"', deploy_script)
        self.assertIn('record_preflight_blocker "origin-fetch-failed"', deploy_script)
        self.assertIn('record_preflight_blocker "backend-inventory-empty"', deploy_script)
        self.assertIn('git_ls_remote_release_tags "$remote"', deploy_script)
        self.assertIn('github_latest_release_tag "$remote"', deploy_script)
        self.assertIn('ALL_PROXY="socks5h://127.0.0.1:${port}" git -C "$REPO_DIR" fetch "$remote"', deploy_script)
        self.assertIn('ALL_PROXY="socks5h://127.0.0.1:${port}" git ls-remote --tags --refs "$remote" \'v*\'', deploy_script)
        self.assertIn('DEPLOY_TARGET_REF="${DEPLOY_TARGET_REF:-}"', deploy_script)
        self.assertIn('resolve_target_ref_locally "$DEPLOY_TARGET_REF"', deploy_script)
        self.assertIn('resolve_target_ref_for_remote vps-mirror "$ORIGIN_SOURCE_REF"', deploy_script)
        self.assertIn('curl -fsSL "$api_url"', deploy_script)
        self.assertIn('rev-parse "${ORIGIN_SOURCE_REF}^{}"', deploy_script)
        self.assertIn('rev-parse "${source_ref}^{}"', deploy_script)
        self.assertIn('TARGET_RELEASE_VERSION="$(normalized_version_for_sha "$TARGET_RELEASE_SHA" "$(version_for_git_ref "$source_ref"', deploy_script)
        self.assertIn('remove_conflicting_named_container() {', deploy_script)
        self.assertIn('remove_conflicting_named_container "telegram-bot" "telegram-bot"', deploy_script)
        self.assertIn('docker rm -f "$container_name"', deploy_script)
        self.assertNotIn('refs/remotes/${remote}/master', deploy_script)
        self.assertIn('home_pull_remote_services', deploy_script)
        self.assertIn('docker compose -f "$REPO_DIR/docker-compose.yml" pull "${services[@]}"', deploy_script)
        self.assertIn('home_pull_remote_services', deploy_script)
        self.assertIn('docker compose build $bot_no_cache --build-arg GIT_HASH="$bot_git_hash" telegram-bot', deploy_script)
        self.assertIn('^[[:space:]]+\\[FAIL\\][[:space:]]+[a-z0-9_]+[[:space:]]*$', deploy_script)
        self.assertIn('BLOCKED_SITES_RETRY_ATTEMPTS="${BLOCKED_SITES_RETRY_ATTEMPTS:-2}"', deploy_script)
        self.assertIn('run-smoke-tests.sh" --test blocked_sites', deploy_script)
        self.assertIn('blocked_sites стабилизировался после retry', deploy_script)
        self.assertIn('dnsmasq runtime verify failed, retry via restart', deploy_script)
        self.assertIn('systemctl restart dnsmasq || die "home runtime verify failed: dnsmasq restart failed"', deploy_script)
        self.assertIn('if [[ -d "$unit_src" && "$unit_name" == *.service.d ]]; then', deploy_script)
        self.assertIn('diff -qr "$unit_src" "$unit_dst"', deploy_script)
        self.assertIn('rsync -a --delete "$unit_src"/ "$unit_dst"/', deploy_script)
        self.assertNotIn('printf \'%s\\n\' "${TARGET_RELEASE_VERSION#v}" > "$REPO_DIR/version"', deploy_script)

    def test_deploy_script_repairs_state_versions_from_release_sha(self):
        deploy_script = DEPLOY.read_text(encoding="utf-8")
        self.assertIn('repair_state_versions "$CURRENT_STATE_FILE"', deploy_script)
        self.assertIn('current_ver="$(normalized_version_for_sha "${CURRENT_RELEASE_SHA:-}"', deploy_script)
        self.assertIn('target_ver="$(normalized_version_for_sha "$target_sha" "$target_ver")"', deploy_script)
        self.assertIn('previous_ver="$(normalized_version_for_sha "$previous_sha" "$previous_ver")"', deploy_script)
        self.assertIn('verify_home_apply', deploy_script)
        self.assertIn('docker exec "$running_id" test -f /app/bot.py', deploy_script)
        self.assertIn('show_preflight_report', deploy_script)
        self.assertIn('Origin sha:', deploy_script)
        self.assertIn('Mirror parity:', deploy_script)
        self.assertIn('Repo head:', deploy_script)
        do_deploy_section = deploy_script.split("do_deploy() {", 1)[1].split("show_status()", 1)[0]
        self.assertLess(
            do_deploy_section.index('if [[ "$CURRENT_RELEASE_SHA" == "$TARGET_RELEASE_SHA" && "${FORCE_DEPLOY:-false}" != "true" ]]; then'),
            do_deploy_section.index('collect_baseline_smoke_failures')
        )

    def test_update_doc_describes_origin_authority_and_local_build_split(self):
        update_doc = (ROOT / "docs" / "UPDATE.md").read_text(encoding="utf-8")
        self.assertIn("latest release tag из `origin`", update_doc)
        self.assertIn("vps-mirror", update_doc)
        self.assertIn("mirror parity", update_doc)
        self.assertIn("build-local", update_doc)
        self.assertIn("telegram-bot", update_doc)
        self.assertIn("fail fast strict", update_doc)

    def test_home_vision_template_uses_plain_env_vars_after_render_defaults(self):
        vision_template = (ROOT / "home" / "xray" / "config-vision.json").read_text(encoding="utf-8")
        self.assertIn("${XRAY_VISION_UUID}", vision_template)
        self.assertIn("${XRAY_VISION_PUBLIC_KEY}", vision_template)
        self.assertIn("${XRAY_VISION_SHORT_ID}", vision_template)
        self.assertNotIn(":-", vision_template)

    def test_vps_cloudflared_is_not_in_default_compose_startup(self):
        vps_compose = (ROOT / "vps" / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn('profiles: ["manual"]', vps_compose)
        self.assertNotIn('entrypoint: ["/bin/sh"]', vps_compose)
        self.assertIn("./nginx/ssl:/etc/sing-box/certs:ro", vps_compose)

    def test_default_deploy_does_not_force_start_extra_stacks(self):
        deploy_script = DEPLOY.read_text(encoding="utf-8")
        self.assertNotIn("docker compose --profile extra-stacks pull sing-box-tuic-client sing-box-trojan-client", deploy_script)
        self.assertNotIn("docker compose --profile extra-stacks up -d sing-box-tuic-client sing-box-trojan-client", deploy_script)
        self.assertNotIn("docker compose --profile extra-stacks pull trojan-server tuic-server", deploy_script)
        self.assertNotIn("docker compose --profile extra-stacks up -d trojan-server tuic-server", deploy_script)
        self.assertIn("docker compose up -d --force-recreate xray-client-xhttp xray-client-cdn xray-client-vision", deploy_script)

    def test_vpn_policy_routing_reload_does_not_flush_live_control_plane_table(self):
        routing_script = (ROOT / "home" / "scripts" / "vpn-policy-routing.sh").read_text(encoding="utf-8")
        setup_section = routing_script.split("teardown_routing() {", 1)[0]
        self.assertNotIn("ip route flush table $TABLE_VPN", setup_section)
        self.assertNotIn("ip route flush table $TABLE_DPI", setup_section)
        self.assertIn('ip route replace default via "$GATEWAY" dev "$ETH_IFACE" table $TABLE_VPN', setup_section)
        self.assertIn('ip route replace default via "$GATEWAY" dev "$ETH_IFACE" table $TABLE_DPI', setup_section)
        self.assertIn('ip route del "$FUNCTIONAL_NS_SUBNET" dev br-fh table $TABLE_VPN', setup_section)

    def test_watchdog_uses_source_tree_for_telegram_repo_sync(self):
        watchdog = (ROOT / "home" / "watchdog" / "watchdog.py").read_text(encoding="utf-8")
        base_plugin = (ROOT / "home" / "watchdog" / "plugins" / "base.py").read_text(encoding="utf-8")
        self.assertIn('BOT_RUNTIME_DIR = Path("/opt/vpn/telegram-bot")', watchdog)
        self.assertIn('BOT_SOURCE_DIR = Path("/opt/vpn/home/telegram-bot")', watchdog)
        self.assertIn("async def _ensure_active_stack_dataplane()", watchdog)
        self.assertIn('table marked drift: ожидался default dev %s для стека %s, восстанавливаю маршрут', watchdog)
        self.assertIn('Активный dataplane отсутствует: tun %s не найден для стека %s, запускаю self-heal', watchdog)
        self.assertIn("Dataplane self-heal completed: стек %s, tun=%s", watchdog)
        self.assertIn("await _ensure_active_stack_dataplane()", watchdog)
        self.assertIn("ACTIVE_STACK_DATAPLANE_ALERT_COOLDOWN_SECONDS", watchdog)
        self.assertIn("active_stack has socks but no tun", watchdog)
        self.assertIn("active_stack_dataplane_alert_last_ts", watchdog)
        self.assertIn("PROCESS_LOG_DIR = Path(\"/var/log/vpn\")", base_plugin)
        self.assertIn("def process_log_path", base_plugin)
        self.assertIn("def process_meta_path", base_plugin)
        self.assertNotIn("stdout=subprocess.DEVNULL", base_plugin)
        self.assertNotIn("stderr=subprocess.DEVNULL", base_plugin)
        self.assertIn("log_file", base_plugin)
        self.assertIn("started_at_ts", base_plugin)
        self.assertIn("BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()", watchdog)
        self.assertIn("def spawn_background_job(name: str, coro: Any) -> None:", watchdog)
        self.assertIn('spawn_background_job("deploy", _deploy_task(req))', watchdog)
        self.assertIn('spawn_background_job("rollback", _rollback_task())', watchdog)
        self.assertIn("asyncio.create_task(_runner(), name=name)", watchdog)
        self.assertIn('supplied = credentials.credentials.encode("utf-8")', watchdog)
        self.assertIn('expected = API_TOKEN.encode("utf-8")', watchdog)
        self.assertIn('logger.error("Deploy task failed rc=%s last_status=%s detail=%s", rc, last_status, detail[:300])', watchdog)
        self.assertIn('Path("/opt/vpn/.deploy-state/current.json")', watchdog)

        admin_handler = (ROOT / "home" / "telegram-bot" / "handlers" / "admin.py").read_text(encoding="utf-8")
        bot_source = (ROOT / "home" / "telegram-bot" / "bot.py").read_text(encoding="utf-8")
        backup_source = (ROOT / "home" / "scripts" / "backup.sh").read_text(encoding="utf-8")
        compose_source = (ROOT / "home" / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn('Path("/opt/vpn/.deploy-state/current.json")', admin_handler)
        self.assertIn('Path("/opt/vpn/.deploy-state/current.json")', bot_source)
        self.assertIn('Path("/opt/vpn/.deploy-state/current.json").read_text', backup_source)
        self.assertIn('/opt/vpn/.deploy-state:/opt/vpn/.deploy-state:ro', compose_source)

    def test_admin_bot_texts_match_deploy_status_contract(self):
        admin_handler = (ROOT / "home" / "telegram-bot" / "handlers" / "admin.py").read_text(encoding="utf-8")
        watchdog_client = (ROOT / "home" / "telegram-bot" / "services" / "watchdog_client.py").read_text(encoding="utf-8")
        keyboards = (ROOT / "home" / "telegram-bot" / "handlers" / "keyboards.py").read_text(encoding="utf-8")
        self.assertIn("Deploy запущен. Прогресс и итог доступны через /status.", admin_handler)
        self.assertIn("Rollback запущен. Прогресс и итог доступны через /status.", admin_handler)
        self.assertIn("Запустить rollback к последнему подтвержденному snapshot?", admin_handler)
        self.assertIn("version в callback используется только для UX и skip-version, не как аргумент deploy.", admin_handler)
        self.assertIn("_wc().resolve_domain_decision(domain, chat_id=chat_id, source_ip=source_ip)", admin_handler)
        self.assertIn("_wc().choose_backend(", admin_handler)
        self.assertIn("_wc().apply_backend_decision(", admin_handler)
        self.assertIn("Route class:", admin_handler)
        self.assertIn("Decision:", admin_handler)
        self.assertIn("Backend:", admin_handler)
        self.assertIn("Execution:", admin_handler)
        self.assertIn("Family:", admin_handler)
        self.assertIn("Identity:", admin_handler)
        self.assertIn("Client pref:", admin_handler)
        self.assertIn("Pref status:", admin_handler)
        self.assertIn("Pref reason:", admin_handler)
        self.assertIn("Backend path runtime", admin_handler)
        self.assertIn("Path:", admin_handler)
        self.assertIn("Path verify:", admin_handler)
        self.assertIn('title = f"<b>Manual {label} list</b>"', admin_handler)
        self.assertIn("Effective verdict подтверждён:", admin_handler)
        self.assertIn("Effective verdict пока не подтверждён", admin_handler)
        self.assertIn("Это ручные override-домены. Effective verdict смотрите через <code>/check &lt;домен&gt;</code>.", admin_handler)
        self.assertIn("Для effective verdict используйте <code>/check &lt;домен&gt;</code>.", admin_handler)
        self.assertIn("def _apply_manual_route_change(", admin_handler)
        self.assertIn("def _render_manual_route_list_html(", admin_handler)
        self.assertIn("def _parse_check_args(", admin_handler)
        self.assertIn("_read_manual_route_lines(MANUAL_VPN)", admin_handler)
        self.assertIn("_read_manual_route_lines(MANUAL_DIRECT)", admin_handler)
        self.assertIn('/backend_pref add <chat_id> <service|domain|cidr> <value> <backend-id>', admin_handler)
        self.assertIn('@router.message(Command("backend_pref")', admin_handler)
        self.assertIn('AdminFSM.backend_pref_add', admin_handler)
        self.assertIn('adm:backend_pref_add', admin_handler)
        self.assertIn('adm:backend_pref_rm:', admin_handler)
        self.assertIn("/backends", admin_handler)
        self.assertIn("/backend add <IP> [SSH_PORT]", admin_handler)
        self.assertIn("/balancer", admin_handler)
        self.assertIn("/lan_clients", admin_handler)
        self.assertIn("/lan_client add <name> <src_ip>", admin_handler)
        self.assertIn("/lan_backend_pref add <lan_client_id> <service|domain|cidr> <value> <backend-id>", admin_handler)
        self.assertIn("Gateway mode не включён", admin_handler)
        self.assertIn('AdminFSM.client_device_name', admin_handler)
        self.assertIn('AdminFSM.client_device_protocol', admin_handler)
        self.assertIn('adm:cl_devices:', admin_handler)
        self.assertIn('adm:cl_dev_add:', admin_handler)
        self.assertIn('adm:cl_dev_getconf:', admin_handler)
        self.assertIn('adm:cl_dev_refresh:', admin_handler)
        self.assertIn('adm:cl_dev_del:', admin_handler)
        self.assertIn("get_device_with_client", admin_handler)
        self.assertIn("_send_admin_device_config", admin_handler)
        self.assertIn("Recovery-пакет", admin_handler)
        self.assertIn('_FUNCTIONAL_SYSTEM_PEERS = {', admin_handler)
        self.assertIn('"10.177.1.250/32": "synthetic bootstrap awg"', admin_handler)
        self.assertIn('"10.177.3.250/32": "synthetic bootstrap wg"', admin_handler)
        self.assertIn("def _peer_client_traffic_bytes(peer: dict) -> tuple[int, int]:", admin_handler)
        self.assertIn("download = server tx, upload = server rx", admin_handler)
        self.assertIn('system_peers.append(f"{peer_line} | {system_label}")', admin_handler)
        self.assertIn("Автовыбор сервера", keyboards)
        self.assertIn("Сделать активным", keyboards)
        self.assertIn("Предпочтения клиентов", keyboards)
        self.assertIn("Добавить предпочтение клиента", keyboards)
        self.assertIn("adm:backend_pref_rm:", keyboards)
        self.assertIn("Устройства клиента", keyboards)
        self.assertIn("Добавить устройство", keyboards)
        self.assertIn("Получить конфиг", keyboards)
        self.assertIn("Обновить конфиг", keyboards)
        self.assertIn("Manual VPN", keyboards)
        self.assertIn("Manual Direct", keyboards)
        self.assertIn("Локальная сеть", keyboards)
        self.assertIn("Устройства локальной сети", keyboards)
        self.assertIn("Предпочтения для локальной сети", keyboards)
        self.assertIn('return await self._post("/balancer/switch"', watchdog_client)
        self.assertIn('return await self._post("/balancer/auto-select"', watchdog_client)
        self.assertIn('return await self._get("/decision/backends")', watchdog_client)
        self.assertIn("def _normalize_decision_runtime_status", watchdog_client)
        self.assertIn('return await self.get_decision_runtime_status()', watchdog_client)
        self.assertIn('return self._normalize_decision_runtime_status(await self._get("/decision/status"))', watchdog_client)
        self.assertIn('return self._normalize_decision_runtime_status(await self._get("/decision/backend-paths"))', watchdog_client)
        self.assertIn('return await self._get("/gateway/lan-clients")', watchdog_client)
        self.assertIn('return await self._get("/gateway/lan-prefs")', watchdog_client)
        self.assertIn('"/gateway/lan-client/upsert"', watchdog_client)
        self.assertIn('"/gateway/lan-pref/add"', watchdog_client)
        self.assertIn('"/decision/choose-backend"', watchdog_client)
        self.assertIn('"/decision/apply-backend"', watchdog_client)
        self.assertIn('return await self._post("/decision/reassign"', watchdog_client)
        self.assertIn('return await self._post("/decision/reconcile-assignments"', watchdog_client)
        self.assertIn('return await self._post("/decision/resolve-domain"', watchdog_client)
        self.assertIn('payload["source_ip"] = source_ip', watchdog_client)
        self.assertIn('return await self.resolve_domain_decision(domain, chat_id=chat_id, source_ip=source_ip)', watchdog_client)
        self.assertIn('@app.post("/decision/explain-domain")', (ROOT / "home" / "watchdog" / "watchdog.py").read_text(encoding="utf-8"))
        self.assertIn('@app.post("/decision/resolve-domain")', (ROOT / "home" / "watchdog" / "watchdog.py").read_text(encoding="utf-8"))
        self.assertIn('@app.get("/decision/backends")', (ROOT / "home" / "watchdog" / "watchdog.py").read_text(encoding="utf-8"))
        self.assertIn('@app.get("/decision/status")', (ROOT / "home" / "watchdog" / "watchdog.py").read_text(encoding="utf-8"))
        self.assertIn('@app.get("/decision/backend-paths")', (ROOT / "home" / "watchdog" / "watchdog.py").read_text(encoding="utf-8"))
        self.assertIn('@app.post("/decision/choose-backend")', (ROOT / "home" / "watchdog" / "watchdog.py").read_text(encoding="utf-8"))
        self.assertIn('@app.post("/decision/apply-backend")', (ROOT / "home" / "watchdog" / "watchdog.py").read_text(encoding="utf-8"))

    def test_database_exposes_device_owner_join_for_admin_recovery(self):
        database_source = (ROOT / "home" / "telegram-bot" / "database.py").read_text(encoding="utf-8")
        admin_handler = (ROOT / "home" / "telegram-bot" / "handlers" / "admin.py").read_text(encoding="utf-8")
        keyboards = (ROOT / "home" / "telegram-bot" / "handlers" / "keyboards.py").read_text(encoding="utf-8")
        self.assertIn("async def get_device_with_client", database_source)
        self.assertIn("JOIN clients c ON c.id = d.client_id", database_source)
        self.assertIn("c.chat_id", database_source)
        self.assertIn('@app.post("/decision/reassign")', (ROOT / "home" / "watchdog" / "watchdog.py").read_text(encoding="utf-8"))
        self.assertIn('@app.post("/decision/reconcile-assignments")', (ROOT / "home" / "watchdog" / "watchdog.py").read_text(encoding="utf-8"))
        self.assertIn('@app.get("/gateway/lan-clients")', (ROOT / "home" / "watchdog" / "watchdog.py").read_text(encoding="utf-8"))
        self.assertIn('@app.get("/gateway/lan-prefs")', (ROOT / "home" / "watchdog" / "watchdog.py").read_text(encoding="utf-8"))
        self.assertIn('@app.post("/gateway/lan-client/upsert")', (ROOT / "home" / "watchdog" / "watchdog.py").read_text(encoding="utf-8"))
        self.assertIn('@app.post("/gateway/lan-pref/add")', (ROOT / "home" / "watchdog" / "watchdog.py").read_text(encoding="utf-8"))
        self.assertIn('SELECT id, chat_id, match_type, match_value, backend_id, enabled, created_at, updated_at', (ROOT / "home" / "watchdog" / "watchdog.py").read_text(encoding="utf-8"))
        self.assertTrue((ROOT / "home" / "watchdog" / "decision_maker.py").exists())
        self.assertIn('/run/vpn-active-backend.env', (ROOT / "home" / "scripts" / "tier2-connect.sh").read_text(encoding="utf-8"))
        self.assertIn('EnvironmentFile=-/run/vpn-active-backend.env', (ROOT / "home" / "watchdog" / "watchdog.py").read_text(encoding="utf-8"))
        self.assertNotIn("Отчёт придёт по завершении.", admin_handler)

    def test_traffic_stats_contract_is_exposed_in_bot_and_db(self):
        database_source = (ROOT / "home" / "telegram-bot" / "database.py").read_text(encoding="utf-8")
        admin_handler = (ROOT / "home" / "telegram-bot" / "handlers" / "admin.py").read_text(encoding="utf-8")
        keyboards = (ROOT / "home" / "telegram-bot" / "handlers" / "keyboards.py").read_text(encoding="utf-8")
        bot_source = (ROOT / "home" / "telegram-bot" / "bot.py").read_text(encoding="utf-8")

        self.assertIn("CREATE TABLE IF NOT EXISTS device_traffic_state", database_source)
        self.assertIn("CREATE TABLE IF NOT EXISTS device_traffic_samples", database_source)
        self.assertIn("async def record_traffic_snapshot", database_source)
        self.assertIn("async def get_client_traffic_totals", database_source)
        self.assertIn('callback_data="adm:stats_totals"', keyboards)
        self.assertIn("def admin_traffic_menu", keyboards)
        self.assertIn('@router.callback_query(F.data == "adm:stats_totals")', admin_handler)
        self.assertIn("traffic-stats-loop", bot_source)
        self.assertIn('"trojan"', admin_handler)
        self.assertIn('"tuic"', admin_handler)
        self.assertIn("Path status:", admin_handler)
        self.assertIn("Runtime active:", admin_handler)
        self.assertIn("Assignment leases</b>:", admin_handler)
        self.assertIn("backend_path_status", admin_handler)
        self.assertIn('("🔐 Trojan (exp)", "trojan")', keyboards)
        self.assertIn('("🚀 TUIC (exp)", "tuic")', keyboards)
        self.assertIn("experimental: true", (ROOT / "home" / "watchdog" / "plugins" / "trojan" / "metadata.yaml").read_text(encoding="utf-8"))
        self.assertIn("auto_enabled: false", (ROOT / "home" / "watchdog" / "plugins" / "trojan" / "metadata.yaml").read_text(encoding="utf-8"))
        self.assertIn("experimental: true", (ROOT / "home" / "watchdog" / "plugins" / "tuic" / "metadata.yaml").read_text(encoding="utf-8"))
        self.assertIn("auto_enabled: false", (ROOT / "home" / "watchdog" / "plugins" / "tuic" / "metadata.yaml").read_text(encoding="utf-8"))
        dm_source = (ROOT / "home" / "watchdog" / "decision_maker.py").read_text(encoding="utf-8")
        self.assertIn("matched_preference", dm_source)
        self.assertIn("lan_client_pref", dm_source)
        self.assertIn("ignored_by_policy", dm_source)
        self.assertIn("single_active_backend", dm_source)
        self.assertIn("reconcile_assignments_to_active_backend", dm_source)
        self.assertIn("preference_status", dm_source)
        self.assertIn("preference_reason", dm_source)
        self.assertTrue((ROOT / "home" / "systemd" / "tier2-connect.service").exists())
        install_home = (ROOT / "install-home.sh").read_text(encoding="utf-8")
        self.assertIn("tier2-connect.service", install_home)
        self.assertIn("systemctl enable tier2-connect", install_home)
        self.assertIn("desired_backend_path_family", dm_source)
        self.assertIn("build_backend_path_entry", dm_source)
        self.assertIn("build_backend_path_runtime_record", dm_source)
        self.assertIn("build_backend_path_verify_record", dm_source)
        self.assertIn("build_backend_path_status", dm_source)
        self.assertIn("build_backend_paths_view", dm_source)
        self.assertIn("build_assignments_view", dm_source)
        self.assertIn("build_decision_status_view", dm_source)
        self.assertIn("build_backends_view", dm_source)
        self.assertIn('"reconciled"', dm_source)
        self.assertIn('"backend_paths"', dm_source)
        self.assertIn('"execution_family"', dm_source)
        watchdog_source = (ROOT / "home" / "watchdog" / "watchdog.py").read_text(encoding="utf-8")
        self.assertIn("_backend_path_snapshot", watchdog_source)
        self.assertIn("_build_hysteria2_config", watchdog_source)
        self.assertIn("_verify_hysteria2_backend_path", watchdog_source)
        self.assertIn("_refresh_tier2_health", watchdog_source)
        self.assertIn("vpn_tier2_service_active", watchdog_source)
        self.assertIn('"tier2_health"', watchdog_source)
        self.assertIn('"decision"', dm_source)
        self.assertIn('"runtime"', dm_source)

    def test_bot_reads_systemd_logs_via_watchdog_api(self):
        admin_handler = (ROOT / "home" / "telegram-bot" / "handlers" / "admin.py").read_text(encoding="utf-8")
        watchdog_client = (ROOT / "home" / "telegram-bot" / "services" / "watchdog_client.py").read_text(encoding="utf-8")
        system_service = (ROOT / "home" / "telegram-bot" / "services" / "system.py").read_text(encoding="utf-8")
        watchdog_source = (ROOT / "home" / "watchdog" / "watchdog.py").read_text(encoding="utf-8")

        self.assertIn('@app.get("/logs/systemd")', watchdog_source)
        self.assertIn("SYSTEMD_LOG_ALLOWED_UNITS", watchdog_source)
        self.assertIn("async def get_systemd_logs", watchdog_client)
        self.assertIn('/logs/systemd?', watchdog_client)
        self.assertIn("await _wc().get_systemd_logs", admin_handler)
        self.assertNotIn('["journalctl", "-u", service', admin_handler)
        self.assertIn("get_systemd_logs(service, lines)", system_service)

    def test_graph_panel_contract_matches_dashboard_json(self):
        watchdog_source = (ROOT / "home" / "watchdog" / "watchdog.py").read_text(encoding="utf-8")
        provisioning = (ROOT / "home" / "grafana" / "provisioning" / "dashboards" / "dashboard.yml").read_text(encoding="utf-8")
        tunnel = json.loads((ROOT / "home" / "grafana" / "dashboards" / "vpn-tunnel.json").read_text(encoding="utf-8"))
        clients = json.loads((ROOT / "home" / "grafana" / "dashboards" / "vpn-clients.json").read_text(encoding="utf-8"))
        system = json.loads((ROOT / "home" / "grafana" / "dashboards" / "vpn-system.json").read_text(encoding="utf-8"))

        self.assertIn('"tunnel"', watchdog_source)
        self.assertIn('"dashboard_uid": "vpn-tunnel"', watchdog_source)
        self.assertIn('"panel_id": 1', watchdog_source)
        self.assertIn('"clients"', watchdog_source)
        self.assertIn('"dashboard_uid": "vpn-clients"', watchdog_source)
        self.assertIn('"panel_id": 10', watchdog_source)
        self.assertIn('"system"', watchdog_source)
        self.assertIn('"dashboard_uid": "vpn-system"', watchdog_source)
        self.assertIn('#   vpn-system   : /graph system  → CPU история', provisioning)
        self.assertEqual(tunnel.get("uid"), "vpn-tunnel")
        self.assertEqual(clients.get("uid"), "vpn-clients")
        self.assertEqual(system.get("uid"), "vpn-system")
        self.assertIn('"id": 1', (ROOT / "home" / "grafana" / "dashboards" / "vpn-tunnel.json").read_text(encoding="utf-8"))
        self.assertIn('"id": 10', (ROOT / "home" / "grafana" / "dashboards" / "vpn-clients.json").read_text(encoding="utf-8"))
        self.assertIn('"id": 10', (ROOT / "home" / "grafana" / "dashboards" / "vpn-system.json").read_text(encoding="utf-8"))
        post_install = (ROOT / "home" / "scripts" / "post-install-check.sh").read_text(encoding="utf-8")
        self.assertIn("tier-2 iperf3 endpoint", post_install)
        self.assertIn("_reconcile_backend_path_runtime_state", watchdog_source)
        self.assertIn("self.desired_backend_path", watchdog_source)
        self.assertIn("self.applied_backend_path", watchdog_source)
        self.assertIn("self.backend_path_health", watchdog_source)
        self.assertIn('"status": "verification_failed"', watchdog_source)
        self.assertIn('"status": "rollback_completed"', watchdog_source)
        self.assertIn('rsync -a "$REPO_DIR/home/watchdog/decision_maker.py" "$REPO_DIR/watchdog/decision_maker.py"', (ROOT / "deploy.sh").read_text(encoding="utf-8"))
        self.assertIn('rsync -a "${REPO_DIR}/home/watchdog/decision_maker.py" /opt/vpn/watchdog/decision_maker.py', (ROOT / "install-home.sh").read_text(encoding="utf-8"))
        docker_smoke = (ROOT / "tests" / "smoke" / "test_docker.sh").read_text(encoding="utf-8")
        self.assertIn("OPTIONAL_RESIDUAL_CONTAINERS", docker_smoke)
        self.assertIn("Optional stale containers in restart loop", docker_smoke)
        post_install = (ROOT / "home" / "scripts" / "post-install-check.sh").read_text(encoding="utf-8")
        self.assertIn("docker optional: $cname", post_install)
        self.assertIn("VPS optional docker: $cname", post_install)
        self.assertIn("VPS nftables 8444/tcp", post_install)
        self.assertIn("VPS nftables 8448/udp", post_install)
        dnsmasq_conf = (ROOT / "home" / "dnsmasq" / "dnsmasq.conf").read_text(encoding="utf-8")
        self.assertIn("listen-address=127.0.0.1,10.177.1.1,10.177.3.1,172.21.0.1", dnsmasq_conf)
        dnsmasq_dropin = (ROOT / "home" / "systemd" / "dnsmasq.service.d" / "restart-on-failure.conf").read_text(encoding="utf-8")
        self.assertIn("[Unit]", dnsmasq_dropin)
        self.assertIn("StartLimitIntervalSec=60", dnsmasq_dropin)
        self.assertIn("StartLimitBurst=5", dnsmasq_dropin)
        self.assertIn("Restart=on-failure", dnsmasq_dropin)
        self.assertIn("RestartSec=5", dnsmasq_dropin)
        vpn_routes_unit = (ROOT / "home" / "systemd" / "vpn-routes.service").read_text(encoding="utf-8")
        self.assertIn("Before=watchdog.service docker.service", vpn_routes_unit)
        self.assertNotIn("dnsmasq.service", vpn_routes_unit.split("Before=", 1)[1].splitlines()[0])
        installer = (ROOT / "install-home.sh").read_text(encoding="utf-8")
        self.assertIn("write_ipv6_disable_sysctl", installer)
        self.assertIn("fix_missing_pam_lastlog", installer)
        self.assertIn("IGNORE_RESOLVCONF=yes", installer)
        self.assertIn('DNSMASQ_EXCEPT="lo"', installer)
        self.assertNotIn('"registry-mirrors"', installer)
        self.assertIn("mkdir -p /etc/systemd/system/dnsmasq.service.d", installer)
        self.assertIn("dnsmasq.service.d/restart-on-failure.conf", installer)
        self.assertIn("systemctl daemon-reload", installer)
        self.assertIn("systemctl mask systemd-resolved", installer)
        self.assertIn("0 3 * * * root flock -n /var/run/vpn-routes.lock python3 /opt/vpn/scripts/update-routes.py >> /var/log/vpn-routes.log 2>&1", installer)
        self.assertIn("@reboot root sleep 60 && bash /opt/vpn/scripts/dns-warmup.sh >> /var/log/vpn-dns-warmup.log 2>&1", installer)
        self.assertIn("dnsmasq systemd drop-in valid", post_install)
        self.assertIn("dnsmasq ignores resolvconf upstream", post_install)
        self.assertIn("PAM lastlog journal noise", post_install)
        self.assertIn("docker signature validation journal noise", post_install)
        self.assertIn('["systemctl", "is-active", "ssh.socket"]', watchdog_source)
        self.assertIn("ACTIVE_STACK_RUNTIME_PROBE_TARGETS", watchdog_source)
        self.assertIn("_run_active_stack_runtime_probes", watchdog_source)
        self.assertIn("_active_stack_runtime_failover_reason", watchdog_source)
        vpn_policy = (ROOT / "home" / "scripts" / "vpn-policy-routing.sh").read_text(encoding="utf-8")
        self.assertIn('ip route replace "$FUNCTIONAL_NS_SUBNET" dev br-fh table $TABLE_VPN', vpn_policy)
        self.assertIn('Table $TABLE_VPN: $FUNCTIONAL_NS_SUBNET dev br-fh (functional namespaces)', vpn_policy)
        self.assertIn('systemctl disable --now autossh-tier2', installer)
        self.assertIn('rm -f /etc/systemd/system/autossh-tier2.service', installer)
        self.assertIn('log_ok "Удалён устаревший autossh-tier2.service"', installer)
        self.assertNotIn('HAS_AUTOSSH_TIER2', post_install)
        self.assertNotIn('check_warn "autossh-tier2"', post_install)

    def test_sing_box_extra_client_templates_do_not_use_removed_legacy_inbound_fields(self):
        tuic_client = (ROOT / "home" / "sing-box" / "tuic-client.json").read_text(encoding="utf-8")
        trojan_client = (ROOT / "home" / "sing-box" / "trojan-client.json").read_text(encoding="utf-8")
        self.assertNotIn('"sniff"', tuic_client)
        self.assertNotIn('"sniff"', trojan_client)
        self.assertIn('"type": "socks"', tuic_client)
        self.assertIn('"type": "socks"', trojan_client)

    def test_deploy_status_reads_explicit_release_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, state_dir, snapshot_dir, _ = self.make_repo_with_state(Path(tmp))
            (snapshot_dir / "latest").write_text("20260403_130000\n", encoding="utf-8")
            (state_dir / "current.json").write_text(
                json.dumps(
                    {
                        "current_release": {"id": "abc123def456", "sha": "abc123def4567890", "version": "v0.test"},
                        "previous_release": {"id": "prev123456789", "sha": "prev1234567890", "version": "v0.prev"},
                        "status": "ready",
                        "message": "release applied",
                    }
                ),
                encoding="utf-8",
            )
            (state_dir / "last-attempt.json").write_text(
                json.dumps({"status": "success", "phase": "commit", "message": "release abc123def456 applied"}),
                encoding="utf-8",
            )
            env = {
                "ALLOW_NON_ROOT": "1",
                "REPO_DIR": str(repo),
                "STATE_DIR": str(state_dir),
                "SNAPSHOT_DIR": str(snapshot_dir),
                "LOG_FILE": str(Path(tmp) / "deploy.log"),
                "LOCK_FILE": str(Path(tmp) / "deploy.lock"),
            }
            result = self.run_cmd(["bash", str(DEPLOY), "--status"], env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Current release:  abc123def456 (v0.test)", result.stdout)
            self.assertIn("Previous release: prev123456789 (v0.prev)", result.stdout)
            self.assertIn("Latest snapshot:  20260403_130000", result.stdout)

    def test_successful_mock_deploy_commits_release_and_syncs_vps_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, state_dir, snapshot_dir, vps_state_dir = self.make_repo_with_state(Path(tmp))
            env = self.deploy_env(
                tmp,
                repo,
                state_dir,
                snapshot_dir,
                vps_state_dir,
                MOCK_TARGET_RELEASE_SHA="fedcba9876543210fedcba9876543210fedcba98",
                MOCK_TARGET_RELEASE_VERSION="v1.2.3",
            )
            result = self.run_cmd(["bash", str(DEPLOY)], env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            current = json.loads((state_dir / "current.json").read_text(encoding="utf-8"))
            last_attempt = json.loads((state_dir / "last-attempt.json").read_text(encoding="utf-8"))
            remote_current = json.loads((vps_state_dir / "current.json").read_text(encoding="utf-8"))

            self.assertEqual(current["current_release"]["id"], "fedcba987654")
            self.assertEqual(current["current_release"]["version"], "v1.2.3")
            self.assertEqual(current["previous_release"]["id"], "abc123def456")
            self.assertFalse((state_dir / "pending.json").exists())
            self.assertEqual(last_attempt["status"], "success")
            self.assertEqual(last_attempt["phase"], "commit")
            self.assertEqual(remote_current["current_release"]["id"], "fedcba987654")
            self.assertFalse((vps_state_dir / "pending.json").exists())

    def test_verify_failure_triggers_auto_rollback_to_previous_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, state_dir, snapshot_dir, vps_state_dir = self.make_repo_with_state(Path(tmp))
            env = self.deploy_env(
                tmp,
                repo,
                state_dir,
                snapshot_dir,
                vps_state_dir,
                MOCK_TARGET_RELEASE_SHA="fedcba9876543210fedcba9876543210fedcba98",
                MOCK_TARGET_RELEASE_VERSION="v1.2.3",
                DEPLOY_FAIL_PHASES="verify",
            )
            result = self.run_cmd(["bash", str(DEPLOY)], env=env)
            self.assertNotEqual(result.returncode, 0)

            current = json.loads((state_dir / "current.json").read_text(encoding="utf-8"))
            last_attempt = json.loads((state_dir / "last-attempt.json").read_text(encoding="utf-8"))
            remote_current = json.loads((vps_state_dir / "current.json").read_text(encoding="utf-8"))

            self.assertEqual(current["current_release"]["id"], "abc123def456")
            self.assertEqual(current["message"], "rollback completed")
            self.assertFalse((state_dir / "pending.json").exists())
            self.assertEqual(last_attempt["status"], "rollback-completed")
            self.assertEqual(remote_current["current_release"]["id"], "abc123def456")

    def test_rollback_failure_leaves_failed_pending_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, state_dir, snapshot_dir, vps_state_dir = self.make_repo_with_state(Path(tmp))
            env = self.deploy_env(
                tmp,
                repo,
                state_dir,
                snapshot_dir,
                vps_state_dir,
                MOCK_TARGET_RELEASE_SHA="fedcba9876543210fedcba9876543210fedcba98",
                MOCK_TARGET_RELEASE_VERSION="v1.2.3",
                DEPLOY_FAIL_PHASES="verify,rollback-verify",
            )
            result = self.run_cmd(["bash", str(DEPLOY)], env=env)
            self.assertNotEqual(result.returncode, 0)

            pending = json.loads((state_dir / "pending.json").read_text(encoding="utf-8"))
            last_attempt = json.loads((state_dir / "last-attempt.json").read_text(encoding="utf-8"))
            current = json.loads((state_dir / "current.json").read_text(encoding="utf-8"))

            self.assertEqual(pending["phase"], "rollback")
            self.assertEqual(pending["status"], "failed")
            self.assertEqual(last_attempt["status"], "rollback-failed")
            self.assertEqual(current["current_release"]["id"], "abc123def456")

    def test_manual_rollback_rejects_selector_arguments(self):
        result = self.run_cmd(["bash", str(DEPLOY), "--rollback", "--to", "abc"], env={"ALLOW_NON_ROOT": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Использование", result.stdout)

    def test_manual_rollback_rejects_explicit_ref(self):
        result = self.run_cmd(["bash", str(DEPLOY), "--rollback", "--ref", "v0.3.3.222"], env={"ALLOW_NON_ROOT": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Использование", result.stdout)


if __name__ == "__main__":
    unittest.main()
