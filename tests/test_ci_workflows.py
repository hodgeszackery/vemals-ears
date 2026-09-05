"""Standalone VE CI retains Windows, real PCM coverage, and signed-release trust."""
from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    return yaml.load((ROOT / ".github/workflows" / name).read_text(encoding="ascii"),
                     Loader=yaml.BaseLoader)


class WorkflowContracts(unittest.TestCase):
    def test_complete_ci_runs_on_every_change(self):
        data = workflow("tests.yml")
        self.assertEqual({"pull_request", "push", "merge_group", "workflow_dispatch"}, set(data["on"]))
        self.assertEqual(["master"], data["on"]["push"]["branches"])
        self.assertNotIn("paths", data["on"]["push"])
        self.assertEqual({"contents": "read"}, data["permissions"])
        self.assertEqual({"validate", "python-and-node", "ci"}, set(data["jobs"]))

    def test_only_superseded_pr_runs_cancel(self):
        data = workflow("tests.yml")["concurrency"]
        self.assertIn("github.event.pull_request.number || github.run_id", data["group"])
        self.assertEqual("${{ github.event_name == 'pull_request' }}", data["cancel-in-progress"])

    def test_aggregate_gate_requires_both_jobs_even_after_failure(self):
        job = workflow("tests.yml")["jobs"]["ci"]
        self.assertEqual("CI", job["name"])
        self.assertEqual("always()", job["if"])
        self.assertEqual({"validate", "python-and-node"}, set(job["needs"]))
        self.assertIn("ci.check_results", job["steps"][-1]["run"])

    def test_native_tk_and_real_node_pcm_share_windows_job(self):
        job = workflow("tests.yml")["jobs"]["python-and-node"]
        self.assertTrue(job["runs-on"].startswith("windows-"))
        commands = [step.get("run", "") for step in job["steps"]]
        install = next(i for i, command in enumerate(commands) if command.startswith("npm ci"))
        check = next(i for i, command in enumerate(commands) if "ci.check_dependencies" in command)
        tests = next(i for i, command in enumerate(commands) if "run_all_tests.py" in command)
        self.assertLess(install, check)
        self.assertLess(check, tests)
        self.assertIn("--timeout 180", commands[tests])
        self.assertIn("--report .ci-results/python.json", commands[tests])
        node = next(step for step in job["steps"] if step.get("id") == "discord")
        self.assertEqual("node --test", node["run"])
        self.assertNotIn("if", node)
        self.assertEqual("${{ steps.discord.outcome }}", job["outputs"]["discord"])

    def test_linux_validation_includes_the_worker_payload_import(self):
        job = workflow("tests.yml")["jobs"]["validate"]
        self.assertTrue(job["runs-on"].startswith("ubuntu-"))
        commands = "\n".join(step.get("run", "") for step in job["steps"])
        self.assertIn("GuildHub.tests.test_deployment", commands)

    def test_actions_are_immutable_jobs_bounded_and_checkout_read_only(self):
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            for name, job in workflow(path.name)["jobs"].items():
                with self.subTest(workflow=path.name, job=name):
                    self.assertGreater(int(job["timeout-minutes"]), 0)
                    self.assertLessEqual(int(job["timeout-minutes"]), 45)
                for step in job.get("steps", []):
                    if "uses" in step:
                        self.assertRegex(step["uses"], r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
                    if step.get("uses", "").startswith("actions/checkout@"):
                        self.assertEqual("false", step["with"]["persist-credentials"])
                    self.assertNotEqual("true", step.get("continue-on-error"))

    def test_python_and_node_dependencies_stay_locked(self):
        job = workflow("tests.yml")["jobs"]["python-and-node"]
        install = next(step["run"] for step in job["steps"] if "pip install" in step.get("run", ""))
        self.assertIn("--require-hashes", install)
        self.assertIn("--only-binary=:all:", install)
        for step in job["steps"]:
            if step.get("uses", "").startswith(("actions/setup-python@", "actions/setup-node@")):
                self.assertIn(step["with"]["cache"], ("pip", "npm"))
                self.assertTrue((ROOT / step["with"]["cache-dependency-path"]).is_file())

    def test_timing_evidence_survives_test_failure(self):
        steps = workflow("tests.yml")["jobs"]["python-and-node"]["steps"]
        summary = next(step for step in steps if "ci.test_summary" in step.get("run", ""))
        artifact = next(step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@"))
        self.assertEqual("always()", summary["if"])
        self.assertEqual("always()", artifact["if"])
        self.assertEqual(".ci-results/python.json", artifact["with"]["path"])
        self.assertEqual("true", artifact["with"]["include-hidden-files"])
        self.assertLessEqual(int(artifact["with"]["retention-days"]), 14)

    def test_signing_requires_unsigned_validation_and_protected_environment(self):
        signing = workflow("vemals-ears-windows.yml")
        job = signing["jobs"]["build_sign"]
        self.assertEqual("validate", job["needs"])
        self.assertEqual("windows-release", job["environment"])
        self.assertEqual("write", job["permissions"]["id-token"])
        self.assertNotIn("id-token", signing["jobs"]["validate"]["permissions"])


if __name__ == "__main__":
    unittest.main()
