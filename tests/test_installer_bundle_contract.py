import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InstallerBundleContractTests(unittest.TestCase):
    def test_docker_groups_include_sing_box(self):
        content = (ROOT / "scripts" / "docker-image-groups.sh").read_text(encoding="utf-8")
        self.assertIn("ghcr.io/sagernet/sing-box:latest", content)

    def test_python_wheel_groups_cover_required_components(self):
        content = (ROOT / "scripts" / "python-wheel-groups.sh").read_text(encoding="utf-8")
        self.assertIn("installer-gui", content)
        self.assertIn("watchdog", content)
        self.assertIn("telegram-bot", content)
        self.assertIn("installer-gui-wheels.tar.gz", content)
        self.assertIn("watchdog-wheels.tar.gz", content)
        self.assertIn("telegram-bot-wheels.tar.gz", content)

    def test_release_asset_script_lists_core_manifests(self):
        content = (ROOT / "scripts" / "release-bundle-assets.sh").read_text(encoding="utf-8")
        self.assertIn('echo "install.sh"', content)
        self.assertIn("release-assets-manifest.txt", content)
        self.assertIn("docker-bundles-manifest.txt", content)
        self.assertIn("system-packages-manifest.txt", content)
        self.assertIn("python-wheel-bundles-manifest.txt", content)

    def test_install_sh_enforces_strict_bundle_mode_and_python_wheels(self):
        content = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("export VPN_STRICT_BUNDLE=1", content)
        self.assertIn('require_release_asset_url "$_release_json" "python-wheel-bundles-manifest.txt"', content)
        self.assertIn('RELEASE_API="https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/releases/tags/${RELEASE_TAG}"', content)
        self.assertIn('python_wheel_bundle_groups', content)
        self.assertIn('VPN_STRICT_BUNDLE=1 bash "${OPT_VPN}/setup.sh"', content)
        self.assertNotIn('read_install_version "${OPT_VPN}/version"', content)
        self.assertNotIn("for pkg in curl git; do", content)
        self.assertNotIn('git clone --depth=1 "$REPO_URL"', content)

    def test_release_assets_manifest_embeds_release_metadata(self):
        content = (ROOT / "scripts" / "build-release-assets-manifest.sh").read_text(encoding="utf-8")
        self.assertIn('echo "release_tag=${RELEASE_TAG:-unknown}"', content)
        self.assertIn('echo "commit_sha=${RELEASE_COMMIT_SHA:-unknown}"', content)
        self.assertIn('echo "builder_digest=', content)

    def test_setup_sh_blocks_bootstrap_network_fallback_in_strict_mode(self):
        content = (ROOT / "setup.sh").read_text(encoding="utf-8")
        self.assertIn('strict_bundle_bootstrap_enabled()', content)
        self.assertNotIn("raw.githubusercontent.com/Cyrillicspb/vpn-infra/master", content)
        self.assertNotIn("archive/refs/heads/master.tar.gz", content)
        self.assertIn('clean install должен запускаться только из полного release bundle', content)
        self.assertIn('installer GUI отсутствует локально — запускаем консольный установщик', content)

    def test_setup_sh_uses_duckdns_only_ddns_prompt(self):
        content = (ROOT / "setup.sh").read_text(encoding="utf-8")
        self.assertNotIn("duckdns/noip/cloudflare", content)
        self.assertIn('DDNS_PROVIDER="duckdns"', content)
        self.assertIn("DuckDNS домен для клиентского Endpoint / home ingress", content)
        self.assertIn("DuckDNS token", content)

    def test_setup_sh_defers_vps_ssh_hardening_until_successful_commit(self):
        content = (ROOT / "setup.sh").read_text(encoding="utf-8")
        self.assertIn('VPS_BOOTSTRAP_STATE_FILE="/opt/vpn/.vps-ssh-bootstrap-state"', content)
        self.assertIn("rollback_vps_ssh_prepare()", content)
        self.assertIn("commit_vps_ssh_bootstrap()", content)
        self.assertIn("VPS подготовлен: sysadmin создан, SSH hardening будет выполнен после успешной установки", content)
        self.assertIn('env_set "VPS_ROOT_PASSWORD" ""', content)
        self.assertIn("commit_vps_ssh_bootstrap", content)

    def test_add_vps_closes_root_ssh_only_after_successful_run(self):
        content = (ROOT / "add-vps.sh").read_text(encoding="utf-8")
        self.assertIn("Финализация SSH-доступа на VPS2", content)
        self.assertNotIn('log_info "Закрытие root SSH-доступа на VPS2..."', content)

    def test_common_sh_disables_ansi_when_stdout_is_not_a_tty(self):
        content = (ROOT / "common.sh").read_text(encoding="utf-8")
        self.assertIn('[[ -t 1 && "${TERM:-}" != "dumb" ]]', content)
        self.assertIn("ui_step_result()", content)
        self.assertIn("run_with_compact_progress()", content)
        self.assertIn('log_info "${label} · выполняется · ${elapsed_text}"', content)

    def test_install_home_requires_bundled_transport_binaries(self):
        content = (ROOT / "install-home.sh").read_text(encoding="utf-8")
        self.assertIn('tools/hysteria2-linux-${_ARCH}', content)
        self.assertIn('tools/tun2socks-linux-${_ARCH}', content)
        self.assertNotIn("api.github.com/repos/apernet/hysteria/releases/latest", content)
        self.assertNotIn("api.github.com/repos/xjasonlyu/tun2socks/releases/latest", content)
        self.assertIn("Clean install должен использовать полный release bundle", content)

    def test_watchdog_reload_and_logrotate_do_not_signal_child_processes(self):
        install_home = (ROOT / "install-home.sh").read_text(encoding="utf-8")
        source_unit = (ROOT / "home" / "systemd" / "watchdog.service").read_text(encoding="utf-8")
        migration = (ROOT / "migrations" / "20240201_003_logrotate_update.sh").read_text(encoding="utf-8")

        self.assertIn("ExecReload=/bin/kill -HUP $MAINPID", install_home)
        self.assertIn("ExecReload=/bin/kill -HUP $MAINPID", source_unit)
        self.assertIn("KillMode=control-group", install_home)
        self.assertIn("KillMode=control-group", source_unit)

        self.assertIn("/var/log/vpn-[!wb]*.log {", install_home)
        self.assertIn("/var/log/vpn-watchdog.log {", install_home)
        self.assertIn("/var/log/vpn-backup.log {", install_home)
        self.assertIn("su root adm", install_home)
        self.assertNotIn("/var/log/vpn-*.log {", install_home)
        self.assertIn("systemctl reload watchdog", install_home)
        self.assertNotIn("systemctl kill -s HUP watchdog.service", install_home)

        self.assertIn("/var/log/vpn-[!wb]*.log {", migration)
        self.assertIn("su root adm", migration)
        self.assertIn("systemctl reload watchdog", migration)
        self.assertNotIn("systemctl kill -s HUP watchdog.service", migration)

    def test_zapret_install_is_bundled_only(self):
        content = (ROOT / "home" / "watchdog" / "plugins" / "zapret" / "install.sh").read_text(encoding="utf-8")
        self.assertIn("Clean install должен использовать bundled binary", content)
        self.assertNotIn("api.github.com/repos/bol-van/zapret/releases/latest", content)
        self.assertNotIn("archive/refs/heads/master.tar.gz", content)
        self.assertNotIn("git clone --depth=1 \"https://github.com/bol-van/zapret.git\"", content)

    def test_docs_state_tui_is_primary_install_mode(self):
        content = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("основной режим установки: `TUI`", content)
        self.assertIn("консольный режим: только fallback", content)
        self.assertNotIn("GUI installer как основной путь", content)

    def test_install_contract_documents_duckdns_only(self):
        docs = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("installer поддерживает DDNS только через `DuckDNS`", docs)
        self.assertIn("Cloudflare CDN` и `DDNS` не смешиваются", docs)
        self.assertIn("DDNS_PROVIDER=          # duckdns only", env_example)
        self.assertNotIn("duckdns | noip | cloudflare", env_example)

    def test_welcome_screen_is_keyboard_driven_and_small_terminal_safe(self):
        content = (ROOT / "installers" / "gui" / "screens" / "welcome.py").read_text(encoding="utf-8")
        self.assertIn('Binding("enter", "start"', content)
        self.assertIn('Binding("space", "start"', content)
        self.assertIn('self.query_one("#btn-start", Button).focus()', content)
        self.assertIn("ScrollableContainer", content)
        self.assertIn("max-height: 20;", content)

    def test_shell_installer_uses_consistent_compact_output_contract(self):
        install_sh = (ROOT / "install.sh").read_text(encoding="utf-8")
        install_home = (ROOT / "install-home.sh").read_text(encoding="utf-8")
        install_vps = (ROOT / "install-vps.sh").read_text(encoding="utf-8")
        self.assertIn('info()  { echo -e "${CYAN}[INFO]${NC} $*"; }', install_sh)
        self.assertIn("download_with_progress()", install_sh)
        self.assertNotIn("--progress-bar", install_sh)
        self.assertIn('run_with_compact_progress "Сборка telegram-bot', install_home)
        self.assertNotIn("| tee /tmp/docker-build.log", install_home)
        self.assertNotIn("| tee /tmp/docker-up.log", install_home)
        self.assertIn("vps_copy_progress()", install_vps)
        self.assertIn('log_info "${label} · этап: ${remote_diag}"', install_vps)

    def test_claude_specific_artifacts_are_removed(self):
        self.assertFalse((ROOT / "CLAUDE.md").exists())
        self.assertFalse((ROOT / "home" / "scripts" / "install-claude-code.sh").exists())
        scan = subprocess.run(
            ["rg", "-n", "--glob", "!tests/**", "CLAUDE\\.md|INSTALL_CLAUDE_CODE|install-claude-code", "."],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(scan.returncode, 1, scan.stdout + scan.stderr)

    def test_install_scripts_have_no_syntax_errors(self):
        result = subprocess.run(
            ["bash", "-n", "install.sh", "setup.sh", "install-home.sh", "install-vps.sh", "installers/bootstrap.sh"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
