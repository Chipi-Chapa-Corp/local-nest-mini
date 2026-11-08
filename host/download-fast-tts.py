"""Prepare portable local assets; downloads happen only during setup."""

from pathlib import Path
import shutil
from urllib.request import urlretrieve

import yaml
from pocket_tts.utils.config import CONFIGS_DIR
from pocket_tts.utils.utils import download_if_necessary, get_predefined_voice


root = Path(__file__).resolve().parent.parent
assets = root / "models/fast-tts"
assets.mkdir(parents=True, exist_ok=True)
silero = assets / "silero-v5_6_ru.pt"
if not silero.is_file():
    temporary = silero.with_suffix(".download")
    urlretrieve("https://models.silero.ai/models/tts/ru/v5_6_ru.pt", temporary)
    temporary.replace(silero)

language = "english_2m_2026-10"
config = yaml.safe_load((CONFIGS_DIR / f"{language}.yaml").read_text())
for source, filename in (
    (config["weights_path_without_voice_cloning"], "pocket-model.safetensors"),
    (config["flow_lm"]["lookup_table"]["tokenizer_path"], "pocket-tokenizer.model"),
    (get_predefined_voice(language, "alba"), "pocket-alba.safetensors"),
):
    destination = assets / filename
    if not destination.is_file():
        shutil.copyfile(download_if_necessary(source), destination)

# Relative paths keep the bundle usable after moving the checkout or in Compose.
config["weights_path"] = "pocket-model.safetensors"
config["weights_path_without_voice_cloning"] = None
config["flow_lm"]["lookup_table"]["tokenizer_path"] = "pocket-tokenizer.model"
(assets / "pocket.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
