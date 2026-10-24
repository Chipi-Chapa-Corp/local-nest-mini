#!/usr/bin/env python3
"""Focus the audition on approved English and its fully Russian translation."""
from pathlib import Path
import html
import json
import os
import re
import runpy
import wave

ROOT=Path(__file__).resolve().parents[2]
BASE=ROOT/'data/benchmarks/omnivoice'
REVIEW=ROOT/'data/benchmarks/tts-correctness'
en=json.loads((BASE/'sound-suite/en-results.json').read_text())
candidate=BASE/'translation/ru-results.json'
validation=BASE/'translation/validation.json'
validation_result=json.loads(validation.read_text()) if validation.exists() else {}
ru=json.loads(candidate.read_text()) if candidate.exists() and validation_result.get('speech_present') and validation_result.get('publishable_word_completeness') else None
spec=runpy.run_path(str(ROOT/'host/benchmarks/run_omnivoice.py'))
ru_text=spec['passage'](spec['RU'])
esc=lambda value:html.escape(str(value),quote=True)
CSS='''
:root{color-scheme:dark}*{box-sizing:border-box}body{font:16px/1.65 system-ui,sans-serif;margin:36px auto;padding:0 20px;max-width:920px;background:#10151c;color:#edf1f5}h1{font-size:28px;line-height:1.3}h2{font-size:21px;margin:0 0 10px}p{color:#bfccda}a{color:#8ed2ff}section{padding:22px;margin:24px 0;background:#18222e;border:1px solid #304052;border-radius:12px}audio{display:block;width:100%;margin:16px 0}.metrics{display:flex;gap:8px 22px;flex-wrap:wrap;font-size:15px;color:#ccdbe9}summary{cursor:pointer;color:#b6d8f4}details{margin-top:16px}code{color:#ffd198;font-size:.85em}li{padding:6px 0}img{width:100%;height:auto}.note{font-size:14px;color:#aebdcc}.good{color:#a5e7b9}.pending{color:#ffd198}.scroll{overflow:auto}table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}td,th{text-align:right;padding:10px;border-bottom:1px solid #344253;white-space:nowrap}td:first-child,th:first-child{text-align:left}footer{margin:32px 0}
'''

def plot(r,folder):
    graph=folder/f'{r["id"]}-resources.svg'
    if graph.exists():return
    import csv
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.style.use('dark_background')
    with (folder/f'{r["id"]}-resources.csv').open() as f:s=list(csv.DictReader(f))
    t=[float(x['seconds']) for x in s];cpu_t=[];cpu=[];base=0
    for i in range(1,len(s)):
        dt=t[i]-t[base]
        if dt>=1:
            cpu_t.append(t[i]);cpu.append(100*(float(s[i]['cpu_seconds'])-float(s[base]['cpu_seconds']))/dt);base=i
    fig,(a,b)=plt.subplots(2,1,figsize=(9,3.8),sharex=True,layout='constrained')
    fig.patch.set_facecolor('#18222e')
    for ax in (a,b):ax.set_facecolor('#18222e');ax.grid(alpha=.18)
    a.plot(cpu_t,cpu,color='#80d8ff');a.set_ylabel('CPU %');a.axhline(600,color='white',alpha=.3,ls='--')
    b.plot(t,[float(x['rss_mib'])/1024 for x in s],color='#a3e0b5');b.set_ylabel('RSS (GiB)');b.set_xlabel('Seconds since model load began')
    for ax in (a,b):ax.set_ylim(bottom=0)
    fig.savefig(graph,facecolor=fig.get_facecolor());fig.savefig(graph.with_suffix('.png'),facecolor=fig.get_facecolor(),dpi=150);plt.close(fig)

tracks=[('English — same story',en,BASE/'sound-suite','Approved by listener')]
if ru:tracks.append(('Russian — full translation',ru,BASE/'translation','Replacement candidate — speech present; pronunciation and sounds need listening'))
for _,r,folder,_ in tracks:
    with wave.open(str(folder/r['output'])) as w:
        assert w.getnchannels()==1 and w.getframerate()==24000 and w.getsampwidth()==2
        assert abs(w.getnframes()/w.getframerate()-r['audio_seconds'])<.001
    plot(r,folder)

