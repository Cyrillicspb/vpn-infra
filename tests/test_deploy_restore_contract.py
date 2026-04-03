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

    def test_deploy_script_guards_tg_send_and_uses_sudo_for_vps_apply(self):
        deploy_script = DEPLOY.read_text(encoding="utf-8")
        self.assertIn('if [[ ! -x "$tg_send" ]]; then', deploy_script)
        self.assertIn('sudo -n bash -lc', deploy_script)
        self.assertNotIn('vps_tmux_exec "$cmd" 300 >/dev/null', deploy_script)

    def test_vps_cloudflared_is_not_in_default_compose_startup(self):
        vps_compose = (ROOT / "vps" / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn('profiles: ["manual"]', vps_compose)
        self.assertNotIn('entrypoint: ["/bin/sh"]', vps_compose)

    def test_admin_bot_texts_match_deploy_status_contract(self):
        admin_handler = (ROOT / "home" / "telegram-bot" / "handlers" / "admin.py").read_text(encoding="utf-8")
        self.assertIn("Deploy запущен. Прогресс и итог доступны через /status.", admin_handler)
        self.assertIn("Rollback запущен. Прогресс и итог доступны через /status.", admin_handler)
        self.assertIn("Запустить rollback к последнему подтвержденному snapshot?", admin_handler)
        self.assertIn("version в callback используется только для UX и skip-version, не как аргумент deploy.", admin_handler)
        self.assertNotIn("Отчёт придёт по завершении.", admin_handler)

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


if __name__ == "__main__":
    unittest.main()
