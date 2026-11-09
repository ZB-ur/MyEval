"""Optional local Langfuse v4 export. MyEval remains the evidence source of truth.

One immutable OTLP batch per completed trial. Credentials never enter the batch,
errors, or return value. No automatic network retry can duplicate observations.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
_SENSITIVE = {'apikey', 'api_key', 'authorization', 'password', 'secret_key', 'access_token', 'refresh_token', 'reasoning_content', 'chain_of_thought', 'internal_reasoning'}
_TERMINAL = {'succeeded', 'failed', 'refused', 'unsupported', 'cancelled', 'timed_out', 'interrupted'}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _safe(value):
    if isinstance(value, dict):
        return {k: ('[redacted]' if str(k).lower() in _SENSITIVE else _safe(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    return value


def _id(value, length=32):
    return hashlib.sha256(str(value).encode()).hexdigest()[:length]


def _nano(value):
    if not value:
        raise ValueError('Missing observed timestamp')
    timestamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if timestamp.tzinfo is None:
        raise ValueError('Timestamp timezone is required')
    return str(int(timestamp.timestamp() * 1_000_000_000))


def _value(value):
    if isinstance(value, bool):
        return {'boolValue': value}
    if isinstance(value, int):
        return {'intValue': str(value)}
    if isinstance(value, float):
        return {'doubleValue': value}
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return {'arrayValue': {'values': [{'stringValue': v} for v in value]}}
    return {'stringValue': value if isinstance(value, str) else _dumps(value)}


def _attrs(values):
    return [{'key': key, 'value': _value(value)} for key, value in values.items() if value is not None]


def _config():
    path = Path(os.environ.get('MYEVAL_LANGFUSE_CONFIG', str(ROOT / '.local' / 'platform' / 'client.json')))
    if not path.exists():
        return None
    config = json.loads(path.read_text())
    parsed = urlsplit(config.get('base_url', ''))
    local = parsed.hostname == 'localhost'
    if not local:
        try:
            local = ipaddress.ip_address(parsed.hostname or '').is_loopback
        except ValueError:
            local = False
    if not local or parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'', '/'}:
        raise ValueError('Langfuse export is restricted to a loopback base URL')
    if not all(isinstance(config.get(k), str) and config[k] for k in ('public_key', 'secret_key', 'project_id')):
        raise ValueError('Missing local project credentials')
    config['base_url'] = config['base_url'].rstrip('/')
    return config


def _request(config, path, payload=None):
    credential = base64.b64encode((config['public_key'] + ':' + config['secret_key']).encode()).decode()
    headers = {'Authorization': 'Basic ' + credential, 'Content-Type': 'application/json', 'x-langfuse-ingestion-version': '4', 'User-Agent': 'MyEval-local-export/1'}
    request = Request(config['base_url'] + path, data=_dumps(payload).encode() if payload is not None else None, headers=headers)
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=12) as response:
        raw = response.read(16 * 1024 * 1024)
        return response.status, json.loads(raw) if raw else {}


def _batch(trial, events):
    trial_id = str(trial['id'])
    identity = trial_id + ':attempt:' + str(trial.get('attempt', 1))
    trace_id = _id('myeval:trial:' + identity)
    root_id = _id('myeval:root:' + identity, 16)
    configuration = trial.get('configuration') or {}
    case = trial.get('case') or {}
    result = trial.get('result') or {}
    run_key = ':'.join(str(trial.get(k, 'unknown')) for k in ('experiment_id', 'configuration_id', 'repeat_index', 'attempt'))
    experiment_id = _id('myeval:experiment-run:' + run_key)
    case_id = str(trial.get('case_id', case.get('id', 'unknown')))
    project = str(trial.get('project', 'unknown'))
    metadata = {k: trial.get(k) for k in ('id', 'experiment_id', 'project', 'case_id', 'configuration_id', 'repeat_index', 'attempt', 'status')}
    shared = {
        'langfuse.environment': 'myeval-local',
        'langfuse.trace.name': f'{project}/{case_id}',
        'langfuse.trace.tags': ['myeval', project, str(trial.get('status', 'unknown'))],
        'langfuse.trace.public': False,
        'langfuse.session.id': str(trial['experiment_id']),
        'langfuse.experiment.id': experiment_id,
        'langfuse.experiment.name': run_key,
        'langfuse.experiment.dataset.id': f'myeval:{project}',
        'langfuse.experiment.item.id': case_id,
        'langfuse.experiment.item.root_observation_id': root_id,
        **{'langfuse.trace.metadata.' + key: value for key, value in metadata.items()},
    }
    started = trial.get('started_at') or trial.get('queued_at')
    ended = trial.get('ended_at') or started
    root_attributes = {
        **shared,
        'langfuse.observation.type': 'agent',
        'langfuse.observation.input': _dumps(_safe(case)),
        'langfuse.observation.output': _dumps(_safe(result.get('output'))),
        'langfuse.observation.metadata.configuration': _safe(configuration),
        'langfuse.observation.metadata.observed_configuration': _safe(result.get('observed_configuration')),
        'langfuse.observation.metadata.usage': {'value': _safe(result.get('usage')), 'unknown_is_not_zero': result.get('usage') is None},
        'langfuse.observation.metadata.cost': {'value': _safe(result.get('cost')), 'unknown_is_not_zero': result.get('cost') is None},
        'langfuse.observation.metadata.assessments': _safe(result.get('assessments', [])),
        'langfuse.observation.metadata.limitations': _safe(result.get('limitations', [])),
        'langfuse.observation.metadata.error': _safe(result.get('error')),
        'langfuse.observation.metadata.attempts': _safe(trial.get('attempts', [])),
        'langfuse.observation.status_message': str(trial.get('status', 'unknown')),
        'langfuse.observation.level': 'DEFAULT' if trial.get('status') == 'succeeded' else 'WARNING',
        'langfuse.experiment.item.expected_output': _dumps(case.get('expected_behavior')),
        'langfuse.experiment.item.metadata.case_version': str(case.get('version', 'unknown')),
        'langfuse.experiment.description': 'MyEval frozen local task pack. Repeats and attempts are separate experiment runs; no composite quality score.',
    }
    spans = [{'traceId': trace_id, 'spanId': root_id, 'name': f'{project}/{case_id}', 'kind': 1, 'startTimeUnixNano': _nano(started), 'endTimeUnixNano': _nano(ended), 'attributes': _attrs(root_attributes), 'status': {'code': 1 if trial.get('status') == 'succeeded' else 2, 'message': str(trial.get('status', 'unknown'))}}]
    for index, event in enumerate(events):
        timestamp = event.get('timestamp') or started
        attributes = {
            **shared,
            'langfuse.observation.type': 'event',
            'langfuse.observation.output': _dumps(_safe(event.get('data'))),
            'langfuse.observation.metadata.event_id': event.get('id'),
            'langfuse.observation.metadata.event_kind': event.get('kind'),
            **{'langfuse.observation.metadata.' + key: event.get(key) for key in ('episode', 'turn', 'stage', 'attempt')},
            'langfuse.observation.metadata.timing_semantics': 'observed event instant; not an invented stage duration',
        }
        spans.append({'traceId': trace_id, 'spanId': _id(f'myeval:event:{identity}:{index}:{event.get("id", "")}', 16), 'parentSpanId': root_id, 'name': str(event.get('kind', 'event')), 'kind': 1, 'startTimeUnixNano': _nano(timestamp), 'endTimeUnixNano': _nano(timestamp), 'attributes': _attrs(attributes)})
    payload = {'resourceSpans': [{'resource': {'attributes': _attrs({'service.name': 'myeval', 'service.version': '1', 'deployment.environment.name': 'myeval-local'})}, 'scopeSpans': [{'scope': {'name': 'myeval.export', 'version': '1'}, 'spans': spans}]}]}
    if len(_dumps(payload).encode()) > 10 * 1024 * 1024:
        raise ValueError('Trial exceeds 10 MiB single-batch export limit; local evidence retained')
    return payload, trace_id, root_id, experiment_id, [span['spanId'] for span in spans]


def _readback(config, trace_id, expected_ids):
    """Read accepted observation IDs only; do not relay platform payloads or secrets."""
    found = []
    cursor = None
    for _ in range(20):
        query = {'traceId': trace_id, 'limit': 100, 'fields': 'core'}
        if cursor:
            query['cursor'] = cursor
        _, response = _request(config, '/api/public/v2/observations?' + urlencode(query))
        found.extend(item['id'] for item in response.get('data', []) if 'id' in item)
        cursor = response.get('meta', {}).get('cursor')
        if not cursor:
            break
    return {'visible': set(expected_ids).issubset(found), 'observations_found': len(found), 'observations_expected': len(expected_ids), 'duplicate_observation_ids': len(found) - len(set(found))}


def _publish_trial(trial: dict, events: list, data_dir: Path) -> dict:
    """Send one completed trial, keeping durable transport receipts and unknowns.

    A confirmed earlier receipt is returned without sending again. An ambiguous
    previous response is reconciled by reads only; it is never blindly retried.
    ``published`` means accepted by ingestion; ``visible`` separately records a
    readback when available. This function never determines trial success.
    """
    base = {'status': 'failed', 'trace_id': None, 'url': None, 'error': None}
    try:
        trial = {**trial, 'attempt': trial.get('attempt', 1)}
        if not isinstance(trial['attempt'], int) or isinstance(trial['attempt'], bool) or trial['attempt'] < 1:
            raise ValueError('Attempt must be a positive integer')
        config = _config()
        if config is None:
            return {**base, 'status': 'unconfigured', 'error': 'Local Langfuse has not been configured.'}
        if trial.get('status') not in _TERMINAL:
            return {**base, 'error': 'Only a terminal trial can be published.', 'retry_safe': True}
        payload, trace_id, root_id, experiment_id, span_ids = _batch(trial, events)
    except Exception as error:
        return {**base, 'error': f'Invalid local export configuration or evidence ({type(error).__name__}).', 'retry_safe': True}
    base.update(attempt=trial['attempt'], trace_id=trace_id, observation_id=root_id, platform_experiment_id=experiment_id, url=config['base_url'] + '/project/' + quote(config['project_id'], safe='') + '/traces/' + trace_id)
    fingerprint = hashlib.sha256(_dumps(payload).encode()).hexdigest()
    receipt_dir = Path(data_dir) / 'platform' / 'receipts'
    receipt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt_path = receipt_dir / (_id(str(trial['id']) + ':attempt:' + str(trial['attempt'])) + '.json')
    # Preserve the initial transport canary recorded before attempt identities
    # were finalized; never resend those accepted spans under a second identity.
    legacy_path = receipt_dir / (_id(trial['id']) + '.json')
    if trial['attempt'] == 1 and legacy_path.exists() and not receipt_path.exists():
        receipt_path = legacy_path
    lock_path = receipt_path.with_suffix('.lock')

    def save(value):
        temp = receipt_path.with_suffix('.tmp')
        with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as file:
            json.dump({'fingerprint': fingerprint, 'span_ids': span_ids, **value}, file, ensure_ascii=False, indent=2)
        os.replace(temp, receipt_path)

    with lock_path.open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if receipt_path.exists():
            previous = json.loads(receipt_path.read_text())
            if previous.get('status') == 'published':
                previous.pop('span_ids', None)
                previous.pop('fingerprint', None)
                return {**previous, 'already_published': True, 'current_evidence_matches': json.loads(receipt_path.read_text()).get('fingerprint') == fingerprint}
            if not previous.get('retry_safe', False):
                try:
                    readback = _readback(config, trace_id, previous.get('span_ids', span_ids))
                except Exception:
                    readback = {'visible': False, 'readback': 'unavailable'}
                response = {**base, **readback, 'status': 'published' if readback.get('visible') else 'failed', 'error': None if readback.get('visible') else 'Previous ingestion outcome is unknown; not resending to avoid duplicate observations.', 'retry_safe': False}
                save(response)
                return response
        save({**base, 'error': 'Ingestion in progress; a crash leaves acceptance unknown.', 'retry_safe': False})
        try:
            status, response = _request(config, '/api/public/otel/v1/traces', payload)
            rejected = int(response.get('partialSuccess', {}).get('rejectedSpans', 0))
            if rejected:
                result = {**base, 'error': f'Langfuse rejected {rejected} spans; partial acceptance requires review.', 'retry_safe': False, 'http_status': status}
            else:
                result = {**base, 'status': 'published', 'http_status': status, 'accepted_at': datetime.now(timezone.utc).isoformat(), 'observations_sent': len(span_ids), 'visible': None, 'retry_safe': False}
        except HTTPError as error:
            result = {**base, 'error': f'Langfuse returned HTTP {error.code}. Local evidence retained.', 'http_status': error.code, 'retry_safe': error.code in {400, 401, 403, 404, 413, 415, 429}}
        except URLError as error:
            refused = isinstance(error.reason, ConnectionRefusedError)
            result = {**base, 'error': 'Local Langfuse connection refused.' if refused else 'Network response unavailable; ingestion acceptance is unknown.', 'retry_safe': refused}
        except Exception as error:
            result = {**base, 'error': f'Ingestion response unavailable ({type(error).__name__}); acceptance is unknown.', 'retry_safe': False}
        save(result)
        return result


def publish_trial(trial: dict, events: list, data_dir: Path) -> dict:
    """Optional publication must never turn an already saved trial into a failure."""
    try:
        return _publish_trial(trial, events, data_dir)
    except Exception as error:
        return {'status': 'failed', 'trace_id': None, 'url': None, 'error': f'Local publication bookkeeping failed ({type(error).__name__}). Evidence is still local.', 'retry_safe': False}


def verify_publication(trial_id: str, data_dir: Path, attempt: int = 1) -> dict:
    """Read-only check for actual persisted observations after accepted ingestion."""
    directory = Path(data_dir) / 'platform' / 'receipts'
    path = directory / (_id(str(trial_id) + ':attempt:' + str(attempt)) + '.json')
    if attempt == 1 and not path.exists():
        path = directory / (_id(trial_id) + '.json')
    if not path.exists():
        return {'visible': False, 'error': 'No publication receipt.'}
    try:
        receipt = json.loads(path.read_text())
        config = _config()
        if not config:
            return {'visible': False, 'error': 'Langfuse is unconfigured.'}
        return {'trace_id': receipt['trace_id'], **_readback(config, receipt['trace_id'], receipt['span_ids'])}
    except Exception as error:
        return {'visible': False, 'error': f'Readback unavailable ({type(error).__name__}).'}
