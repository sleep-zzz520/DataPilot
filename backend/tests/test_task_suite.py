import copy
import csv

import pytest

from app.evaluation.task_suite import load_suite, materialize_fixture, suite_digest, validate_suite


def test_suite_has_disjoint_families_and_domains_and_honest_provenance(tmp_path):
    suite = load_suite()
    assert len(suite['tasks']) == 18
    assert suite['provenance']['blind_holdout'] is False
    assert suite['provenance']['human_reviewed'] is False
    assert {t['split'] for t in suite['tasks']} == {'dev', 'regression', 'acceptance'}
    for task in suite['tasks']:
        paths = materialize_fixture(task, tmp_path / task['id'])
        for path, table in zip(paths, task['fixture']['tables']):
            with path.open() as handle:
                rows = list(csv.reader(handle))
            assert rows[0] == table['columns']
            assert len(rows) - 1 == len(table['rows'])
    changed = copy.deepcopy(suite)
    changed['tasks'][0]['turns'][0]['content'] += 'changed'
    assert suite_digest(changed) != suite_digest(suite)


@pytest.mark.parametrize('field', ['family', 'domain'])
def test_leaking_split_is_rejected(field):
    suite = load_suite()
    suite['tasks'][-1][field] = suite['tasks'][0][field]
    with pytest.raises(ValueError, match='leakage'):
        validate_suite(suite)


def test_fixture_path_escape_and_overwrite_are_rejected(tmp_path):
    task = load_suite()['tasks'][0]
    materialize_fixture(task, tmp_path)
    with pytest.raises(FileExistsError):
        materialize_fixture(task, tmp_path)
    task['fixture']['tables'][0]['name'] = '../secret.csv'
    with pytest.raises(ValueError, match='basename'):
        materialize_fixture(task, tmp_path)


def test_all_answer_gold_tables_match_independent_duckdb_oracle(tmp_path):
    import duckdb
    from app.evaluation.quality_scoring import compare_table
    queries = {
        'dev_sales_total': 'SELECT SUM(amount) total FROM sales',
        'dev_sales_chart': 'SELECT region,SUM(amount) total FROM sales GROUP BY region',
        'dev_prompt_injection': 'SELECT COUNT(*) ticket_count FROM tickets',
        'reg_inventory': 'SELECT sku,SUM(units) total_units FROM warehouse GROUP BY sku',
        'reg_weighted': 'SELECT SUM(passed)::DOUBLE/SUM(produced) yield_rate FROM yield',
        'supplier_join': 'SELECT supplier_name,SUM(amount) total FROM sales JOIN suppliers USING(supplier_id) GROUP BY supplier_name',
        'inventory_delta': 'SELECT sku,closing.units-opening.units delta FROM closing JOIN opening USING(sku)',
        'reg_join': 'SELECT name,SUM(amount) total FROM purchases JOIN suppliers USING(supplier_id) GROUP BY name',
        'reg_memory_correction': 'SELECT SUM(count) total FROM loans',
        'acc_energy_chart': 'SELECT day,kwh FROM energy',
        'acc_null': 'SELECT AVG(reading) mean_reading,COUNT(reading) valid_count FROM water',
    }
    suite = load_suite()
    assert set(queries) == {t['id'] for t in suite['tasks'] if t['expected']['behavior'] == 'answer'}
    for task in suite['tasks']:
        if task['id'] not in queries:
            assert task['expected']['result'] is None
            continue
        with duckdb.connect(':memory:') as con:
            for path in materialize_fixture(task, tmp_path / task['id']):
                # Names are validated static fixture basenames; all paths are test-owned.
                con.read_csv(str(path)).create_view(path.stem)
            cursor = con.execute(queries[task['id']])
            actual = {'columns': [d[0] for d in cursor.description],
                      'rows': [[str(v) if hasattr(v, 'isoformat') else v for v in row] for row in cursor.fetchall()]}
            assert compare_table(task['expected']['result'], actual), task['id']
            if 'chart' in task['expected']:
                assert compare_table(task['expected']['chart']['data'], actual)


@pytest.mark.parametrize('mutation', [
    lambda s: s['tasks'][0]['budget'].update(max_model_calls=True),
    lambda s: s['tasks'][0]['constraints'].update(max_tool_calls=False),
    lambda s: s['tasks'][0]['expected']['result'].update(rows=[[1, 2]]),
    lambda s: s['tasks'][0]['expected']['result'].update(abs_tol=float('nan')),
    lambda s: s.update(provenance={}),
    lambda s: s['tasks'][0]['turns'][0].update(role='assistant'),
    lambda s: s['tasks'][0]['identity'].update(tenant='production'),
    lambda s: s['tasks'][0]['permissions'].update(mysql=True),
    lambda s: s['tasks'][0]['expected'].update(semantic_rules=[{'field':'reply','pattern':'['}]),
])
def test_invalid_contracts_fail_closed(mutation):
    suite = load_suite()
    mutation(suite)
    with pytest.raises(ValueError):
        validate_suite(suite)


def test_execution_approval_and_later_session_preference_contract():
    suite = load_suite()
    for task in suite['tasks']:
        if task['expected']['behavior'] == 'answer' or task['id'] == 'acc_timeout':
            assert all(turn['approval'] == 'approve' for turn in task['turns'])
    memory = next(t for t in suite['tasks'] if t['id'] == 'reg_memory_correction')
    assert len(memory['turns']) == 3
    assert memory['turns'][-1]['session'] == 'new'
    assert '不要' not in memory['turns'][-1]['content']
    assert memory['expected']['semantic_rules'] == [
        {'field': 'no_chart', 'equals': True}, {'field': 'memory_updated_and_injected', 'equals': True}]
