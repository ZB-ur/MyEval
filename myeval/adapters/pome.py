"""Isolated HTTP execution of pomegranagent-v2's real text business pipeline."""
from __future__ import annotations

import ast
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler

ADAPTER_VERSION = 'pome-http-1'
DEFAULT_ROOT = Path('/Users/lddmay/AiCoding/pomegranagent-v2')
LIMITATIONS = [
    '仅虚构文字 HTTP 旅程；没有执行儿童端浏览器自动保存、语音倒计时、ASR、TTS 或真人儿童质量验收。',
    '候选后的审校器属于被评测系统，不能作为独立模型评分。',
    '取消会停止本地等待和后续步骤；供应商没有接入取消协议，已送达的远端计算和计费是否停止未知。',
    '供应商返回的 model 单独记录；thinking 与 reasoning_effort 没有响应回显，生效值保持 unknown。',
    '费用未绑定计价版本，保持 null；缺失用量保持未知，不用 0 代替。',
]


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _sha(value):
    if not isinstance(value, bytes):
        value = _json(value).encode()
    return hashlib.sha256(value).hexdigest()


def _root():
    return Path(os.environ.get('MYEVAL_POME_ROOT', str(DEFAULT_ROOT))).expanduser().resolve()


def _git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, timeout=15)
    return result.stdout if result.returncode == 0 else b''


def describe(configuration):
    root = _root()
    files = ['server.py', 'backend/app.py', 'backend/evaluation.py', 'backend/local_voice.py']
    hashes = {name: _sha((root / name).read_bytes()) if (root / name).is_file() else None for name in files}
    prompts = {}
    if (root / 'backend/app.py').is_file():
        for node in ast.parse((root / 'backend/app.py').read_text()).body:
            if isinstance(node, ast.Assign):
                for name in node.targets:
                    if isinstance(name, ast.Name) and name.id in ('PROMPT', 'REVIEW_PROMPT', 'SUMMARY_PROMPT'):
                        prompts[name.id] = _sha(ast.literal_eval(node.value).encode())
    parameters = configuration.get('parameters', {})
    return {
        'adapter': ADAPTER_VERSION, 'project': 'pome', 'source_root': str(root),
        'environment': {'python': sys.version, 'platform': sys.platform, 'transport': 'stdlib HTTP on loopback'},
        'code_head': _git(root, 'rev-parse', 'HEAD').decode().strip() or None,
        'dirty_diff_sha256': _sha(_git(root, 'diff', 'HEAD', '--', *files)),
        'source_files_sha256': hashes, 'prompt_sha256': prompts,
        'adapter_sha256': _sha(Path(__file__).read_bytes()),
        'entrypoints': ['/api/state', '/api/chat', '/api/summary'],
        'requested': {'model': parameters.get('model', 'deepseek-flash'),
                      'chat_thinking': 'enabled',
                      'chat_reasoning_effort': parameters.get('chat_reasoning_effort', 'low'),
                      'chat_max_tokens': 4096, 'summary_thinking': 'disabled', 'summary_max_tokens': 2048},
        'effective': {'model': None, 'chat_reasoning_effort': None, 'thinking': None},
        'capabilities': {'multi_turn': 'supported', 'audio': 'unsupported',
                         'cancel_local_wait': 'supported', 'cancel_provider': 'unsupported',
                         'environment_reset': 'supported', 'state_snapshot': 'supported',
                         'isolated_full_run': 'supported', 'fixed_context_rerun': 'unsupported',
                         'trace_rescore': 'unsupported'},
        'mode': configuration.get('mode'), 'limitations': LIMITATIONS,
    }


class RunStopped(Exception):
    def __init__(self, status):
        self.status = status


class RequestFailed(Exception):
    def __init__(self, status, body, endpoint):
        self.status, self.body, self.endpoint = status, body, endpoint


def _credential_source(root):
    override = os.environ.get('MYEVAL_POME_CREDENTIAL_DB')
    candidates = [Path(override).expanduser()] if override else [
        Path.home() / 'Library/Application Support/PenegranagentV2/diary.sqlite3',
        root / '.acceptance/dialogue-optimization/runtime/diary.sqlite3',
    ]
    return next((p.resolve() for p in candidates if p.is_file()), None)


