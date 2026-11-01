"""Shared assistant silence switch, visible to the monitor and S3 processes."""

import os
from pathlib import Path


class AssistantControl:
    def __init__(self, marker: Path):
        self.marker = marker

    def silenced(self) -> bool:
        return self.marker.is_file()

    def set_silenced(self, value: bool) -> None:
        if value:
            self.marker.parent.mkdir(parents=True, exist_ok=True)
            self.marker.touch(mode=0o600, exist_ok=True)
        else:
            self.marker.unlink(missing_ok=True)


CONTROL = AssistantControl(Path(os.environ.get("DATA_DIR", "data")) / "assistant-silenced")
