#!/usr/bin/env python3
"""Show only the four requested OmniVoice tracks, prompts and measured costs."""
import csv
import html
import json
import os
from pathlib import Path
import re
import wave

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT/'data/benchmarks/omnivoice/sound-suite'
REVIEW = ROOT/'data/benchmarks/tts-correctness'
expected=[('ru','Russian'),('en','English — same story'),('ru-en','Russian → English within sentences'),('en-ru','English → Russian within sentences')]
results=[json.loads((OUT/f'{identifier}-results.json').read_text()) for identifier,_ in expected if (OUT/f'{identifier}-results.json').exists()]
assert results

def esc(text):
    return html.escape(str(text), quote=True)

for r in results:
    with wave.open(str(OUT/r['output'])) as w:
        assert w.getnchannels()==1 and w.getsampwidth()==2 and w.getframerate()==24000
        assert abs(w.getnframes()/w.getframerate()-r['audio_seconds']) < 0.001

# Standard plotting library produces self-contained SVG artifacts and PNG exports.
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
plt.style.use('dark_background')
for r in results:
    if (OUT/f'{r["id"]}-resources.svg').exists() and (OUT/f'{r["id"]}-resources.png').exists():
        continue
    with (OUT/f'{r["id"]}-resources.csv').open() as f:
        samples=list(csv.DictReader(f))
    t=[float(s['seconds']) for s in samples]
    rss=[float(s['rss_mib'])/1024 for s in samples]
    # One-second windows make CPU readings interpretable instead of 100ms spikes.
    cpu_t=[]; cpu=[]; base=0
    for i in range(1,len(samples)):
        dt=t[i]-t[base]
        if dt>=1.0:
            cpu_t.append(t[i]); cpu.append(100*(float(samples[i]['cpu_seconds'])-float(samples[base]['cpu_seconds']))/dt)
            base=i
    fig,(ax1,ax2)=plt.subplots(2,1,figsize=(9,3.8),sharex=True,layout='constrained')
    fig.patch.set_facecolor('#18222e')
    for ax in (ax1,ax2):
        ax.set_facecolor('#18222e'); ax.grid(alpha=.18); ax.spines[['top','right']].set_visible(False)
    ax1.plot(cpu_t,cpu,color='#80d8ff',lw=1.5); ax1.set_ylabel('CPU %'); ax1.set_ylim(bottom=0)
    ax1.axhline(600,alpha=.3,ls='--',color='white')
    ax2.plot(t,rss,color='#a3e0b5',lw=1.5); ax2.set_ylabel('RSS (GiB)'); ax2.set_xlabel('Seconds since model load began'); ax2.set_ylim(bottom=0)
    for ax in (ax1,ax2):
        ax.axvline(r['load_seconds'],alpha=.5,ls=':',color='#ffc784')
    fig.savefig(OUT/f'{r["id"]}-resources.svg',facecolor=fig.get_facecolor())
    fig.savefig(OUT/f'{r["id"]}-resources.png',facecolor=fig.get_facecolor(),dpi=150)
    plt.close(fig)

CSS='''
:root{color-scheme:dark}*{box-sizing:border-box}body{font:16px/1.6 system-ui,sans-serif;margin:36px auto;padding:0 20px;max-width:1000px;background:#10151c;color:#edf1f5}h1{font-size:29px;line-height:1.25;margin-bottom:12px}h2{font-size:21px;margin:0 0 12px}p{color:#bdc9d8}a{color:#86cfff}section{margin:24px 0;padding:22px;background:#18222e;border:1px solid #2b3949;border-radius:12px}audio{display:block;width:100%;margin:14px 0}.scroll{overflow:auto}table{width:100%;border-collapse:collapse;font-size:15px;font-variant-numeric:tabular-nums}th,td{padding:10px 12px;border-bottom:1px solid #34404d;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}th{font-weight:600;color:#bdc9d8}details{margin-top:14px}summary{cursor:pointer;color:#b8d8f4}code{font-size:.83em;color:#ffd198;background:#29313b;border-radius:4px;padding:2px 5px}ol{padding-left:26px}li{margin:10px 0}.stats{display:flex;flex-wrap:wrap;gap:8px 20px;color:#c1d2e3;font-size:14px}.taglist{line-height:2.2}.caption{font-size:14px;color:#aab9ca}img{max-width:100%;height:auto}footer{margin:32px 0;color:#aab9ca;font-size:14px}.chip{color:#b8ead0}.language{font-size:14px;color:#abbccc}button{font:inherit;cursor:pointer;border-radius:6px;background:#293d52;color:#eef6ff;border:1px solid #47627e;padding:8px 12px}@media(max-width:600px){body{padding:0 12px}section{padding:16px}h1{font-size:25px}}
'''

