"""Select a diverse curriculum using measured baseline outcomes, never hints alone."""
import argparse
from collections import Counter
import json
from pathlib import Path

from benchmark.baselines import METHODS
from benchmark.candidates import distance
from benchmark.simulation import canonical_hash, save_json


def observed_level(methods, minimum_trials=10, hard_trials=20):
    hard_trials = max(20, hard_trials)
    if set(methods) != set(METHODS) or any(stats["completed_trials"] < minimum_trials for stats in methods.values()):
        return "incomplete"
    if any(stats["success_at"]["30"]["rate"] >= 0.8 and
           stats["median_calls_censored_at_30"] <= 10 for stats in methods.values()):
        return "easy"
    if all(stats["success_at"]["30"]["rate"] <= 0.2 for stats in methods.values()):
        return "hard" if all(stats["completed_trials"] >= hard_trials for stats in methods.values()) else "hard_candidate"
    return "medium"


def load_observations(candidate_directory, calibration_directories, *, minimum_trials=10, hard_trials=20):
    candidate_directory = Path(candidate_directory)
    index = json.loads((candidate_directory / "index.json").read_text())
    candidates = {entry["id"]: json.loads((candidate_directory / entry["path"]).read_text()) for entry in index["tasks"]}
    observations = {}
    for directory in calibration_directories:
        directory = Path(directory)
        summary = json.loads((directory / "summary.json").read_text())
        for row in summary["tasks"]:
            if row["id"] not in candidates or not row["admitted"]:
                continue
            level = observed_level(row["methods"], minimum_trials, hard_trials)
            if level == "incomplete":
                continue
            observations[row["id"]] = {"candidate": candidates[row["id"]], "observation": row,
                                        "level": level, "calibration_directory": str(directory)}
    return list(observations.values())


def choose(observations, counts, *, per_topology_limit=35, minimum_distance=0.025,
           allow_hard_candidates=False):
    selected, rejection_counts = [], Counter()
    topology_counts, level_counts = Counter(), Counter()
    seen_refs, seen_groups, seen_signatures = set(), set(), set()
    reference_vectors = {}
    available = Counter(row["level"] for row in observations)
    for level in ("hard", "medium", "easy"):
        pool = [row for row in observations if row["level"] == level or
                level == "hard" and allow_hard_candidates and row["level"] == "hard_candidate"]
        while level_counts[level] < counts[level] and pool:
            def priority(row):
                candidate = row["candidate"]
                existing = reference_vectors.get(candidate["topology"], [])
                separation = min((distance(candidate["task"], candidate["reference"], vector) for vector in existing), default=1)
                return (topology_counts[candidate["topology"]], -separation,
                        candidate["empirical_pool_feasible_count"] if level == "hard" else -candidate["empirical_pool_feasible_count"],
                        candidate["id"])
            row = min(pool, key=priority)
            pool.remove(row)
            candidate = row["candidate"]
            topology = candidate["topology"]
            ref_key = (topology, canonical_hash(candidate["reference"]))
            signature = (topology, candidate["empirical_solution_signature"])
            if topology_counts[topology] >= per_topology_limit:
                rejection_counts["topology_cap"] += 1
                continue
            if ref_key in seen_refs:
                rejection_counts["duplicate_reference"] += 1
                continue
            if candidate["requirement_group"] in seen_groups:
                rejection_counts["duplicate_requirement_group"] += 1
                continue
            if signature in seen_signatures:
                rejection_counts["same_characterization_solution_set"] += 1
                continue
            if any(distance(candidate["task"], candidate["reference"], vector) < minimum_distance
                   for vector in reference_vectors.get(topology, [])):
                rejection_counts["reference_too_close"] += 1
                continue
            selected.append({**row, "selected_level": level})
            topology_counts[topology] += 1
            level_counts[level] += 1
            seen_refs.add(ref_key)
            seen_groups.add(candidate["requirement_group"])
            seen_signatures.add(signature)
            reference_vectors.setdefault(topology, []).append(candidate["reference"])
    return selected, {"requested": counts, "available_levels": dict(available), "selected_levels": dict(level_counts),
                      "selected_topologies": dict(topology_counts), "rejected_by_selection": dict(rejection_counts),
                      "complete": all(level_counts[level] == count for level, count in counts.items()),
                      "minimum_normalized_reference_distance": minimum_distance,
                      "characterization_signature_note": "Rejects identical measured feasible subsets within a topology. The nonuniform characterization pool does not estimate solver success probability."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--counts", nargs=3, type=int, default=[100, 125, 25], metavar=("EASY", "MEDIUM", "HARD"))
    parser.add_argument("--minimum-trials", type=int, default=10)
    parser.add_argument("--hard-trials", type=int, default=20)
    parser.add_argument("--per-topology-limit", type=int, default=35)
    parser.add_argument("--allow-hard-candidates", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Selection output must be new.")
    rows = load_observations(args.candidates, args.calibration, minimum_trials=args.minimum_trials, hard_trials=args.hard_trials)
    selected, report = choose(rows, dict(zip(("easy", "medium", "hard"), args.counts)),
                              per_topology_limit=args.per_topology_limit, allow_hard_candidates=args.allow_hard_candidates)
    save_json(args.output, {"status": "selection_complete" if report["complete"] else "selection_partial",
                            "report": report, "ids": [row["candidate"]["id"] for row in selected],
                            "tasks": [{"id": row["candidate"]["id"], "difficulty": row["selected_level"],
                                       "observed_level": row["level"], "topology": row["candidate"]["topology"],
                                       "calibration_directory": row["calibration_directory"]} for row in selected]})
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
