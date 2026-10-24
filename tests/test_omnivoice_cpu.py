"""Check guidance padding removal against a small bidirectional attention model."""
import copy
from types import SimpleNamespace
import unittest

try:
    import torch
    from omnivoice.models.omnivoice import OmniVoiceModelOutput
except ImportError:
    torch = None

from app.omnivoice_cpu import configure_cpu_inference

if torch is not None:
    class Backbone(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.embed = torch.nn.Embedding(32, 8)
            self.proj = torch.nn.Linear(8, 8)
            self.config = SimpleNamespace(use_cache=True)
            self.calls = []

        def forward(self, inputs_embeds, attention_mask, use_cache=None, return_dict=True):
            self.calls.append((inputs_embeds.shape[:2], use_cache))
            scores = inputs_embeds @ inputs_embeds.transpose(-1, -2) / 8**0.5
            scores = scores.masked_fill(~attention_mask[:, 0], -float("inf"))
            hidden = self.proj(scores.softmax(-1) @ inputs_embeds) + inputs_embeds
            return SimpleNamespace(last_hidden_state=hidden)

    class Model(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.llm = Backbone()
            self.audio_embeddings = torch.nn.Embedding(18, 8)
            self.audio_heads = torch.nn.Linear(8, 18, bias=False)
            self.audio_tokenizer = torch.nn.Linear(8, 8)
            self.config = SimpleNamespace(num_audio_codebook=2, audio_vocab_size=9)

        @property
        def device(self):
            return self.audio_heads.weight.device

        def _prepare_embed_inputs(self, ids, audio_mask):
            offsets = torch.tensor([0, 9]).view(1, 2, 1)
            audio = self.audio_embeddings(ids * audio_mask[:, None] + offsets).sum(1)
            text = self.llm.embed(ids[:, 0])
            return torch.where(audio_mask[:, :, None], audio, text)

        def forward(self, input_ids, audio_mask, attention_mask=None, **kwargs):
            hidden = self.llm(self._prepare_embed_inputs(input_ids, audio_mask),
                              attention_mask, use_cache=self.llm.config.use_cache).last_hidden_state
            b, s, _ = hidden.shape
            logits = self.audio_heads(hidden).view(b, s, 2, 9).permute(0, 2, 1, 3)
            return OmniVoiceModelOutput(logits=logits)


@unittest.skipIf(torch is None, "Requires the separate OmniVoice environment")
class OmniVoiceCPUTests(unittest.TestCase):
    def test_used_logits_match_with_different_padding_and_audio_codes(self):
        for prefix, target in [(0, 2), (3, 5), (7, 3)]:
            with self.subTest(prefix=prefix, target=target):
                torch.manual_seed(42)
                model = Model().eval()
                reference = copy.deepcopy(model)
                length = prefix + target
                ids = torch.randint(0, 9, (2, 2, length))
                ids[1, :, :target] = ids[0, :, -target:]
                audio = torch.zeros((2, length), dtype=torch.bool)
                audio[0, -target:] = True
                audio[1, :target] = True
                mask = torch.zeros((2, 1, length, length), dtype=torch.bool)
                mask[0] = True
                mask[1, :, :target, :target] = True
                for i in range(target, length):
                    mask[1, :, i, i] = True
                inputs = dict(input_ids=ids, audio_mask=audio, attention_mask=mask)
                configure_cpu_inference(model, precision="fp32")
                with torch.inference_mode():
                    expected = reference(**inputs).logits
                    observed = model(**inputs).logits
                torch.testing.assert_close(observed[:1, :, -target:], expected[:1, :, -target:])
                torch.testing.assert_close(observed[1:, :, :target], expected[1:, :, :target])
                self.assertEqual([tuple(s) for s, _ in model.llm.calls], [(1, length), (1, target)])
                self.assertTrue(all(cache is False for _, cache in model.llm.calls))

    def test_codec_stays_in_full_precision(self):
        model = Model().eval()
        configure_cpu_inference(model, precision="bf16")
        self.assertTrue(all(p.dtype == torch.bfloat16 for p in model.llm.parameters()))
        self.assertTrue(all(p.dtype == torch.float32 for p in model.audio_tokenizer.parameters()))

    def test_non_guidance_batch_falls_back_to_original_forward(self):
        model = Model().eval()
        reference = copy.deepcopy(model)
        configure_cpu_inference(model, precision="fp32")
        inputs = dict(input_ids=torch.ones((1, 2, 4), dtype=torch.long),
                      audio_mask=torch.ones((1, 4), dtype=torch.bool),
                      attention_mask=torch.ones((1, 1, 4, 4), dtype=torch.bool))
        with torch.inference_mode():
            torch.testing.assert_close(model(**inputs).logits, reference(**inputs).logits)
        self.assertEqual(len(model.llm.calls), 1)

    def test_non_guidance_audio_layout_falls_back(self):
        model = Model().eval()
        reference = copy.deepcopy(model)
        configure_cpu_inference(model, precision="fp32")
        inputs = dict(input_ids=torch.ones((2, 2, 4), dtype=torch.long),
                      audio_mask=torch.tensor([[True, False, True, False],
                                               [False, False, True, False]]),
                      attention_mask=torch.ones((2, 1, 4, 4), dtype=torch.bool))
        with torch.inference_mode():
            torch.testing.assert_close(model(**inputs).logits, reference(**inputs).logits)
        self.assertEqual(len(model.llm.calls), 1)


if __name__ == "__main__":
    unittest.main()
