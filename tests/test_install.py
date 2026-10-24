"""Portable install checks, using temporary checkouts and fake dependencies."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "original checkout"
        self.repo.mkdir()
        for name in ("run.sh", "start-proxy.sh", "config.env.example", "requirements.txt",
                     "host/setup-host.sh", "host/service-run.sh", "host/render-service.py",
                     "host/link-omnivoice.py", "host/local-nest-mini.service"):
            target = self.repo / name
            target.parent.mkdir(exist_ok=True)
            # No executable bits: entrypoints invoked with bash must still work.
            shutil.copyfile(ROOT / name, target)

    def run_command(self, *args, env=None):
        return subprocess.run(args, cwd=self.base, env=env, text=True,
                              capture_output=True, timeout=30)

    def move(self):
        destination = self.base / 'moved % checkout $HOME "quoted" \\ path'
        self.repo.rename(destination)
        self.repo = destination

    def make_env(self, relative):
        target = self.repo / relative
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(target)],
                       check=True, capture_output=True)
        python = target / "bin/python"
        site = Path(subprocess.check_output(
            [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
            text=True).strip())
        return python, site

    def test_unit_is_rendered_again_after_move_without_modifying_template(self):
        original = self.run_command(sys.executable, str(self.repo / "host/render-service.py"))
        self.assertEqual(original.returncode, 0, original.stderr)
        old_path = str(self.repo)
        self.assertIn(old_path, original.stdout)
        self.move()
        moved = self.run_command(sys.executable, str(self.repo / "host/render-service.py"))
        self.assertEqual(moved.returncode, 0, moved.stderr)
        self.assertNotIn(old_path, moved.stdout)
        self.assertIn('moved %% checkout $$HOME \\"quoted\\" \\\\ path', moved.stdout)
        self.assertNotIn("@SERVICE_RUN@", moved.stdout)
        self.assertEqual((self.repo / "host/local-nest-mini.service").read_bytes(),
                         (ROOT / "host/local-nest-mini.service").read_bytes())
        if shutil.which("systemd-analyze"):
            unit = self.base / "local-nest-mini.service"
            unit.write_text(moved.stdout)
            verified = self.run_command("systemd-analyze", "verify", str(unit))
            self.assertEqual(verified.returncode, 0, verified.stderr)

    def test_missing_configuration_explains_setup_before_sudo(self):
        self.move()
        result = self.run_command("bash", str(self.repo / "run.sh"), "install")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("copy config.env.example", result.stderr)
        self.assertNotIn("sudo", result.stderr)

    def test_stop_does_not_need_local_configuration(self):
        commands = self.base / "commands"
        commands.mkdir()
        for name, content in {
            "distrobox-host-exec": '#!/bin/bash\nexec "$@"\n',
            "sudo": '#!/bin/bash\nprintf "%s\\n" "$*" >> "$CALLS"\n',
        }.items():
            path = commands / name
            path.write_text(content)
            path.chmod(0o755)
        calls = self.base / "calls"
        env = dict(os.environ, PATH=f"{commands}:{os.defpath}", CALLS=str(calls))
        result = self.run_command("bash", str(self.repo / "run.sh"), "stop", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("systemctl stop local-nest-mini.service", calls.read_text())

    def test_setup_and_proxy_ignore_stale_console_scripts_after_move(self):
        _, site = self.make_env(".venv")
        (site / "pychromecast.py").touch()
        package = site / "mitmproxy"
        (package / "tools").mkdir(parents=True)
        (package / "__init__.py").touch()
        (package / "tools/__init__.py").touch()
        (package / "tools/main.py").write_text(
            'import json, os, sys\nfrom pathlib import Path\n'
            'def mitmdump():\n'
            '    Path(os.environ["OUTPUT"]).write_text(json.dumps(sys.argv[1:]))\n')
        (package / "certs.py").write_text(
            'from pathlib import Path\nclass CertStore:\n'
            '    @staticmethod\n    def from_store(directory, *args):\n'
            '        directory = Path(directory)\n'
            '        directory.mkdir(parents=True, exist_ok=True)\n'
            '        (directory / "mitmproxy-ca.pem").write_text("test certificate")\n')
        for name in ("pip", "mitmdump"):
            launcher = self.repo / ".venv/bin" / name
            launcher.write_text(f"#!{self.repo}/missing-python\n")
            launcher.chmod(0o755)
        self.move()
        setup = self.run_command("bash", str(self.repo / "host/setup-host.sh"))
        self.assertEqual(setup.returncode, 0, setup.stderr)
        self.assertTrue((self.repo / "proxy-config/mitmproxy-ca.pem").is_file())
        output = self.base / "proxy-args.json"
        proxy = self.run_command("bash", str(self.repo / "start-proxy.sh"),
                                 env=dict(os.environ, OUTPUT=str(output)))
        self.assertEqual(proxy.returncode, 0, proxy.stderr)
        self.assertIn(str(self.repo / "nest_relay.py"), json.loads(output.read_text()))

    def test_missing_dependencies_use_python_module_pip(self):
        binary_dir = self.repo / ".venv/bin"
        binary_dir.mkdir(parents=True)
        python = binary_dir / "python"
        python.write_text('''#!/bin/bash
printf '%s\\n' "$*" >> "$CALLS"
if [[ "$*" == *'import mitmproxy'* ]]; then exit 1; fi
if [[ "$1" == -c && "$2" == *CertStore* ]]; then
  mkdir -p proxy-config
  echo 'test certificate' > proxy-config/mitmproxy-ca.pem
fi
''')
        python.chmod(0o755)
        calls = self.base / "calls"
        result = self.run_command("bash", str(self.repo / "host/setup-host.sh"),
                                  env=dict(os.environ, CALLS=str(calls)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-m pip install -r requirements.txt", calls.read_text())

    def test_omnivoice_source_link_survives_another_move(self):
        python, site = self.make_env("models/omnivoice-env")
        source = self.repo / "models/omnivoice-src/omnivoice"
        source.mkdir(parents=True)
        (source / "__init__.py").write_text("VALUE = 42\n")
        link = site / "omnivoice-source.pth"
        link.write_text("/nonexistent/old/checkout/models/omnivoice-src\n")
        linked = self.run_command(str(python), "-I", str(self.repo / "host/link-omnivoice.py"))
        self.assertEqual(linked.returncode, 0, linked.stderr)
        self.assertFalse(Path(link.read_text().strip()).is_absolute())
        self.move()
        imported = self.run_command(str(self.repo / "models/omnivoice-env/bin/python"),
                                    "-I", "-c", "from omnivoice import VALUE; assert VALUE == 42")
        self.assertEqual(imported.returncode, 0, imported.stderr)


if __name__ == "__main__":
    unittest.main()
