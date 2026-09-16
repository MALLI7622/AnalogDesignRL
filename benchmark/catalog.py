"""Prepare trusted amplifier domains from pinned source artifacts."""
from copy import deepcopy
import json
import math
from pathlib import Path
import re
import shutil

from analog_design.simulator import ROOT, digest
from benchmark.research.probe_amplifiers import parse_parameters
from benchmark.simulation import save_json
from benchmark.explore import rounded_parameters


PREFERRED_RELEASES = ["Fan_SMC_Pin_3", "Leung_NMCNR_Pin_3", "Leung_DFCFC1_Pin_3",
                      "Leung_DFCFC2_Pin_3", "HoiLee_AFFC_Pin_3", "Peng_ACBC_Pin_3",
                      "Peng_IAC_Pin_3", "Ramos_PFC_Pin_3", "Song_DACFC_Pin_3"]

FAMILIES = {"autockt_two_stage": "two_stage_miller",
            "Fan_SMC_Pin_3": "fan_single_miller",
            "Leung_NMCNR_Pin_3": "leung_multistage_compensation",
            "Leung_DFCFC1_Pin_3": "leung_multistage_compensation",
            "Leung_DFCFC2_Pin_3": "leung_multistage_compensation",
            "HoiLee_AFFC_Pin_3": "lee_active_feedback",
            "Peng_ACBC_Pin_3": "peng_ac_boosting",
            "Peng_IAC_Pin_3": "peng_impedance_adapting",
            "Ramos_PFC_Pin_3": "ramos_positive_feedback",
            "Song_DACFC_Pin_3": "song_dual_active_capacitive_feedback"}


def parameter_rule(name, value):
    integer = "_M_" in name
    role = "compensation capacitance" if name.startswith("CAPACITOR_") else "bias reference current"
    if integer:
        role = re.search(r"_M_(.*?)_(?:P|N)MOS", name)[1] + " stage transistor multiplicity"
    return {"min": max(1, math.ceil(value / 4)) if integer else value / 4,
            "max": min(512, max(4, round(value * 4))) if integer else value * 4,
            "unit": "multiplicity" if integer else "F" if name.startswith("CAPACITOR_") else "A",
            "sampling_scale": "log", "role": role,
            **({"integer": True} if integer else {})}


