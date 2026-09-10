"""Run the verification suite and retain every simulator input and output."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analog_design.verification import verify


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or ROOT / "runs" / ("verification_" + datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8])
    report = verify(output)
    print(json.dumps({"report": str(output.resolve() / "verification.json"),
                      "automated_checks_passed": report["automated_checks_passed"],
                      "checks": report["checks"], "training_ready": report["training_ready"],
                      "remaining_reviews": report["remaining_reviews"]}, indent=2))
    return 0 if report["automated_checks_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
