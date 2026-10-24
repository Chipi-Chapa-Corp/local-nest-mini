#!/usr/bin/env python3
"""Check transcript coverage after synthesis, outside its resource measurement."""
import argparse
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
import subprocess

ROOT=Path(__file__).resolve().parents[2]
parser=argparse.ArgumentParser()
parser.add_argument('folder',type=Path)
parser.add_argument('--case',default='ru')
parser.add_argument('--reuse-asr',action='store_true')
args=parser.parse_args()
folder=args.folder.resolve()
result=json.loads((folder/f'{args.case}-results.json').read_text())
asr=folder/f'{args.case}-asr.json'
command=None
if not args.reuse_asr:
    wav=folder/f'{args.case}-asr-16k.wav'
    subprocess.run(['ffmpeg','-v','error','-threads','1','-filter_threads','1','-y',
                    '-i',str(folder/result['output']),'-ar','16000','-ac','1',str(wav)],check=True)
    command=[str(ROOT/'bin/whisper-cli'),'-m',str(ROOT/'models/stt/futo.bin'),
             '-l',result['language'] or 'auto','-t','6','-np','-ng','-ojf',
             '-of',str(folder/f'{args.case}-asr'),'-f',str(wav)]
    with (folder/f'{args.case}-asr.log').open('w') as log:
        subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=180)
transcript=' '.join(segment['text'].strip() for segment in json.loads(asr.read_text())['transcription'])
def words(text):
    text=re.sub(r'\[[^]]+\]','',text).lower().replace('ё','е').replace('’',"'").replace('favourite','favorite')
    return re.findall(r"[^\W\d_]+(?:[’'][^\W\d_]+)*",text)
expected=words(result['text']);observed=words(transcript)
blocks=SequenceMatcher(None,expected,observed,autojunk=False).get_matching_blocks()
matched={i for block in blocks for i in range(block.a,block.a+block.size)}
coverage=len(matched)/max(1,len(expected))
head=sum(i in matched for i in range(min(10,len(expected))))/min(10,len(expected))
tail=sum(i in matched for i in range(max(0,len(expected)-10),len(expected)))/min(10,len(expected))
validation=dict(speech_present=coverage>=0.5,publishable_word_completeness=coverage>=0.95 and head>=0.9 and tail>=0.9,
    target_word_coverage=coverage,target_words=len(expected),matched_target_words=len(matched),
    unmatched_target_words=[word for i,word in enumerate(expected) if i not in matched],
    transcript=transcript,reference=result['text'],asr_command=command,reused_existing_asr=args.reuse_asr,
    purpose='Detect noise and omitted story content; ASR does not grade pronunciation, naturalness, or sound-event success.',
    resource_accounting='Separate post-synthesis check; not included in generation metrics.')
path=folder/('validation.json' if args.case=='ru' else f'{args.case}-validation.json')
path.write_text(json.dumps(validation,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(validation,ensure_ascii=False,indent=2))
raise SystemExit(0 if validation['publishable_word_completeness'] else 1)
