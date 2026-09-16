"""A bounded physical-factor search baseline using only public observations.

The initial sweep checks simple half/double repairs around the supplied starting
design. This is a useful control for a curriculum made from parameter changes;
distance from a reference and failure of another optimizer do not establish
hardness. No reference solution, circuit file, or construction metadata is read.
"""
from copy import deepcopy
import math
import random

from benchmark.baselines import _Domain


METHOD = "engineering_sweep"


def run_engineering_sweep(task, evaluate_action, seed, budget=30):
    """Run an anchored engineering sweep, followed by feedback-driven search.

    The initial design is charged first. In seeded order, each positive-domain
    control is individually multiplied by 0.5 and 2, anchored to that original
    design. Bounds, integer rounding and simulator numeric serialization apply;
    duplicate actions are skipped. With enough budget, an exact single-control
    half/double repair is therefore tested within at most ``2*N + 1`` calls.

    Independently improving single-control moves are then combined in one
    trial. Remaining calls use incumbent-centered coordinate sweeps at factors
    1.5 and 2/3, reducing the log step after a stalled sweep and restarting after
    two stalls. Nonpositive domains use additive quarter-range steps instead of
    multiplication (scaled to the log factor); bounded random restarts use the
    same domain sampling rules as the existing log/coordinate baselines.

    Every callback, including an invalid result, consumes budget. Callback
    exceptions propagate. Returned fields match ``benchmark.baselines.run_search``.
    """
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer.")
    maximum = task.get("max_evaluations", budget)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 1
           for value in (budget, maximum)):
        raise ValueError("Evaluation budgets must be positive integers.")
    if not callable(evaluate_action):
        raise ValueError("evaluate_action must be callable.")
    limit = min(budget, maximum)
    domain = _Domain(task, logarithmic=True)
    rng = random.Random(seed)
    seen, trajectory = set(), []
    finite_points = domain.finite_points()
    exhausted = False

    def key(action):
        return tuple(action[name] for name in domain.names)

    def finished():
        return len(trajectory) >= limit or bool(trajectory and trajectory[-1]["success"])

    def submit(action):
        action = domain.canonical(action)
        identity = key(action)
        if finished() or identity in seen:
            return None
        seen.add(identity)
        result = evaluate_action(deepcopy(action))
        if not isinstance(result, dict):
            raise ValueError("The evaluator must return a result dictionary.")
        status = result.get("status", "failed")
        success = status == "ok" and result.get("success") is True
        reward = 1.0 if success else result.get("reward", -1.0)
        if (status != "ok" or isinstance(reward, bool) or not isinstance(reward, (int, float))
                or not math.isfinite(reward) or (not success and not -1 <= reward <= 0)):
            reward = -1.0
        observation = {"evaluation": len(trajectory) + 1, "parameters": action,
                       "status": status, "success": success, "reward": float(reward),
                       "metrics": deepcopy(result.get("metrics", {})),
                       "checks": deepcopy(result.get("checks", {}))}
        trajectory.append(observation)
        return observation

    def factor_action(center, name, factor):
        low, high, integer, _ = domain.rules[name]
        value = center[name]
        if low > 0:
            value *= factor
        else:
            value += math.log(factor) / math.log(2) * 0.25 * (high - low)
        value = min(high, max(low, value))
        if integer:
            value = min(high, max(low, round(value)))
        return domain.canonical({**center, name: value})

    def random_unique():
        for _ in range(256):
            candidate = domain.sample(rng)
            if key(candidate) not in seen:
                return candidate
        if finite_points is not None:
            remaining = [action for action in finite_points if key(action) not in seen]
            if remaining:
                return deepcopy(rng.choice(remaining))
        return None

    initial = submit(domain.initial)
    axis_improvements = {}
    names = list(domain.names)
    rng.shuffle(names)
    for name in names:
        if finished():
            break
        best = initial
        factors = [0.5, 2.0]
        rng.shuffle(factors)
        for factor in factors:
            observation = submit(factor_action(domain.initial, name, factor))
            if observation is not None and observation["reward"] > best["reward"]:
                best = observation
            if finished():
                break
        if best["reward"] > initial["reward"]:
            axis_improvements[name] = best["parameters"][name]

    if not finished() and len(axis_improvements) >= 2:
        submit({**domain.initial, **axis_improvements})
    incumbent = max(trajectory, key=lambda item: (item["success"], item["reward"]))

    log_step, stalled = math.log(1.5), 0
    for _ in range(max(100, limit * 20)):
        if finished():
            break
        improved = False
        calls_before = len(trajectory)
        names = list(domain.names)
        rng.shuffle(names)
        for name in names:
            if finished():
                break
            center, best = incumbent["parameters"], incumbent
            factors = [math.exp(-log_step), math.exp(log_step)]
            rng.shuffle(factors)
            for factor in factors:
                observation = submit(factor_action(center, name, factor))
                if observation is not None and observation["reward"] > best["reward"]:
                    best = observation
                if finished():
                    break
            if best["reward"] > incumbent["reward"]:
                incumbent, improved = best, True
        if improved:
            stalled = 0
        else:
            stalled += 1
            log_step *= 0.5
        if finished():
            break
        if stalled >= 2 or len(trajectory) == calls_before:
            candidate = random_unique()
            if candidate is None:
                exhausted = True
                break
            incumbent = submit(candidate)
            log_step, stalled = math.log(1.5), 0
    else:
        exhausted = True

    best = max(trajectory, key=lambda item: (item["success"], item["reward"]))
    success = any(item["success"] for item in trajectory)
    return {"task_id": task.get("id"), "method": METHOD, "seed": seed,
            "max_evaluations": limit, "evaluations_used": len(trajectory),
            "success": success, "first_success_evaluation": next(
                (item["evaluation"] for item in trajectory if item["success"]), None),
            "stop_reason": "success" if success else "no_unique_candidate" if exhausted else "budget_exhausted",
            "best_parameters": deepcopy(best["parameters"]), "best_reward": best["reward"],
            "trajectory": trajectory}
