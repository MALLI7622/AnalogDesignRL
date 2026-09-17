"""Metric extraction with explicit failures for incomplete simulation data."""
import math
from pathlib import Path


def read_table(path, columns):
    rows = []
    with Path(path).open() as stream:
        if next(stream, None) is None:  # ngspice wr_vecnames header
            raise ValueError(f"Empty simulation output: {path}")
        for line in stream:
            if not line.strip():
                continue
            row = [float(value) for value in line.split()]
            if len(row) != columns or not all(math.isfinite(value) for value in row):
                raise ValueError(f"Invalid row in {path}")
            rows.append(row)
    if not rows:
        raise ValueError(f"Empty simulation output: {path}")
    return rows


def ac_metrics(rows):
    if len(rows) < 2 or rows[0][0] <= 0:
        raise ValueError("AC sweep is empty or has invalid frequencies.")
    if any(b[0] <= a[0] for a, b in zip(rows, rows[1:])):
        raise ValueError("AC frequencies must increase strictly.")
    if rows[0][1] <= 0 or abs(rows[0][2]) > math.pi / 4:
        raise ValueError("Expected positive low-frequency gain and non-inverting phase.")
    if rows[-1][1] >= 0:
        raise ValueError("AC sweep must extend below unity gain.")
    for index, row in enumerate(rows[:-1]):
        if row[1] == 0 and (index == 0 or rows[index - 1][1] <= 0 or rows[index + 1][1] >= 0):
            raise ValueError("Ambiguous or upward zero-dB crossing.")
    crossings = []
    for first, second in zip(rows, rows[1:]):
        if first[1] > 0 >= second[1]:
            fraction = first[1] / (first[1] - second[1])
            frequency = math.exp(math.log(first[0]) + fraction * math.log(second[0] / first[0]))
            phase = first[2] + fraction * (second[2] - first[2])
            crossings.append((frequency, 180 + math.degrees(phase)))
        elif first[1] < 0 <= second[1]:
            raise ValueError("Multiple unity crossings: stability requires further analysis.")
    if len(crossings) != 1:
        raise ValueError("Expected one downward unity-gain crossing in the measured band.")
    frequency, margin = crossings[0]
    return {"gain_db": rows[0][1], "unity_gain_hz": frequency, "phase_margin_deg": margin}


def settling_time(rows, start, end, target, tolerance, minimum_hold):
    segment = [row for row in rows if start <= row[0] < end]
    if len(segment) < 2:
        raise ValueError("Insufficient transient samples.")
    last_bad = -1
    for index, row in enumerate(segment):
        if abs(row[2] - target) > tolerance:
            last_bad = index
    first_good = last_bad + 1
    if first_good >= len(segment):
        raise ValueError("Transient response does not settle within the observation window.")
    settled_at = segment[first_good][0]
    if segment[-1][0] - settled_at < minimum_hold:
        raise ValueError("Transient response has insufficient time settled inside the tolerance.")
    return max(0.0, settled_at - start)


def transient_metrics(rows, test):
    stop = test["stop_s"]
    rise = test["rise_start_s"]
    fall = rise + test["edge_s"] + test["high_duration_s"]
    if len(rows) < 10 or rows[0][0] > 1e-12 or rows[-1][0] < stop * (1 - 1e-6):
        raise ValueError("Transient simulation is incomplete.")
    if any(b[0] < a[0] for a, b in zip(rows, rows[1:])):
        raise ValueError("Transient time moves backwards.")
    if any(b[0] - a[0] > test["max_step_s"] * (1 + 1e-5) for a, b in zip(rows, rows[1:])):
        raise ValueError("Transient output contains a gap larger than the permitted time step.")
    tolerance = test["settling_tolerance_v"]
    hold = test["minimum_hold_s"]
    up = settling_time(rows, rise, fall, test["high_v"], tolerance, hold)
    down = settling_time(rows, fall, stop * (1 + 1e-8), test["low_v"], tolerance, hold)
    # Check tracking before the stimulus and across the final quarter of each plateau.
    steady = [r for r in rows if r[0] < rise or
              rise + 0.75 * (fall - rise) <= r[0] < fall or
              fall + 0.75 * (stop - fall) <= r[0] <= stop]
    error = max(abs(row[2] - row[1]) for row in steady)
    return {"settling_rise_s": up, "settling_fall_s": down,
            "max_tracking_error_v": error,
            "output_min_v": min(row[2] for row in rows),
            "output_max_v": max(row[2] for row in rows)}


def score(metrics, constraints):
    if not constraints:
        raise ValueError("A task must define constraints.")
    passed = {}
    penalties = []
    for name, limits in constraints.items():
        if not limits or set(limits) - {"min", "max"}:
            raise ValueError(f"Invalid constraint definition: {name}")
        value = metrics.get(name)
        finite = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        ok = finite
        for direction, target in limits.items():
            if not math.isfinite(target) or target <= 0:
                raise ValueError(f"Constraint target must be finite and positive: {name}")
            violation = 1.0 if not finite else max(
                0.0, (target - value) / target if direction == "min" else (value - target) / target)
            penalties.append(min(1.0, violation))
            ok = ok and violation == 0
        passed[name] = bool(ok)
    success = all(passed.values())
    return {"success": success, "checks": passed,
            "reward": 1.0 if success else -sum(penalties) / len(penalties)}
