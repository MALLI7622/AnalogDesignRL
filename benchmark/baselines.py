"""Bounded, reproducible search baselines with no access to reference solutions.

The evaluator callback receives full absolute parameter values. Every callback
invocation, including the initial design and failures, consumes one evaluation.
Duplicate proposals are skipped before evaluation; proposal retries are bounded.
"""
from copy import deepcopy
import itertools
import math
import random


METHODS = ("uniform_linear", "uniform_log", "coordinate_search", "engineering_sweep")
_LOG_UNITS = {"F", "A", "um", "m", "pF", "nF", "uA", "mA", "nm"}


class _Domain:
    def __init__(self, task, logarithmic):
        self.names = sorted(task["parameters"])
        if not self.names or set(task["initial_parameters"]) != set(self.names):
            raise ValueError("A task needs complete initial values for every parameter.")
        self.rules = {}
        for name in self.names:
            rule = task["parameters"][name]
            low, high = rule["min"], rule["max"]
            if any(isinstance(x, bool) or not isinstance(x, (int, float)) or
                   not math.isfinite(x) for x in (low, high)) or low > high:
                raise ValueError(f"Invalid parameter bounds: {name}")
            integer = bool(rule.get("integer", False))
            if integer:
                low, high = math.ceil(low), math.floor(high)
                if low > high:
                    raise ValueError(f"No integer within bounds: {name}")
            scale = rule.get("sampling_scale")
            log = (logarithmic and not integer and low > 0
                   and (scale == "log" or (scale is None and rule.get("unit") in _LOG_UNITS)))
            self.rules[name] = (low, high, integer, log)
        self.initial = self.canonical(task["initial_parameters"])

    def canonical(self, values):
        action = {}
        for name in self.names:
            value = values[name]
            low, high, integer, _ = self.rules[name]
            if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                    not math.isfinite(value) or not low <= value <= high or
                    (integer and value != int(value))):
                raise ValueError(f"Invalid parameter value: {name}")
            # Match the simulator's 15-significant-digit deck serialization so
            # formatting-only differences do not create extra unique trials.
            action[name] = int(value) if integer else min(high, max(low, float(f"{value:.15g}")))
        return action

    def encode(self, action):
        coordinates = {}
        for name in self.names:
            low, high, _, log = self.rules[name]
            if low == high:
                coordinates[name] = 0.0
            elif log:
                coordinates[name] = (math.log(action[name]) - math.log(low)) / (math.log(high) - math.log(low))
            else:
                coordinates[name] = (action[name] - low) / (high - low)
        return coordinates

    def decode(self, coordinates):
        action = {}
        for name in self.names:
            low, high, integer, log = self.rules[name]
            coordinate = min(1.0, max(0.0, coordinates[name]))
            value = (math.exp(math.log(low) + coordinate * (math.log(high) - math.log(low)))
                     if log else low + coordinate * (high - low))
            action[name] = min(high, max(low, round(value) if integer else value))
        return self.canonical(action)

    def sample(self, rng):
        values = {}
        for name in self.names:
            low, high, integer, log = self.rules[name]
            if integer:
                values[name] = rng.randint(low, high)
            elif log:
                values[name] = min(high, max(low, math.exp(rng.uniform(math.log(low), math.log(high)))))
            else:
                values[name] = rng.uniform(low, high)
        return self.canonical(values)

    def finite_points(self):
        """Enumerate a small fully discrete domain, if available, for exhaustion."""
        axes, size = [], 1
        for name in self.names:
            low, high, integer, _ = self.rules[name]
            if low == high:
                values = [low]
            elif integer:
                if high - low + 1 > 10000:
                    return None
                values = range(low, high + 1)
            else:
                return None
            size *= len(values)
            if size > 10000:
                return None
            axes.append(values)
        return [self.canonical(dict(zip(self.names, point))) for point in itertools.product(*axes)]


def run_search(task, evaluate_action, method, seed, budget=30):
    """Run one search episode and return only public observations and actions.

    ``uniform_log`` honors explicit log sampling for positive continuous values;
    without an explicit scale, capacitance, current and geometry units use log
    coordinates. Integer multiplicities remain uniform discrete. The adaptive
    method uses those same coordinates, tries both directions per coordinate,
    halves its step after a sweep without improvement, and restarts after two
    stalled sweeps. No callback exceptions are converted into model rewards:
    infrastructure failures abort the caller's run, as in the remote worker.
    """
    if method not in METHODS:
        raise ValueError(f"Unknown baseline method: {method}")
    if method == "engineering_sweep":
        from benchmark.engineering_search import run_engineering_sweep
        return run_engineering_sweep(task, evaluate_action, seed, budget=budget)
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer.")
    maximum = task.get("max_evaluations", budget)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 1
           for value in (budget, maximum)):
        raise ValueError("Evaluation budgets must be positive integers.")
    if not callable(evaluate_action):
        raise ValueError("evaluate_action must be callable.")
    limit = min(budget, maximum)
    domain = _Domain(task, logarithmic=method != "uniform_linear")
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
        entry = {"evaluation": len(trajectory) + 1, "parameters": action,
                 "status": status, "success": success, "reward": float(reward),
                 "metrics": deepcopy(result.get("metrics", {})),
                 "checks": deepcopy(result.get("checks", {}))}
        trajectory.append(entry)
        return entry

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

    incumbent = submit(domain.initial)
    if method.startswith("uniform_"):
        while not finished():
            candidate = random_unique()
            if candidate is None:
                exhausted = True
                break
            submit(candidate)
    else:
        radius, stalled = 0.25, 0
        # Even fully rounded-away moves have a fixed upper bound on work.
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
                center = domain.encode(incumbent["parameters"])
                best = incumbent
                directions = [-1, 1]
                rng.shuffle(directions)
                for direction in directions:
                    proposed = {**center, name: center[name] + direction * radius}
                    candidate = domain.decode(proposed)
                    observation = submit(candidate)
                    if observation is not None and observation["reward"] > best["reward"]:
                        best = observation
                    if finished():
                        break
                if best["reward"] > incumbent["reward"]:
                    incumbent = best
                    improved = True
            if improved:
                stalled = 0
            else:
                stalled += 1
                radius *= 0.5
            if finished():
                break
            if stalled >= 2 or len(trajectory) == calls_before:
                candidate = random_unique()
                if candidate is None:
                    exhausted = True
                    break
                incumbent = submit(candidate)
                radius, stalled = 0.25, 0
        else:
            exhausted = True

    best = max(trajectory, key=lambda item: (item["success"], item["reward"]))
    success = any(item["success"] for item in trajectory)
    return {"task_id": task.get("id"), "method": method, "seed": seed,
            "max_evaluations": limit, "evaluations_used": len(trajectory),
            "success": success, "first_success_evaluation": next(
                (item["evaluation"] for item in trajectory if item["success"]), None),
            "stop_reason": "success" if success else "no_unique_candidate" if exhausted else "budget_exhausted",
            "best_parameters": deepcopy(best["parameters"]), "best_reward": best["reward"],
            "trajectory": trajectory}
