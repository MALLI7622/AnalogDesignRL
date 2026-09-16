"""Print reviewable Google Cloud commands. Never creates or deletes resources."""
import argparse
import json
from pathlib import Path
import re
import shlex

BUILDER_V6E_ZONES = {"us-east5-a", "us-east5-b", "us-central1-a", "europe-west4-a", "southamerica-west1-a"}
PHASES = {
    "inspect": ("inspect_project", "inspect_billing", "inspect_apis", "inspect_network", "inspect_machine", "inspect_global_quota", "inspect_region_quota", "inspect_service_account"),
    "enable": ("enable_api",),
    "storage": ("create_bucket", "grant_bucket_access", "create_data_disk"),
    "provision": ("create_tpu",),
    "connect": ("describe", "startup_log", "connect"),
    "cleanup": ("delete_compute_after_backup",),
}


def duration_seconds(value):
    match = re.fullmatch(r"([1-9][0-9]*)([smhd])", str(value))
    if not match:
        raise ValueError("Use a positive duration such as 30m, 2h, or 7d")
    return int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]


def validate(config):
    if config["machine_type"] != "ct6e-standard-4t":
        raise ValueError("This TPU Builders preset targets a single four-chip v6e VM")
    if config["zone"] not in BUILDER_V6E_ZONES:
        raise ValueError("Zone is not in the supplied TPU Builders v6e capacity table")
    if config["zone"].rsplit("-", 1)[0] != config["region"]:
        raise ValueError("Keep the bucket region and data disk/TPU zone in the same region")
    if config["provisioning_model"] != "FLEX_START":
        raise ValueError("Start with FLEX_START; validate checkpoint recovery before adding Spot support")
    if not 600 <= duration_seconds(config["max_run_duration"]) <= 7 * 86400:
        raise ValueError("Run duration must be between 10 minutes and 7 days")
    if not 90 <= duration_seconds(config["request_valid_for_duration"]) <= 7200:
        raise ValueError("This zonal preset uses a queue wait between 90 seconds and 2 hours")
    if config["data_disk_type"] != "hyperdisk-balanced":
        raise ValueError("Use Hyperdisk Balanced for this writable experiment disk")
    for field in ("boot_disk_gb", "data_disk_gb"):
        if type(config[field]) is not int or config[field] < 50:
            raise ValueError(f"{field} must be an integer of at least 50 GB")
    for field in ("instance", "data_disk", "network"):
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,61}[a-z0-9]|[a-z]", config[field]):
            raise ValueError(f"Invalid resource name: {field}")


def commands(config, *, initialize_data_disk=False):
    validate(config)
    c = config
    common = ["--project", c["project"], "--account", c["account"]]
    location = [*common, "--zone", c["zone"]]
    return {
        "inspect_project": ["gcloud", "projects", "describe", c["project"], *common],
        "inspect_billing": ["gcloud", "billing", "projects", "describe", c["project"], *common],
        "inspect_apis": ["gcloud", "services", "list", "--enabled", *common],
        "inspect_network": ["gcloud", "compute", "networks", "describe", c["network"], *common],
        "inspect_machine": ["gcloud", "compute", "machine-types", "describe", c["machine_type"], *location],
        "inspect_global_quota": ["gcloud", "compute", "project-info", "describe", *common, "--format=json(quotas)"],
        "inspect_region_quota": ["gcloud", "compute", "regions", "describe", c["region"], *common, "--format=json(quotas)"],
        "inspect_service_account": ["gcloud", "iam", "service-accounts", "describe", c["service_account"], *common],
        "enable_api": ["gcloud", "services", "enable", "compute.googleapis.com", "tpu.googleapis.com", *common],
        "create_bucket": ["gcloud", "storage", "buckets", "create", "gs://" + c["bucket"], *common,
                          "--location", c["region"], "--uniform-bucket-level-access", "--public-access-prevention"],
        "grant_bucket_access": ["gcloud", "storage", "buckets", "add-iam-policy-binding", "gs://" + c["bucket"],
                                "--member=serviceAccount:" + c["service_account"], "--role=roles/storage.objectUser", *common],
        "create_data_disk": ["gcloud", "compute", "disks", "create", c["data_disk"], *location,
                             "--type", c["data_disk_type"], "--size", str(c["data_disk_gb"]) + "GB",
                             "--labels=research=analog-rl"],
        "create_tpu": ["gcloud", "compute", "instances", "create", c["instance"], *location,
                       "--machine-type", c["machine_type"], "--image-project", c["image_project"],
                       "--image-family", c["image_family"], "--boot-disk-size", str(c["boot_disk_gb"]) + "GB",
                       "--service-account", c["service_account"], "--scopes", "cloud-platform",
                       "--network", c["network"], "--provisioning-model=FLEX_START",
                       "--request-valid-for-duration", c["request_valid_for_duration"],
                       "--max-run-duration", c["max_run_duration"], "--instance-termination-action=DELETE",
                       "--disk", f"name={c['data_disk']},device-name=analog-rl-data,mode=rw,boot=no,auto-delete=no",
                       "--metadata-from-file=startup-script=training/startup.sh",
                       "--metadata=analog-rl-format-blank-disk=" + ("true" if initialize_data_disk else "false"),
                       "--maintenance-policy=TERMINATE", "--labels=research=analog-rl"],
        "connect": ["gcloud", "compute", "ssh", c["instance"], *location],
        "describe": ["gcloud", "compute", "instances", "describe", c["instance"], *location],
        "startup_log": ["gcloud", "compute", "instances", "get-serial-port-output", c["instance"], *location, "--port=1"],
        "delete_compute_after_backup": ["gcloud", "compute", "instances", "delete", c["instance"], *location],
    }


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--config", default="training/cloud.example.json")
    cli.add_argument("--phase", choices=[*PHASES, "all"], default="inspect")
    cli.add_argument("--initialize-data-disk", action="store_true", help="Allow formatting a newly created blank data disk on its first boot")
    args = cli.parse_args()
    plan = commands(json.loads(Path(args.config).read_text()), initialize_data_disk=args.initialize_data_disk)
    labels = plan if args.phase == "all" else PHASES[args.phase]
    for label in labels:
        command = plan[label]
        print(f"# {label}\n{shlex.join(command)}\n")


if __name__ == "__main__":
    main()
