"""Executable verification evidence, separate from training task generation."""
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import subprocess
import sys

from .author_reference import complex_response, run_case
from .crosscheck import ac_commands, ac_measurements, compare, measured_value, transient_commands
from .metrics import ac_metrics, read_table, score, transient_metrics
from .simulator import ROOT, digest, evaluate


def analytic_case(directory, name, poles, expected_success):
    directory.mkdir(parents=True, exist_ok=False)
    gain = 1000
    lines = [f"Analytic isolated RC cascade: {name}", "VIN input 0 dc 0 ac 1"]
    previous = "input"
    for index, pole in enumerate(poles):
        node = "out" if index == len(poles) - 1 else f"stage{index}"
        lines.extend([f"E{index} drive{index} 0 {previous} 0 {gain if index == 0 else 1}",
                      f"R{index} drive{index} {node} 1000",
                      f"C{index} {node} 0 {1 / (2 * math.pi * pole * 1000):.16g}"])
        previous = node
    deck = "\n".join(lines) + "\n.control\nset numdgt=15\nset wr_vecnames\n"
    deck += "ac dec 100 1 10G\nwrdata response.tsv v(out)\n" + ac_commands("out", 1) + "quit\n.endc\n.end\n"
    (directory / "design.cir").write_text(deck)
    process = subprocess.run(["ngspice", "-n", "-D", "num_threads=1", "-b", "-o", "simulation.log", "design.cir"],
                             cwd=directory, capture_output=True, text=True, timeout=30)
    if process.returncode:
        raise RuntimeError(f"Analytic fixture failed: {name}")
    metrics = ac_metrics(complex_response(read_table(directory / "response.tsv", 3)))
    native = ac_measurements((directory / "simulation.log").read_text())
    low, high = 1.0, 1e10
    for _ in range(100):
        middle = math.sqrt(low * high)
        magnitude = gain / math.prod(math.sqrt(1 + (middle / pole) ** 2) for pole in poles)
        if magnitude > 1:
            low = middle
        else:
            high = middle
    unity = math.sqrt(low * high)
    theoretical = {"gain_db": 20 * math.log10(gain / math.prod(math.sqrt(1 + (1 / p) ** 2) for p in poles)),
                   "unity_gain_hz": unity,
                   "phase_margin_deg": 180 - sum(math.degrees(math.atan(unity / p)) for p in poles)}
    native_comparison = compare(metrics, native)
    theory_comparison = compare(metrics, theoretical)
    constraints = {"gain_db": {"min": 40}, "unity_gain_hz": {"min": 1}, "phase_margin_deg": {"min": 60}}
    success = score(metrics, constraints)["success"]
    result = {"id": name, "poles_hz": poles, "metrics": metrics, "theoretical": theoretical,
              "native_comparison": native_comparison, "theory_comparison": theory_comparison,
              "success": success, "expected_success": expected_success,
              "audit_passed": native_comparison["agrees"] and theory_comparison["agrees"] and success == expected_success}
    (directory / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def compare_release_curves(directory):
    exact = read_table(directory / "release_default_exact/ac.csv", 3)
    instrumented = read_table(directory / "release_default_instrumented/ac.csv", 3)
    if len(exact) != len(instrumented):
        return {"agrees": False, "error": "Different AC sample counts."}
    differences = []
    for first, second in zip(exact, instrumented):
        if abs(first[0] / second[0] - 1) > 1e-6:
            return {"agrees": False, "error": "Different frequency samples."}
        a, b = complex(*first[1:]), complex(*second[1:])
        differences.append(abs(a - b) / max(abs(a), 1e-12))
    exact_current = read_table(directory / "release_default_exact/dc.csv", 2)[0][1]
    instrumented_current = read_table(directory / "release_default_instrumented/dc.csv", 2)[0][1]
    current_difference = abs(exact_current - instrumented_current) / abs(exact_current)
    return {"agrees": max(differences) <= 1e-6 and current_difference <= 1e-6,
            "maximum_relative_transfer_difference": max(differences),
            "relative_current_difference": current_difference, "relative_tolerance": 1e-6,
            "scope": "Confirms explicit capacitor syntax and measurement instrumentation preserve the released circuit's response."}


def analytic_transient(directory):
    directory.mkdir(parents=True, exist_ok=False)
    test = {"low_v": 0.3, "high_v": 0.5, "rise_start_s": 1e-6, "edge_s": 1e-8,
            "high_duration_s": 5e-6, "period_s": 20e-6, "stop_s": 12e-6,
            "max_step_s": 1e-9, "settling_tolerance_v": 0.002, "minimum_hold_s": 2e-6}
    tau = 1e-7
    deck = """Analytic RC settling with finite input rise/fall time
VIN signal 0 pulse(0.3 0.5 1u 10n 10n 5u 20u)
R signal out 1000
C out 0 100p
.control
set numdgt=15
set wr_vecnames
set wr_singlescale
tran 1n 12u 0 1n
wrdata transient.tsv v(signal) v(out) i(vin)
""" + transient_commands(test) + "quit\n.endc\n.end\n"
    (directory / "design.cir").write_text(deck)
    subprocess.run(["ngspice", "-n", "-b", "-o", "simulation.log", "design.cir"], cwd=directory,
                   capture_output=True, text=True, timeout=30, check=True)
    metrics = transient_metrics(read_table(directory / "transient.tsv", 4), test)
    # Exact first-order response to a linear ramp, followed by exponential settling.
    ramp_end_error = (test["high_v"] - test["low_v"]) * tau / test["edge_s"] * (-math.expm1(-test["edge_s"] / tau))
    settling = test["edge_s"] + tau * math.log(ramp_end_error / test["settling_tolerance_v"])
    theory = {"settling_rise_s": settling, "settling_fall_s": settling}
    log = (directory / "simulation.log").read_text()
    native = {"settling_rise_s": measured_value(log, "check_rise_cross_result") - test["rise_start_s"],
              "settling_fall_s": measured_value(log, "check_fall_cross_result") -
              (test["rise_start_s"] + test["edge_s"] + test["high_duration_s"])}
    comparisons = {"native_comparison": compare(metrics, native, test["max_step_s"]),
                   "theory_comparison": compare(metrics, theory, test["max_step_s"])}
    result = {"id": "analytic_rc_settling", "tau_s": tau, "test": test, "metrics": metrics,
              "theoretical": theory, **comparisons,
              "audit_passed": all(item["agrees"] for item in comparisons.values())}
    (directory / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def verify(directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    unit = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                          cwd=ROOT, capture_output=True, text=True, timeout=60)
    (directory / "unit_tests.txt").write_text(unit.stdout + unit.stderr)
    analytic = [analytic_case(directory / name, name, poles, expected) for name, poles, expected in
                (("analytic_one_pole", [1e3], True),
                 ("analytic_two_poles", [1e3, 1e7], True),
                 ("analytic_unstable_three_poles", [1e3, 1e3, 1e3], False))]
    analytic.append(analytic_transient(directory / "analytic_rc_settling"))
    definition_path = ROOT / "verification/autockt_cases.json"
    definition = json.loads(definition_path.read_text())
    author = []
    for case in definition["cases"]:
        result = run_case(case, directory / case["id"])
        result["audit_passed"] = result["success"] == case["expected_success"]
        if case["id"].startswith("release_default"):
            result["audit_passed"] &= result["metrics"].get("gain_vv", math.inf) < 200
        elif case["id"] == "insufficient_compensation":
            result["audit_passed"] &= result["status"] == "ok" and not result["checks"]["phase_margin_deg"]
        elif case["id"] == "reversed_inputs":
            result["audit_passed"] &= "non-inverting" in result.get("error", "")
        elif case["id"] == "shorted_output":
            result["audit_passed"] &= result["metrics"].get("gain_db", math.inf) < 0
        author.append(result)
    release_agreement = compare_release_curves(directory)
    main_reference = next(r for r in author if r["id"] == "verified_candidate")
    fine = next(r for r in author if r["id"] == "candidate_fine")
    convergence = compare(main_reference["metrics"], {key: fine["metrics"][key] for key in
                                                     ("gain_db", "unity_gain_hz", "phase_margin_deg")})
    characterization = []
    for condition in definition["characterization"]:
        case = {**condition, "parameters": main_reference["case"]["parameters"]}
        characterization.append(run_case(case, directory / case["id"]))
    pilot = []
    for name in ("fan_smc_nominal", "fan_smc_sizing", "autockt_two_stage_nominal", "autockt_two_stage_sizing"):
        result = evaluate(ROOT / "tasks" / f"{name}.json", {}, directory / name)
        result["audit_passed"] = result["success"] == name.endswith("_nominal")
        pilot.append(result)
    prior = json.loads((ROOT / "reports/baseline.json").read_text())
    prior_solutions = []
    for solution in prior["random_search"]:
        name = solution["task_id"].removesuffix("_v1")
        result = evaluate(ROOT / "tasks" / f"{name}.json", solution["final_parameters"],
                          directory / f"prior_solution_{name}_seed{solution['seed']}")
        result["seed"] = solution["seed"]
        result["audit_passed"] = result["success"]
        prior_solutions.append(result)
    boundary = []
    for name, points, expected in (("ambiguous_boundary", 100, False), ("resolved_boundary", 500, True)):
        task = json.loads((ROOT / "tasks/autockt_two_stage_nominal.json").read_text())
        task["id"] = name
        # This threshold falls between the two interpolation methods on the
        # original grid. It is a rejection test for uncertainty, not a bad circuit.
        task["constraints"]["phase_margin_deg"]["min"] = 64.9642
        task["ac"]["points_per_decade"] = points
        task_path = directory / f"{name}.json"
        task_path.write_text(json.dumps(task, indent=2) + "\n")
        result = evaluate(task_path, {}, directory / name)
        result["audit_passed"] = result["success"] == expected
        if name == "ambiguous_boundary":
            result["audit_passed"] &= "disagree on pass/fail" in result.get("error", "")
        boundary.append(result)
    checks = {"unit_tests": unit.returncode == 0,
              "analytic_fixtures": all(r["audit_passed"] for r in analytic),
              "author_fixture_outcomes": all(r["audit_passed"] for r in author),
              "released_deck_preserved": release_agreement["agrees"],
              "numerical_convergence": convergence["agrees"],
              "existing_pilot_regression": all(r["audit_passed"] for r in pilot)}
    checks["boundary_disagreement_handling"] = all(r["audit_passed"] for r in boundary)
    checks["prior_passing_solutions"] = len(prior_solutions) == 6 and all(r["audit_passed"] for r in prior_solutions)
    code = ["analog_design/author_reference.py", "analog_design/crosscheck.py", "analog_design/metrics.py",
            "analog_design/simulator.py", "analog_design/verification.py", "analog_design/episode.py",
            "analog_design/qualification.py", "dependencies.lock.json", "verification/autockt_cases.json",
            "verification/qualification.json", "scripts/verify.py", "tests/test_crosscheck.py",
            "tests/test_evaluator.py", "tests/test_episode.py", "reports/baseline.json"]
    report = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "raw_directory": str(directory),
              "automated_checks_passed": all(checks.values()), "checks": checks,
              "training_ready": False, "production_ready": False,
              "remaining_reviews": ["Independent analog-engineer review of circuit and testbench semantics",
                                    "Qualified operating envelope and supported device-model limits",
                                    "Agent/evaluator isolation and held-out task protocol"],
              "code_sha256": {name: digest(ROOT / name) for name in code},
              "analytic_fixtures": analytic, "author_fixtures": author,
              "released_deck_comparison": release_agreement, "numerical_convergence": convergence,
              "characterization": characterization,
              "characterization_scope": definition["characterization_note"], "pilot_regressions": pilot,
              "boundary_regressions": boundary, "prior_passing_solutions": prior_solutions}
    (directory / "verification.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    return report
