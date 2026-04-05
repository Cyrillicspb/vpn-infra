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
        self.assertIn('strict bundle mode: отсутствует обязательный локальный файл', content)
        self.assertIn('strict bundle mode: installer GUI отсутствует локально', content)

    def test_install_scripts_have_no_syntax_errors(self):
        result = subprocess.run(
            ["bash", "-n", "install.sh", "setup.sh", "install-home.sh", "install-vps.sh"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
