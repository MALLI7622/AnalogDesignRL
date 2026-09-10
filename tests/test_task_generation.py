from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from analog_design.episode import Episode
from analog_design.generation_sources import load_sources
from analog_design.model_clients import ModelClient, ModelConfig, ReplayClient, strict_json
from analog_design.simulator import ROOT
from analog_design.task_generation import generate_tasks
from analog_design.task_proposals import compile_proposal, load_template, parse_proposal


def fixture(name="autockt"):
    return strict_json((ROOT / f"generation/examples/{name}_reply.json").read_text())


def fake_evaluate(task_path, parameters, directory, timeout):
    task = strict_json(Path(task_path).read_text())
    success = bool(parameters)
    return {"status": "ok", "success": success, "metrics": {"gain_db": 70},
            "checks": {name: success for name in task["constraints"]}, "reward": 1 if success else -0.1}


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.template, _ = load_template("autockt_two_stage")
        self.sources = load_sources(self.template)

    def compile(self, proposal):
        return compile_proposal(proposal, self.template, "autockt_two_stage", self.sources)

    def test_compilation_preserves_tests_and_hides_reference(self):
        task, reference, _, _ = self.compile(fixture())
        for key in ("conditions", "ac", "transient", "parameters", "max_evaluations", "circuit_directory"):
            self.assertEqual(task[key], self.template[key])
        self.assertEqual(task["constraints"]["gain_db"], {"min": 60})
        self.assertEqual(reference["W_GM"], 80)
        self.assertNotIn("reference_parameters", task)
        self.assertNotIn("source_evidence", task)
        self.assertNotIn("rationale", task)

    def test_requires_every_metric_without_weakening_or_changing_direction(self):
        edits = []
        p = fixture(); del p["constraints"]["phase_margin_deg"]; edits.append(p)
        p = fixture(); p["constraints"]["gain_db"]["min"] = 1; edits.append(p)
        p = fixture(); p["constraints"]["power_w"]["max"] = 1; edits.append(p)
        p = fixture(); p["constraints"]["gain_db"] = {"max": 60}; edits.append(p)
        p = fixture(); p["constraints"]["power_w"]["max"] = 0; edits.append(p)
        for proposal in edits:
            with self.subTest(proposal=proposal), self.assertRaises(ValueError):
                self.compile(proposal)

    def test_rejects_paths_netlists_and_testbench_overrides(self):
        for key, value in (("circuit_directory", "/tmp/evil"), ("conditions", {"corner": "tt\n.control"}),
                           ("netlist", ".control\nshell command"), ("max_evaluations", 999999),
                           ("transient", {}), ("reward", 1)):
            proposal = fixture()
            proposal[key] = value
            with self.assertRaises(ValueError):
                self.compile(proposal)

    def test_rejects_invalid_parameters_and_incomplete_reference(self):
        for value in (True, "80\n.include evil", 1000, float("nan"), float("inf")):
            proposal = fixture()
            proposal["reference_parameters"]["W_GM"] = value
            with self.assertRaises(ValueError):
                self.compile(proposal)
        proposal = fixture(); del proposal["reference_parameters"]["IBIAS"]
        with self.assertRaises(ValueError):
            self.compile(proposal)

    def test_citations_must_match_supplied_text_and_source_ids(self):
        proposal = fixture(); proposal["source_evidence"][0]["quote"] = "The paper reports 500000 dB gain."
        with self.assertRaisesRegex(ValueError, "not found"):
            self.compile(proposal)
        proposal = fixture(); proposal["source_evidence"][0]["source_id"] = "imaginary-paper"
        with self.assertRaises(ValueError):
            self.compile(proposal)

    def test_duplicate_identity_ignores_prose_reference_and_numeric_formatting(self):
        first = fixture()
        second = deepcopy(first)
        second["rationale"] = "Different prose must not create another sizing task."
        second["reference_parameters"]["W_GM"] = 90
        second["initial_parameters"]["W_GM"] = float(first["initial_parameters"]["W_GM"])
        self.assertEqual(self.compile(first)[2:], self.compile(second)[2:])
        second["initial_parameters"]["W_GM"] = 45
        self.assertNotEqual(self.compile(first)[2], self.compile(second)[2])
        self.assertEqual(self.compile(first)[3], self.compile(second)[3])

    def test_integer_parameter_rules_remain_boolean(self):
        template, _ = load_template("fan_smc")
        proposal = fixture("fan_smc")
        task, _, _, _ = compile_proposal(proposal, template, "fan_smc", load_sources(template))
        self.assertIs(task["parameters"]["MOSFET_23_1_M_gm3_NMOS"]["integer"], True)
        proposal["initial_parameters"]["MOSFET_23_1_M_gm3_NMOS"] = 16.5
        with self.assertRaises(ValueError):
            compile_proposal(proposal, template, "fan_smc", load_sources(template))

    def test_only_single_json_document_or_fenced_json_is_parsed(self):
        self.assertEqual(parse_proposal('```json\n{"a":1}\n```'), {"a": 1})
        for text in ('{"a":1} trailing', '{} {}', 'Here is JSON: {}', '{"a":1,"a":2}'):
            with self.assertRaises(ValueError):
                parse_proposal(text)


