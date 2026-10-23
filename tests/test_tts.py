"""Backend selection, reporting, and cancellation without loading speech models."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

with tempfile.TemporaryDirectory() as import_dir:
    with patch.dict(os.environ, {"DATA_DIR": import_dir}):
        from app import server

from app.tasks import TaskCancelled, TaskStore


class TTSTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = TaskStore()
        for name, value in {"TTS_DIR": self.root, "TASKS": self.store,
                            "TTS_BACKEND": "kitten"}.items():
            p = patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)

    def generate(self, command, **kwargs):
        command = list(map(str, command))
        Path(command[command.index("--output") + 1]).write_bytes(b"RIFF")
        report = {"load_seconds": 2, "generation_seconds": 10, "peak_rss_mib": 2300}
        return subprocess.CompletedProcess(command, 0, json.dumps(report).encode(), b"")

    def test_kitten_selects_russian_voice_and_keeps_metrics(self):
        task = self.store.create()
        with patch.object(server, "_kitten_ready", return_value=True), \
             patch.object(server, "_run", side_effect=self.generate) as run:
            output = server.synthesize("Привет", "auto", task["id"])
        command = list(map(str, run.call_args.args[0]))
        self.assertEqual(command[command.index("--voice") + 1], "Russian")
        self.assertNotIn("--download", command)
        self.assertTrue(output.is_file())
        details = self.store.get(task["id"])["stages"][0]["detail"]
        self.assertEqual(details["engine"], "Kitten TTS 2")
        self.assertEqual(details["metrics"]["peak_rss_mib"], 2300)

    def test_model_failure_removes_partial_output_and_releases_lock(self):
        def fail(command, **kwargs):
            command = list(map(str, command))
            Path(command[command.index("--output") + 1]).write_bytes(b"partial")
            return subprocess.CompletedProcess(command, 1, b"", b"decoder failed")
        with patch.object(server, "_kitten_ready", return_value=True), \
             patch.object(server, "_run", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "decoder failed"):
                server.synthesize("Hello")
        self.assertFalse(list(self.root.glob("*.wav")))
        self.assertFalse(server.KITTEN_LOCK.locked())

    def test_cancelled_task_does_not_start_kitten(self):
        task = self.store.create()
        self.store.cancel(task["id"])
        with patch.object(server, "_kitten_ready", return_value=True), \
             patch.object(server, "_run") as run:
            with self.assertRaises(TaskCancelled):
                server.synthesize("Hello", task_id=task["id"])
        run.assert_not_called()
        self.assertFalse(server.KITTEN_LOCK.locked())

    def test_pocket_remains_available(self):
        def generate(command, **kwargs):
            command = list(map(str, command))
            Path(command[command.index("--output-path") + 1]).write_bytes(b"RIFF")
            return subprocess.CompletedProcess(command, 0, b"", b"")
        with patch.object(server, "TTS_BACKEND", "pocket"), \
             patch.object(server, "_run", side_effect=generate) as run:
            self.assertTrue(server.synthesize("Hello").is_file())
        self.assertEqual(run.call_args.args[0][1], "generate")

    def test_mini_selects_onnx_worker_for_english(self):
        with patch.object(server, "TTS_BACKEND", "kitten-mini"), \
             patch.object(server, "_kitten_ready", return_value=True), \
             patch.object(server, "_run", side_effect=self.generate) as run:
            output = server.synthesize("Hello")
        command = list(map(str, run.call_args.args[0]))
        self.assertEqual(command[command.index("--family") + 1], "mini")
        self.assertEqual(command[0], str(server.KITTEN_MINI_PYTHON))
        self.assertTrue(output.is_file())

    def test_mini_keeps_pocket_for_russian(self):
        task = self.store.create()
        config, voice = self.root / "ru.yaml", self.root / "voice.safetensors"
        config.write_text("fixture")
        voice.write_bytes(b"fixture")
        def generate(command, **kwargs):
            command = list(map(str, command))
            Path(command[command.index("--output-path") + 1]).write_bytes(b"RIFF")
            return subprocess.CompletedProcess(command, 0, b"", b"")
        with patch.object(server, "TTS_BACKEND", "kitten-mini"), \
             patch.object(server, "RU_CONFIG", config), \
             patch.object(server, "RU_VOICE", voice), \
             patch.object(server, "_run", side_effect=generate) as run:
            server.synthesize("Привет", "auto", task["id"])
        self.assertIn("--config", run.call_args.args[0])
        self.assertEqual(self.store.get(task["id"])["stages"][0]["detail"]["engine"], "Pocket TTS")

    def test_cancellation_terminates_a_just_started_subprocess(self):
        task = self.store.create()
        # This models cancellation racing with Popen/register_process.
        original = subprocess.Popen
        def start(*args, **kwargs):
            proc = original(*args, **kwargs)
            self.store.cancel(task["id"])
            return proc
        with patch.object(server.subprocess, "Popen", side_effect=start), \
             patch.object(server, "_stop_process", wraps=server._stop_process) as stop:
            with self.assertRaises(TaskCancelled):
                server._run(["sleep", "30"], timeout=2, task_id=task["id"])
        stop.assert_called_once()
        self.assertIsNotNone(stop.call_args.args[0].poll())
        self.assertFalse(self.store._procs.get(task["id"]))


if __name__ == "__main__":
    unittest.main()
