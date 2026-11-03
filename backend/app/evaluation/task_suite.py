"""Versioned, agent-authored synthetic tasks; no claim of blind evaluation."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from pathlib import Path

from app.evaluation.trajectory import validate_trajectory

DEFAULT_SUITE = Path(__file__).with_name('suites') / 'agent_quality_v1.json'


def _table(value: dict) -> None:
    if not isinstance(value, dict):
        raise ValueError('invalid oracle table')
    columns, rows = value.get('columns'), value.get('rows')
    if not isinstance(columns, list) or not columns or any(not isinstance(c, str) or not c for c in columns) or len(set(columns)) != len(columns):
        raise ValueError('invalid table columns')
    if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) != len(columns) for row in rows):
        raise ValueError('invalid table rows')
    for key in ('abs_tol', 'rel_tol'):
        v = value.get(key, 0)
        if type(v) not in (int, float) or not math.isfinite(v) or v < 0:
            raise ValueError('invalid oracle tolerance')
    if 'ordered' in value and type(value['ordered']) is not bool:
        raise ValueError('invalid oracle ordering')


def validate_task(task: dict) -> None:
    required = {'id', 'family', 'domain', 'split', 'fixture', 'identity', 'permissions',
                'turns', 'expected', 'constraints', 'budget', 'tags', 'label_version', 'critical_safety'}
    if not isinstance(task, dict) or required - task.keys():
        raise ValueError('missing task fields')
    for field in ('id', 'family', 'domain', 'label_version'):
        if not isinstance(task[field], str) or not task[field].strip():
            raise ValueError('invalid task identity')
    if task['split'] not in {'dev', 'regression', 'acceptance'}:
        raise ValueError('invalid split')
    if not isinstance(task['turns'], list) or not task['turns']:
        raise ValueError('invalid turns')
    for turn in task['turns']:
        if turn.get('role') != 'user' or not isinstance(turn.get('content'), str) or not turn['content'].strip():
            raise ValueError('only actual nonempty user turns are supported')
        if turn.get('session', 'current') not in {'new', 'current'} or turn.get('approval', 'none') not in {'approve', 'reject', 'none'}:
            raise ValueError('invalid scripted turn decision')
    expected = task['expected']
    if expected.get('behavior') not in {'answer', 'clarify', 'refuse', 'approval'}:
        raise ValueError('invalid expected behavior')
    if expected['behavior'] == 'answer':
        _table(expected.get('result'))
    elif expected.get('result') is not None:
        raise ValueError('non-answer must not carry result oracle')
    for field in ('sources', 'semantic_rules'):
        if not isinstance(expected.get(field), list):
            raise ValueError(f'invalid expected {field}')
    for rule in expected['semantic_rules']:
        if not isinstance(rule, dict) or not isinstance(rule.get('field'), str):
            raise ValueError('invalid semantic rule')
        if rule['field'] == 'reply' and isinstance(rule.get('pattern'), str):
            try:
                re.compile(rule['pattern'])
            except re.error as exc:
                raise ValueError('invalid semantic pattern') from exc
        elif 'equals' not in rule:
            raise ValueError('invalid semantic rule')
    if 'chart' in expected:
        chart = expected['chart']
        if not isinstance(chart, dict) or chart.get('type') not in {'line', 'bar', 'pie'}:
            raise ValueError('invalid chart oracle')
        _table(chart.get('data'))
    fixture = task['fixture']
    if fixture.get('kind') not in {'isolated_csv', 'no_data'} or fixture.get('fault') not in {None, 'query_timeout', 'query_error'}:
        raise ValueError('unsupported fixture')
    names = []
    for table in fixture.get('tables', []):
        _table(table)
        name = table.get('name', '')
        if not name or Path(name).name != name or not name.endswith('.csv') or name in names:
            raise ValueError('fixture table must have a unique safe CSV basename')
        names.append(name)
    if 'trajectory' in expected:
        validate_trajectory(expected['trajectory'], names)
    if (fixture['kind'] == 'no_data') != (not names):
        raise ValueError('fixture kind mismatch')
    if task['identity'] != {'role': 'analyst', 'tenant': 'synthetic_tenant'}:
        raise ValueError('unsupported runner identity')
    if task['permissions'] != {'read_files': names, 'mysql': False, 'write_data': False}:
        raise ValueError('unsupported runner permissions')
    if type(task['critical_safety']) is not bool:
        raise ValueError('invalid critical safety flag')
    v = task['constraints'].get('max_tool_calls')
    if type(v) is not int or v < 0:
        raise ValueError('invalid tool budget')
    forbidden = task['constraints'].get('forbidden_actions')
    if not isinstance(forbidden, list) or any(not isinstance(x, str) for x in forbidden):
        raise ValueError('missing forbidden actions')
    for key in ('max_model_calls', 'max_total_tokens', 'max_latency_seconds'):
        v = task['budget'].get(key)
        if type(v) is not int or v <= 0:
            raise ValueError('invalid resource budget')


def validate_suite(suite: dict) -> None:
    if suite.get('schema_version') != 1 or not suite.get('suite_version'):
        raise ValueError('unsupported suite version')
    provenance = suite.get('provenance')
    if not isinstance(provenance, dict) or provenance.get('origin') != 'synthetic_agent_authored' or type(provenance.get('human_reviewed')) is not bool or provenance.get('blind_holdout') is not False or not provenance.get('scope'):
        raise ValueError('missing or unsupported provenance')
    ids, families, domains = set(), {}, {}
    for task in suite.get('tasks', []):
        validate_task(task)
        if task['id'] in ids:
            raise ValueError('duplicate task id')
        ids.add(task['id'])
        for value, seen in ((task['family'], families), (task['domain'], domains)):
            if value in seen and seen[value] != task['split']:
                raise ValueError('family/domain leakage across splits')
            seen[value] = task['split']
    if not ids:
        raise ValueError('empty suite')


def load_suite(path: str | Path | None = None) -> dict:
    suite = json.loads(Path(path or DEFAULT_SUITE).read_text(encoding='utf-8'))
    validate_suite(suite)
    return suite


def suite_digest(suite: dict) -> str:
    return hashlib.sha256(json.dumps(suite, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def materialize_fixture(task: dict, root: str | Path) -> list[Path]:
    """Write only synthetic CSVs to an explicitly supplied isolated directory."""
    validate_task(task)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    paths = []
    for table in task['fixture'].get('tables', []):
        path = root / table['name']
        # Exclusive creation prevents overwriting a user's upload or another test run.
        with path.open('x', newline='', encoding='utf-8') as handle:
            writer = csv.writer(handle)
            writer.writerow(table['columns'])
            writer.writerows(table['rows'])
        paths.append(path)
    return paths
