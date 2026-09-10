"""Print reviewable Google Cloud commands. Never creates or deletes resources."""
import argparse
import json
from pathlib import Path
import shlex


def commands(config):
    c = config
    common = ["--project", c["project"]]
    location = [*common, "--zone", c["zone"]]
    return {
        "inspect_machine": ["gcloud", "compute", "machine-types", "describe", c["machine_type"], *location],
        "inspect_region_quota": ["gcloud", "compute", "regions", "describe", c["region"], *common, "--format=json(quotas)"],
        "enable_api": ["gcloud", "services", "enable", "compute.googleapis.com", *common],
        "create_bucket": ["gcloud", "storage", "buckets", "create", "gs://" + c["bucket"], *common,
                          "--location", c["region"], "--uniform-bucket-level-access", "--public-access-prevention"],
        "grant_bucket_access": ["gcloud", "storage", "buckets", "add-iam-policy-binding", "gs://" + c["bucket"],
                                "--member=serviceAccount:" + c["service_account"], "--role=roles/storage.objectUser", *common],
        "create_tpu": ["gcloud", "compute", "instances", "create", c["instance"], *location,
                       "--machine-type", c["machine_type"], "--image-project", c["image_project"],
                       "--image-family", c["image_family"], "--boot-disk-size", str(c["boot_disk_gb"]) + "GB",
                       "--service-account", c["service_account"], "--scopes", "cloud-platform",
                       "--maintenance-policy=TERMINATE", "--labels=research=analog-rl"],
        "connect": ["gcloud", "compute", "ssh", c["instance"], *location],
        "describe": ["gcloud", "compute", "instances", "describe", c["instance"], *location],
        "delete_compute_after_backup": ["gcloud", "compute", "instances", "delete", c["instance"], *location],
    }


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--config", default="training/cloud.example.json")
    args = cli.parse_args()
    for label, command in commands(json.loads(Path(args.config).read_text())).items():
        print(f"# {label}\n{shlex.join(command)}\n")


if __name__ == "__main__":
    main()
