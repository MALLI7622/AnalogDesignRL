"""Load explicit local text sources; PDFs use optional Poppler text extraction."""
import hashlib
from pathlib import Path
import shutil
import subprocess

from .simulator import ROOT


def load_sources(template, paths=(), max_chars=100000):
    circuit = ROOT / template["circuit_directory"]
    selected = [("circuit_source", circuit / "source.json"),
                ("circuit_netlist", circuit / "netlist.spice"),
                ("circuit_parameters", circuit / "reference.params")]
    selected.extend((f"source_{index:03d}", Path(path).resolve()) for index, path in enumerate(paths, 1))
    sources = {}
    for source_id, path in selected:
        original = path.read_bytes()
        if len(original) > 32 * 1024 * 1024:
            raise ValueError(f"Source exceeds 32 MiB: {path.name}")
        if path.suffix.lower() == ".pdf":
            executable = shutil.which("pdftotext")
            if not executable:
                raise ValueError("PDF input requires pdftotext (Poppler), or supply an extracted .txt file.")
            try:
                result = subprocess.run([executable, "-layout", "-enc", "UTF-8", str(path), "-"],
                                        capture_output=True, timeout=30, check=True)
            except (OSError, subprocess.SubprocessError):
                raise ValueError(f"Could not extract PDF text: {path.name}; supply a readable text excerpt.") from None
            text = result.stdout.decode("utf-8")
            extraction = "pdftotext -layout -enc UTF-8; text only, no figure interpretation"
        else:
            if path.suffix.lower() not in {".txt", ".md", ".json", ".spice", ".cir", ".net", ".params", ".tex"}:
                raise ValueError(f"Unsupported source format: {path.suffix}; use PDF or text.")
            text = original.decode("utf-8")
            extraction = "UTF-8 text"
        if not text.strip() or "\x00" in text:
            raise ValueError(f"Source has no usable text: {path.name}")
        sources[source_id] = {"name": path.name, "text": text, "extraction": extraction,
                              "original_sha256": hashlib.sha256(original).hexdigest(),
                              "text_sha256": hashlib.sha256(text.encode()).hexdigest()}
    if sum(len(source["text"]) for source in sources.values()) > max_chars:
        raise ValueError("Sources exceed --max-source-chars; supply a shorter excerpt or increase the limit. Nothing was truncated.")
    return sources
