"""Evaluate one candidate and retain the exact simulator inputs and outputs."""
import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analog_design.simulator import evaluate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", type=Path, default=ROOT / "tasks/fan_smc_nominal.json")
    parser.add_argument("--parameters", type=Path, help="JSON object containing permitted numeric overrides")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    overrides = json.loads(args.parameters.read_text()) if args.parameters else {}
    if not isinstance(overrides, dict):
        parser.error("--parameters must contain a JSON object")
    output = args.output or ROOT / "runs" / (datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8])
    result = evaluate(args.task, overrides, output, args.timeout)
    print(json.dumps({"run_directory": str(output.resolve()), **result}, indent=2, allow_nan=False))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
