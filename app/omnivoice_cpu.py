"""CPU inference adjustments for the upstream OmniVoice model.

The audio codec and upstream sampling loop remain unchanged. The transformer
evaluates the two guidance paths at their actual lengths; unused context logits
are never projected. This helper is opt-in and does not change the server backend.
"""
from types import MethodType


def configure_cpu_inference(model, *, precision="bf16", split_guidance=True):
    import torch
    from omnivoice.models.omnivoice import OmniVoiceModelOutput

    if precision not in {"fp32", "bf16"}:
        raise ValueError("precision must be fp32 or bf16")
    if model.device.type != "cpu" or any(p.device.type != "cpu" for p in model.parameters()):
        raise ValueError("This configuration targets CPU inference")
    dtype = torch.bfloat16 if precision == "bf16" else torch.float32
    # The codec includes sensitive nearest-neighbor operations; keep it in F32.
    for component in (model.llm, model.audio_embeddings, model.audio_heads):
        component.to(dtype=dtype)
    # MaskGIT recomputes the full context every step, so a KV cache is unused.
    model.llm.config.use_cache = False
    original = getattr(model, "_cpu_original_forward", model.forward)
    model._cpu_original_forward = original
    if not split_guidance:
        model.forward = original
        return

    def forward(self, input_ids, audio_mask, labels=None, attention_mask=None,
                document_ids=None, position_ids=None):
        if (self.training or labels is not None or document_ids is not None
                or position_ids is not None or input_ids.shape[0] != 2
                or attention_mask is None or attention_mask.ndim != 4
                or attention_mask.shape[0] != 2 or attention_mask.dtype != torch.bool):
            return original(input_ids=input_ids, audio_mask=audio_mask,
                            labels=labels, attention_mask=attention_mask,
                            document_ids=document_ids, position_ids=position_ids)
        target_length = int(audio_mask[1].sum().item())
        context_length = input_ids.shape[-1]
        if target_length < 1 or not bool(audio_mask[1, :target_length].all()):
            return original(input_ids=input_ids, audio_mask=audio_mask,
                            labels=labels, attention_mask=attention_mask,
                            document_ids=document_ids, position_ids=position_ids)
        cond = self.llm(
            inputs_embeds=self._prepare_embed_inputs(input_ids[:1], audio_mask[:1]),
            attention_mask=attention_mask[:1], use_cache=False, return_dict=True,
        ).last_hidden_state[:, -target_length:]
        uncond = self.llm(
            inputs_embeds=self._prepare_embed_inputs(
                input_ids[1:, :, :target_length], audio_mask[1:, :target_length]),
            attention_mask=attention_mask[1:, :, :target_length, :target_length],
            use_cache=False, return_dict=True,
        ).last_hidden_state
        hidden = torch.cat((cond, uncond), dim=0)
        logits = self.audio_heads(hidden).view(
            2, target_length, self.config.num_audio_codebook,
            self.config.audio_vocab_size).permute(0, 2, 1, 3)
        # Preserve the upstream sampler's indexing contract. It reads only the
        # conditional tail and unconditional prefix, never the context slots.
        result = logits.new_zeros((2, self.config.num_audio_codebook,
                                   context_length, self.config.audio_vocab_size))
        result[:1, :, -target_length:] = logits[:1]
        result[1:, :, :target_length] = logits[1:]
        return OmniVoiceModelOutput(logits=result)

    model.forward = MethodType(forward, model)
