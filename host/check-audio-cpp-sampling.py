#!/usr/bin/env python3
"""Compare audio.cpp's Kitten repetition penalty with the pinned upstream code.

Run in models/kitten-reference-env after preparing the reference checkout.
The native code block is extracted from the actual checkout, compiled, and
exercised against the upstream PyTorch processor. Nothing is inferred from ASR.
"""

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess

import numpy as np
import torch

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "data/benchmarks/tts-correctness"
REFERENCE_COMMIT = "afd9dfda7ffdca35dd0151b149c221140c80ec3f"
AUDIO_BASE = 151675
AUDIO_COUNT = 6561
ENDING_IDS = [151670, 151674]


def compile_native_check():
    source = ROOT / "models/audio-cpp-src/src/community_models/kitten_tts2/session.cpp"
    text = source.read_text()
    start = text.index("            std::unordered_set<int32_t> seen;")
    end = text.index("            auto token=sampler.sample", start)
    block = text[start:end]
    program = """#include <iostream>
#include <iomanip>
#include <vector>
#include <unordered_set>
#include <cstdint>
int main() {
    int count;
    while (std::cin >> count) {
        std::vector<int32_t> codes(count);
        for (auto & code : codes) std::cin >> code;
        std::vector<float> logits(6563);
        for (auto & value : logits) std::cin >> value;
        float repetition = 1.1f;
""" + block + """
        std::cout << std::setprecision(9);
        for (float value : logits) std::cout << value << " ";
        std::cout << "\\n";
    }
}
"""
    source_file = OUTPUT_DIR / "native-sampling-check.cpp"
    executable = OUTPUT_DIR / "native-sampling-check"
    source_file.write_text(program)
    subprocess.run(["g++", "-O2", "-std=c++17", str(source_file), "-o", str(executable)], check=True)
    return executable


def load_reference():
    path = ROOT / "models/kitten-reference-src/kittenml/kittentts2/logits.py"
    spec = importlib.util.spec_from_file_location("kitten_upstream_logits", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_case(executable, reference, name, codes):
    compact = np.linspace(-2, 2, AUDIO_COUNT + 2, dtype=np.float32)
    compact[4299] = -10 if name == "silence_negative" else 10
    full = torch.zeros(1, 158241, dtype=torch.float32)
    full[0, AUDIO_BASE:AUDIO_BASE + AUDIO_COUNT] = torch.from_numpy(compact[:AUDIO_COUNT])
    full[0, ENDING_IDS] = torch.from_numpy(compact[AUDIO_COUNT:])
    history = torch.tensor([[151673, *[AUDIO_BASE + code for code in codes]]])
    processor = reference.RepetitionPenaltyExempt(1.1, ENDING_IDS, window=50, prompt_length=1)
    expected_full = processor(history, full)
    expected = torch.cat([
        expected_full[0, AUDIO_BASE:AUDIO_BASE + AUDIO_COUNT],
        expected_full[0, ENDING_IDS],
    ]).numpy()
    stdin = f"{len(codes)} " + " ".join(map(str, codes)) + " " + " ".join(map(str, compact)) + "\n"
    stdout = subprocess.check_output([str(executable)], input=stdin, text=True)
    actual = np.fromstring(stdout, sep=" ", dtype=np.float32)
    error = float(np.max(np.abs(actual - expected)))
    return {
        "case": name,
        "max_abs_error": error,
        "passed": error < 1e-6,
        "silence_native": float(actual[4299]),
        "silence_reference": float(expected[4299]),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", default="sampling-parity.json", type=Path,
                        help="Result path, relative to data/benchmarks/tts-correctness unless absolute")
    args = parser.parse_args()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    executable = compile_native_check()
    reference = load_reference()
    cases = [
        ("ordinary", [22]),
        ("silence_positive", [4299]),
        ("silence_negative", [4299]),
        ("duplicates", [4299, 4299, 22]),
        ("window", [4299] + [11] * 50),
        ("empty", []),
    ]
    results = [check_case(executable, reference, name, codes) for name, codes in cases]
    report = {"upstream_source_commit": REFERENCE_COMMIT, "cases": results,
              "passed": all(row["passed"] for row in results)}
    destination = OUTPUT_DIR / args.output
    destination.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
