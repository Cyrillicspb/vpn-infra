import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleaseWorkflowContractTests(unittest.TestCase):
    def test_create_release_is_tag_first_and_does_not_push_master(self):
        content = (ROOT / ".github" / "workflows" / "auto-release.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", content)
        self.assertNotIn("push:\n", content)
        self.assertNotIn("branches: [master]", content)
        self.assertNotIn("git push origin HEAD:master", content)
        self.assertIn('EXACT_TAG="$(release_tag_for_ref "$TARGET_SHA")"', content)
        self.assertIn('git tag -a "$TAG" "$TARGET_SHA"', content)
        self.assertIn('git push origin "$TAG"', content)
        self.assertIn('if gh release view "$TAG"', content)
        self.assertIn("release-version.sh", content)
        self.assertIn("release-external-versions.sh", content)
        self.assertNotIn("api.github.com/repos/apernet/hysteria/releases/latest", content)
        self.assertNotIn("api.github.com/repos/xjasonlyu/tun2socks/releases/latest", content)
        self.assertIn("install.sh", content)

    def test_rebuild_release_uses_pinned_external_versions_and_uploads_install_script(self):
        content = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", content)
        self.assertIn("release-version.sh", content)
        self.assertIn("release-external-versions.sh", content)
        self.assertNotIn("releases/latest", content)
        self.assertIn("install.sh", content)
        self.assertIn('gh release download "${{ inputs.tag }}"', content)
        self.assertIn('grep -Fqx "release_tag=${{ inputs.tag }}"', content)
        self.assertIn('grep -Fqx "commit_sha=$(git rev-parse HEAD)"', content)

    def test_install_docs_use_release_assets_not_raw_master(self):
        content = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("releases/latest/download/install.sh", content)
        self.assertIn("releases/download/vX.Y.Z/install.sh", content)
        self.assertNotIn("raw.githubusercontent.com/Cyrillicspb/vpn-infra/master/install.sh", content)

    def test_release_helpers_have_no_syntax_errors(self):
        result = subprocess.run(
            [
                "bash",
                "-n",
                "scripts/release-version.sh",
                "scripts/release-external-versions.sh",
                "scripts/build-release-assets-manifest.sh",
                "install.sh",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
