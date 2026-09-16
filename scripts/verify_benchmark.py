"""Independently verify a benchmark release, optionally with fresh simulations."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmark.verify_release import verify_release


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=ROOT / "datasets/analog_benchmark_250_v1/index.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--simulate", action="store_true", help="Freshly evaluate each start, reference, and refined reference.")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--simulator-timeout", type=float, default=30)
    parser.add_argument("--domains", type=Path)
    parser.add_argument("--minimum-reference-distance", type=float, default=0.025,
                        help="Minimum normalized log RMS reference separation within a topology; 0 records distances without a separation gate.")
    args = parser.parse_args()
    output = args.output or ROOT / "runs" / ("benchmark_verification_" +
        datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8])
    try:
        result = verify_release(args.index, output, expected_count=args.expected_count,
                                simulate=args.simulate, workers=args.workers,
                                simulator_timeout=args.simulator_timeout, domains_path=args.domains,
                                minimum_reference_distance=args.minimum_reference_distance,
                                progress=lambda event: print(json.dumps(event), flush=True))
    except (ValueError, OSError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}))
        return 2
    print(json.dumps({"status": result["status"], "publication_ready": result["publication_ready"],
                      "scope": result["scope"], "task_count": result["task_count"],
                      "error_count": len(result["errors"]), "report": str(output / "verification.json")}))
    return 0 if result["status"] == "verified" else 1


if __name__ == "__main__":
    sys.exit(main())
