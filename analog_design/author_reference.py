"""Reproduce the released AutoCkt deck and audit its AC measurement pipeline.

The paper reports aggregate optimization results, not expected measurements for
the repository's default sizing. Project verification fixtures are labeled as such.
"""
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import time

from .crosscheck import ac_commands, ac_measurements, compare
from .metrics import ac_metrics, read_table, score
from .simulator import ROOT, digest

SOURCE_DIRECTORY = ROOT / "external/AutoCkt"
INPUT_DIRECTORY = SOURCE_DIRECTORY / "eval_engines/ngspice/ngspice_inputs"
PARAMETERS = {name: (1, 99) for name in ("mp1", "mn1", "mp3", "mn3", "mn4", "mn5")}
PARAMETERS["cc"] = (0.1e-12, 9.9e-12)
CONSTRAINTS = {"gain_db": {"min": 20 * math.log10(200)},
               "unity_gain_hz": {"min": 1e6}, "phase_margin_deg": {"min": 60},
               "supply_current_a": {"max": 0.001}}


def verify_assets():
    lock = json.loads((ROOT / "dependencies.lock.json").read_text())["autockt"]
    for relative, expected in lock["files_sha256"].items():
        if digest(SOURCE_DIRECTORY / relative) != expected:
            raise ValueError(f"AutoCkt dependency changed: {relative}. Run scripts/setup.py.")
    return lock


def complex_response(rows):
    """Convert complex transfer samples to dB/unwrapped phase using Python math."""
    converted = []
    previous = None
    accumulated = 0
    for frequency, real, imaginary in rows:
        magnitude = math.hypot(real, imaginary)
        if magnitude <= 0:
            raise ValueError("Zero transfer magnitude is outside the supported measurement domain.")
        angle = math.atan2(imaginary, real)
        accumulated = angle if previous is None else accumulated + math.remainder(angle - previous, 2 * math.pi)
        previous = angle
        converted.append([frequency, 20 * math.log10(magnitude), accumulated])
    return converted


def render_deck(case):
    original = (INPUT_DIRECTORY / "netlist/two_stage_opamp.cir").read_text()
    model = INPUT_DIRECTORY / "spice_models/45nm_bulk.txt"
    relocated, count = re.subn(r'(?m)^\.include "[^"]+45nm_bulk\.txt"$',
                              f'.include "{model}"', original)
    if count != 1:
        raise ValueError("Unexpected author netlist include structure.")
    if case.get("exact_release"):
        if set(case) - {"id", "exact_release", "expected_success"}:
            raise ValueError("The exact-release case cannot change parameters or tests.")
        return relocated
    head, separator, _ = relocated.partition(".ac dec 10 1 10G")
    if not separator:
        raise ValueError("Unexpected author AC directive.")
    for name, value in case.get("parameters", {}).items():
        if name not in PARAMETERS or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"Invalid author-space parameter: {name}")
        low, high = PARAMETERS[name]
        if not low <= value <= high or (name != "cc" and value != int(value)):
            raise ValueError(f"Parameter outside the author's discrete range: {name}")
        if name == "cc" and abs(value / 1e-13 - round(value / 1e-13)) > 1e-8:
            raise ValueError("Compensation capacitance must use 0.1 pF steps.")
        head, count = re.subn(rf"(?<!\w){name}=[^\s]+", f"{name}={value:.15g}", head)
        if count != 1:
            raise ValueError(f"Unexpected parameter occurrence count: {name}")
    # Braces make parameter references explicit; the audit compares the full
    # transfer curve against the original unbraced syntax before accepting this.
    head = head.replace("cc net5 net6 cc", "cc net5 net6 {cc}")
    head = head.replace("CL net6 0 cload", "CL net6 0 {cload}")
    if "load_f" in case:
        head = head.replace("cload=10p", f"cload={case['load_f']:.15g}")
    if "supply_v" in case:
        head = head.replace("vdd VDD 0 dc=1.2", f"vdd VDD 0 dc={case['supply_v']:.15g}")
    fault = case.get("fault")
    if fault == "reverse_input_polarity":
        head = head.replace("ein1 net1 cm in 0 0.5", "ein1 net1 cm in 0 -0.5")
        head = head.replace("ein2 net2 cm in 0 -0.5", "ein2 net2 cm in 0 0.5")
    elif fault == "short_output":
        head += "Rfault net6 0 1m\n"
    elif fault == "missing_model":
        head = head.replace(str(model), str(model.with_name("deliberately_missing_model.txt")))
    elif fault is not None:
        raise ValueError(f"Unknown verification fault: {fault}")
    head += f".temp {case.get('temperature_c', 27)}\n"
    if case.get("tight_tolerances"):
        head += ".options reltol=1e-5 abstol=1e-14 vntol=1e-8\n"
    return head + f"""
.control
set numdgt=15
set wr_vecnames
op
wrdata dc.csv i(vdd)
print v(net6)
ac dec {case.get('points_per_decade', 100)} 1 10G
wrdata ac.csv v(net6)
{ac_commands('net6', 1)}
quit
.endc
.end
"""


