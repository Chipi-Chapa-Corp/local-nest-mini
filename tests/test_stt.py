import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

with tempfile.TemporaryDirectory() as import_dir:
    with patch.dict(os.environ, {"DATA_DIR": import_dir}):
        from app import server

from app.tasks import TaskCancelled


class TranscriptionTests(unittest.TestCase):
    def prepare_wav(self, source, wav, task_id):
        with wave.open(str(wav), "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\x00\x00" * 8000)

    def test_vulkan_failure_retries_with_cpu_and_same_audio_context(self):
        gpu = subprocess.CompletedProcess([], 1, b"", b"GPU unavailable")
        cpu = subprocess.CompletedProcess([], 0, "Привет.\n".encode(), b"")
        with tempfile.TemporaryDirectory() as temp:
            model = Path(temp) / "model.bin"
            model.touch()
            with patch.object(server, "STT_MODEL", model), \
                 patch.object(server, "WHISPER_GPU_BIN", "whisper-vulkan"), \
                 patch.object(server, "_prepare_audio", side_effect=self.prepare_wav), \
                 patch.object(server, "_run", side_effect=[gpu, cpu]) as run:
                self.assertEqual(server.transcribe_audio(Path("source"), "ru"), "Привет.")
        gpu_call, cpu_call = run.call_args_list
        self.assertEqual(gpu_call.args[0][0], "whisper-vulkan")
        self.assertEqual(cpu_call.args[0][0], server.WHISPER_BIN)
        self.assertEqual(gpu_call.args[0][1:], cpu_call.args[0][1:])
        args = cpu_call.args[0]
        self.assertEqual(args[args.index("--audio-ctx") + 1], "89")

    def test_interrupted_vulkan_run_does_not_start_cpu(self):
        with tempfile.TemporaryDirectory() as temp:
            model = Path(temp) / "model.bin"
            model.touch()
            with patch.object(server, "STT_MODEL", model), \
                 patch.object(server, "WHISPER_GPU_BIN", "whisper-vulkan"), \
                 patch.object(server, "_prepare_audio", side_effect=self.prepare_wav), \
                 patch.object(server, "_run", side_effect=TaskCancelled) as run:
                with self.assertRaises(TaskCancelled):
                    server.transcribe_audio(Path("source"), "en")
            run.assert_called_once()
