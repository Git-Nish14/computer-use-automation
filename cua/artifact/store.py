# Saves and loads CapabilityArtifact as JSON files.

from __future__ import annotations

import json
from pathlib import Path

from cua.artifact.schema import CapabilityArtifact


class ArtifactStore:
    def __init__(self, base_dir: Path | str = "capabilities"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save(self, artifact: CapabilityArtifact, name: str | None = None) -> Path:
        filename = name or f"{artifact.name}.json"
        if not filename.endswith(".json"):
            filename += ".json"
        path = self.base_dir / filename
        path.write_text(artifact.model_dump_json(indent=2), encoding="utf-8")
        return path

    def load(self, path: Path | str) -> CapabilityArtifact:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return CapabilityArtifact.model_validate(data)

    def list(self) -> list[Path]:
        return sorted(self.base_dir.glob("*.json"))
