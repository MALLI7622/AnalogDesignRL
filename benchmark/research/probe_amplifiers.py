"""Explore released amplifier seeds with the existing evaluator; not final tasks."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import json
from pathlib import Path
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from analog_design.simulator import evaluate, digest


def spice_number(value):
    match = re.fullmatch(r"([+-]?[\d.]+(?:[eE][+-]?\d+)?)([a-zA-Z]*)", value)
    if not match:
        raise ValueError(value)
    factors = {"": 1, "f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6,
               "m": 1e-3, "k": 1e3, "meg": 1e6, "g": 1e9}
    return float(match[1]) * factors[match[2].lower()]


def parse_parameters(path):
    return {key: spice_number(value) for key, value in
            re.findall(r"([\w]+)\s*=\s*([^\s]+)", Path(path).read_text())}


def main():
    output = Path(sys.argv[1]).resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = ROOT / "external/AnalogGym/AnalogGym/Amplifier"
    template = json.loads((ROOT / "tasks/fan_smc_sizing.json").read_text())
    jobs = []
    for netlist in sorted((source / "spice_netlist").iterdir()):
        if not netlist.is_file():
            continue
        params = source / "design_variables" / netlist.name
        match = re.search(r"(?im)^\.subckt\s+(\S+)\s+(.*)", netlist.read_text())
        if not match:
            print(json.dumps({"circuit": netlist.name, "skipped": "No supported .subckt"}), flush=True)
            continue
        circuit = output / "circuits" / netlist.name
        circuit.mkdir(parents=True)
        shutil.copyfile(netlist, circuit / "netlist.spice")
        shutil.copyfile(params, circuit / "reference.params")
        values = parse_parameters(params)
        task = deepcopy(template)
        task.update(id="probe_" + netlist.name, subcircuit=match[1],
                    circuit_directory=str(circuit.relative_to(ROOT)),
                    initial_parameters={}, parameters={})
        task["conditions"].update(common_mode_v=values["VCM"], load_f=values["CLOAD"])
        task["transient"].update(low_v=values["VCM"], high_v=values["VCM"] + 0.2)
        task_path = circuit / "probe.json"
        task_path.write_text(json.dumps(task, indent=2) + "\n")
        jobs.append((netlist.name, task_path, digest(netlist), values))
    summary = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = {pool.submit(evaluate, path, {}, output / "evaluations" / name, 30):
                   (name, sha, values) for name, path, sha, values in jobs}
        for future in as_completed(pending):
            name, sha, values = pending[future]
            result = future.result()
            item = {"circuit": name, "netlist_sha256": sha, "load_f": values["CLOAD"],
                    "common_mode_v": values["VCM"],
                    **{key: result[key] for key in ("status", "success", "metrics", "elapsed_s", "warnings")},
                    "error": result.get("error")}
            summary.append(item)
            print(json.dumps(item), flush=True)
            (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    main()
