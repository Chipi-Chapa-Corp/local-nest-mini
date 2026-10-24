#!/usr/bin/env python3
"""Two players, with selectable approved references and passing CPU candidates."""
import html
import json
import os
from pathlib import Path
import re
import wave
from omnivoice_review_common import plot_resources

ROOT=Path(__file__).resolve().parents[2]
BASE=ROOT/'data/benchmarks/omnivoice'
OUT=BASE/'optimization'
REVIEW=ROOT/'data/benchmarks/tts-correctness'
esc=lambda text:html.escape(str(text),quote=True)
references={'en':BASE/'sound-suite','ru':BASE/'translation'}
candidates={
    'en':[('16 steps — CPU candidate',OUT/'fast16-en'),('32 steps — CPU candidate',OUT/'quality32')],
    'ru':[('32 steps — CPU candidate',OUT/'quality32-ru'),('32 steps — full precision CPU candidate',OUT/'precise32-ru')],
}
tracks={}
for language in ['en','ru']:
    baseline=json.loads((references[language]/f'{language}-results.json').read_text())
    entries=[]
    for label,folder in candidates[language]+[('Approved English reference' if language=='en' else 'Previous Russian reference',references[language])]:
        result_file=folder/f'{language}-results.json'
        validation_file=folder/('validation.json' if language=='ru' else 'en-validation.json')
        if not (result_file.exists() and validation_file.exists()):continue
        result=json.loads(result_file.read_text());validation=json.loads(validation_file.read_text())
        if not validation.get('publishable_word_completeness'):continue
        assert result['text']==baseline['text']
        with wave.open(str(folder/result['output'])) as audio:
            assert (audio.getnchannels(),audio.getsampwidth(),audio.getframerate())==(1,2,24000)
            assert abs(audio.getnframes()/audio.getframerate()-result['audio_seconds'])<.001
        plot_resources(result,folder)
        words=len(re.findall(r"[^\W\d_]+(?:[’'][^\W\d_]+)*",re.sub(r'\[[^]]+\]','',result['text'])))
        entries.append(dict(label=label,folder=folder,result=result,validation=validation,words=words,
            speedup=baseline['generation_seconds']/result['generation_seconds'],
            status='Listener approved' if language=='en' and folder==references[language] else 'Candidate — naturalness and sounds need listening'))
    assert entries
    tracks[language]=entries

CSS='''
:root{color-scheme:dark}*{box-sizing:border-box}body{font:16px/1.6 system-ui,sans-serif;margin:36px auto;padding:0 20px;max-width:940px;background:#10151c;color:#edf1f5}h1{font-size:28px;line-height:1.3}h2{font-size:22px;margin:0 0 12px}p{color:#c0cddd}a{color:#8ed2ff}section{padding:22px;margin:24px 0;background:#18222e;border:1px solid #304052;border-radius:12px}audio,select{display:block;width:100%;margin:14px 0}select{font:inherit;padding:10px;color:#edf1f5;background:#23374c;border:1px solid #536b83;border-radius:6px}.status{font-size:14px;color:#ffd198}.metrics{display:flex;flex-wrap:wrap;gap:8px 22px;font-size:15px;color:#d3e1ef}.metrics b{color:#a7e7bd}.note{font-size:14px;color:#acbed0}summary{cursor:pointer;color:#b6d8f4}details{margin-top:16px}code{color:#ffd198;font-size:.85em}li{padding:5px 0}img{width:100%;height:auto}.scroll{overflow:auto}table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}td,th{text-align:right;padding:9px 10px;border-bottom:1px solid #344253;white-space:nowrap}td:first-child,th:first-child{text-align:left}footer{margin:30px 0}
'''