for folder,names in [(OUT,['index.html']),(OUT.parent,['index.html']),(REVIEW,['index.html','controls.html'])]:
    rel=lambda path: esc(os.path.relpath(path,folder))
    rows=[];cards=[]
    for r in results:
        identifier=r['id']
        rows.append(f'<tr><td><a href="#{identifier}">{esc(r["title"])}</a></td><td>{r["audio_seconds"]:.1f}s</td><td>{r["generation_seconds"]:.1f}s</td><td>{r["rtf"]:.2f}×</td><td>{r["average_cpu_percent"]:.0f}%</td><td>{r["peak_process_rss_mib"]/1024:.2f} GiB</td></tr>')
        text_items=''.join(f'<li><code>{esc(tag)}</code> {esc(sentence)}</li>' for tag,sentence in zip(re.findall(r'\[[^]]+\]',r['text']),r['sentences']))
        language_label={'ru':'Russian (ru)','en':'English (en)',None:'Language-agnostic (no forced language)'}[r['language']]
        cards.append(f'''<section id="{identifier}"><h2>{esc(r['title'])}</h2><div class="language">{language_label} · all 13 sound tags, once each</div><audio controls preload="none" src="{rel(OUT/r['output'])}"></audio><div class="stats"><span>Audio <b>{r['audio_seconds']:.2f}s</b></span><span>Generation <b>{r['generation_seconds']:.2f}s</b></span><span>CPU <b>{r['average_cpu_percent']:.0f}%</b> ({r['average_cpu_cores']:.2f} equivalents)</span><span>Peak RAM <b>{r['peak_process_rss_mib']/1024:.2f} GiB</b></span></div><details><summary>Exact text and sound tags</summary><ol lang="{r['language'] or 'mul'}">{text_items}</ol></details><details><summary>Resource timeline and detailed measurements</summary><img src="{rel(OUT/(identifier+'-resources.svg'))}" alt="CPU percentage and resident RAM over time for {esc(r['title'])}"><div class="scroll"><table><tbody><tr><th>Model and audio-tokenizer load</th><td>{r['load_seconds']:.2f}s</td></tr><tr><th>Total fresh-process wall time (imports → exit)</th><td>{r['process_wall_seconds_including_imports_load_write_exit']:.2f}s</td></tr><tr><th>Generation user / system CPU time</th><td>{r['cpu_user_seconds']:.2f}s / {r['cpu_system_seconds']:.2f}s</td></tr><tr><th>Generation peak RSS, sampled</th><td>{r['peak_generation_sampled_rss_mib']/1024:.2f} GiB</td></tr><tr><th>Process peak swap, sampled</th><td>{r['peak_sampled_swap_mib']:.1f} MiB</td></tr><tr><th>Generation major page faults</th><td>{r['major_faults']}</td></tr><tr><th>Waveform clipping</th><td>{r['clipped_samples']} samples</td></tr></tbody></table></div><p class="caption">Six inference threads. The process can also have idle library threads. CPU: 100% = one logical CPU busy; 600% = six CPU equivalents. Orange dotted line marks model load completion. RSS/swap samples every 100 ms; peak process RSS comes from the kernel high-water mark.</p><a href="{rel(OUT/(identifier+'-resources.csv'))}" download>Resource CSV</a> · <a href="{rel(OUT/(identifier+'-results.json'))}" download>Full measurements and configuration</a> · <a href="{rel(OUT/r['output'])}" download>Download WAV</a></details></section>''')
    for identifier,title in expected:
        if identifier not in {r['id'] for r in results}:
            rows.append(f'<tr><td>{esc(title)}</td><td colspan="5">Generation pending</td></tr>')
            cards.append(f'<section id="{identifier}"><h2>{esc(title)}</h2><p>Generating — reload this page after the next progress update.</p></section>')
    tags=re.findall(r'\[[^]]+\]',results[0]['text'])
    page=f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>OmniVoice — Russian, English and mixed speech</title><style>{CSS}</style></head><body><h1>OmniVoice: Russian, English and mixed speech</h1><p>One breakfast-and-travel story. Russian and English are translations; the mixed versions switch languages within the same sentences. All four request every supported vocal sound in the same order.</p><p class="caption">Official OmniVoice CPU · F32 · 6 threads · 32 steps · seed 1234 · voice instruction: female, young adult · text normalization off. Four sequential fresh processes, one run per track; automatic upstream long-text chunking left at defaults.</p><div class="scroll"><table><thead><tr><th>Track</th><th>Audio</th><th>Generate</th><th>RTF ↓</th><th>Avg CPU</th><th>Peak RAM</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div><p class="caption">RTF = generation time ÷ audio duration (1× means real-time). CPU/RAM refer to the synthesis process, not the whole computer. Peak RAM includes load and generation. Runs share the host with other applications; these are measurements of this audition, not repeated throughput benchmarks. Sound tags are requests, not verified successful events.</p>{''.join(cards)}<details><summary>All requested sounds</summary><p class="taglist">{' '.join('<code>'+esc(tag)+'</code>' for tag in tags)}</p></details><footer>Ryzen 5 7600X · 6 physical cores / 12 logical CPUs · about 30 GiB RAM · CPU-only inference, no GPU. Power and energy were not measured. <a href="{rel(OUT/'host.json')}">Host and measurement method</a>.</footer><script>document.querySelectorAll('audio').forEach(player=>player.addEventListener('play',()=>document.querySelectorAll('audio').forEach(other=>{{if(other!==player)other.pause()}})))</script></body></html>'''
    for name in names:
        path=folder/name
        if path.exists() and folder!=OUT:
            archive=folder/(path.stem+'-before-omnivoice-sound-suite.html')
            if not archive.exists(): archive.write_bytes(path.read_bytes())
        path.write_text(page)
(REVIEW/'active-review.json').write_text(json.dumps(dict(page='index.html',purpose='OmniVoice only: translated Russian/English and two within-sentence code-switch tracks, all 13 sounds, per-track resource measurements',players=len(results),results='../omnivoice/sound-suite/results.json'),indent=2)+'\n')
print(f'Focused review updated: {len(results)}/4 OmniVoice players ready, measurements and expandable exact prompts.')
