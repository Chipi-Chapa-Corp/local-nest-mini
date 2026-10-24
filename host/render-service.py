"""Render the service template for this checkout; write only to stdout."""

from pathlib import Path


def quote(value: str) -> str:
    # ExecStart expands both specifiers and environment variables.
    value = value.replace("%", "%%").replace("$", "$$")
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    value = value.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{value}"'


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    template = (root / "host/local-nest-mini.service").read_text()
    # service-run.sh changes to its own checkout before reading any files.
    print(template.replace("@SERVICE_RUN@", quote(str(root / "host/service-run.sh"))),
          end="")
