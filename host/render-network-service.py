"""Render the host-only network helper unit for this checkout."""
from pathlib import Path
import runpy
quote = runpy.run_path(str(Path(__file__).with_name("render-service.py")))["quote"]

root = Path(__file__).resolve().parent.parent
print((root / 'host/local-nest-network.service').read_text().replace(
    '@HELPER_RUN@', quote(str(root / 'host/helper-run.sh'))), end='')
