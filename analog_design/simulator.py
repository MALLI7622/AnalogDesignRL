"""Run isolated ngspice evaluations. The agent supplies numeric parameters only."""
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import re
import shutil
import subprocess
import time

from .metrics import ac_metrics, read_table, score, transient_metrics
from .crosscheck import ac_commands, all_measurements, compare, transient_commands

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def parameters_for(task, overrides):
    if not isinstance(overrides, dict):
        raise ValueError("An action must be a JSON object of parameter values.")
    parameters = dict(task["initial_parameters"])
    parameters.update(overrides)
    for name, value in parameters.items():
        if name not in task["parameters"]:
            raise ValueError(f"Parameter is not editable: {name}")
        rule = task["parameters"][name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"Parameter must be a finite number: {name}")
        if not rule["min"] <= value <= rule["max"]:
            raise ValueError(f"Parameter outside allowed bounds: {name}")
        if rule.get("integer") and value != int(value):
            raise ValueError(f"Parameter must be an integer: {name}")
    return parameters


def write_inputs(task, parameters, run_dir):
    circuit = ROOT / task["circuit_directory"]
    reference = (circuit / "reference.params").read_text()
    for name, value in parameters.items():
        pattern = rf"(?i)(?<!\w){re.escape(name)}\s*=\s*[^\s]+"
        reference, count = re.subn(pattern, f"{name}={value:.15g}", reference)
        if count != 1:
            raise ValueError(f"Expected exactly one parameter declaration: {name}")
    (run_dir / "parameters.spice").write_text(reference)
    model = ROOT / ".deps/sky130_pdk/libs.tech/ngspice/corners" / (task["conditions"]["corner"] + ".spice")
    if not model.is_file():
        raise FileNotFoundError("Device models are missing. Run python3 scripts/setup.py.")
    condition = task["conditions"]
    header = f'''{task['id']} - controlled evaluation
.include "{circuit / 'netlist.spice'}"
.include "{run_dir / 'parameters.spice'}"
.param mc_mm_switch=0 mc_pr_switch=0
.include "{model}"
.temp {condition['temperature_c']}
VDD vdd 0 {condition['supply_v']}
'''
    control = '''.control
set noaskquit
set numdgt=12
set wr_vecnames
set wr_singlescale
'''
    ac = task["ac"]
    (run_dir / "ac.cir").write_text(header + f'''
VIN signal 0 dc {condition['common_mode_v']} ac 1
* DC feedback biases the amplifier; at AC the inverting input is grounded.
Lfeedback out inv 1T
Cbreak inv 0 1T
Xamp 0 vdd inv signal out {task['subcircuit']}
CL out 0 {condition['load_f']}
''' + control + f'''
op
wrdata operating_point.tsv v(out) i(vdd)
let check_power_result = -i(vdd)*{condition['supply_v']}
let check_dc_error_result = abs(v(out)-{condition['common_mode_v']})
print check_power_result check_dc_error_result
ac dec {ac['points_per_decade']} {ac['start_hz']} {ac['stop_hz']}
let gain_db = db(v(out))
let phase_rad = cph(v(out))
wrdata ac.tsv gain_db phase_rad
{ac_commands('out', ac['start_hz'])}
quit
.endc
.end
''')
    tran = task["transient"]
    (run_dir / "transient.cir").write_text(header + f'''
VIN signal 0 pulse({tran['low_v']} {tran['high_v']} {tran['rise_start_s']} {tran['edge_s']} {tran['edge_s']} {tran['high_duration_s']} {tran['period_s']})
Xamp 0 vdd out signal out {task['subcircuit']}
CL out 0 {condition['load_f']}
''' + control + f'''
tran {tran['max_step_s']} {tran['stop_s']} 0 {tran['max_step_s']}
wrdata transient.tsv v(signal) v(out) i(vdd)
{transient_commands(tran)}
quit
.endc
.end
''')
    return circuit, model


