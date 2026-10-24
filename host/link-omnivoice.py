"""Run with the OmniVoice interpreter to make its source link relocatable."""

import os
from pathlib import Path
import sysconfig


root = Path(__file__).resolve().parent.parent
source = root / "models/omnivoice-src"
if not (source / "omnivoice").is_dir():
    raise SystemExit("missing OmniVoice source; run bash host/setup-omnivoice.sh")
site = Path(sysconfig.get_path("purelib"))
# .pth paths are resolved relative to site-packages, including after a move.
(site / "omnivoice-source.pth").write_text(os.path.relpath(source, site) + "\n")
