#!/usr/bin/env python3
"""Time real OmniVoice forward shapes without generating an audio sample matrix."""
import json
import os
from pathlib import Path
import resource
import runpy
import time
os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',OMP_NUM_THREADS='6',MKL_NUM_THREADS='6',OPENBLAS_NUM_THREADS='1')
import torch
from omnivoice import OmniVoice
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'data/benchmarks/omnivoice/optimization'
OUT.mkdir(exist_ok=True)
torch.set_num_threads(6);torch.set_num_interop_threads(1)
spec=runpy.run_path(str(ROOT/'host/benchmarks/run_omnivoice.py'))
model=OmniVoice.from_pretrained(str(ROOT/'models/omnivoice'),device_map='cpu',dtype=torch.float32,attn_implementation='sdpa',local_files_only=True).eval()
captured=[]
class Captured(Exception):pass
def capture(module,args,kwargs):
    captured.append({k:v.detach().clone() for k,v in kwargs.items()})
    raise Captured()

workloads=[]
for language,text,ref,ref_text in [
    ('en',spec['passage'](spec['EN']),None,None),
    ('ru',spec['passage'](spec['RU']),ROOT/'data/benchmarks/omnivoice/sound-suite/approved-en-reference.wav','We finally made breakfast without burning the toast.'),
]:
    handle=model.register_forward_pre_hook(capture,with_kwargs=True)
    try:
        with torch.inference_mode():
            model.generate(text=text,language=language,instruct='female, young adult' if ref is None else None,
                ref_audio=str(ref) if ref else None,ref_text=ref_text,num_step=32,normalize_text=False)
    except Captured:pass
    finally:handle.remove()
    workloads.append((language,captured[-1]))
results=[]
for dtype in [torch.float32,torch.bfloat16]:
    model.llm.to(dtype);model.audio_embeddings.to(dtype);model.audio_heads.to(dtype)
    for language,inputs in workloads:
        for threads in [4,6,8]:
            torch.set_num_threads(threads)
            with torch.inference_mode():
                model(**inputs)
                times=[]
                for _ in range(3):
                    started=time.monotonic();out=model(**inputs).logits.float();times.append(time.monotonic()-started)
            row=dict(language=language,dtype=str(dtype),threads=threads,shape=list(inputs['input_ids'].shape),seconds=times,
                median_seconds=sorted(times)[1],finite=bool(out.isfinite().all()),peak_rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024)
            results.append(row)
            (OUT/'forward-profile.json').write_text(json.dumps(results,indent=2)+'\n')
            print(json.dumps(row),flush=True)