def run_case(case, run_directory):
    dependency = verify_assets()
    directory = Path(run_directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    start = time.monotonic()
    result = {"id": case["id"], "status": "failed", "success": False, "reward": -1.0,
              "metrics": {}, "checks": {}, "case": case,
              "timestamp_utc": datetime.now(timezone.utc).isoformat(), "dependency": dependency,
              "constraints": CONSTRAINTS, "warnings": []}
    try:
        deck = render_deck(case)
        (directory / "design.cir").write_text(deck)
        executable = shutil.which("ngspice")
        if not executable:
            raise FileNotFoundError("ngspice is missing.")
        result["ngspice_version"] = subprocess.check_output([executable, "--version"], text=True, timeout=5).strip()
        command = [executable, "-n", "-D", "num_threads=1", "-b", "-o", "simulation.log", "design.cir"]
        result["command"] = command
        process = subprocess.run(command, cwd=directory, capture_output=True, text=True, timeout=30)
        (directory / "console.txt").write_text(process.stdout + process.stderr)
        log = (directory / "simulation.log").read_text()
        result["warnings"] = sorted(set(line for line in log.splitlines() if "warning" in line.lower()))
        if process.returncode != 0:
            raise RuntimeError(f"Simulator exited with code {process.returncode}.")
        rows = read_table(directory / "ac.csv", 3)
        if abs(rows[0][0] - 1) > 1e-8 or abs(rows[-1][0] / 1e10 - 1) > 1e-8:
            raise ValueError("Incomplete author AC sweep.")
        dc = read_table(directory / "dc.csv", 2)
        if len(dc) != 1:
            raise ValueError("Expected exactly one author operating point.")
        current = -dc[0][1]
        if current <= 0:
            raise ValueError("Nonpositive supply current.")
        converted = complex_response(rows)
        result["metrics"].update(gain_db=converted[0][1], gain_vv=math.hypot(rows[0][1], rows[0][2]),
                                 supply_current_a=current, power_w=current * case.get("supply_v", 1.2))
        result["metrics"].update(ac_metrics(converted))
        if not case.get("exact_release"):
            independent = ac_measurements(log)
            result["independent_metrics"] = independent
            result["crosscheck"] = compare(result["metrics"], independent)
            if not result["crosscheck"]["agrees"]:
                raise ValueError("Independent AC measurements disagree.")
        primary = score(result["metrics"], CONSTRAINTS)
        if not case.get("exact_release"):
            independent["supply_current_a"] = current
            if score(independent, CONSTRAINTS)["success"] != primary["success"]:
                raise ValueError("Independent AC measurements disagree on pass/fail.")
        result.update(primary)
        result["status"] = "ok"
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        result.update(status="failed", success=False, reward=-1.0, error=str(error))
        result["checks"] = score(result["metrics"], CONSTRAINTS)["checks"]
    result["elapsed_s"] = time.monotonic() - start
    result["files_sha256"] = {path.name: digest(path) for path in directory.iterdir() if path.is_file()}
    result["code_sha256"] = {name: digest(Path(__file__).with_name(name)) for name in
                             ("author_reference.py", "metrics.py", "crosscheck.py")}
    (directory / "result.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    return result
