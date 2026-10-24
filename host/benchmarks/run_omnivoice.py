#!/usr/bin/env python3
"""Reproducible, sequential official OmniVoice sound/code-switch CPU audition."""
import argparse
import csv
import datetime
import json
import os
from pathlib import Path
import platform
import resource
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'data/benchmarks/omnivoice/sound-suite'
TAGS = ['[laughter]', '[sigh]', '[confirmation-en]', '[question-en]',
        '[question-ah]', '[question-oh]', '[question-ei]', '[question-yi]',
        '[surprise-ah]', '[surprise-oh]', '[surprise-wa]', '[surprise-yo]',
        '[dissatisfaction-hnn]']
RU = [
    'Мы наконец приготовили завтрак и не сожгли тосты.',
    'После такой долгой недели можно немного отдохнуть.',
    'Да, чай готов, а билеты лежат у меня в кармане.',
    'Ты положил чашки на стол?',
    'Подожди, мы правда уезжаем сегодня?',
    'Ты уже собрал все наши вещи?',
    'Эй, а куда делся синий рюкзак?',
    'Этот маленький щенок тоже едет с нами?',
    'Смотри, дождь закончился и выглянуло солнце!',
    'Оказывается, поезд отправляется на час позже!',
    'Какой красивый вид из окна!',
    'Ты даже взял мой любимый шоколад!',
    'Но телефон опять разрядился. Давай зарядим его, спокойно позавтракаем и пойдём на вокзал.',
]
EN = [
    'We finally made breakfast without burning the toast.',
    'After such a long week, we can rest for a while.',
    'Yes, the tea is ready, and the tickets are in my pocket.',
    'Did you put the cups on the table?',
    'Wait, are we really leaving today?',
    'Have you already packed all our things?',
    'Hey, where did the blue backpack go?',
    'Is that little puppy coming with us too?',
    'Look, the rain has stopped and the sun has come out!',
    'It turns out the train leaves an hour later!',
    'What a beautiful view from the window!',
    'You even brought my favourite chocolate!',
    'But the phone is dead again. Let’s charge it, enjoy breakfast, and walk to the station.',
]
RU_MIX = [
    'Мы наконец приготовили breakfast without burning the toast.',
    'После такой долгой недели we can rest for a while.',
    'Да, чай готов, and the tickets are in my pocket.',
    'Ты положил the cups on the table?',
    'Подожди, are we really leaving today?',
    'Ты уже собрал all our things?',
    'Эй, where did the blue backpack go?',
    'Этот маленький щенок тоже едет with us?',
    'Смотри, the rain has stopped и выглянуло солнце!',
    'Оказывается, the train leaves на час позже!',
    'Какой красивый view from the window!',
    'Ты даже взял my favourite chocolate!',
    'Но телефон опять разрядился. Let’s charge it, спокойно позавтракаем and walk to the station.',
]
EN_MIX = [
    'We finally made завтрак и не сожгли тосты.',
    'After such a long week, можно немного отдохнуть.',
    'Yes, the tea is ready, а билеты лежат у меня в кармане.',
    'Did you put чашки на стол?',
    'Wait, мы правда уезжаем сегодня?',
    'Have you already packed все наши вещи?',
    'Hey, а куда делся синий рюкзак?',
    'Is that little puppy coming с нами тоже?',
    'Look, дождь закончился and the sun has come out!',
    'It turns out поезд отправляется an hour later!',
    'What a beautiful вид из окна!',
    'You even brought мой любимый шоколад!',
    'But the phone is dead again. Давай зарядим его, enjoy breakfast и пойдём на вокзал.',
]

def passage(sentences):
    assert 0 < len(sentences) <= len(TAGS) == 13
    return ' '.join(f'{tag} {sentence}' for tag, sentence in zip(TAGS, sentences))

CASES = [
    dict(id='ru', title='Russian', language='ru', sentences=RU),
    dict(id='en', title='English — same story', language='en', sentences=EN),
    dict(id='ru-en', title='Russian → English within sentences', language=None, sentences=RU_MIX),
    dict(id='en-ru', title='English → Russian within sentences', language=None, sentences=EN_MIX),
]

