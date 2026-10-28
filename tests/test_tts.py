"""Backend selection, reporting, and cancellation without loading speech models."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from app.omnivoice_tts import generation_options

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
        # The real machine may have a usable GPU; tests opt into the GPU path explicitly.
        for name, value in {"TTS_DIR": self.root, "TASKS": self.store,
                            "TTS_BACKEND": "omnivoice", "OMNIVOICE_GPU": "off"}.items():
            p = patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)

    def generate(self, command, **kwargs):
        command = list(map(str, command))
        Path(command[command.index("--output") + 1]).write_bytes(b"RIFF")
        report = {"load_seconds": 2, "generation_seconds": 10, "peak_rss_mib": 2300}
        return subprocess.CompletedProcess(command, 0, json.dumps(report).encode(), b"")


    def test_omnivoice_routes_both_languages_with_selected_settings(self):
        with patch.object(server, "TTS_BACKEND", "omnivoice"), \
             patch.object(server, "_omnivoice_ready", return_value=True), \
             patch.object(server, "_run", side_effect=self.generate) as run:
            for text, spoken, lang in [("Hello [laughter]", "Hello", "en"), ("Привет [sigh]!", "Привет!", "auto")]:
                with self.subTest(lang=lang):
                    task = self.store.create()
                    output = server.synthesize(text, lang, task["id"])
                    command = list(map(str, run.call_args.args[0]))
                    self.assertEqual(command[0], str(server.OMNIVOICE_PYTHON))
                    self.assertTrue(command[1].endswith("omnivoice_tts.py"))
                    self.assertEqual(command[command.index("--steps") + 1], "24")
                    self.assertEqual(command[command.index("--text") + 1], spoken)
                    self.assertEqual(command[command.index("--lang") + 1], "ru" if lang == "auto" else lang)
                    self.assertEqual(output.stat().st_mode & 0o777, 0o644)
                    detail = self.store.get(task["id"])["stages"][0]["detail"]
                    self.assertEqual(detail["engine"], "OmniVoice CPU")
                    self.assertEqual(detail["metrics"]["peak_rss_mib"], 2300)

    def test_omnivoice_failure_cleans_up_and_releases_lock(self):
        def fail(command, **kwargs):
            command = list(map(str, command))
            Path(command[command.index("--output") + 1]).write_bytes(b"partial")
            return subprocess.CompletedProcess(command, 1, b"", b"OmniVoice failed")
        with patch.object(server, "TTS_BACKEND", "omnivoice"), \
             patch.object(server, "_omnivoice_ready", return_value=True), \
             patch.object(server, "_run", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "OmniVoice failed"):
                server.synthesize("Hello")
        self.assertFalse(list(self.root.glob("*.wav")))
        self.assertFalse(server.OMNIVOICE_LOCK.locked())

    def test_omnivoice_cancellation_does_not_start_model(self):
        task = self.store.create()
        self.store.cancel(task["id"])
        with patch.object(server, "TTS_BACKEND", "omnivoice"), \
             patch.object(server, "_omnivoice_ready", return_value=True), \
             patch.object(server, "_run") as run:
            with self.assertRaises(TaskCancelled):
                server.synthesize("Hello", task_id=task["id"])
        run.assert_not_called()
        self.assertFalse(server.OMNIVOICE_LOCK.locked())

    def test_omnivoice_missing_assets_does_not_fall_back(self):
        with patch.object(server, "TTS_BACKEND", "omnivoice"), \
             patch.object(server, "_omnivoice_ready", return_value=False), \
             patch.object(server, "_run") as run:
            with self.assertRaisesRegex(RuntimeError, "setup-omnivoice.sh"):
                server.synthesize("Hello")
        run.assert_not_called()

    def gpu(self, command, **kwargs):
        """Fake omnivoice.cpp: write the WAV, print its perf log."""
        command = list(map(str, command))
        Path(command[command.index("-o") + 1]).write_bytes(b"RIFF" + bytes(100))
        log = (b"[Perf] Total 1510.0 ms (T=138, audio 5.52 s, RTF 0.27)\n"
               b"[TTS-Long] Post-proc: 132480 -> 137280 samples (5.72s at 24000 Hz, ref_rms=0.07)\n")
        return subprocess.CompletedProcess(command, 0, b"", log)

    def omnivoice(self, free_mib, run):
        task = self.store.create()
        with patch.object(server, "TTS_BACKEND", "omnivoice"), \
             patch.object(server, "_omnivoice_ready", return_value=True), \
             patch.object(server, "_omnivoice_gpu_ready", return_value=True), \
             patch.object(server, "_gpu_free_vram_mib", return_value=free_mib), \
             patch.object(server, "_run", side_effect=run) as mock:
            output = server.synthesize("[surprise-wa] Привет!! Как дела? [laughter]", "ru", task["id"])
        detail = self.store.get(task["id"])["stages"][0]["detail"]
        return output, mock, detail

    def test_omnivoice_uses_gpu_when_enough_vram_is_free(self):
        output, run, detail = self.omnivoice(5000, self.gpu)
        self.assertTrue(output.is_file())
        self.assertEqual(run.call_count, 1)
        command = list(map(str, run.call_args.args[0]))
        self.assertEqual(command[0], str(server.OMNIVOICE_GPU_BIN))
        self.assertEqual(command[command.index("--device") + 1], "Vulkan0")
        self.assertEqual(command[command.index("--steps") + 1], "24")
        self.assertTrue(command[command.index("--model") + 1].endswith("Q8_0.gguf"))
        self.assertEqual(run.call_args.kwargs["stdin"], "Привет!! Как дела?".encode())  # tags removed
        self.assertEqual(detail["engine"], "OmniVoice GPU (Vulkan)")
        self.assertEqual(detail["metrics"]["generation_seconds"], 1.51)
        self.assertEqual(detail["metrics"]["audio_seconds"], 5.72)
        self.assertEqual(detail["metrics"]["vram_free_mib"], 5000)
        self.assertFalse(server.OMNIVOICE_LOCK.locked())

    def test_omnivoice_uses_cpu_at_or_below_threshold(self):
        for free in (1700, 1740, None):
            with self.subTest(free=free):
                _, run, detail = self.omnivoice(free, self.generate)
                command = list(map(str, run.call_args.args[0]))
                self.assertEqual(command[0], str(server.OMNIVOICE_PYTHON))
                self.assertEqual(command[command.index("--steps") + 1], "24")
                self.assertEqual(command[command.index("--text") + 1], "Привет!! Как дела?")
                self.assertEqual(detail["engine"], "OmniVoice CPU")
                self.assertIn("VRAM" if free else "no VRAM information", detail["cpu_reason"])

    def test_omnivoice_gpu_failure_falls_back_to_cpu(self):
        def run(command, **kwargs):
            if str(command[0]) == str(server.OMNIVOICE_GPU_BIN):
                Path(list(map(str, command))[list(map(str, command)).index("-o") + 1]).write_bytes(b"partial")
                return subprocess.CompletedProcess(command, 1, b"", b"ggml_vulkan: device lost")
            return self.generate(command)
        with patch("builtins.print"):
            output, mock, detail = self.omnivoice(5000, run)
        self.assertTrue(output.is_file())
        self.assertEqual(mock.call_count, 2)
        self.assertEqual(detail["engine"], "OmniVoice CPU")
        self.assertIn("GPU failed: ggml_vulkan: device lost", detail["cpu_reason"])
        self.assertEqual(detail["metrics"]["peak_rss_mib"], 2300)

    def test_omnivoice_rejects_text_that_is_only_tags(self):
        with patch.object(server, "TTS_BACKEND", "omnivoice"), \
             patch.object(server, "_omnivoice_ready", return_value=True), \
             patch.object(server, "_run") as run:
            with self.assertRaises(ValueError):
                server.synthesize("[laughter] [sigh]")
        run.assert_not_called()

    def test_sound_tags_are_removed_without_touching_speech(self):
        cases = {
            "[surprise-wa] Привет!! [question-oh] Хмм... [sigh] Ну [laughter]": "Привет!! Хмм... Ну",
            "Ура [laughter]! Бешеный нахрюк": "Ура! Бешеный нахрюк",
            "Сумма [1] и (скобки)": "Сумма [1] и (скобки)",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(server._strip_sound_tags(text), expected)

    def test_free_vram_comes_from_the_card_with_most_vram(self):
        root = self.root / "drm"
        for card, total, used in (("card0", 512, 15), ("card1", 16368, 2148)):
            device = root / card / "device"
            device.mkdir(parents=True)
            (device / "mem_info_vram_total").write_text(str(total * 1048576))
            (device / "mem_info_vram_used").write_text(str(used * 1048576))
        (root / "card1-DP-1").mkdir()
        self.assertEqual(server._gpu_free_vram_mib(root), 16368 - 2148)
        self.assertIsNone(server._gpu_free_vram_mib(self.root / "empty"))


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


class OmniVoiceWorkerTests(unittest.TestCase):
    def test_language_specific_voice_and_steps(self):
        reference = Path("saved-reference.wav")
        english = generation_options("en", reference)
        russian = generation_options("ru", reference)
        self.assertEqual(english["num_step"], 16)
        self.assertEqual(english["ref_audio"], str(reference))
        self.assertNotIn("instruct", english)
        self.assertEqual(russian["num_step"], 32)
        self.assertEqual(russian["ref_audio"], str(reference))
        self.assertNotIn("instruct", russian)
        for options in (english, russian):
            self.assertFalse(options["normalize_text"])
            self.assertNotIn("preset", options)

    def test_reference_bootstrap_is_explicit_and_english_only(self):
        options = generation_options("en", Path("missing.wav"), 32, bootstrap_reference=True)
        self.assertEqual(options["instruct"], "female, young adult")
        self.assertNotIn("ref_audio", options)
        with self.assertRaises(ValueError):
            generation_options("ru", Path("missing.wav"), bootstrap_reference=True)


if __name__ == "__main__":
    unittest.main()