for folder,names in [(REVIEW,['index.html','controls.html']),(BASE,['index.html']),(BASE/'translation',['index.html'])]:
    folder.mkdir(exist_ok=True)
    rel=lambda p:esc(os.path.relpath(p,folder))
    cards=[];rows=[]
    for title,r,source,status in tracks:
        words=len(re.findall(r"[^\W\d_]+(?:[’'][^\W\d_]+)*",re.sub(r'\[[^]]+\]','',r['text'])))
        speed=words/r['generation_seconds'];spoken=words/r['audio_seconds']
        rows.append(f'<tr><td>{esc(title)}</td><td>{r["audio_seconds"]:.2f}s</td><td>{r["generation_seconds"]:.2f}s</td><td>{speed:.2f}</td><td>{r["average_cpu_percent"]:.0f}%</td><td>{r["peak_process_rss_mib"]/1024:.2f} GiB</td></tr>')
        text=''.join(f'<li><code>{esc(tag)}</code> {esc(sentence)}</li>' for tag,sentence in zip(re.findall(r'\[[^]]+\]',r['text']),r['sentences']))
        cards.append(f'''<section><h2>{esc(title)}</h2><div class="good">{esc(status)}</div><audio controls preload="none" src="{rel(source/r['output'])}"></audio><div class="metrics"><span>Generation <b>{speed:.2f} script words/s</b></span><span>Speaking <b>{spoken:.2f} words/s</b> (~{60*spoken:.0f} wpm)</span><span>RTF <b>{r['rtf']:.2f}×</b></span></div><details><summary>Exact text and sound requests</summary><ol lang="{r['language']}">{text}</ol></details><details><summary>CPU, RAM and full measurements</summary><img src="{rel(source/(r['id']+'-resources.svg'))}" alt="CPU percentage and RAM over time"><p class="note">Average CPU {r['average_cpu_percent']:.1f}% · peak RSS {r['peak_process_rss_mib']/1024:.2f} GiB · sampled peak swap {r['peak_sampled_swap_mib']:.1f} MiB · model load {r['load_seconds']:.2f}s · total process {r['process_wall_seconds_including_imports_load_write_exit']:.2f}s.<br>CPU 100% = one logical CPU fully busy. RSS/swap sampled every 100 ms; peak RSS is the kernel high-water mark.</p><a href="{rel(source/(r['id']+'-results.json'))}">Full measurements</a> · <a href="{rel(source/(r['id']+'-resources.csv'))}" download>Resource CSV</a> · <a href="{rel(source/r['output'])}" download>Download WAV</a></details></section>''')
        if r['language']=='ru':
            issue_words=', '.join('«'+esc(w)+'»' for w in validation_result['unmatched_target_words'])
            note=f'<p class="note">Speech check recovered {validation_result["matched_target_words"]}/{validation_result["target_words"]} scripted words. Listen especially to {issue_words}. Differences may be pronunciation or recognition errors; sounds and naturalness are not scored by ASR.</p><details><summary>Automatic transcript</summary><p lang="ru">{esc(validation_result["transcript"])}</p></details>'
            cards[-1]=cards[-1].replace('</audio>','</audio>'+note,1).replace('<div class="good">','<div class="pending">',1)
    if not ru:
        ru_items=''.join(f'<li><code>{esc(tag)}</code> {esc(sentence)}</li>' for tag,sentence in zip(spec['TAGS'],spec['RU']))
        cards.append(f'<section><h2>Russian — full translation</h2><p class="pending">The failed noise recording has been removed. A Russian translation using the approved English voice as a reference is being checked.</p><details><summary>Exact Russian translation and sound requests</summary><ol lang="ru">{ru_items}</ol></details></section>')
    page=f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>OmniVoice — English and Russian translation</title><style>{CSS}</style></head><body><h1>OmniVoice — English and Russian translation</h1><p>The approved English recording is preserved. Russian uses the same story translated entirely into Russian, with the same 13 sound requests.</p><p class="note">F32 · official PyTorch CPU · 6 threads · 32 diffusion steps · seed 1234 · normalization off. Russian uses a 4.32-second voice reference from this English recording.</p><div class="scroll"><table><thead><tr><th>Track</th><th>Audio</th><th>Generation</th><th>Script words/s</th><th>Avg CPU</th><th>Peak RAM</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div><p class="note">Generation rate counts scripted words divided by synthesis time. Speaking rate counts scripted words divided by full recording duration, including pauses and vocal sounds. Single runs on a shared Ryzen 5 7600X; no power measurement. Tags are requested events, not automatically scored as successful.</p>{''.join(cards)}<script>document.querySelectorAll('audio').forEach(p=>p.addEventListener('play',()=>document.querySelectorAll('audio').forEach(q=>{{if(q!==p)q.pause()}})))</script></body></html>'''
    for name in names:(folder/name).write_text(page)
(REVIEW/'active-review.json').write_text(json.dumps(dict(page='index.html',purpose='Approved English and fully Russian translation; failed noise and mixed samples removed',players=len(tracks)),indent=2)+'\n')
print(f'Focused translation review: {len(tracks)} players ready.')
