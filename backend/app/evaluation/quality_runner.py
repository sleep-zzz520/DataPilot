"""Local, versioned Agent experiments. No external tracing service is required.

python -m app.evaluation.quality_runner validate
python -m app.evaluation.quality_runner run --split dev --repeat 2 --out data/eval/run.json
A nonzero run exit means evaluation/release failed, not a missing report.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile
import time

from app.evaluation.task_suite import load_suite, suite_digest
from app.evaluation.quality_scoring import score_trial, SCORER_VERSION
from app.evaluation.quality_gate import check_release

BACKEND = Path(__file__).resolve().parents[2]
REPO = BACKEND.parent


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def read_model_config(config_id=None):
    """Read secrets without store migrations or writes. Only send via worker stdin."""
    external = os.environ.get('DATAPILOT_EVAL_MODEL_CONFIG')
    if external:
        config = json.loads(external)
    else:
        configs = json.loads((BACKEND / 'data/llm_configs.json').read_text())
        selected = [c for c in configs if c.get('id') == config_id] if config_id is not None else [c for c in configs if c.get('is_default')]
        if len(selected) != 1:
            raise ValueError('Select exactly one default or explicit model configuration')
        config = dict(selected[0])
        if config.get('api_key_enc'):
            from cryptography.fernet import Fernet
            key = os.environ.get('AGENT_MASTER_KEY')
            if key is None:
                key = Path(os.environ.get('AGENT_SECRET_FILE', str(BACKEND / 'data/.master_key'))).read_text().strip()
            config['api_key'] = Fernet(key.encode()).decrypt(config.pop('api_key_enc').encode()).decode()
    if not config.get('api_key') or not config.get('model_name'):
        raise ValueError('Missing configured model credentials/name')
    return config


def safe_model_config(config):
    # URL is fingerprinted, never serialized: private endpoints may contain keys.
    result = {key: config.get(key) for key in ('provider', 'model_name', 'temperature', 'max_tokens', 'reasoning_effort')}
    result['endpoint_sha256'] = digest(config.get('base_url', ''))
    return result


def provenance(suite, config, settings):
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=REPO, text=True).strip()
    sources = {}
    for base in (BACKEND / 'app', BACKEND / 'config'):
        for path in sorted(base.rglob('*')):
            if path.is_file() and path.suffix in ('.py', '.json'):
                sources[str(path.relative_to(REPO))] = hashlib.sha256(path.read_bytes()).hexdigest()
    for path in BACKEND.glob('requirements*.txt'):
        sources[str(path.relative_to(REPO))] = hashlib.sha256(path.read_bytes()).hexdigest()
    # Include tracked dirty content and untracked implementation files, without exposing content.
    tracked = subprocess.check_output(['git', 'diff', 'HEAD', '--name-only', '-z'], cwd=REPO)
    untracked = subprocess.check_output(['git', 'ls-files', '-o', '--exclude-standard', '-z'], cwd=REPO)
    dirty = {}
    for name in set((tracked + untracked).split(b'\0')) - {b''}:
        name = os.fsdecode(name)
        path = REPO / name
        if path.is_symlink():
            dirty[name] = digest(os.readlink(path))
        else:
            dirty[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else 'removed'
    packages = {}
    for name in ('langgraph', 'langchain-core', 'langchain-openai', 'langchain-anthropic',
                 'duckdb', 'pandas', 'pydantic', 'matplotlib', 'sqlglot'):
        packages[name] = importlib.metadata.version(name)
    safe_config = safe_model_config(config)
    return {'suite_id': suite['suite_version'], 'suite_sha256': suite_digest(suite),
            'scorer_version': SCORER_VERSION, 'code_commit': git('rev-parse', 'HEAD'),
            'dirty': bool(git('status', '--porcelain')), 'dirty_sha256': digest(dirty),
            'source_sha256': sources, 'model': safe_config,
            'candidate_config_sha256': digest({'model': safe_config, 'settings': settings}),
            'judge': {'kind': 'deterministic_rules', 'model': None, 'cost': 0},
            'environment': {'python': platform.python_version(), 'platform': platform.platform(), 'packages': packages},
            'settings': settings, 'dataset_snapshot': suite,
            'started_at': datetime.now(timezone.utc).isoformat(),
            'evaluation_kind': 'product_chain_synthetic',
            'limitations': ['Synthetic agent-authored labels, not human gold or blind holdout',
                'Calls real chat application handler, not HTTP/SSE/browser transport',
                'No MySQL engine; file SQL is the product DuckDB path, not MySQL acceptance',
                'Behavior/explicit explanation rules are fallible; full free-form semantics remain ungraded',
                'No RAG workload or RAG integration', 'No external trace/judge upload',
                'Hard worker deadline may end provider work without complete final usage; marked incomplete']}


def run_trial(task, config, *, timeout=180, max_calls=12, request_timeout=20, mode='auto'):
    with tempfile.TemporaryDirectory(prefix='datapilot-eval-') as directory:
        root = Path(directory)
        payload = {'task': task, 'config': config, 'root': directory, 'mode': mode,
                   'max_calls': min(max_calls, task['budget']['max_model_calls']),
                   'request_timeout': request_timeout}
        env = dict(os.environ)
        env.update({'LANGCHAIN_TRACING': 'false', 'LANGCHAIN_TRACING_V2': 'false',
                    'OTEL_SDK_DISABLED': 'true', 'MPLCONFIGDIR': str(root / 'matplotlib')})
        # Never inherit an optional tracing API key into experiments.
        for key in list(env):
            if key.startswith(('LANGSMITH_', 'LANGCHAIN_API', 'LANGFUSE_', 'OTEL_EXPORTER_')):
                env.pop(key)
        for key in ('DATAPILOT_EVAL_MODEL_CONFIG', 'AGENT_MASTER_KEY', 'AGENT_SECRET_FILE'):
            env.pop(key, None)
        env['LANGSMITH_TRACING'] = 'false'
        started = time.monotonic()
        process = subprocess.Popen([sys.executable, '-m', 'app.evaluation.product_trial'], cwd=BACKEND,
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            text=True, env=env, start_new_session=True)
        timeout = min(timeout, task['budget']['max_latency_seconds'])
        try:
            process.communicate(json.dumps(payload), timeout=timeout)
            path = root / 'result.json'
            observation = json.loads(path.read_text()) if path.exists() else {
                'error': 'WORKER_EXIT_WITHOUT_RESULT', 'behavior': 'unknown', 'usage_complete': False}
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            path = root / 'progress.json'
            try:
                partial = json.loads(path.read_text()) if path.exists() else {}
            except ValueError:
                partial = {}
            calls_path = root / 'tools-progress.json'
            try:
                calls = json.loads(calls_path.read_text()) if calls_path.exists() else None
            except ValueError:
                calls = None
            observation = {'error': 'TRIAL_TIMEOUT', 'behavior': 'unknown', 'usage_complete': False,
                           'partial_usage': partial, 'calls': calls, 'calls_complete': False}
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            raise
        observation['latency_s'] = round(time.monotonic() - started, 3)
        return redact_observation(observation, config)


def redact_observation(value, config):
    """Scrub known configured secrets from exception text and nested tool output."""
    secrets = [config.get(key) for key in ('api_key', 'api_key_enc', 'base_url')]
    secrets = [secret for secret in secrets if isinstance(secret, str) and len(secret) >= 8]
    def scrub(item):
        if isinstance(item, str):
            for secret in secrets:
                item = item.replace(secret, '[REDACTED]')
            return item
        if isinstance(item, list):
            return [scrub(entry) for entry in item]
        if isinstance(item, dict):
            return {scrub(key): scrub(entry) for key, entry in item.items()}
        return item
    return scrub(value)


def summarize(trials, tasks, trials_per_task=None):
    by_task = defaultdict(list)
    for trial in trials:
        by_task[trial['task_id']].append(trial)
    def group(rows):
        latencies = sorted(r['observation'].get('latency_s', 0) for r in rows)
        def percentile(p):
            return latencies[max(0, math.ceil(len(latencies)*p)-1)] if latencies else None
        return {'runs': len(rows), 'passed': sum(r['score']['passed'] for r in rows),
                'business_completed': sum(r['score']['business_completed'] for r in rows),
                'input_tokens': sum(r['observation'].get('input_tokens', r['observation'].get('partial_usage', {}).get('input_tokens', 0)) for r in rows),
                'output_tokens': sum(r['observation'].get('output_tokens', r['observation'].get('partial_usage', {}).get('output_tokens', 0)) for r in rows),
                'attempted_model_calls': sum(r['observation'].get('attempted_model_calls', r['observation'].get('partial_usage', {}).get('attempted_calls', 0)) for r in rows),
                'completed_model_calls': sum(r['observation'].get('completed_model_calls', r['observation'].get('partial_usage', {}).get('completed_calls', 0)) for r in rows),
                'usage_incomplete_runs': sum(not r['observation'].get('usage_complete') for r in rows),
                'latency_s': {'p50': percentile(.5), 'p95': percentile(.95), 'max': max(latencies) if latencies else None}}
    first = [min(rows, key=lambda r: r['trial_index']) for rows in by_task.values()]
    def outcome(row):
        if not row['score']['passed']:
            return 'failure'
        if row['score']['business_completed']:
            return 'success'
        return row['observation'].get('behavior', 'unknown')
    return {'unique_tasks': len(by_task), **group(trials),
            'first_trial': group(first),
            'all_repeats_pass_tasks': sum((trials_per_task is None or len(rows) == trials_per_task)
                                          and all(r['score']['passed'] for r in rows) for rows in by_task.values()),
            'by_task': {key: group(rows) for key, rows in by_task.items()},
            'by_expected_behavior': {b: group([r for r in trials if next(t for t in tasks if t['id'] == r['task_id'])['expected']['behavior'] == b])
                                     for b in sorted({t['expected']['behavior'] for t in tasks})},
            'by_outcome': {b: group([r for r in trials if outcome(r) == b])
                           for b in ('success', 'clarify', 'refuse', 'approval', 'failure')},
            'by_split': {s: group([r for r in trials if r['task_id'] in {t['id'] for t in tasks if t['split'] == s}])
                         for s in sorted({t['split'] for t in tasks})},
            'scoring_cost': {'provider_calls': 0, 'tokens': 0, 'currency': 0},
            'monetary_cost': 'not priced: provider-specific price not configured; tokens reported instead'}


def write_report(path, report):
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, default=str)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def failure_kind(observation, score):
    error = observation.get('error')
    if error in ('TRIAL_TIMEOUT', 'WORKER_EXIT_WITHOUT_RESULT', 'WORKER_INFRASTRUCTURE_ERROR'):
        return 'infrastructure'
    if error in ('MODEL_BUDGET_EXHAUSTED', 'TOKEN_BUDGET_EXCEEDED'):
        return 'budget'
    if error:
        return 'execution'
    if score['passed']:
        return None
    if any(entry.get('status') in ('error', 'timeout', 'busy') for entry in observation.get('trace', [])):
        return 'tool_or_graph'
    if score['status'] == 'unknown':
        return 'grader_unknown'
    return 'task'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['validate', 'run', 'gate'])
    parser.add_argument('--suite')
    parser.add_argument('--split', choices=['dev', 'regression', 'acceptance', 'all'], default='dev')
    parser.add_argument('--tasks', help='Comma-separated exact IDs; unknown IDs are errors')
    parser.add_argument('--repeat', type=int, default=2)
    parser.add_argument('--max-calls', type=int, default=12)
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--request-timeout', type=float, default=20)
    parser.add_argument('--config-id', type=int)
    parser.add_argument('--mode', choices=['auto', 'single', 'multi'], default='auto')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--policy', choices=['all_required', 'baseline'], default='all_required')
    args = parser.parse_args(argv)
    suite = load_suite(args.suite)
    if args.command == 'validate':
        print(json.dumps({'suite_id': suite['suite_version'], 'suite_sha256': suite_digest(suite), 'tasks': len(suite['tasks']), 'blind': False}))
        return 0
    if not args.out:
        parser.error('--out required')
    if (not 1 <= args.repeat <= 5 or not 1 <= args.max_calls <= 30
            or not all(math.isfinite(v) and v > 0 for v in (args.timeout, args.request_timeout))):
        parser.error('invalid repetition/call/time budget')
    tasks = [t for t in suite['tasks'] if args.split == 'all' or t['split'] == args.split]
    if args.tasks:
        ids = args.tasks.split(',')
        if len(ids) != len(set(ids)) or set(ids) - {t['id'] for t in tasks}:
            parser.error('unknown/duplicate task IDs or task outside split')
        tasks = [t for t in tasks if t['id'] in ids]
    if not tasks:
        parser.error('no selected tasks')
    settings = {'mode': args.mode, 'workers': 1, 'max_calls': args.max_calls,
                'request_timeout': args.request_timeout, 'trial_timeout': args.timeout,
                'provider_max_retries': 0}
    baseline = json.loads(args.baseline.read_text()) if args.baseline else None
    if args.command == 'gate':
        report = json.loads(args.out.read_text())
        valid = (isinstance(report, dict)
                 and report.get('suite_id') == suite['suite_version']
                 and report.get('suite_sha256') == suite_digest(suite)
                 and report.get('dataset_snapshot') == suite
                 and report.get('selected_task_ids') == [t['id'] for t in tasks]
                 and report.get('trials_per_task') == args.repeat
                 and report.get('expected_runs') == len(tasks) * args.repeat
                 and report.get('scorer_version') == SCORER_VERSION
                 and report.get('evaluation_kind') == 'product_chain_synthetic'
                 and report.get('settings') == settings
                 and isinstance(report.get('model'), dict)
                 and report.get('candidate_config_sha256') == digest({'model': report['model'], 'settings': report['settings']}))
        if not valid:
            print('invalid experiment: suite/selection/repetition/scorer/settings mismatch')
            return 1
        decision = check_release(report, tasks, baseline, policy=args.policy)
        print(json.dumps(decision))
        return 0 if decision['passed'] else 1
    if args.out.exists():
        parser.error('output already exists; preserve prior experiment and choose a new path')
    try:
        config = read_model_config(args.config_id)
    except Exception as exc:
        # Invalid credentials may appear in a provider/configuration exception message.
        parser.error(f'model configuration unavailable ({type(exc).__name__})')
    report = provenance(suite, config, settings)
    report.update({'selected_task_ids': [t['id'] for t in tasks], 'trials_per_task': args.repeat,
                   'expected_runs': len(tasks)*args.repeat, 'trials': [],
                   'reproduce': ['python', '-m', 'app.evaluation.quality_runner'] + list(argv or sys.argv[1:])})
    args.out.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    write_report(args.out, report)
    for task in tasks:
        for trial_index in range(args.repeat):
            try:
                obs = run_trial(task, config, timeout=args.timeout, max_calls=args.max_calls,
                                request_timeout=args.request_timeout, mode=args.mode)
            except Exception as exc:
                obs = {'error': 'WORKER_INFRASTRUCTURE_ERROR', 'detail': type(exc).__name__,
                       'behavior': 'unknown', 'usage_complete': False}
            score = score_trial(task, obs)
            row = {'task_id': task['id'], 'trial_index': trial_index, 'error': obs.get('error'),
                   'observation': obs, 'score': score,
                   'failure_kind': failure_kind(obs, score)}
            report['trials'].append(row)
            report['summary'] = summarize(report['trials'], tasks, args.repeat)
            write_report(args.out, report)
            print(task['id'], trial_index, score['status'], obs.get('error'), obs.get('latency_s'), flush=True)
    report['gate'] = check_release(report, tasks, baseline, policy=args.policy)
    report['completed_at'] = datetime.now(timezone.utc).isoformat()
    write_report(args.out, report)
    print(json.dumps({'summary': report['summary'], 'gate': report['gate']}, ensure_ascii=False, indent=2))
    return 0 if report['gate']['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
