"""Validate model proposals before building an executable task from a trusted template."""
from copy import deepcopy
import hashlib
import json
import math
import re

from .model_clients import strict_json
from .simulator import ROOT, parameters_for


TEMPLATES = {
    "fan_smc": "tasks/fan_smc_sizing.json",
    "autockt_two_stage": "tasks/autockt_two_stage_sizing.json",
}


def load_template(name):
    if name not in TEMPLATES:
        raise ValueError(f"Unsupported template: {name}")
    task = strict_json((ROOT / TEMPLATES[name]).read_text())
    nominal = strict_json((ROOT / TEMPLATES[name].replace("_sizing", "_nominal")).read_text())
    return task, nominal["initial_parameters"]


def object_schema(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def proposal_schema(template, source_ids):
    parameters = object_schema({name: {"type": "integer" if rule.get("integer") else "number",
                                       "minimum": rule["min"], "maximum": rule["max"]}
                                for name, rule in template["parameters"].items()})
    constraints = object_schema({metric: object_schema({direction: {
        "type": "number", "minimum": target if direction == "min" else 0,
        **({"maximum": target} if direction == "max" else {})}
        for direction, target in limits.items()}) for metric, limits in template["constraints"].items()})
    return object_schema({
        "rationale": {"type": "string", "minLength": 20, "maxLength": 2000},
        "initial_parameters": parameters,
        "reference_parameters": deepcopy(parameters),
        "constraints": constraints,
        "source_evidence": {"type": "array", "minItems": 1, "maxItems": 4,
                            "items": object_schema({
                                "source_id": {"type": "string", "enum": list(source_ids)},
                                "quote": {"type": "string", "minLength": 12, "maxLength": 300},
                                "relevance": {"type": "string", "minLength": 10, "maxLength": 600}})},
        "assumptions": {"type": "array", "minItems": 1, "maxItems": 8,
                        "items": {"type": "string", "minLength": 5, "maxLength": 500}},
    })


def validate_schema(value, schema, path="proposal"):
    """Validate the small schema subset emitted above, including JSON number edge cases."""
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{path} must be an object.")
        expected = set(schema["properties"])
        if set(value) != expected:
            raise ValueError(f"{path}: missing {sorted(expected - set(value))}; unexpected {sorted(set(value) - expected)}.")
        for key, child in value.items():
            validate_schema(child, schema["properties"][key], path + "." + key)
    elif kind == "array":
        if not isinstance(value, list) or not schema["minItems"] <= len(value) <= schema["maxItems"]:
            raise ValueError(f"{path} must have {schema['minItems']}–{schema['maxItems']} items.")
        for index, child in enumerate(value):
            validate_schema(child, schema["items"], f"{path}[{index}]")
    elif kind == "string":
        if not isinstance(value, str) or not schema.get("minLength", 1) <= len(value.strip()) <= schema.get("maxLength", 2000):
            raise ValueError(f"{path} must be nonempty text within the schema length limits.")
        if "enum" in schema and value not in schema["enum"]:
            raise ValueError(f"{path} must name a supplied source.")
    elif kind in {"number", "integer"}:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{path} must be a finite number.")
        if kind == "integer" and value != int(value):
            raise ValueError(f"{path} must be an integer.")
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            raise ValueError(f"{path} is outside permitted limits; requirements cannot be weakened.")
    else:
        raise ValueError(f"Unsupported schema type: {kind}")


def parse_proposal(text):
    text = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    return strict_json(text)


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def compile_proposal(proposal, template, template_name, sources):
    schema = proposal_schema(template, sources)
    validate_schema(proposal, schema)
    for evidence in proposal["source_evidence"]:
        normalized = lambda text: " ".join(text.split())
        if normalized(evidence["quote"]) not in normalized(sources[evidence["source_id"]]["text"]):
            raise ValueError(f"Quoted evidence was not found in source {evidence['source_id']}.")
    for limits in proposal["constraints"].values():
        if any(value <= 0 for value in limits.values()):
            raise ValueError("All constraint targets must be positive.")
    # Start from trusted data; never interpolate model-written netlists, paths, or conditions.
    task = deepcopy(template)
    task["initial_parameters"] = parameters_for(template, proposal["initial_parameters"])
    reference = parameters_for(template, proposal["reference_parameters"])
    task["constraints"] = deepcopy(proposal["constraints"])
    # Normalize integral floats so formatting cannot defeat duplicate detection.
    def normalize(value):
        if isinstance(value, dict):
            return {key: normalize(child) for key, child in value.items()}
        if not isinstance(value, bool) and isinstance(value, (int, float)) and value == int(value):
            return int(value)
        return value

    task = normalize(task)
    reference = normalize(reference)
    identity = {key: task[key] for key in ("circuit_directory", "subcircuit", "conditions", "parameters",
                                          "ac", "transient", "constraints", "max_evaluations")}
    group_hash = canonical_hash(identity)  # All starting points for one requirement set stay together.
    fingerprint = canonical_hash({**identity, "initial_parameters": task["initial_parameters"]})
    task["id"] = template_name + "_ai_" + fingerprint[:20]
    task["purpose"] = "Adjust the permitted circuit parameters to meet every listed simulation requirement."
    if task["initial_parameters"] == reference:
        raise ValueError("Starting and reference parameters must differ.")
    return task, reference, fingerprint, group_hash
