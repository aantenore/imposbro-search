from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


class EnterpriseAssuranceWorkflowTest(unittest.TestCase):
    def test_kind_installer_cannot_dirty_release_evidence_checkout(self) -> None:
        workflow = (ROOT / ".github/workflows/enterprise-assurance.yml").read_text()
        install_step = workflow.split(
            "- name: Install checksum-verified kind", 1
        )[1].split(
            "- name: Execute fail-closed multi-node and benchmark harness", 1
        )[0]

        self.assertIn(
            'kind_binary="${RUNNER_TEMP}/kind-linux-amd64"', install_step
        )
        self.assertIn('--output "$kind_binary"', install_step)
        self.assertIn("sha256sum --check --strict", install_step)
        self.assertIn(
            'install --mode 0755 "$kind_binary" /usr/local/bin/kind', install_step
        )
        self.assertNotIn("--output kind-linux-amd64", install_step)


if __name__ == "__main__":
    unittest.main()
