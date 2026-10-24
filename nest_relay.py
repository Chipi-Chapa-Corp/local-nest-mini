"""Route the owned Nest to local bootstrap and speech endpoints.

The privileged helper admits only the owned Nest. Offline mode answers known
local endpoints and rejects every other request without opening a cloud socket.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from mitmproxy import http, tls

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.native import build_response
from app.s3_audio import extract_flac_segments, extract_query_audio, strip_wake_word
from app.bootstrap import load_token_response

DATA_DIR = Path(os.environ.get('DATA_DIR', '/data'))
STREAMS = DATA_DIR / 'streams'
TTS = Path(os.environ.get('TTS_DIR', str(DATA_DIR / 'tts')))
LOGS = DATA_DIR / 'logs'
for directory in (STREAMS, TTS, LOGS):
    directory.mkdir(parents=True, exist_ok=True)
for path in TTS.glob('*'):
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass
ROOTS = ('google.com', 'googleapis.com', 'gstatic.com', 'googleusercontent.com', 'googlezip.net', 'gvt1.com', 'gvt2.com')
BLOCK_HOSTS = ('dl.google.com', 'ota.googlezip.net', 'redirector.gvt1.com', 'update.googleapis.com')
PASSTHROUGH_HOSTS = ('play.googleapis.com',)
ASSISTANT_URL = os.environ.get('ASSISTANT_URL', 'http://127.0.0.1:8099').rstrip('/')
ASSISTANT_ENABLED = os.environ.get('ASSISTANT_ENABLED', '1').lower() in {'1', 'true', 'yes'}
ASSISTANT_LANG = os.environ.get('ASSISTANT_LANG', 'auto')
OUTPUT_MODE = os.environ.get('OUTPUT_MODE', 'cast').lower()
ALLOW_GOOGLE_UPSTREAM = os.environ.get('ALLOW_GOOGLE_UPSTREAM', '0') == '1'
S3_SERVER_PORT = int(os.environ.get('S3_SERVER_PORT', '18444'))
OFFLINE_BOOTSTRAP_ENABLED = os.environ.get('OFFLINE_BOOTSTRAP_ENABLED', '1') == '1'
NATIVE_TEMPLATE = Path(os.environ.get('NATIVE_TEMPLATE', str(DATA_DIR / 'native-template.bin')))
CAST_ASSETS = {
    '/cast/sdk/libs/caf_receiver/v3/cast_receiver_framework.js': 'cast_receiver_framework.js',
    '/external_hosted/cast_3p/js_libs/prod/castlite_js_lib.js': 'castlite_js_lib.js',
    '/external_hosted/cast_3p/js_libs/prod/sizzle.js': 'sizzle.js',
}


def local_asset(host, path, data_dir):
    # Exact local routes only; all other Google requests remain blocked.
    if host != 'www.gstatic.com':
        return None
    if path == '/cast/sdk/default_receiver/1.0/app.html':
        return Path(__file__).parent / 'app/cast_receiver.html', 'text/html; charset=utf-8'
    if path in CAST_ASSETS:
        return Path(data_dir) / 'cast' / CAST_ASSETS[path], 'text/javascript'
    return None


def _has_cyrillic(text):
    return any('\u0400' <= char <= '\u04ff' for char in text)


def _reply_lang(text):
    if ASSISTANT_LANG != 'auto':
        return ASSISTANT_LANG
    return 'ru' if _has_cyrillic(text) else 'en'


def capture_chunk(kind, flow_id):
    path = STREAMS / f'{flow_id}-{kind}.bin'

    def capture(chunk: bytes) -> bytes:
        with path.open('ab') as out:
            os.fchmod(out.fileno(), 0o600)
            out.write(chunk)
        return chunk

    return capture


def log(kind, **fields):
    line = json.dumps(dict(time=datetime.now(timezone.utc).isoformat(), kind=kind, **fields))
    with (LOGS / 'proxy-events.jsonl').open('a') as out:
        out.write(line + '\n')
    print(line, flush=True)


def allowed(host):
    return any(host == root or host.endswith('.' + root) for root in ROOTS)


def _assistant(path, body, content_type, task_id=None):
    if task_id:
        separator = '&' if '?' in path else '?'
        path = f'{path}{separator}task_id={urllib.parse.quote(task_id)}'
    request = urllib.request.Request(
        ASSISTANT_URL + path,
        data=body,
        headers={'content-type': content_type},
        method='POST',
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.loads(response.read() or b'{}')


def assistant_query(audio, flow_id, byte_count, segment_sizes, received=None):
    if not ASSISTANT_ENABLED or not audio:
        return None
    task_id = flow_id
    start = {
        'task_id': task_id,
        'flow': flow_id,
        'bytes': byte_count,
        'segments': segment_sizes,
        'received': received or time.time(),
    }
    try:
        _assistant('/task/start', json.dumps(start).encode(), 'application/json')
    except Exception as exc:
        log('task_start_error', error=f'{type(exc).__name__}: {exc}', flow=flow_id)
    text = _assistant(f'/transcribe?lang={ASSISTANT_LANG}', audio, 'application/octet-stream', task_id).get('text', '')
    query = strip_wake_word(text)
    if not query:
        log('local_wake_only', text=text, task_id=task_id)
        return None
    result = _assistant('/answer', json.dumps({
        'text': query,
        'lang': _reply_lang(query),
        'play': OUTPUT_MODE == 'cast',
        'task_id': task_id,
    }).encode(), 'application/json', task_id)
    native_body = None
    if OUTPUT_MODE == 'native' and result.get('audio_ogg_path'):
        try:
            native_body = build_response(NATIVE_TEMPLATE.read_bytes(),
                                         Path(result['audio_ogg_path']).read_bytes())
            log('native_built', task_id=task_id, bytes=len(native_body))
        except Exception as exc:
            log('native_error', task_id=task_id, error=f'{type(exc).__name__}: {exc}')
            result['native_error'] = f'{type(exc).__name__}: {exc}'
    return {'text': text, 'query': query, 'task_id': task_id, 'native_body': native_body, **result}


class NestRelay:
    def tls_clienthello(self, data: tls.ClientHelloData):
        host = (data.client_hello.sni or '').lower().rstrip('.')
        log('client_hello', host=host)
        if ASSISTANT_ENABLED and OUTPUT_MODE == 'native' and host == 'assistant-s3-pa.googleapis.com':
            data.context.server.address = ('127.0.0.1', S3_SERVER_PORT)
            data.ignore_connection = True
            log('s3_local_route', host=host, port=S3_SERVER_PORT)
            return
        # Reverse-proxy connections are established lazily, after this event.
        # No DNS change is made for the PC itself, so this resolves upstream.
        upstream_host = host if allowed(host) and ALLOW_GOOGLE_UPSTREAM else 'blocked.invalid'
        data.context.server.address = (upstream_host, 443)
        data.context.server.sni = upstream_host
        if host in PASSTHROUGH_HOSTS and ALLOW_GOOGLE_UPSTREAM:
            data.ignore_connection = True
            log('passthrough', host=host)

    def tls_established_client(self, data: tls.TlsData):
        log('tls_accepted', host=data.context.client.sni or '')

    def tls_failed_client(self, data: tls.TlsData):
        log('tls_rejected', host=data.context.client.sni or '', error=str(data.context.client.error))

    def requestheaders(self, flow: http.HTTPFlow):
        path_only = flow.request.path.split('?', 1)[0]
        if path_only.startswith('/tts/'):
            name = Path(path_only).name
            audio = TTS / name
            if audio.is_file() and audio.suffix.lower() in ('.wav', '.mp3', '.ogg'):
                data = audio.read_bytes()
                ctype = {'.wav': 'audio/wav', '.mp3': 'audio/mpeg', '.ogg': 'audio/ogg'}[audio.suffix.lower()]
                headers = {'content-type': ctype, 'accept-ranges': 'bytes', 'cache-control': 'no-store',
                           'access-control-allow-origin': '*'}
                status, body = 200, data
                rng = flow.request.headers.get('range', '')
                if rng.startswith('bytes='):
                    first = rng[6:].split(',', 1)[0].strip()
                    start_s, _, end_s = first.partition('-')
                    try:
                        start = int(start_s) if start_s else 0
                        end = int(end_s) if end_s else len(data) - 1
                        if 0 <= start <= end < len(data):
                            status, body = 206, data[start:end + 1]
                            headers['content-range'] = f'bytes {start}-{end}/{len(data)}'
                    except ValueError:
                        pass
                headers['content-length'] = str(len(body))
                flow.response = http.Response.make(status, body, headers)
                log('tts_served', path=path_only, status=status, bytes=len(body))
                return
        if not flow.client_conn.tls:
            # Reverse HTTP mode initially substitutes its configured destination.
            # Use the original Host header from the owned device instead.
            flow.request.host = flow.request.headers.get('host', '').split(':', 1)[0]
            flow.request.port = 80
            flow.request.scheme = 'http'
        elif not ALLOW_GOOGLE_UPSTREAM and flow.client_conn.sni:
            # Reverse mode initially substitutes the deliberately blocked
            # destination. Keep the original SNI for local endpoint routing.
            flow.request.host = flow.client_conn.sni
        host = flow.request.host.lower().rstrip('.')
        path = flow.request.path.split('?', 1)[0]
        if not ALLOW_GOOGLE_UPSTREAM:
            asset = local_asset(host, path, DATA_DIR)
            if asset:
                file, content_type = asset
                try:
                    body = file.read_bytes()
                except OSError:
                    flow.response = http.Response.make(503, b'Local Cast SDK missing; run host/setup-cast.sh\n')
                    log('local_cast_error', host=host, path=path, error='asset missing')
                else:
                    flow.response = http.Response.make(200, body, {
                        'content-type': content_type, 'cache-control': 'no-store',
                        'access-control-allow-origin': '*', 'alt-svc': 'clear'})
                    log('local_cast_asset', host=host, path=path, bytes=len(body))
            elif host in {'connectivitycheck.gstatic.com', 'clients1.google.com'} and path == '/generate_204':
                flow.response = http.Response.make(204, b'', {'alt-svc': 'clear'})
                log('local_connectivity', host=host, path=path)
            elif OFFLINE_BOOTSTRAP_ENABLED and host == 'clients3.google.com' and path == '/cast/orchestration/googleoauthtoken':
                try:
                    body = load_token_response(DATA_DIR)
                except (OSError, ValueError, TypeError) as exc:
                    flow.response = http.Response.make(503, b'Local bootstrap unavailable\n')
                    log('local_bootstrap_error', error=type(exc).__name__)
                else:
                    flow.response = http.Response.make(200, body, {
                        'content-type': 'application/x-protobuf',
                        'cache-control': 'no-store', 'alt-svc': 'clear'})
                    log('local_bootstrap', host=host, path=path, bytes=len(body))
            else:
                flow.response = http.Response.make(503, b'Google upstream disabled; local processing only\n',
                    {'content-type': 'text/plain', 'alt-svc': 'clear'})
                log('upstream_blocked', host=host, path=path)
            return
        blocked = (not allowed(host) or host in BLOCK_HOSTS or
                   path.lower().endswith(('.zip', '.bin', '.img', '.crx')) or
                   any(part in path.lower() for part in ('/edgedl/', '/ota/', '/service/update2')))
        log('request', host=host, path=path, method=flow.request.method,
            content_type=flow.request.headers.get('content-type', ''), blocked=blocked)
        if blocked:
            flow.response = http.Response.make(503, b'Blocked by owned Nest lab: firmware or out-of-scope host\n', {'content-type': 'text/plain'})
            return
        flow.server_conn.address = (host, 443 if flow.request.scheme == 'https' else 80)
        flow.server_conn.sni = host
        if host == 'assistant-s3-pa.googleapis.com' and path.startswith('/speech.s3.'):
            flow.request.stream = capture_chunk('req', flow.id)
            log('stream_pending', host=host, path=path, direction='request', flow=flow.id)

    def request(self, flow: http.HTTPFlow):
        host = flow.request.host.lower().rstrip('.')
        path = flow.request.path.split('?', 1)[0]
        if host != 'assistant-s3-pa.googleapis.com' or not path.startswith('/speech.s3.'):
            return
        flow.metadata['request_started'] = time.time()
        log('stream_request', host=host, path=path, direction='request', flow=flow.id)

    def _native_response(self, flow: http.HTTPFlow):
        capture = STREAMS / f'{flow.id}-req.bin'
        body = capture.read_bytes() if capture.is_file() else (flow.request.content or b'')
        segments = extract_flac_segments(body)
        log('stream_segments', flow=flow.id, bytes=len(body), count=len(segments),
            sizes=[len(x) for x in segments])
        if not segments:
            log('native_no_audio', flow=flow.id)
            return False
        audio = extract_query_audio(body)
        try:
            result = assistant_query(audio, flow.id, len(body), [len(x) for x in segments],
                                     flow.metadata.get('request_started'))
        except Exception as exc:
            log('local_assistant_error', error=f'{type(exc).__name__}: {exc}', flow=flow.id)
            return False
        if not result:
            return False
        native_body = result.get('native_body')
        log('local_answer', text=result.get('text', ''), query=result.get('query', ''),
            audio_url=result.get('audio_url', ''), played=result.get('player', {}).get('played', False),
            native_bytes=len(native_body or b''), task_id=result.get('task_id', flow.id))
        response = flow.response
        response.status_code = 200
        response.headers['content-type'] = 'application/grpc'
        response.headers.pop('grpc-status', None)
        response.headers.pop('grpc-message', None)
        response.headers['content-length'] = str(len(native_body or b''))
        response.content = native_body or b''
        response.trailers = http.Headers([(b'grpc-status', b'0' if native_body else b'2')])
        response.stream = False
        flow.request.stream = False
        flow.metadata['native_response'] = True
        log('synthetic_set', task_id=result.get('task_id'), status=flow.response.status_code,
            body=len(native_body or b''), content_length=flow.response.headers.get('content-length'))
        if result.get('task_id'):
            try:
                _assistant('/task/output', json.dumps({
                    'task_id': result['task_id'],
                    'transport': OUTPUT_MODE,
                    'bytes': len(native_body or b''),
                    'error': result.get('native_error'),
                }).encode(), 'application/json', result['task_id'])
            except Exception as exc:
                log('task_output_error', task_id=result.get('task_id'), error=f'{type(exc).__name__}: {exc}')
        return bool(native_body)

    def responseheaders(self, flow: http.HTTPFlow):
        host = flow.request.host.lower().rstrip('.')
        path = flow.request.path.split('?', 1)[0]
        if host == 'assistant-s3-pa.googleapis.com' and path.startswith('/speech.s3.') and OUTPUT_MODE == 'native':
            if self._native_response(flow):
                return
        log('responseheaders', host=host, path=path, status=flow.response.status_code if flow.response else None,
            grpc_status=flow.response.headers.get('grpc-status') if flow.response else None,
            content_length=flow.response.headers.get('content-length') if flow.response else None)
        if (flow.response is not None and 'alt-svc' in flow.response.headers):
            flow.response.headers['alt-svc'] = 'clear'
            log('alt_svc_cleared', host=host)
        if host == 'assistant-s3-pa.googleapis.com' and path.startswith('/speech.s3.') and not flow.metadata.get('native_response'):
            flow.response.stream = capture_chunk('resp', flow.id)
            log('stream_capture', host=host, path=path, direction='response', flow=flow.id)

    def response(self, flow: http.HTTPFlow):
        host = flow.request.host.lower().rstrip('.')
        path = flow.request.path.split('?', 1)[0]
        if host == 'assistant-s3-pa.googleapis.com' and path.startswith('/speech.s3.') and OUTPUT_MODE == 'native':
            pass
        log('response', host=host, path=path,
            status=flow.response.status_code, content_type=flow.response.headers.get('content-type', ''),
            grpc_status=flow.response.headers.get('grpc-status'),
            body_bytes=len(flow.response.raw_content or b''))


addons = [NestRelay()]