class GenerationTests(unittest.TestCase):
    def run_batch(self, directory, proposals, *, count=1, max_attempts=None, evaluator=fake_evaluate):
        replay = Path(directory) / "reply.json"
        replay.write_text(json.dumps(proposals))
        output = Path(directory) / "dataset"
        with patch("analog_design.task_generation.preflight"), \
             patch("analog_design.task_generation.evaluate", side_effect=evaluator) as simulator:
            manifest = generate_tasks(ReplayClient(replay), "autockt_two_stage", output,
                                      count=count, max_attempts=max_attempts)
        return manifest, output, simulator.call_count

    def test_accepted_dataset_has_runnable_tasks_private_reference_and_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, output, calls = self.run_batch(directory, [fixture()])
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["generation_mode"], "replay")
            self.assertFalse(manifest["training_approved"])
            self.assertEqual(calls, 2)
            task = manifest["accepted"][0]
            self.assertTrue((output / task["path"]).is_file())
            self.assertTrue((output / "private" / task["id"] / "reference.json").is_file())
            self.assertTrue((output / "attempts/00001/request.json").is_file())
            with self.assertRaisesRegex(RuntimeError, "not approved"):
                Episode(output / task["path"], output / "training", for_training=True)

    def test_invalid_proposal_is_revised_with_feedback_before_simulation(self):
        bad = fixture(); bad["netlist"] = "untrusted code"
        with tempfile.TemporaryDirectory() as directory:
            manifest, output, calls = self.run_batch(directory, [bad, fixture()], max_attempts=2)
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["attempts_used"], 2)
            self.assertEqual(len(manifest["rejected"]), 1)
            self.assertEqual(calls, 2)
            request = strict_json((output / "attempts/00002/request.json").read_text())
            self.assertIn("validation_feedback", request["body"]["messages"][-1]["content"])

    def test_duplicates_are_rejected_without_additional_simulations(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, _, calls = self.run_batch(directory, [fixture(), fixture()], count=2, max_attempts=2)
            self.assertEqual(manifest["status"], "attempt_budget_exhausted")
            self.assertEqual(len(manifest["accepted"]), 1)
            self.assertIn("Duplicate", manifest["rejected"][0]["reason"])
            self.assertEqual(calls, 2)

    def test_partial_batch_can_be_excluded_from_new_proposals_and_revisions(self):
        with tempfile.TemporaryDirectory() as directory:
            prior, prior_output, _ = self.run_batch(directory, [fixture()], count=2, max_attempts=1)
            self.assertEqual(prior["status"], "attempt_budget_exhausted")
            revised = fixture()
            revised["initial_parameters"]["CCOMP"] = 1.2e-11
            replies = Path(directory) / "retry.json"
            replies.write_text(json.dumps([fixture(), revised]))
            output = Path(directory) / "retry"
            with patch("analog_design.task_generation.preflight"), \
                 patch("analog_design.task_generation.evaluate", side_effect=fake_evaluate) as simulator:
                result = generate_tasks(ReplayClient(replies), "autockt_two_stage", output,
                                        max_attempts=2, exclude_batches=[prior_output])
            self.assertEqual(result["status"], "complete")
            self.assertEqual(len(result["accepted"]), 1)
            self.assertNotEqual(result["accepted"][0]["id"], prior["accepted"][0]["id"])
            self.assertIn("Duplicate", result["rejected"][0]["reason"])
            self.assertEqual(simulator.call_count, 2)
            self.assertEqual(len(result["excluded_tasks"]), 1)
            for attempt in (1, 2):
                request = strict_json((output / f"attempts/{attempt:05d}/request.json").read_text())
                content = request["body"]["messages"][0]["content"]
                _, end = json.JSONDecoder().raw_decode(content)
                context = json.loads(content[end:])
                self.assertEqual(context["accepted_tasks"][0]["initial_parameters"], prior["accepted"][0]["initial_parameters"])

    def test_excluded_batch_integrity_is_checked_before_generation(self):
        for case in ("wrong_template", "changed_task", "outside_path"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                prior, prior_output, _ = self.run_batch(directory, [fixture()])
                if case == "wrong_template":
                    prior["template"] = "fan_smc"
                elif case == "changed_task":
                    (prior_output / prior["accepted"][0]["path"]).write_text("{}")
                else:
                    prior["accepted"][0]["path"] = "../outside.json"
                (prior_output / "manifest.json").write_text(json.dumps(prior))
                client = ReplayClient(Path(directory) / "reply.json")
                with patch.object(client, "complete") as complete, self.assertRaises(ValueError):
                    generate_tasks(client, "autockt_two_stage", Path(directory) / "retry",
                                   dry_run=True, exclude_batches=[prior_output])
                complete.assert_not_called()

    def test_failing_reference_error_start_and_passing_start_are_not_accepted(self):
        for case in ("bad_reference", "error_start", "passing_start"):
            def evaluator(task_path, parameters, directory, timeout):
                result = fake_evaluate(task_path, parameters, directory, timeout)
                if case == "bad_reference":
                    result["success"] = False
                elif not parameters:
                    if case == "error_start":
                        result["status"] = "failed"
                    else:
                        result["success"] = True
                return result
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                manifest, output, calls = self.run_batch(directory, [fixture()], max_attempts=1, evaluator=evaluator)
                self.assertEqual(manifest["accepted"], [])
                self.assertEqual(list((output / "tasks").iterdir()), [])
                self.assertEqual(calls, 1 if case == "bad_reference" else 2)

    def test_malformed_reply_uses_attempt_budget_and_provider_failure_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, _, calls = self.run_batch(directory, ["not JSON", "{}"], max_attempts=2)
            self.assertEqual(manifest["attempts_used"], 2)
            self.assertEqual(len(manifest["rejected"]), 2)
            self.assertEqual(calls, 0)
        with tempfile.TemporaryDirectory() as directory:
            manifest, output, _ = self.run_batch(directory, [], max_attempts=1)
            self.assertEqual(manifest["status"], "provider_error")
            self.assertIn("Replay exhausted", manifest["error"])
            self.assertEqual(strict_json((output / "manifest.json").read_text())["status"], "provider_error")

    def test_dry_run_does_not_need_api_key_or_simulator(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ModelClient(ModelConfig(provider="openai", model="example", api_key_env="ABSENT_TEST_KEY"))
            with patch.object(client, "complete") as complete, patch("analog_design.task_generation.preflight") as preflight:
                manifest = generate_tasks(client, "autockt_two_stage", Path(directory) / "preview", dry_run=True)
                self.assertEqual(manifest["status"], "dry_run")
                self.assertEqual(manifest["attempts_used"], 0)
                complete.assert_not_called()
                preflight.assert_not_called()

    def test_existing_output_directory_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ModelClient(ModelConfig(provider="gemini", model="example"))
            with self.assertRaises(FileExistsError):
                generate_tasks(client, "autockt_two_stage", directory, dry_run=True)

    def test_source_limit_is_explicit_and_text_is_preserved(self):
        template, _ = load_template("autockt_two_stage")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.txt"
            path.write_text("An exact source passage for the model to consider.")
            sources = load_sources(template, [path])
            self.assertEqual(sources["source_001"]["text"], path.read_text())
            with self.assertRaisesRegex(ValueError, "Nothing was truncated"):
                load_sources(template, [path], max_chars=10)


if __name__ == "__main__":
    unittest.main()