def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')

def worker(case_id, voice_reference=False, probe=False, cpu_mode=None, num_step=32, threads=6):
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', OMP_NUM_THREADS=str(threads),
                      MKL_NUM_THREADS=str(threads), OPENBLAS_NUM_THREADS='1', TOKENIZERS_PARALLELISM='false')
    if cpu_mode:
        os.environ['HF_DEACTIVATE_ASYNC_LOAD']='1'
    import numpy as np
    import soundfile as sf
    import torch
    import torchaudio
    import transformers
    from omnivoice import OmniVoice
    from omnivoice.models.omnivoice import _NONVERBAL_PATTERN, _tokenize_with_nonverbal_tags

    torch.set_num_threads(threads)
    torch.set_num_interop_threads(1)
    case = dict(next(c for c in CASES if c['id'] == case_id))
    if probe:
        case['sentences'] = case['sentences'][:2]
    text = passage(case['sentences'])
    assert [m.group() for m in _NONVERBAL_PATTERN.finditer(text)] == TAGS[:len(case['sentences'])]
    reference = ROOT/'data/benchmarks/omnivoice/sound-suite/approved-en-reference.wav'
    reference_text = 'We finally made breakfast without burning the toast.'
    if voice_reference:
        assert case_id == 'ru' and reference.exists()
    metadata = dict(case, text=text, model='k2-fsa/OmniVoice',
        model_revision='c5fdb5ccb189668d56333f77ba2629f4cd7535f4',
        source_commit=subprocess.check_output(['git', '-C', str(ROOT/'models/omnivoice-src'), 'rev-parse', 'HEAD'], text=True).strip(),
        created_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        backend='official PyTorch CPU / SDPA', dtype='float32', threads=threads,
        cpu_mode=cpu_mode, interop_threads=1, seed=1234, num_step=num_step, guidance_scale=2.0,
        instruct=None if voice_reference else 'female, young adult', normalize_text=False,
        voice_mode='English reference clone' if voice_reference else 'voice design',
        voice_reference=str(reference.relative_to(ROOT)) if voice_reference else None,
        reference_text=reference_text if voice_reference else None,
        reference_source_range_seconds=[0,4.32] if voice_reference else None,
        chunk_duration_seconds=15, chunk_threshold_seconds=30,
        torch=torch.__version__, torchaudio=torchaudio.__version__, transformers=transformers.__version__)
    if cpu_mode:
        metadata['dtype']='bfloat16 transformer / float32 codec' if cpu_mode in {'bf16','split-bf16'} else 'float32'
        metadata['backend']+=' + CPU adjustments'
        metadata['hf_async_loading']=False
    save(OUT/f'{case_id}-request.json', metadata)
    samples = []
    phase = 'load'
    stop = threading.Event()
    monitor_start = time.monotonic()

    def sample():
        status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
        usage = resource.getrusage(resource.RUSAGE_SELF)
        samples.append(dict(seconds=time.monotonic()-monitor_start, phase=phase,
            rss_mib=int(status['VmRSS'].split()[0])/1024,
            swap_mib=int(status.get('VmSwap', '0').split()[0])/1024,
            threads=int(status['Threads']), cpu_seconds=usage.ru_utime+usage.ru_stime))

    def monitor():
        while not stop.is_set():
            sample()
            stop.wait(0.1)

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    print(f'{case_id}: loading model', flush=True)
    started = time.monotonic()
    dtype=torch.bfloat16 if cpu_mode in {'bf16','split-bf16'} else torch.float32
    extra={}
    if dtype==torch.bfloat16:
        from omnivoice.models.omnivoice import OmniVoiceConfig
        config=OmniVoiceConfig.from_pretrained(str(ROOT/'models/omnivoice'),local_files_only=True)
        # The nested Qwen config otherwise explicitly constructs F32 weights,
        # causing a second full backbone allocation before the BF16 conversion.
        config.llm_config.dtype=dtype
        config.dtype=dtype
        extra['config']=config
    model = OmniVoice.from_pretrained(str(ROOT/'models/omnivoice'), device_map='cpu',
                                     dtype=dtype, attn_implementation='sdpa', local_files_only=True,**extra)
    if cpu_mode:
        sys.path.insert(0,str(ROOT))
        from app.omnivoice_cpu import configure_cpu_inference
        configure_cpu_inference(model, precision='bf16' if dtype==torch.bfloat16 else 'fp32',
                                split_guidance=cpu_mode.startswith('split-'))
        metadata['dtype']=f'{dtype} transformer / float32 codec'
        assert all(p.dtype==torch.float32 for p in model.audio_tokenizer.parameters())
        import gc
        gc.collect()
        # Return temporary conversion buffers held in glibc's CPU arenas.
        if sys.platform.startswith('linux'):
            import ctypes
            libc=ctypes.CDLL(None)
            if hasattr(libc,'malloc_trim'):libc.malloc_trim(0)
    metadata['load_seconds'] = time.monotonic()-started
    metadata['input_tokens'] = _tokenize_with_nonverbal_tags(text, model.text_tokenizer).tolist()
    metadata['tag_tokens'] = {tag: model.text_tokenizer.encode(tag, add_special_tokens=False) for tag in TAGS}
    print(f'{case_id}: loaded in {metadata["load_seconds"]:.2f}s; generating', flush=True)
    steps = 0
    def hook(module, args, output):
        nonlocal steps
        steps += 1
        if steps == 1 or steps % 16 == 0:
            print(f'{case_id}: model forward {steps}, {time.monotonic()-started:.1f}s generation', flush=True)
    handle = model.register_forward_hook(hook)
    torch.manual_seed(1234)
    np.random.seed(1234)
    sample()
    before = resource.getrusage(resource.RUSAGE_SELF)
    phase = 'generation'
    started = time.monotonic()
    with torch.inference_mode():
        audio = model.generate(text=text, language=case['language'], instruct=metadata['instruct'],
            ref_audio=str(reference) if voice_reference else None,
            ref_text=reference_text if voice_reference else None,
            num_step=num_step, guidance_scale=2.0, normalize_text=False)[0]
    elapsed = time.monotonic()-started
    after = resource.getrusage(resource.RUSAGE_SELF)
    sample()
    phase = 'write'
    handle.remove()
    stop.set()
    thread.join()
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    assert np.isfinite(audio).all() and np.sqrt(np.mean(audio**2)) > 1e-5
    sf.write(OUT/f'{case_id}.wav', audio, model.sampling_rate, subtype='PCM_16')
    generation_samples = [s for s in samples if s['phase']=='generation']
    cpu_user = after.ru_utime-before.ru_utime
    cpu_system = after.ru_stime-before.ru_stime
    metadata.update(generation_seconds=elapsed, audio_seconds=len(audio)/model.sampling_rate,
        sample_rate=model.sampling_rate, cpu_user_seconds=cpu_user, cpu_system_seconds=cpu_system,
        cpu_seconds=cpu_user+cpu_system, average_cpu_cores=(cpu_user+cpu_system)/elapsed,
        average_cpu_percent=100*(cpu_user+cpu_system)/elapsed,
        peak_process_rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,
        peak_generation_sampled_rss_mib=max(s['rss_mib'] for s in generation_samples),
        peak_sampled_swap_mib=max(s['swap_mib'] for s in samples),
        peak_sampled_threads=max(s['threads'] for s in samples),
        major_faults=after.ru_majflt-before.ru_majflt, minor_faults=after.ru_minflt-before.ru_minflt,
        voluntary_context_switches=after.ru_nvcsw-before.ru_nvcsw,
        involuntary_context_switches=after.ru_nivcsw-before.ru_nivcsw,
        model_forward_calls=steps, audio_peak=float(np.abs(audio).max()),
        audio_rms=float(np.sqrt(np.mean(audio**2))), clipped_samples=int((np.abs(audio)>=1).sum()),
        output=f'{case_id}.wav')
    metadata['rtf'] = elapsed/metadata['audio_seconds']
    save(OUT/f'{case_id}-results.json', metadata)
    with (OUT/f'{case_id}-resources.csv').open('w') as f:
        writer = csv.DictWriter(f, fieldnames=list(samples[0]))
        writer.writeheader()
        writer.writerows(samples)
    print(json.dumps({k: metadata[k] for k in ['id','generation_seconds','audio_seconds','rtf','average_cpu_percent','peak_process_rss_mib']}, indent=2), flush=True)