for folder,names in [(REVIEW,['index.html','controls.html']),(BASE,['index.html']),(BASE/'translation',['index.html']),(OUT,['index.html'])]:
    folder.mkdir(exist_ok=True)
    data={};cards=[]
    for language,entries in tracks.items():
        serialized=[]
        for entry in entries:
            source=entry['folder'];r=entry['result'];v=entry['validation']
            rel=lambda file:os.path.relpath(source/file,folder)
            serialized.append(dict(label=entry['label'],status=entry['status'],audio=rel(r['output']),
                plot=rel(f'{language}-resources.svg'),csv=rel(f'{language}-resources.csv'),json=rel(f'{language}-results.json'),
                generation=r['generation_seconds'],duration=r['audio_seconds'],rtf=r['rtf'],cpu=r['average_cpu_percent'],
                ram=r['peak_process_rss_mib']/1024,swap=r['peak_sampled_swap_mib'],load=r['load_seconds'],
                total=r['process_wall_seconds_including_imports_load_write_exit'],steps=r['num_step'],
                words_per_second=entry['words']/r['generation_seconds'],speedup=entry['speedup'],
                speech=f"Speech check: {v['matched_target_words']}/{v['target_words']} scripted words recovered."+
                    (' Check '+', '.join('«'+word+'»' for word in v['unmatched_target_words'])+'.' if v['unmatched_target_words'] else ''),
                transcript=v['transcript']))
        data[language]=serialized
        first=serialized[0];r=entries[0]['result']
        options=''.join(f'<option value="{i}">{esc(entry["label"])}</option>' for i,entry in enumerate(entries))
        text=''.join(f'<li><code>{esc(tag)}</code> {esc(sentence)}</li>' for tag,sentence in zip(re.findall(r'\[[^]]+\]',r['text']),r['sentences']))
        title='English — same story' if language=='en' else 'Russian — full translation'
        cards.append(f'''<section id="{language}"><h2>{title}</h2><label for="choose-{language}">Recording</label><select id="choose-{language}" aria-label="{title} recording">{options}</select><div class="status" data-field="status">{esc(first['status'])}</div><audio controls preload="none" src="{esc(first['audio'])}"></audio><div class="metrics"><span>Generate <b data-field="generation">{first['generation']:.2f}s</b></span><span>Speedup <b data-field="speedup">{first['speedup']:.2f}×</b></span><span><b data-field="words_per_second">{first['words_per_second']:.2f} words/s</b></span><span>RTF <b data-field="rtf">{first['rtf']:.2f}×</b></span></div><p class="note" data-field="speech">{esc(first['speech'])}</p><details><summary>Exact text and sound requests</summary><ol lang="{language}">{text}</ol></details><details><summary>CPU, RAM and detailed timings</summary><img src="{esc(first['plot'])}" alt="CPU and RAM timeline"><div class="scroll"><table><tbody><tr><th>Audio duration</th><td data-field="duration">{first['duration']:.2f}s</td></tr><tr><th>Average CPU</th><td data-field="cpu">{first['cpu']:.1f}%</td></tr><tr><th>Peak process RAM (RSS)</th><td data-field="ram">{first['ram']:.2f} GiB</td></tr><tr><th>Sampled peak swap</th><td data-field="swap">{first['swap']:.1f} MiB</td></tr><tr><th>Model load</th><td data-field="load">{first['load']:.2f}s</td></tr><tr><th>Total process time</th><td data-field="total">{first['total']:.2f}s</td></tr><tr><th>Steps per chunk</th><td data-field="steps">{first['steps']}</td></tr></tbody></table></div><p class="note">CPU 100% means one logical CPU busy. Six inference threads; CPU-only, no GPU inference. Peak RSS includes loading and synthesis. RSS/swap sampled every 100 ms. Single runs on a shared host; generation time includes waveform decoding. RTF = generation time ÷ audio duration.</p><a data-link="csv" href="{esc(first['csv'])}" download>Resource CSV</a> · <a data-link="json" href="{esc(first['json'])}">Full measurements</a> · <a data-link="audio" href="{esc(first['audio'])}" download>Download WAV</a></details><details><summary>Automatic transcript</summary><p lang="{language}" data-field="transcript">{esc(first['transcript'])}</p></details></section>''')
    data_json=json.dumps(data,ensure_ascii=False).replace('<','\\u003c')
    script='''const recordings=DATA;const format={generation:x=>x.toFixed(2)+'s',speedup:x=>x.toFixed(2)+'×',words_per_second:x=>x.toFixed(2)+' words/s',rtf:x=>x.toFixed(2)+'×',duration:x=>x.toFixed(2)+'s',cpu:x=>x.toFixed(1)+'%',ram:x=>x.toFixed(2)+' GiB',swap:x=>x.toFixed(1)+' MiB',load:x=>x.toFixed(2)+'s',total:x=>x.toFixed(2)+'s'};for(const language of ['en','ru']){const card=document.getElementById(language);card.querySelector('select').addEventListener('change',event=>{const r=recordings[language][Number(event.target.value)],player=card.querySelector('audio');player.pause();player.src=r.audio;player.load();card.querySelector('img').src=r.plot;card.querySelectorAll('[data-field]').forEach(node=>{const key=node.dataset.field;node.textContent=format[key]?format[key](r[key]):r[key]});card.querySelectorAll('[data-link]').forEach(node=>node.href=r[node.dataset.link])})}document.querySelectorAll('audio').forEach(player=>player.addEventListener('play',()=>document.querySelectorAll('audio').forEach(other=>{if(other!==player)other.pause()})));'''.replace('DATA',data_json,1)
    page=f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>OmniVoice CPU optimization</title><style>{CSS}</style></head><body><h1>OmniVoice CPU optimization</h1><p>Two players: English and the fully Russian translation. Use the recording selector to compare CPU candidates with the original references. The approved English recording is preserved.</p><p class="note">Candidates use BF16 transformer computation, separate guidance paths and an FP32 audio codec. The 16-step option changes refinement count; 32 steps retains the original count. All request the same 13 sounds. Quality is pending listening; speech checks do not grade naturalness or sound rendering. The rejected 16-step Russian run is excluded.</p>{''.join(cards)}<footer class="note">Ryzen 5 7600X · 6 physical cores / 12 logical CPUs · about 30 GiB RAM · CPU-only inference. Power and energy were not measured.</footer><script>{script}</script></body></html>'''
    for name in names:
        path=folder/name
        archive=folder/(path.stem+'-before-cpu-optimization.html')
        if path.exists() and not archive.exists():archive.write_bytes(path.read_bytes())
        path.write_text(page)
(REVIEW/'active-review.json').write_text(json.dumps(dict(page='index.html',purpose='OmniVoice CPU optimization: two players, references selectable, rejected Russian 16-step output excluded',players=2),indent=2)+'\n')
print('Optimized review updated: exactly two players, validated variants only.')
