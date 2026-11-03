import copy
import json

import pytest

from app.evaluation.feedback_dataset import admit_candidate, annotate_candidate, export_candidate
from app.evaluation.task_suite import load_suite


def candidate(tmp_path):
    return export_candidate({'user_id': 'sensitive-id', 'prompt': 'private salary',
        'trace': [{'tool': 'evil-private-tool', 'status': 'ok', 'duration_ms': 3,
                   'input': 'secret-password', 'output': 'private rows', 'agent': 'private-name'}],
        'usage': {'input_tokens': 10, 'output_tokens': 3, 'model': 'private-server',
                  'cost_usd': float('nan'), 'calls': 'secret'}}, 'incorrect_result', tmp_path / 'pending.json')


def test_allowlist_omits_unknown_and_free_text(tmp_path):
    item = candidate(tmp_path)
    text = json.dumps(item)
    for secret in ('sensitive-id', 'private', 'secret', 'evil-'):
        assert secret not in text
    assert item['trace'][0]['tool'] == 'other'
    assert item['usage']['cost_usd'] is None
    assert item['usage']['calls'] is None
    assert (tmp_path / 'pending.json').stat().st_mode & 0o777 == 0o600


def test_unreviewed_and_incomplete_reviews_cannot_admit(tmp_path):
    item = candidate(tmp_path)
    with pytest.raises(ValueError, match='unreviewed'):
        admit_candidate(item, tmp_path / 'regression')
    task = next(t for t in load_suite()['tasks'] if t['split'] == 'regression')
    with pytest.raises(ValueError, match='review'):
        annotate_candidate(item, task=task, reviewer='r1', privacy_checked=False, oracle_checked=True)
    reviewed = annotate_candidate(item, task=task, reviewer='r1', privacy_checked=True, oracle_checked=True)
    path = admit_candidate(reviewed, tmp_path / 'regression')
    assert json.loads(path.read_text())['state'] == 'admitted'
    with pytest.raises(FileExistsError):
        admit_candidate(reviewed, tmp_path / 'regression')
    tampered = copy.deepcopy(reviewed)
    tampered['task']['turns'][0]['content'] = 'changed after review'
    with pytest.raises(ValueError, match='match'):
        admit_candidate(tampered, tmp_path / 'other')


def test_export_from_snapshot_checks_owner_and_never_exports_payload(tmp_path):
    import sqlite3
    from contextlib import closing
    from app.evaluation.feedback_dataset import export_session_candidate
    snapshot = tmp_path / 'snapshot.db'
    with closing(sqlite3.connect(snapshot)) as conn:
        conn.executescript('''CREATE TABLE sessions(id TEXT, user_id INTEGER);
            CREATE TABLE traces(session_id TEXT, turn_seq INTEGER, data TEXT);
            CREATE TABLE usage_logs(session_id TEXT, user_id INTEGER, input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL);
            INSERT INTO sessions VALUES ('private-session', 17);
            INSERT INTO usage_logs VALUES ('private-session', 17, 23, 4, 0.01);''')
        conn.execute('INSERT INTO traces VALUES (?, ?, ?)', ('private-session', 0,
            json.dumps([{'tool': 'query_file', 'status': 'ok', 'input': 'private-sql', 'output': 'private-data'}])))
        conn.commit()
    out = tmp_path / 'pending.json'
    with pytest.raises(ValueError, match='ownership'):
        export_session_candidate(snapshot, 'private-session', 1, 'incorrect_result', out)
    assert not out.exists()
    result = export_session_candidate(snapshot, 'private-session', 17, 'incorrect_result', out)
    assert 'private-' not in out.read_text()
    assert result['usage']['input_tokens'] == 23
    assert result['trace'][0]['tool'] == 'query_file'
