#!/usr/bin/env python3
"""Compare used logits and speed on actual first-chunk English/Russian inputs."""
import json,os,runpy,sys,time
from pathlib import Path
os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',OMP_NUM_THREADS='6',MKL_NUM_THREADS='6',OPENBLAS_NUM_THREADS='1')
import torch
from omnivoice import OmniVoice
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from app.omnivoice_cpu import configure_cpu_inference
OUT=ROOT/'data/benchmarks/omnivoice/optimization'
torch.set_num_threads(6);torch.set_num_interop_threads(1)
spec=runpy.run_path(str(ROOT/'host/benchmarks/run_omnivoice.py'))
model=OmniVoice.from_pretrained(str(ROOT/'models/omnivoice'),device_map='cpu',dtype=torch.float32,attn_implementation='sdpa',local_files_only=True).eval()
class Captured(Exception):pass
inputs=[]
def capture(module,args,kwargs):
    inputs.append({k:v.detach().clone() for k,v in kwargs.items()});raise Captured()
for lang in ['en','ru']:
    h=model.register_forward_pre_hook(capture,with_kwargs=True)
    try:
        with torch.inference_mode():
            model.generate(text=spec['passage'](spec[lang.upper()]),language=lang,
                instruct='female, young adult' if lang=='en' else None,
                ref_audio=str(ROOT/'data/benchmarks/omnivoice/sound-suite/approved-en-reference.wav') if lang=='ru' else None,
                ref_text='We finally made breakfast without burning the toast.' if lang=='ru' else None,num_step=32,normalize_text=False)
    except Captured:pass
    finally:h.remove()
torch.save(inputs,OUT/'forward-inputs.pt')
references={};rows=[]
def used(logits,inp):
    n=int(inp['audio_mask'][1].sum());return torch.cat((logits[:1,:,-n:],logits[1:,:,:n]),0).float()
for mode in ['upstream-fp32','split-fp32','split-bf16']:
    if mode!='upstream-fp32':configure_cpu_inference(model,precision=mode.split('-')[1])
    for lang,inp in zip(['en','ru'],inputs):
        with torch.inference_mode():
            model(**inp)
            times=[]
            for _ in range(3):
                start=time.monotonic();out=model(**inp).logits;times.append(time.monotonic()-start)
            selected=used(out,inp)
        if mode=='upstream-fp32':references[lang]=selected.clone()
        diff=(selected-references[lang]).abs()
        row=dict(mode=mode,language=lang,seconds=times,median_seconds=sorted(times)[1],
            max_logit_error=float(diff.max()),mean_logit_error=float(diff.mean()),
            argmax_agreement=float((selected.argmax(-1)==references[lang].argmax(-1)).float().mean()))
        rows.append(row);(OUT/'optimized-forward-profile.json').write_text(json.dumps(rows,indent=2)+'\n');print(json.dumps(row),flush=True)