def prepare_catalog(output):
    output = Path(output)
    metadata = json.loads((ROOT / "benchmark/research/paper_candidates.json").read_text())
    candidates = {entry["release_name"]: entry for entry in metadata["candidates"]}
    template = json.loads((ROOT / "tasks/fan_smc_sizing.json").read_text())
    entries = []
    for release in PREFERRED_RELEASES:
        candidate = candidates[release]
        source_netlist = ROOT / candidate["netlist_path"]
        source_parameters = ROOT / candidate["parameters_path"]
        if digest(source_netlist) != candidate["netlist_sha256"] or digest(source_parameters) != candidate["parameters_sha256"]:
            raise ValueError("Reviewed source artifacts changed: " + release)
        directory = ROOT / "circuits" / ("benchmark_" + release.lower())
        directory.mkdir(parents=True, exist_ok=True)
        for source, filename in ((source_netlist, "netlist.spice"), (source_parameters, "reference.params")):
            destination = directory / filename
            if destination.exists() and digest(source) != digest(destination):
                raise ValueError("Refusing to replace changed circuit: " + str(destination))
            if not destination.exists():
                shutil.copyfile(source, destination)
        values = parse_parameters(source_parameters)
        task = deepcopy(template)
        task.update(id="domain_" + release.lower(), circuit_directory=str(directory.relative_to(ROOT)),
                    subcircuit=candidate["subcircuit"], parameters={}, initial_parameters={})
        controls = [name for name in values if name.startswith(("CAPACITOR_", "CURRENT_")) or re.search(r"_M_gm[123]_", name)]
        for name in sorted(controls):
            rule = parameter_rule(name, values[name])
            task["parameters"][name] = rule
            task["initial_parameters"][name] = int(values[name]) if rule.get("integer") else values[name]
        task["initial_parameters"] = rounded_parameters(task, task["initial_parameters"])
        if release == "Peng_IAC_Pin_3":
            # Paper variables: Cm=CAPACITOR_0, Ca=CAPACITOR_1,
            # Ra=RESISTOR_0. The initial 2 pF Cm ceiling excluded the
            # compensation region suggested by the paper's pole-separation
            # analysis; feasibility is still established by simulation.
            task["parameters"]["CAPACITOR_0"]["max"] = 10e-12
            task["parameters"]["CAPACITOR_0"]["role"] = "Miller compensation capacitance Cm"
            task["parameters"]["CAPACITOR_1"]["role"] = "impedance-adapting capacitance Ca"
            task["parameters"]["RESISTOR_0"] = {
                "min": values["RESISTOR_0"] / 4,
                "max": values["RESISTOR_0"] * 4,
                "unit": "ohm", "sampling_scale": "log",
                "role": "compensation impedance Ra",
            }
            task["initial_parameters"]["RESISTOR_0"] = values["RESISTOR_0"]
        task["conditions"].update(common_mode_v=values["VCM"], load_f=values["CLOAD"])
        task["transient"].update(low_v=values["VCM"], high_v=values["VCM"] + 0.2)
        # These are domain floors, not numerical claims about the source paper.
        task["constraints"]["unity_gain_hz"]["min"] = 1e5
        task["constraints"]["settling_rise_s"]["max"] = 1e-5
        task["constraints"]["settling_fall_s"]["max"] = 1e-5
        source_record = {"repository": metadata["repository"], "commit": metadata["repository_commit"],
                         "license": metadata["license"], "release": candidate,
                         "adaptations": ["Netlist and original fixed parameter file copied unchanged.",
                                         "Project-authored follower/AC testbench at 1.8 V, 27 C, SKY130 TT; released CLOAD/VCM retained.",
                                         "Project-selected editable bounds and simulation-guided targets; no recovery of paper performance.",
                                         "Large-signal settling uses a 20 mV absolute band for a 200 mV step (10%)."],
                         "training_approved": False}
        if release == "Peng_IAC_Pin_3":
            source_record["adaptations"].append(
                "IAC Cm search range expanded to 0.125-10 pF and Ra exposed over 187.5 kohm-3 Mohm after source-equation review; the 60 degree phase-margin floor is unchanged.")
        save_json(directory / "source.json", source_record)
        description = candidate["notes"]
        if release == "Leung_NMCNR_Pin_3":
            description = description.replace(
                "; DFC high-impedance DC node robustness is not established by nominal sizing.", ".")
        if release == "Song_DACFC_Pin_3":
            description += (" These circuit roles follow the released AnalogGym schematic and netlist. "
                            "Original-paper review was limited to the abstract and author bibliography; "
                            "the original full text and original schematic were unavailable.")
        context = {"topology": release, "topology_family": FAMILIES[release],
                   "paper": {key: value for key, value in candidate["paper"].items()
                             if key in {"title", "authors", "year", "doi", "source_url", "figure"}},
                   "circuit_description": description,
                   "netlist": source_netlist.read_text(),
                   "fixed_parameters": {name: value for name, value in values.items()
                                        if name not in task["parameters"] and name not in {"CLOAD", "VCM"}},
                   "scope": "Nominal SKY130 amplifier sizing; source architecture adapted by AnalogGym. Targets are project-generated."}
        task["design_context"] = context
        entry = {"name": release, "family": FAMILIES[release], "task": task,
                 "public_default": dict(task["initial_parameters"]), "source": source_record}
        entries.append(entry)
    task = json.loads((ROOT / "tasks/autockt_two_stage_sizing.json").read_text())
    defaults = parse_parameters(ROOT / "circuits/autockt_two_stage/reference.params")
    controls = {"CCOMP": (1e-12, 80e-12, "F", "Miller compensation capacitance"),
                "IBIAS": (7.5e-6, 120e-6, "A", "bias reference current"),
                "W_IN": (2.5, 40, "um", "matched differential input-pair width"),
                "W_LOAD": (5, 80, "um", "matched first-stage PMOS current-mirror width"),
                "W_GM": (20, 160, "um", "second-stage PMOS gain-device width"),
                "W_LOAD2": (5, 80, "um", "second-stage NMOS current-source width")}
    task["parameters"] = {name: {"min": low, "max": high, "unit": unit,
                                  "sampling_scale": "log", "role": role}
                          for name, (low, high, unit, role) in controls.items()}
    task["initial_parameters"] = {name: defaults[name] for name in controls}
    task["initial_parameters"] = rounded_parameters(task, task["initial_parameters"])
    task["id"] = "domain_autockt_two_stage"
    source = json.loads((ROOT / "circuits/autockt_two_stage/source.json").read_text())
    task["design_context"] = {"topology": "autockt_two_stage", "topology_family": FAMILIES["autockt_two_stage"],
                              "paper": {"title": source["paper"], "source_url": source["source_url"], "figure": "6"},
                              "circuit_description": "Two-stage amplifier with an NMOS differential pair, PMOS current-mirror load, PMOS second stage, NMOS current-source load, and Miller compensation.",
                              "netlist": (ROOT / "circuits/autockt_two_stage/netlist.spice").read_text(),
                              "fixed_parameters": {name: value for name, value in defaults.items() if name not in controls},
                              "scope": "Project's manual Figure 6 reconstruction adapted from 45 nm to SKY130. Sizes and targets are project-generated."}
    entries.insert(0, {"name": "autockt_two_stage", "family": FAMILIES["autockt_two_stage"],
                       "task": task, "public_default": dict(task["initial_parameters"]), "source": source})
    save_json(output, {"schema_version": 1, "status": "domains_prepared_not_qualified", "entries": entries})
    return entries


if __name__ == "__main__":
    prepare_catalog(ROOT / "benchmark/domains.json")
