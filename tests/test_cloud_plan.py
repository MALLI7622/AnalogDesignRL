import contextlib
import io
import json
from pathlib import Path
import shlex
import unittest
from unittest.mock import patch

from training.cloud_plan import PHASES, commands, duration_seconds, main, validate

ROOT = Path(__file__).resolve().parents[1]


class CloudPlanTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "training/cloud.example.json").read_text())

    def test_vm_expires_and_retains_the_separate_data_disk(self):
        create = commands(self.config)["create_tpu"]
        self.assertEqual(create[create.index("--machine-type") + 1], "ct6e-standard-4t")
        self.assertIn("--provisioning-model=FLEX_START", create)
        self.assertEqual(create[create.index("--max-run-duration") + 1], "4h")
        self.assertEqual(create[create.index("--request-valid-for-duration") + 1], "2h")
        self.assertIn("--instance-termination-action=DELETE", create)
        disk = dict(item.split("=", 1) for item in create[create.index("--disk") + 1].split(","))
        self.assertEqual(disk["name"], self.config["data_disk"])
        self.assertEqual(disk["device-name"], "analog-rl-data")
        self.assertEqual(disk["auto-delete"], "no")
        self.assertEqual(disk["boot"], "no")

    def test_formatting_requires_explicit_first_boot_flag(self):
        self.assertIn("--metadata=analog-rl-format-blank-disk=false", commands(self.config)["create_tpu"])
        self.assertIn("--metadata=analog-rl-format-blank-disk=true",
                      commands(self.config, initialize_data_disk=True)["create_tpu"])

    def test_every_command_uses_the_selected_account_and_project(self):
        for label, command in commands(self.config).items():
            with self.subTest(command=label):
                self.assertEqual(command[command.index("--project") + 1], self.config["project"])
                self.assertEqual(command[command.index("--account") + 1], self.config["account"])

    def test_cleanup_does_not_delete_retained_storage(self):
        plan = commands(self.config)
        self.assertEqual(PHASES["cleanup"], ("delete_compute_after_backup",))
        self.assertEqual(plan[PHASES["cleanup"][0]][:4], ["gcloud", "compute", "instances", "delete"])
        self.assertNotIn("--delete-disks", plan[PHASES["cleanup"][0]])
        bucket = plan["create_bucket"]
        self.assertIn("--uniform-bucket-level-access", bucket)
        self.assertIn("--public-access-prevention", bucket)

    def test_default_cli_only_prints_read_only_inspection(self):
        output = io.StringIO()
        with patch("sys.argv", ["cloud_plan", "--config", str(ROOT / "training/cloud.example.json")]), \
                contextlib.redirect_stdout(output):
            main()
        lines = [shlex.split(line) for line in output.getvalue().splitlines()
                 if line and not line.startswith("#")]
        self.assertEqual(len(lines), len(PHASES["inspect"]))
        for command in lines:
            self.assertNotIn("create", command)
            self.assertNotIn("delete", command)
            self.assertNotIn("enable", command)
        self.assertTrue(any(command[:4] == ["gcloud", "billing", "projects", "describe"] for command in lines))

    def test_valid_queue_and_runtime_boundaries(self):
        self.assertEqual(duration_seconds("90s"), 90)
        for queue, runtime in (("90s", "10m"), ("2h", "7d")):
            with self.subTest(queue=queue, runtime=runtime):
                validate({**self.config, "request_valid_for_duration": queue, "max_run_duration": runtime})
        for value in ("0h", "-1h", "1.5h", "1h30m", True, None):
            with self.subTest(duration=value), self.assertRaises(ValueError):
                duration_seconds(value)

    def test_reject_inconsistent_or_unbounded_allocation(self):
        cases = [
            ("machine_type", "ct6e-standard-8t"),
            ("provisioning_model", "SPOT"),
            ("zone", "us-west4-a"),
            ("region", "us-central1"),
            ("max_run_duration", "8d"),
            ("max_run_duration", "9m"),
            ("request_valid_for_duration", "3h"),
            ("request_valid_for_duration", "60s"),
            ("data_disk_type", "pd-standard"),
            ("data_disk_gb", 49),
            ("boot_disk_gb", True),
            ("instance", "bad name"),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                commands({**self.config, field: value})


if __name__ == "__main__":
    unittest.main()