def evaluate(task_path, overrides, run_dir, timeout_s=30):
    """One design evaluation uses two simulator invocations (AC/DC and transient)."""
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("The simulator timeout must be finite and positive.")
    task_path = Path(task_path).resolve()
    task = json.loads(task_path.read_text())
    run_dir = Path(run_dir).resolve()
    # Exclusive directories prevent accidental reuse of stale outputs after failure.
    run_dir.mkdir(parents=True, exist_ok=False)
    start = time.monotonic()
    result = {"task_id": task["id"], "status": "error", "success": False,
              "reward": -1.0, "metrics": {}, "checks": {}, "simulator_invocations": 0,
              "timestamp_utc": datetime.now(timezone.utc).isoformat(),
              "task_sha256": digest(task_path), "host": platform.platform(),
              "python_version": platform.python_version(), "commands": [], "warnings": []}
    # Keep the exact task definition with each run.
    (run_dir / "task.json").write_text(json.dumps(task, indent=2) + "\n")
    try:
        parameters = parameters_for(task, overrides)
        result["parameters"] = parameters
        circuit, model = write_inputs(task, parameters, run_dir)
        executable = shutil.which("ngspice")
        if not executable:
            raise FileNotFoundError("ngspice is not installed.")
        version = subprocess.check_output([executable, "--version"], text=True, timeout=5)
        result["ngspice_version"] = version.strip()
        lock = json.loads((ROOT / "dependencies.lock.json").read_text())
        if f"ngspice-{lock['ngspice_validated_version']} " not in version:
            result["warnings"].append("ngspice version differs from the validated version.")
        result["provenance"] = {
            "dependencies": lock, "netlist_sha256": digest(circuit / "netlist.spice"),
            "reference_parameters_sha256": digest(circuit / "reference.params"),
            "parameters_sha256": digest(run_dir / "parameters.spice"),
            "corner_file_sha256": digest(model),
            "evaluator_sha256": digest(__file__),
            "metrics_code_sha256": digest(Path(__file__).with_name("metrics.py")),
            "crosscheck_code_sha256": digest(Path(__file__).with_name("crosscheck.py")),
            "ac_testbench_sha256": digest(run_dir / "ac.cir"),
            "transient_testbench_sha256": digest(run_dir / "transient.cir")}
        for analysis in ("ac", "transient"):
            command = [executable, "-n", "-D", "ngbehavior=hsa", "-D", "num_threads=1",
                       "-b", "-o", f"{analysis}.log", f"{analysis}.cir"]
            result["commands"].append(command)
            result["simulator_invocations"] += 1
            completed = subprocess.run(command, cwd=run_dir, capture_output=True,
                                       text=True, timeout=timeout_s)
            (run_dir / f"{analysis}.console.txt").write_text(completed.stdout + completed.stderr)
            log = (run_dir / f"{analysis}.log").read_text()
            result["warnings"].extend(line for line in log.splitlines() if line.startswith("Warning:"))
            if completed.returncode != 0 or re.search(r"(?im)^\s*(?:error\b|fatal\b|run simulation\(s\) aborted)", log):
                raise RuntimeError(f"{analysis} simulation failed; inspect {analysis}.log")
        operating = read_table(run_dir / "operating_point.tsv", 3)
        if len(operating) != 1:
            raise ValueError("Expected one DC operating point.")
        _, output, supply_current = operating[0]
        metrics = {"output_dc_v": output,
                   "dc_error_v": abs(output - task["conditions"]["common_mode_v"]),
                   "supply_current_a": -supply_current,
                   "power_w": -supply_current * task["conditions"]["supply_v"]}
        if metrics["power_w"] <= 0:
            raise ValueError("Supply power must be positive.")
        result["metrics"] = metrics
        ac_rows = read_table(run_dir / "ac.tsv", 3)
        if ac_rows[0][0] > task["ac"]["start_hz"] * (1 + 1e-6) or ac_rows[-1][0] < task["ac"]["stop_hz"] * (1 - 1e-6):
            raise ValueError("AC sweep is incomplete.")
        metrics.update(ac_metrics(ac_rows))
        metrics.update(transient_metrics(read_table(run_dir / "transient.tsv", 4), task["transient"]))
        independent = all_measurements((run_dir / "ac.log").read_text(),
                                       (run_dir / "transient.log").read_text(), task["transient"])
        result["independent_metrics"] = independent
        result["crosscheck"] = compare(metrics, independent, task["transient"]["max_step_s"])
        if not result["crosscheck"]["agrees"]:
            raise ValueError("Python and ngspice measurements disagree; inspect the crosscheck.")
        result.update(score(metrics, task["constraints"]))
        independent_score = score(independent, task["constraints"])
        if result["checks"] != independent_score["checks"]:
            result.update(success=False, reward=-1.0)
            raise ValueError("The two measurement paths disagree on pass/fail.")
        result["status"] = "ok"  # A completed measurement can still fail the task.
    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
        result["error"] = f"A simulator invocation exceeded {timeout_s:g} seconds."
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
    result["elapsed_s"] = time.monotonic() - start
    result["warnings"] = sorted(set(result["warnings"]))
    (run_dir / "result.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    return result
