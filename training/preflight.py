"""Check local simulator dependencies or the optional TPU training runtime."""
import argparse
import importlib.metadata
import json
import platform
import shutil
import subprocess

from analog_design.simulator import ROOT, digest


def check_simulator():
    lock = json.loads((ROOT / "dependencies.lock.json").read_text())
    executable = shutil.which("ngspice")
    if not executable:
        raise RuntimeError("Install ngspice 47 and run python3 scripts/setup.py")
    version = subprocess.check_output([executable, "--version"], text=True, timeout=10)
    expected = lock["ngspice_validated_version"]
    if f"ngspice-{expected} " not in version:
        raise RuntimeError(f"Expected the reviewed simulator version, ngspice {expected}")
    corner = ROOT / ".deps/sky130_pdk/libs.tech/ngspice/corners/tt.spice"
    marker = ROOT / ".deps/archive.sha256"
    if not corner.exists() or not marker.exists() or marker.read_text().strip() != lock["analoggym"]["pdk_archive_sha256"]:
        raise RuntimeError("Device models missing or unrecognized; run python3 scripts/setup.py")
    return {"python": platform.python_version(), "ngspice": expected, "host": platform.platform(),
            "dependency_lock_sha256": digest(ROOT / "dependencies.lock.json")}


def check_tpu():
    if importlib.metadata.version("google-tunix") != "0.1.7":
        raise RuntimeError("This integration targets google-tunix==0.1.7")
    import jax
    from tunix.rl.agentic.agentic_grpo_learner import GRPOLearner  # noqa: F401
    devices = jax.devices()
    if not devices or any(device.platform != "tpu" for device in devices):
        raise RuntimeError("No TPU detected by JAX; run on the allocated TPU VM")
    if jax.process_count() != 1 or len(devices) not in {1, 2, 4}:
        raise RuntimeError("Starter configuration supports one host with 1, 2, or 4 TPU devices")
    return {"devices": [str(device) for device in devices], "jax": jax.__version__, "tunix": "0.1.7"}


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--tpu", action="store_true")
    args = cli.parse_args()
    print(json.dumps(check_tpu() if args.tpu else check_simulator(), indent=2))


if __name__ == "__main__":
    main()