def _read_credentials(path):
    if path is None:
        raise RuntimeError('missing_credentials')
    # Extract only these three settings fields. No state records or whole
    # settings object are copied into the isolated execution database.
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute("SELECT json_extract(body,'$.providerBaseUrl'), json_extract(body,'$.apiKey'), "
                         "json_extract(body,'$.chatModel') FROM settings WHERE id=1").fetchone()
    credentials = dict(zip(('providerBaseUrl', 'apiKey', 'chatModel'), row or (None, None, None)))
    if not all(isinstance(v, str) and v.strip() for v in credentials.values()):
        raise RuntimeError('missing_credentials')
    url = urlsplit(credentials['providerBaseUrl'])
    if url.scheme != 'https' or url.hostname != 'api.deepseek.com' or url.username or url.password or url.query or url.fragment:
        raise RuntimeError('unsupported_provider')
    return credentials


def _source_state_hash(path):
    if path is None:
        return None
    # The state is never decoded, logged, or exported; the digest checks that
    # this adapter did not alter the existing source application data.
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute('SELECT body FROM state WHERE id=1').fetchone()
        return _sha(row[0].encode()) if row else None


def _write(root, name, value):
    path = root / name
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    path.chmod(0o600)


def _rule(name, passed, detail):
    return {'kind': 'rule', 'evaluator': 'pome-boundary-rules', 'version': '1',
            'dimension': name, 'status': 'pass' if passed else 'fail',
            'value': passed, 'reason': detail}