def main():
    global OUT
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', choices=[c['id'] for c in CASES])
    parser.add_argument('--voice-reference', action='store_true', help='Russian translation using the approved English voice reference')
    parser.add_argument('--probe', action='store_true', help='Only the first two sentences; requires --voice-reference or --cpu-mode')
    parser.add_argument('--cpu-mode',choices=['fp32','bf16','split-fp32','split-bf16'])
    parser.add_argument('--steps',type=int,choices=[8,16,32],default=32)
    parser.add_argument('--threads',type=int,default=6)
    parser.add_argument('--output',type=Path,help='Separate output folder; required for CPU optimization experiments')
    parser.add_argument('--only',choices=['en','ru'],help='Run one language through the timed fresh-process wrapper')
    args = parser.parse_args()
    if args.probe and not (args.voice_reference or args.cpu_mode):
        parser.error('--probe requires --voice-reference or --cpu-mode')
    if (args.cpu_mode or args.steps!=32 or args.threads!=6) and not args.output:
        parser.error('Experiments require --output to preserve accepted samples')
    if args.voice_reference and args.only=='en':
        parser.error('--voice-reference currently targets the Russian translation')
    if args.output:
        OUT=args.output.resolve()
    elif args.voice_reference:
        OUT = OUT.parent/('translation-probe' if args.probe else 'translation')
    OUT.mkdir(parents=True, exist_ok=True)
    if args.case:
        worker(args.case, args.voice_reference, args.probe, args.cpu_mode, args.steps,args.threads)
        return
    cases = CASES[:1] if args.voice_reference else CASES
    if args.only: cases=[case for case in cases if case['id']==args.only]
    host = dict(platform=platform.platform(), cpu=subprocess.check_output(['lscpu'], text=True),
                memory_before=Path('/proc/meminfo').read_text(), process_policy=f'{len(cases)} sequential fresh processes; no warmup; no batch inference',
                resource_sample_interval_seconds=0.1, power_measurement='not measured')
    save(OUT/'host.json', host)
    for case in cases:
        with (OUT/f'{case["id"]}.log').open('w') as log:
            print(f'Starting {case["id"]}', flush=True)
            process_start = time.monotonic()
            command=[sys.executable, str(Path(__file__).resolve()), '--case', case['id']]
            if args.voice_reference: command.append('--voice-reference')
            if args.probe: command.append('--probe')
            if args.cpu_mode: command.extend(['--cpu-mode',args.cpu_mode])
            command.extend(['--steps',str(args.steps),'--threads',str(args.threads)])
            if args.output: command.extend(['--output',str(OUT)])
            proc = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
        if proc.returncode:
            raise RuntimeError(f'{case["id"]} failed; inspect {log.name}')
        result = json.loads((OUT/f'{case["id"]}-results.json').read_text())
        result['process_wall_seconds_including_imports_load_write_exit'] = time.monotonic()-process_start
        save(OUT/f'{case["id"]}-results.json', result)
        print(f'Finished {case["id"]}: {result["generation_seconds"]:.2f}s generation, {result["audio_seconds"]:.2f}s audio, {result["peak_process_rss_mib"]:.0f} MiB peak RSS', flush=True)
    save(OUT/'results.json', [json.loads((OUT/f'{case["id"]}-results.json').read_text()) for case in cases])

if __name__ == '__main__':
    main()
