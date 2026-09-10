"""Fetch the pinned AnalogGym files and unpack its model archive locally."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    lock = json.loads((ROOT / "dependencies.lock.json").read_text())
    dependency = lock["analoggym"]
    repo = ROOT / "external/AnalogGym"
    if not repo.exists():
        repo.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--filter=blob:none", "--sparse",
                        dependency["url"], str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "checkout", "--detach",
                        dependency["commit"]], check=True)
        subprocess.run(["git", "-C", str(repo), "sparse-checkout", "set",
                        "AnalogGym/Amplifier", "PDK"], check=True)
    revision = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    if revision != dependency["commit"]:
        raise RuntimeError("Existing AnalogGym checkout differs from the lock file.")
    archive = repo / dependency["pdk_archive"]
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != dependency["pdk_archive_sha256"]:
        raise RuntimeError("Model archive checksum does not match the lock file.")
    destination = ROOT / ".deps"
    marker = destination / "archive.sha256"
    corner = destination / "sky130_pdk/libs.tech/ngspice/corners/tt.spice"
    if not marker.exists() or marker.read_text().strip() != digest or not corner.exists():
        destination.mkdir(exist_ok=True)
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                if not (destination / member.filename).resolve().is_relative_to(destination.resolve()):
                    raise RuntimeError("Archive contains an unsafe path.")
            bundle.extractall(destination)
        marker.write_text(digest + "\n")
    if not shutil.which("ngspice"):
        raise RuntimeError("Install ngspice first (macOS: brew install ngspice).")
    author = lock["autockt"]
    author_repo = ROOT / "external/AutoCkt"
    if not author_repo.exists():
        subprocess.run(["git", "clone", "--filter=blob:none", author["url"], str(author_repo)], check=True)
        subprocess.run(["git", "-C", str(author_repo), "checkout", "--detach", author["commit"]], check=True)
    author_revision = subprocess.check_output(
        ["git", "-C", str(author_repo), "rev-parse", "HEAD"], text=True).strip()
    if author_revision != author["commit"]:
        raise RuntimeError("Existing AutoCkt checkout differs from the lock file.")
    for relative, expected in author["files_sha256"].items():
        if hashlib.sha256((author_repo / relative).read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"AutoCkt source checksum mismatch: {relative}")
    print(subprocess.check_output(["ngspice", "--version"], text=True).strip())
    print(f"AnalogGym: {revision}\nAutoCkt: {author_revision}\nModel archive: {digest}\nSetup complete.")


if __name__ == "__main__":
    main()
