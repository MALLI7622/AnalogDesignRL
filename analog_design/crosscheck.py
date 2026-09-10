"""A second measurement path using ngspice's native meas commands.

This checks Python extraction, not the correctness of the shared device model.
"""
import math
import re


def ac_commands(output, start_hz):
    return f"""
let check_magnitude = mag(v({output}))
let check_gain_db = db(v({output}))
let check_margin = 180 + cph(v({output}))*57.29577951308232
meas ac check_gain_db_result find check_gain_db at={start_hz}
meas ac check_unity_hz_result when check_magnitude=1 fall=1
meas ac check_margin_deg_result find check_margin when check_magnitude=1 fall=1
"""


def transient_commands(test):
    rise = test["rise_start_s"]
    fall = rise + test["edge_s"] + test["high_duration_s"]
    high_tail = rise + 0.75 * (fall - rise)
    low_tail = fall + 0.75 * (test["stop_s"] - fall)
    return f"""
let check_rise_error = abs(v(out) - {test['high_v']})
let check_fall_error = abs(v(out) - {test['low_v']})
let check_tracking_error = abs(v(out) - v(signal))
meas tran check_rise_cross_result when check_rise_error={test['settling_tolerance_v']} fall=last from={rise} to={fall}
meas tran check_fall_cross_result when check_fall_error={test['settling_tolerance_v']} fall=last from={fall} to={test['stop_s']}
meas tran check_tracking_before_result max check_tracking_error from=0 to={rise}
meas tran check_tracking_high_result max check_tracking_error from={high_tail} to={fall}
meas tran check_tracking_low_result max check_tracking_error from={low_tail} to={test['stop_s']}
"""


def measured_value(log, name):
    matches = re.findall(rf"(?mi)^\s*{re.escape(name)}\s*=\s*(\S+)", log)
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one independent measurement: {name}")
    value = float(matches[0])
    if not math.isfinite(value):
        raise ValueError(f"Nonfinite independent measurement: {name}")
    return value


def ac_measurements(log):
    return {"gain_db": measured_value(log, "check_gain_db_result"),
            "unity_gain_hz": measured_value(log, "check_unity_hz_result"),
            "phase_margin_deg": measured_value(log, "check_margin_deg_result")}


def all_measurements(ac_log, transient_log, test):
    result = ac_measurements(ac_log)
    result["power_w"] = measured_value(ac_log, "check_power_result")
    result["dc_error_v"] = measured_value(ac_log, "check_dc_error_result")
    result["max_tracking_error_v"] = max(
        measured_value(transient_log, f"check_tracking_{part}_result")
        for part in ("before", "high", "low"))
    rise = test["rise_start_s"]
    fall = rise + test["edge_s"] + test["high_duration_s"]
    result["settling_rise_s"] = measured_value(transient_log, "check_rise_cross_result") - rise
    result["settling_fall_s"] = measured_value(transient_log, "check_fall_cross_result") - fall
    return result


def compare(primary, independent, max_step_s=0):
    # At >=100 AC points/decade, log-frequency and native linear interpolation
    # should agree within these declared tolerances. Crossing times can differ
    # by one transient sample because Python reports the first in-band sample.
    limits = {"gain_db": (0.01, 0), "unity_gain_hz": (1, 0.002),
              "phase_margin_deg": (0.05, 0), "power_w": (1e-9, 1e-4),
              "dc_error_v": (1e-6, 0), "max_tracking_error_v": (2e-6, 0),
              "settling_rise_s": (max_step_s * 1.1 + 1e-12, 0),
              "settling_fall_s": (max_step_s * 1.1 + 1e-12, 0)}
    details = {}
    for name, value in independent.items():
        other = primary.get(name)
        if name not in limits or other is None:
            raise ValueError(f"No comparison policy or primary value for {name}")
        if not math.isfinite(value) or not math.isfinite(other):
            raise ValueError(f"Nonfinite comparison value for {name}")
        absolute, relative = limits[name]
        tolerance = absolute + relative * abs(value)
        difference = abs(other - value)
        details[name] = {"primary": other, "independent": value,
                         "absolute_difference": difference, "tolerance": tolerance,
                         "agrees": difference <= tolerance}
    return {"agrees": bool(details) and all(x["agrees"] for x in details.values()),
            "metrics": details}
