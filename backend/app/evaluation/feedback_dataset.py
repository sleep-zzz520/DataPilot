"""Local-only feedback staging. Raw prompts, SQL, outputs and identifiers never export.

TraceCollector and usage rows are existing observations; this module does not imply
an existing product feedback UI. A reviewer must write a synthetic reproduction,
check its oracle and approve it before regression admission.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
from pathlib import Path

from app.evaluation.task_suite import validate_task

_TOOL_NAMES = {'query_mysql', 'list_schemas', 'get_schema', 'get_table_schema',
               'query_file', 'list_files', 'file_stats', 'make_chart', 'generate_chart',
               'auto_analyze_and_visualize', 'data_executor', 'statistical_validator',
               'insight_writer', 'local_repair', 'calculate_from_evidence', 'chart_from_evidence'}
_STATUSES = {'running', 'ok', 'error', 'timeout', 'busy'}
_REASONS = {'incorrect_result', 'missing_source', 'unsafe_action', 'bad_chart',
            'clarification', 'tool_failure', 'budget', 'other'}


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def export_candidate(record: dict, reason: str, path: str | Path) -> dict:
    """Export an allowlisted summary, never arbitrary nested metadata or free text."""
    if reason not in _REASONS:
        raise ValueError('use a controlled feedback reason')
    trace = []
    for entry in record.get('trace', []):
        trace.append({'tool': entry.get('tool') if entry.get('tool') in _TOOL_NAMES else 'other',
                      'status': entry.get('status') if entry.get('status') in _STATUSES else 'other',
                      'duration_ms': _number(entry.get('duration_ms'))})
    usage = record.get('usage') or {}
    candidate = {'schema_version': 1, 'state': 'pending_review', 'reason': reason,
                 'trace': trace, 'usage': {k: _number(usage.get(k)) for k in
                     ('input_tokens', 'output_tokens', 'calls', 'cost_usd')},
                 'review': None, 'task': None}
    candidate['candidate_id'] = hashlib.sha256(json.dumps(candidate, sort_keys=True).encode()).hexdigest()
    _write_new(path, candidate)
    return candidate


def _write_new(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Caller chooses a local-only location; no network or upload implementation exists.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    path.chmod(0o600)


def annotate_candidate(candidate: dict, *, task: dict, reviewer: str,
                       privacy_checked: bool, oracle_checked: bool) -> dict:
    """Human-callable explicit review; exporting alone can never approve a case.

    reviewer is a local reviewer alias. The operator, not this function, attests
    privacy/oracle checks; this is not an automatic privacy certification.
    """
    if candidate.get('state') != 'pending_review' or not reviewer.strip():
        raise ValueError('pending candidate and reviewer required')
    if privacy_checked is not True or oracle_checked is not True:
        raise ValueError('privacy and oracle review are required')
    validate_task(task)
    if task['split'] != 'regression':
        raise ValueError('feedback can only enter regression')
    result = json.loads(json.dumps(candidate))
    result.update(state='reviewed', task=json.loads(json.dumps(task)), review={
        'reviewer': reviewer.strip(), 'privacy_checked': True, 'oracle_checked': True,
        'task_sha256': hashlib.sha256(json.dumps(task, sort_keys=True, ensure_ascii=False).encode()).hexdigest()})
    return result


def admit_candidate(candidate: dict, directory: str | Path) -> Path:
    """Validate the reviewed reproduction and write one immutable local case."""
    review, task = candidate.get('review') or {}, candidate.get('task')
    if candidate.get('state') != 'reviewed' or not review.get('reviewer') or review.get('privacy_checked') is not True or review.get('oracle_checked') is not True:
        raise ValueError('unreviewed feedback cannot enter regression')
    validate_task(task)
    digest = hashlib.sha256(json.dumps(task, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if task['split'] != 'regression' or review.get('task_sha256') != digest:
        raise ValueError('review does not match task')
    identifier = candidate.get('candidate_id', '')
    if len(identifier) != 64 or any(c not in '0123456789abcdef' for c in identifier):
        raise ValueError('invalid candidate id')
    path = Path(directory) / f'{identifier}.json'
    _write_new(path, {**candidate, 'state': 'admitted'})
    return path


def export_session_candidate(snapshot, session_id, user_id, reason, path):
    """Read one operator-supplied local SQLite snapshot. Export no session IDs."""
    from contextlib import closing
    uri = Path(snapshot).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        if not conn.execute('SELECT 1 FROM sessions WHERE id=? AND user_id=?', (session_id, user_id)).fetchone():
            raise ValueError('session absent from snapshot or ownership does not match')
        trace = [entry for (data,) in conn.execute(
            'SELECT data FROM traces WHERE session_id=? ORDER BY turn_seq', (session_id,))
            for entry in json.loads(data)]
        usage = {}
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='usage_logs'").fetchone():
            values = conn.execute('SELECT sum(input_tokens), sum(output_tokens), sum(cost_usd) '
                                  'FROM usage_logs WHERE session_id=? AND user_id=?', (session_id, user_id)).fetchone()
            usage = dict(zip(('input_tokens', 'output_tokens', 'cost_usd'), values))
    return export_candidate({'trace': trace, 'usage': usage}, reason, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    export = commands.add_parser('export')
    export.add_argument('--snapshot', type=Path, required=True)
    export.add_argument('--session-id', required=True)
    export.add_argument('--user-id', type=int, required=True)
    export.add_argument('--reason', choices=sorted(_REASONS), required=True)
    export.add_argument('--out', type=Path, required=True)
    review = commands.add_parser('review')
    review.add_argument('--candidate', type=Path, required=True)
    review.add_argument('--task', type=Path, required=True)
    review.add_argument('--reviewer', required=True)
    review.add_argument('--privacy-checked', action='store_true')
    review.add_argument('--oracle-checked', action='store_true')
    review.add_argument('--out', type=Path, required=True)
    admit = commands.add_parser('admit')
    admit.add_argument('--candidate', type=Path, required=True)
    admit.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == 'export':
        export_session_candidate(args.snapshot, args.session_id, args.user_id, args.reason, args.out)
    elif args.command == 'review':
        candidate = annotate_candidate(json.loads(args.candidate.read_text()), task=json.loads(args.task.read_text()),
                                       reviewer=args.reviewer, privacy_checked=args.privacy_checked,
                                       oracle_checked=args.oracle_checked)
        _write_new(args.out, candidate)
    else:
        print(admit_candidate(json.loads(args.candidate.read_text()), args.directory))


if __name__ == '__main__':
    main()
