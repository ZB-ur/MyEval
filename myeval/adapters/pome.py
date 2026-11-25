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

ADAPTER_VERSION = 'pome-http-9'
DEFAULT_ROOT = Path.home() / 'AiCoding' / 'pomegranagent-v2'
_PROFILE_CACHE = {}
_PROFILE_CACHE_LOCK = threading.Lock()
LIMITATIONS = [
    '仅虚构文字 HTTP 旅程；没有执行儿童端浏览器自动保存、语音倒计时、ASR、TTS 或真人儿童质量验收。',
    '理解卡、检查或审校阶段都属于被评测系统，不能作为独立模型评分；具体阶段以配置快照为准。',
    '取消会停止本地等待和后续步骤；供应商没有接入取消协议，已送达的远端计算和计费是否停止未知。',
    '供应商返回的 model 单独记录；thinking 与 reasoning_effort 没有响应回显，生效值保持 unknown。',
    '费用未绑定计价版本，保持 null；缺失用量保持未知，不用 0 代替。',
    'interaction_policy 为显式模拟用户；规则分支不等于模型用户或真实儿童反馈，模拟器与产品用量分别记录。',
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


def _requested(configuration):
    parameters = configuration.get('parameters', {})
    if not isinstance(parameters, dict) or parameters.get('model', 'deepseek-flash') != 'deepseek-flash':
        raise ValueError('unsupported_configuration')
    requested = {'model': parameters.get('model', 'deepseek-flash'),
                 'chat_max_tokens': 4096, 'summary_max_tokens': 2048}
    profile = parameters.get('dialogue_profile', 'baseline')
    if profile not in ('baseline', 'grounded-v1', 'contextual-v1',
                       'dialogue-act-v2', 'evidence-ledger-v2', 'selective-memory-v2',
                       'direct-companion-v3', 'perspective-then-speak-v3', 'selective-repair-v3',
                       'warm-direct-v4', 'responsive-friend-v4', 'case-based-friend-v4',
                       'warm-participant-v5', 'reference-companion-v5',
                       'need-aware-companion-v6', 'compact-reference-v6'):
        raise ValueError('unsupported_dialogue_profile')
    requested['dialogue_profile'] = profile
    for operation, default in [('chat', 'enabled'), ('summary', 'disabled')]:
        thinking = parameters.get(operation + '_thinking', default)
        if thinking not in ('enabled', 'disabled'):
            raise ValueError('unsupported_configuration')
        effort = parameters.get(operation + '_reasoning_effort', 'low' if thinking == 'enabled' else None)
        if (thinking == 'enabled' and effort not in ('low', 'high', 'max')) or (thinking == 'disabled' and effort is not None):
            raise ValueError('unsupported_configuration')
        requested[operation + '_thinking'] = thinking
        requested[operation + '_reasoning_effort'] = effort
    return requested


def _profile_metadata(root, profile_id, source_hashes):
    """Describe the exact source version without importing it into the UI server.

    A long-lived catalog process must not mix current file hashes with Python's
    previously imported product modules. Import in a lightweight child process;
    cache only results with the same root and source fingerprint. Importing the
    product module does not construct Store/LocalVoice or load a speech model.
    """
    key = _sha({'root': str(root), 'profile': profile_id, 'files': source_hashes})
    with _PROFILE_CACHE_LOCK:
        if key in _PROFILE_CACHE:
            return copy.deepcopy(_PROFILE_CACHE[key])
        program = (
            'import json,sys\n'
            'from pathlib import Path\n'
            'from backend import app\n'
            'if Path(app.__file__).resolve() != (Path.cwd()/"backend/app.py").resolve():\n'
            '    raise RuntimeError("product_source_mismatch")\n'
            'print(json.dumps(app.resolve_dialogue_profile(sys.argv[1])["metadata"], ensure_ascii=False))\n'
        )
        process = subprocess.run([sys.executable, '-B', '-c', program, profile_id],
                                 cwd=root, capture_output=True, text=True, timeout=20)
        if process.returncode:
            raise RuntimeError('product_profile_description_failed')
        try:
            metadata = json.loads(process.stdout)
        except (TypeError, ValueError):
            raise RuntimeError('product_profile_description_failed') from None
        after = {name: _sha((root / name).read_bytes()) if (root / name).is_file() else None
                 for name in source_hashes}
        if after != source_hashes:
            raise RuntimeError('product_source_changed_during_describe')
        if not isinstance(metadata, dict) or metadata.get('id') != profile_id:
            raise RuntimeError('product_profile_description_failed')
        if len(_PROFILE_CACHE) >= 64:
            _PROFILE_CACHE.clear()
        _PROFILE_CACHE[key] = copy.deepcopy(metadata)
        return metadata


def describe(configuration):
    root = _root()
    files = ['server.py', 'backend/app.py', 'backend/evaluation.py', 'backend/local_voice.py',
             'backend/agent_profiles.py', 'backend/dialogue_evidence.py', 'backend/dialogue_v3.py',
             'backend/friend_profiles.py', 'backend/friend_profiles_v5.py', 'backend/eval_profiles_v6.py']
    hashes = {name: _sha((root / name).read_bytes()) if (root / name).is_file() else None for name in files}
    prompts = {}
    if (root / 'backend/app.py').is_file():
        for node in ast.parse((root / 'backend/app.py').read_text()).body:
            if isinstance(node, ast.Assign):
                for name in node.targets:
                    if isinstance(name, ast.Name) and name.id in ('PROMPT', 'REVIEW_PROMPT', 'SUMMARY_PROMPT'):
                        prompts[name.id] = _sha(ast.literal_eval(node.value).encode())
    requested = _requested(configuration)
    profile_metadata = None
    if (root / 'backend/agent_profiles.py').is_file():
        profile_metadata = _profile_metadata(root, requested['dialogue_profile'], hashes)
    elif requested['dialogue_profile'] != 'baseline':
        raise RuntimeError('product_dialogue_profile_seam_missing')
    return {
        'adapter': ADAPTER_VERSION, 'project': 'pome', 'source_root': str(root),
        'environment': {'python': sys.version, 'platform': sys.platform, 'transport': 'stdlib HTTP on loopback'},
        'code_head': _git(root, 'rev-parse', 'HEAD').decode().strip() or None,
        'dirty_diff_sha256': _sha(_git(root, 'diff', 'HEAD', '--', *files)),
        'source_files_sha256': hashes, 'prompt_sha256': prompts,
        'dialogue_profile': profile_metadata,
        'adapter_sha256': _sha(Path(__file__).read_bytes()),
        'adapter_dependencies_sha256': {
            name: _sha((Path(__file__).resolve().parents[1] / name).read_bytes())
            for name in ('interactions/__init__.py', 'interactions/driver.py',
                         'interactions/providers.py', 'interactions/workspace.py')
            if (Path(__file__).resolve().parents[1] / name).is_file()
        },
        'entrypoints': ['/api/state', '/api/chat', '/api/summary'],
        'requested': requested,
        'effective': {'model': None, 'chat_reasoning_effort': None, 'summary_reasoning_effort': None,
                      'chat_thinking': None, 'summary_thinking': None},
        'capabilities': {'multi_turn': 'supported', 'audio': 'unsupported',
                         'scripted_user': 'supported', 'rule_based_user': 'supported',
                         'model_simulated_user': 'supported',
                         'cancel_local_wait': 'supported', 'cancel_provider': 'unsupported',
                         'environment_reset': 'supported', 'state_snapshot': 'supported',
                         'isolated_full_run': 'supported', 'fixed_context_rerun': 'unsupported',
                         'trace_rescore': 'unsupported'},
        'interaction_provider': {'id': 'codex_cli', 'requested_model': 'gpt-6-sol',
                                 'requested_reasoning_effort': 'low', 'tools': [],
                                 'effective_model': None, 'effective_reasoning_effort': None,
                                 'usage_scope': 'separate_from_product_model_usage',
                                 'activation': 'only_explicit_case_input_interaction_policy_mode_model'},
        'mode': configuration.get('mode'), 'limitations': LIMITATIONS,
    }


class RunStopped(Exception):
    def __init__(self, status):
        self.status = status


class RequestFailed(Exception):
    def __init__(self, status, body, endpoint):
        self.status, self.body, self.endpoint = status, body, endpoint


class InteractionStopped(Exception):
    def __init__(self, reason):
        self.reason = reason


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


def _artifacts(root, interaction):
    # Explicit evidence allowlist. Never walk a product data directory or export
    # arbitrary simulator workspace content/credentials.
    names = ('pome.events.jsonl', 'pome.state-initial.json', 'pome.state-current.json',
             'pome.state-final.json', 'pome.state-last.json', 'pome.result.json')
    result = {name: name for name in names if (root / name).is_file() or name == 'pome.result.json'}
    evidence_names = ('request.json', 'prompt.txt', 'boundary.txt', 'schema.json', 'events.jsonl',
                      'result.json', 'receipt.json', 'invocation.json', 'process.json')
    for receipt in (interaction or {}).get('provider_receipts', []):
        run_id = receipt.get('run_id')
        if not isinstance(run_id, str) or len(run_id) != 32 or any(c not in '0123456789abcdef' for c in run_id):
            continue
        for name in evidence_names:
            relative = Path('user-simulator') / run_id / 'live' / name
            path = root / relative
            if path.is_file() and not path.is_symlink():
                result[f'simulator.{run_id}.{name}'] = relative.as_posix()
    return result


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
    interaction = None
    terminal_assistant_delivery = None
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
        request_started = time.monotonic()

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
        ctx.emit('http_response', {'endpoint': endpoint, 'request_id': request_id, 'status': reply['status'],
                                   'duration_ms': round((time.monotonic() - request_started) * 1000, 3)},
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
        requested = _requested(configuration)
        if case.get('source', {}).get('synthetic') is not True:
            raise RuntimeError('synthetic_material_required')
        product_root = _root()
        source = _credential_source(product_root)
        source_hash = _source_state_hash(source)
        credentials = _read_credentials(source)
        credentials['chatModel'] = requested['model']
        fixture = copy.deepcopy(case['input'])
        interaction_policy = fixture.get('interaction_policy')
        if interaction_policy is not None:
            from ..interactions import TurnDriver, validate_policy
            try:
                interaction_policy = validate_policy(interaction_policy)
            except ValueError:
                raise RuntimeError('invalid_interaction_policy') from None
            if fixture.get('child_turns'):
                raise RuntimeError('ambiguous_interaction_input')
            if interaction_policy['max_turns'] > fixture.get('conversation_rounds', 3):
                raise RuntimeError('interaction_exceeds_product_rounds')
            provider = None
            if interaction_policy['mode'] == 'model':
                from ..interactions import CodexSimulatorProvider
                provider = CodexSimulatorProvider(root / 'user-simulator', model='gpt-6-sol', effort='low')
            interaction = TurnDriver(interaction_policy, ctx, episode, provider=provider)
        elif not isinstance(fixture.get('child_turns'), list) or not fixture['child_turns']:
            raise RuntimeError('missing_child_turns')
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
        # stub chat/summary, inspect recordings, or change the current voice.
        server.local_voice = app.LocalVoice(product_root / 'models')
        server.ai_options = {key: value for key, value in requested.items()
                             if key in ('chat_thinking', 'chat_reasoning_effort', 'summary_thinking', 'summary_reasoning_effort', 'dialogue_profile') and value is not None}
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
        index = 0
        previous_delivery = None
        while True:
            guard()
            if interaction is not None:
                generated = interaction.next_user(previous_delivery)
                previous_delivery = None
                if generated['text'] is None:
                    reason = generated['stop_reason']
                    if reason == 'cancelled':
                        raise RunStopped('cancelled')
                    if reason == 'deadline':
                        raise RunStopped('timed_out')
                    if reason not in ('max_turns', 'script_exhausted', 'simulator_completed',
                                      'simulator_stopped', 'assistant_stop_rule') and not reason.startswith('rule:'):
                        raise InteractionStopped(reason)
                    if not turns_result:
                        raise InteractionStopped('no_user_turns')
                    break
                utterance, finish_requested = generated['text'], False
            else:
                if index >= len(fixture['child_turns']):
                    break
                script = fixture['child_turns'][index]
                utterance = script if isinstance(script, str) else script['text']
                finish_requested = isinstance(script, dict) and script.get('finish_requested', False)
            index += 1
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
            # Only the actual delivered response enters the next turn.
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
                if interaction is not None:
                    # Closing a product episode must not ask a simulator to
                    # generate one extra (undelivered) child turn. Preserve the
                    # final delivery explicitly beside the driver's own view.
                    terminal_assistant_delivery = result['text']
                    ctx.emit('interaction.product_terminal_delivery',
                             {**interaction.identity, 'text': result['text'],
                              'reason': 'product_end_conversation'}, episode=episode, turn=index)
                    interaction.finish('product_end_conversation')
                break
            previous_delivery = result['text'] if interaction is not None else None
        summary_reply = http('POST', '/api/summary', {'storyId': episode, 'childId': child['id'],
                                                   'turns': copy.deepcopy(transcript)})
        summary = summary_reply['text']
        output['summary'] = summary
        output['scripted_turns_not_executed'] = (len(fixture['child_turns']) - len(turns_result)
                                               if interaction is None else None)
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
        required_stages = (provenance.get('dialogue_profile') or {}).get('required_chat_stages', ['candidate', 'review'])
        assessments.append(_rule('configured_stages_observed',
                                 all(any(e['turn'] == t['turn'] and e['stage'] == s for e in stage_results)
                                     for t in turns_result for s in required_stages),
                                 '每个已交付轮次都有配置声明的阶段记录：' + ', '.join(required_stages) + '；不代表内容质量通过。'))
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
    except InteractionStopped as exc:
        status = 'failed'
        error = {'code': 'interaction_stopped', 'reason': exc.reason,
                 'message': '模拟用户未正常完成；保留已交付内容，不当作完整对话成功。'}
    except Exception as exc:
        known = {'missing_credentials': '缺少已授权的 pome 本机服务配置。可用 MYEVAL_POME_CREDENTIAL_DB 指定含设置的本机数据库。',
                 'unsupported_provider': '该适配器首版仅接入已授权的 DeepSeek 官方服务。',
                 'unsupported_configuration': '支持 DeepSeek Flash 的显式思考开/关；开启需 low/high/max，关闭不携带思考强度。',
                 'unsupported_task': '不支持此任务类型。',
                 'ambiguous_interaction_input': 'interaction_policy 与未来 child_turns 不能同时提供。',
                 'invalid_interaction_policy': 'interaction_policy 不符合有界用户模拟契约；请核对模式、可见事实、分支与限制。',
                 'interaction_exceeds_product_rounds': '模拟用户最大轮数不能超过本次产品对话轮数。',
                 'missing_child_turns': '固定脚本执行需提供非空 child_turns。',
                 'synthetic_material_required': '首版执行仅允许明确标注的虚构材料。',
                 'product_observation_seam_missing': '产品源码缺少本次共享观测接口。'}
        code = str(exc) if str(exc) in known else 'adapter_error'
        status = 'unsupported' if code.startswith('unsupported') else 'failed'
        error = {'code': code, 'message': known.get(code, '适配器执行未完成。'), 'exception_type': type(exc).__name__}
    finally:
        if interaction is not None:
            interaction.finish('product_' + status)
            output['interaction'] = {**interaction.snapshot(),
                                     'terminal_assistant_delivery': terminal_assistant_delivery,
                                     'terminal_delivery_scope': 'observed_by_product_adapter_not_used_to_generate_another_user_turn',
                                     'human_feedback': False}
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
        for field in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'reasoning_tokens'):
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
    # The blinded reader needs the facts each side could actually see, without
    # profile names, internal plans, prompts, timings, or configuration hints.
    context_by_turn = [{'turn': e['turn'], 'background': {
        'childName': (e['data'].get('background') or {}).get('childName'),
        'recentStories': (e['data'].get('background') or {}).get('recentStories', []),
        'animals': (e['data'].get('background') or {}).get('animals', []),
    }} for e in events if e['kind'] == 'input' and e.get('operation') == 'chat']
    result = {'status': status, 'output': output,
              'review_output': {'transcript': copy.deepcopy(transcript), 'summary': summary,
                                'context_by_turn': context_by_turn},
              'observed_configuration': observed, 'usage': usage, 'cost': None,
              'assessments': assessments, 'limitations': LIMITATIONS, 'error': error,
              'artifacts': _artifacts(root, output.get('interaction'))}
    _write(root, 'pome.result.json', result)
    return result