def run(case, configuration, ctx):
    root = Path(ctx.root)
    root.mkdir(parents=True, exist_ok=True)
    events = []
    event_lock = threading.Lock()
    sink_closed = threading.Event()
    source = None
    source_hash = None
    server = None
    server_thread = None
    state = None
    transcript = []
    turns_result = []
    summary = None
    status = 'failed'
    error = None
    assessments = []
    started = time.monotonic()
    deadline = started + float(getattr(ctx, 'timeout_seconds', 300))
    episode = 'myeval-' + str(ctx.trial_id)
    provenance = describe(configuration)
    output = {'episode': episode, 'transcript': transcript, 'summary': None, 'turn_results': turns_result}

    def guard():
        if ctx.cancel_requested:
            raise RunStopped('cancelled')
        if time.monotonic() >= deadline:
            raise RunStopped('timed_out')

    def observe(event):
        if sink_closed.is_set():
            return
        event = copy.deepcopy(event)
        with event_lock:
            events.append(event)
            with (root / 'pome.events.jsonl').open('a') as f:
                f.write(_json(event) + '\n')
            (root / 'pome.events.jsonl').chmod(0o600)
            ctx.emit('pome.' + event['kind'], {**event['data'], 'product_request_id': event.get('request_id'),
                                             'operation': event.get('operation')}, episode=event.get('episode'),
                     turn=event.get('turn'), stage=event.get('stage'), attempt=event.get('attempt'),
                     request_id=event.get('request_id'))

    def http(method, endpoint, body=None, turn=None):
        guard()
        request_id = str(uuid.uuid4())
        request = Request(f'http://127.0.0.1:{server.server_port}' + endpoint,
                          data=_json(body).encode() if body is not None else None,
                          method=method, headers={'Content-Type': 'application/json', 'X-Request-ID': request_id})
        finished = threading.Event()
        reply = {}

        def send():
            try:
                with build_opener(ProxyHandler({})).open(request, timeout=max(1, min(80, deadline - time.monotonic()))) as response:
                    reply.update(status=response.status, body=json.loads(response.read()))
            except HTTPError as exc:
                try:
                    response = json.loads(exc.read())
                except (ValueError, OSError):
                    response = {'error': 'HTTP 请求失败，响应不可解析。'}
                reply.update(status=exc.code, body=response)
            except Exception as exc:
                reply.update(status=0, body={'error': '本地 HTTP 请求未完成。', 'type': type(exc).__name__})
            finally:
                finished.set()

        ctx.emit('http_request', {'method': method, 'endpoint': endpoint, 'request_id': request_id,
                                 'body_sha256': _sha(body) if body is not None else None}, episode=episode, turn=turn)
        threading.Thread(target=send, daemon=True).start()
        while not finished.wait(0.1):
            guard()
        guard()
        ctx.emit('http_response', {'endpoint': endpoint, 'request_id': request_id, 'status': reply['status']},
                 episode=episode, turn=turn)
        if reply['status'] != 200:
            raise RequestFailed(reply['status'], reply['body'], endpoint)
        return reply['body']

    try:
        if configuration.get('mode') != 'isolated_full_run':
            return {'status': 'unsupported', 'output': output, 'review_output': None,
                    'observed_configuration': provenance, 'usage': None, 'cost': None,
                    'assessments': [], 'limitations': LIMITATIONS,
                    'error': {'code': 'unsupported_mode', 'message': 'pome 首版支持隔离环境完整文字执行。'}}
        if case.get('task') != 'dialogue_and_summary':
            raise RuntimeError('unsupported_task')
        parameters = configuration.get('parameters', {})
        if parameters.get('model', 'deepseek-flash') != 'deepseek-flash' or parameters.get('chat_reasoning_effort', 'low') not in ('low', 'high', 'max'):
            raise RuntimeError('unsupported_configuration')
        if case.get('source', {}).get('synthetic') is not True:
            raise RuntimeError('synthetic_material_required')
        product_root = _root()
        source = _credential_source(product_root)
        source_hash = _source_state_hash(source)
        credentials = _read_credentials(source)
        credentials['chatModel'] = parameters.get('model', 'deepseek-flash')
        fixture = copy.deepcopy(case['input'])
        children = copy.deepcopy(case['initial_state']['children'])
        child = next(c for c in children if c['id'] == fixture['child_id'])
        guard()
        sys.path.insert(0, str(product_root))
        try:
            app = importlib.import_module('backend.app')
        finally:
            sys.path.remove(str(product_root))
        if Path(app.__file__).resolve() != (product_root / 'backend/app.py').resolve():
            raise RuntimeError('product_source_mismatch')
        if not hasattr(app, 'AIObservation'):
            raise RuntimeError('product_observation_seam_missing')
        server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        server.daemon_threads = True
        server.store = app.Store(root / 'pome-data', runtime_settings=credentials)
        server.web_root = product_root / 'web'
        # Text execution deliberately leaves audio uninitialised. It does not
        # stub chat/summary, inspect recordings, or resume the paused audio work.
        server.local_voice = app.LocalVoice(product_root / 'models')
        server.ai_options = {'chat_reasoning_effort': parameters.get('chat_reasoning_effort', 'low')}
        server.ai_observer = observe
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        ctx.emit('environment_started', {'adapter': ADAPTER_VERSION, 'port': server.server_port,
                                        'data_directory': str(root / 'pome-data'), 'audio': 'unsupported',
                                        'source_state_sha256': source_hash, 'credentials': 'execution_memory_only'})
        state = http('GET', '/api/state')
        state['data'] = {'children': children, 'ducks': copy.deepcopy(case['initial_state'].get('animals', [])),
                         'schedules': {fixture['activity_date']: [child['id']]}}
        state['records'] = copy.deepcopy(case['initial_state'].get('records', []))
        draft_key = child['id'] + ':' + fixture['activity_date']
        draft = {'id': episode, 'child': child, 'activityDate': fixture['activity_date'],
                 'createdAt': fixture['created_at'], 'themeId': fixture.get('theme_id', 'duck'),
                 'conversationRounds': fixture.get('conversation_rounds', 3), 'turns': [],
                 'roundComplete': False, 'finishRequested': False}
        state['drafts'] = {draft_key: draft}
        state = http('PUT', '/api/state', state)
        _write(root, 'pome.state-initial.json', state)
        background = state['drafts'][draft_key]['dialogueBackground']
        expected_background = case.get('expected_background_texts')
        if expected_background is not None:
            actual = [s['text'] for s in background['recentStories']]
            assessments.append(_rule('background_scope', actual == expected_background,
                                     '比较服务器草稿背景快照与用例标注的允许原文；仅验证选材边界。'))
        ctx.emit('state_snapshot', {'name': 'initial', 'sha256': _sha(state), 'background': background}, episode=episode)
        for index, script in enumerate(fixture['child_turns'], 1):
            guard()
            utterance = script if isinstance(script, str) else script['text']
            finish_requested = isinstance(script, dict) and script.get('finish_requested', False)
            transcript.append({'role': 'child', 'text': utterance})
            state['drafts'][draft_key]['turns'] = copy.deepcopy(transcript)
            state['drafts'][draft_key]['finishRequested'] = finish_requested
            state['drafts'][draft_key]['roundComplete'] = False
            state = http('PUT', '/api/state', state, turn=index)
            result = http('POST', '/api/chat', {
                'storyId': episode, 'childId': child['id'], 'activityDate': fixture['activity_date'],
                'themeId': draft['themeId'], 'conversationRounds': draft['conversationRounds'],
                'finishRequested': finish_requested, 'turns': copy.deepcopy(transcript),
            }, turn=index)
            # Only the actual delivered, reviewed response enters the next turn.
            transcript.append({'role': 'assistant', 'text': result['text']})
            turns_result.append({'turn': index, **result})
            state['drafts'][draft_key]['turns'] = copy.deepcopy(transcript)
            state['drafts'][draft_key]['roundComplete'] = True
            state = http('PUT', '/api/state', state, turn=index)
            _write(root, 'pome.state-current.json', state)
            ctx.emit('state_snapshot', {'name': 'after_turn', 'sha256': _sha(state),
                                        'delivered': result, 'persisted_turn_count': len(transcript)},
                     episode=episode, turn=index)
            if result.get('endConversation'):
                break
        summary_reply = http('POST', '/api/summary', {'storyId': episode, 'childId': child['id'],
                                                   'turns': copy.deepcopy(transcript)})
        summary = summary_reply['text']
        output['summary'] = summary
        output['scripted_turns_not_executed'] = len(fixture['child_turns']) - len(turns_result)
        record = {k: copy.deepcopy(state['drafts'][draft_key][k])
                  for k in ('id', 'child', 'activityDate', 'createdAt', 'themeId', 'turns')}
        record.update(text=summary, duckId=None)
        state['records'].append(record)
        del state['drafts'][draft_key]
        state = http('PUT', '/api/state', state)
        restored = http('GET', '/api/state')
        _write(root, 'pome.state-final.json', restored)
        stored = next(r for r in restored['records'] if r['id'] == episode)
        assessments.append(_rule('original_turns_persisted', stored['turns'] == transcript,
                                 '正式 /api/state 事务保存并重新 GET 后原始儿童/最终 AI 发言逐项一致；未执行浏览器保存。'))
        assessments.append(_rule('single_record_and_draft_removed',
                                 sum(r['id'] == episode for r in restored['records']) == 1 and draft_key not in restored['drafts'],
                                 '正式状态事务生成单条记录并移除本草稿。'))
        with event_lock:
            observed_inputs = [e for e in events if e['kind'] == 'input' and e['operation'] == 'chat']
            chain_valid = all(e['data']['turns'] == transcript[:2 * e['turn'] - 1] for e in observed_inputs)
            stage_results = [e for e in events if e['kind'] == 'stage_result']
        assessments.append(_rule('actual_reply_context', chain_valid and len(observed_inputs) == len(turns_result),
                                 '每轮产品收到的上下文与该 trial 实际交付的历史逐项比较；候选不作为历史。'))
        assessments.append(_rule('candidate_review_observed',
                                 all(any(e['turn'] == t['turn'] and e['stage'] == s for e in stage_results)
                                     for t in turns_result for s in ('candidate', 'review')),
                                 '每个已交付轮次都有候选与审校通过记录；不代表内容质量通过。'))
        forbidden = case.get('summary_forbidden_fragments', [])
        if forbidden:
            found = [fragment for fragment in forbidden if fragment in summary]
            assessments.append(_rule('summary_background_literal_screen', not found,
                                     '仅筛查特定历史/他人/未来片段是否字面混入，不代表所有事实均忠实。命中：' + ', '.join(found)))
        output['state_final_sha256'] = _sha(restored)
        ctx.emit('state_snapshot', {'name': 'final', 'sha256': _sha(restored), 'record_count_for_episode': 1}, episode=episode)
        status = 'succeeded'
    except RunStopped as exc:
        status = exc.status
        error = {'code': exc.status, 'message': '已停止本地等待与后续步骤；远端计算/计费是否停止未知。',
                 'remote_cancellation': 'unsupported'}
    except RequestFailed as exc:
        with event_lock:
            refused = any(e['kind'] == 'model_response' and e['data'].get('refusal') for e in events)
        status = 'refused' if refused else 'failed'
        error = {'code': 'product_http_error', 'http_status': exc.status, 'endpoint': exc.endpoint,
                 'details': exc.body}
    except Exception as exc:
        known = {'missing_credentials': '缺少已授权的 pome 本机服务配置。可用 MYEVAL_POME_CREDENTIAL_DB 指定含设置的本机数据库。',
                 'unsupported_provider': '该适配器首版仅接入已授权的 DeepSeek 官方服务。',
                 'unsupported_configuration': '首版支持 deepseek-flash 的 low/high/max 文字配置。',
                 'unsupported_task': '不支持此任务类型。',
                 'synthetic_material_required': '首版执行仅允许明确标注的虚构材料。',
                 'product_observation_seam_missing': '产品源码缺少本次共享观测接口。'}
        code = str(exc) if str(exc) in known else 'adapter_error'
        status = 'unsupported' if code.startswith('unsupported') else 'failed'
        error = {'code': code, 'message': known.get(code, '适配器执行未完成。'), 'exception_type': type(exc).__name__}
    finally:
        sink_closed.set()
        if server is not None:
            if server_thread is not None and server_thread.is_alive():
                server.shutdown()
            server.server_close()
            if getattr(server, 'store', None) is not None:
                server.store.runtime_settings.clear()
        if server_thread is not None:
            server_thread.join(timeout=2)
        if state is not None:
            _write(root, 'pome.state-last.json', state)
        try:
            after_hash = _source_state_hash(source)
            output['source_state_integrity'] = {'before_sha256': source_hash, 'after_sha256': after_hash,
                                               'unchanged': source_hash == after_hash if source_hash is not None else None}
        except Exception:
            output['source_state_integrity'] = {'before_sha256': source_hash, 'after_sha256': None, 'unchanged': None}
    with event_lock:
        responses = [e for e in events if e['kind'] == 'model_response']
        requests = [e for e in events if e['kind'] == 'model_request']
        failures = [e for e in events if e['kind'] == 'stage_error']
    usage = None
    if responses:
        usage = {'calls_requested': len(requests), 'calls_with_response': len(responses),
                 'calls_with_usage': sum(bool(e['data'].get('usage')) for e in responses),
                 'complete': len(requests) == len(responses) and all(e['data'].get('usage') for e in responses)}
        for field in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
            values = [e['data'].get('usage', {}).get(field) if e['data'].get('usage') else None for e in responses]
            usage[field] = sum(values) if all(type(x) is int for x in values) and len(requests) == len(responses) else None
    output['model_call_count'] = len(requests)
    output['retry_count'] = sum(bool(e['data'].get('will_retry')) for e in failures)
    output['elapsed_ms'] = round((time.monotonic() - started) * 1000)
    output['stage_results'] = [{'turn': e['turn'], 'stage': e['stage'], 'attempt': e['attempt'], **e['data']}
                               for e in events if e['kind'] == 'stage_result']
    observed = {'requested': provenance['requested'],
                'reported_models': sorted({e['data']['model'] for e in responses if e['data'].get('model')}),
                'effective_reasoning_effort': None, 'effective_thinking': None,
                'evidence': '供应商 model 响应字段；思考强度没有回显',
                'provenance': provenance}
    result = {'status': status, 'output': output,
              'review_output': {'transcript': copy.deepcopy(transcript), 'summary': summary},
              'observed_configuration': observed, 'usage': usage, 'cost': None,
              'assessments': assessments, 'limitations': LIMITATIONS, 'error': error}
    _write(root, 'pome.result.json', result)
    return result
