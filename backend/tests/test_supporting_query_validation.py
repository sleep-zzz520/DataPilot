"""Primary results and subsequent validation totals have distinct evidence scopes."""
import json

import pytest

from app.agent.analysis_plan import AnalysisPlan, PlanRuntime


def payload(columns, rows):
    return '<!--TABLE:' + json.dumps({'columns': columns, 'rows': rows}) + '-->'


def runtime(*, steps=True, **kwargs):
    return PlanRuntime(AnalysisPlan(
        goal='按品类统计销售额', metrics=['SUM(amount)'], dimensions=['category'],
        grain='category', sort='销售额降序', result_columns=['category', 'total'],
        steps=[{'id': 'q', 'title': '查询Top5', 'kind': 'query'}] if steps else [],
        **kwargs), '按品类统计销售额前5')


def query(rt, sql, columns, rows, **args):
    args = {'sql': sql, **args}
    rt.before_tool('query_mysql', args)
    return rt.after_tool('query_mysql', args, payload(columns, rows))


def primary(rt):
    return query(rt, 'SELECT category,SUM(amount) total FROM items GROUP BY category ORDER BY total DESC LIMIT 5',
                 ['category', 'total'], [['A', 50], ['B', 40], ['C', 30], ['D', 20], ['E', 10]])


def test_correct_top5_and_supporting_scalar_pass_without_replacing_delivery():
    rt = runtime()
    assert primary(rt) == []
    assert query(rt, 'SELECT COUNT(DISTINCT category),SUM(amount) FROM items', ['count', 'total'], [[8, 180]]) == []
    assert rt.plan.evidence[-1]['role'] == 'supporting'
    assert rt.validate_evidence()['status'] == 'approved'
    table, error = rt.result_delivery()
    assert error is None
    assert len(table['rows']) == 5
    # Even a later validation query with the same columns cannot replace Top5.
    assert query(rt, 'SELECT category,SUM(amount) total FROM items GROUP BY category',
                 ['category', 'total'], [['A', 50], ['B', 40]]) == []
    rt.validate_evidence()
    assert len(rt.result_delivery()[0]['rows']) == 5


@pytest.mark.parametrize('steps', [True, False])
def test_primary_and_legacy_queries_still_require_grain_and_sort(steps):
    rt = runtime(steps=steps)
    issues = query(rt, 'SELECT SUM(amount) FROM items', ['total'], [[180]])
    assert '查询未体现计划要求的聚合粒度' in issues
    assert '查询未体现计划要求的排序' in issues
    assert rt.validate_evidence()['status'] == 'rejected'
    # A second unbound query must not cure the invalid primary result.
    query(rt, 'SELECT COUNT(*) FROM items', ['count'], [[8]])
    assert rt.plan.evidence[-1]['role'] == 'result'
    assert rt.validate_evidence()['status'] == 'rejected'


def test_supporting_query_keeps_filter_guards():
    rt = runtime(filters=['amount > 0'])
    assert query(rt, 'SELECT category,SUM(amount) total FROM items WHERE amount>0 GROUP BY category ORDER BY total DESC',
                 ['category', 'total'], [['A', 50], ['B', 40]]) == []
    issues = query(rt, 'SELECT SUM(amount) FROM items', ['total'], [[90]])
    assert issues == ['查询未体现计划中的过滤条件']
    assert rt.validate_evidence()['status'] == 'rejected'


def test_failed_supporting_query_still_rejects_validation():
    rt = runtime()
    primary(rt)
    args = {'sql': 'SELECT SUM(amount) FROM items'}
    rt.before_tool('query_mysql', args)
    rt.after_tool('query_mysql', args, 'SQL 执行错误：connection lost')
    assert rt.plan.evidence[-1]['active'] is False
    assert any(issue['code'] == 'tool_error' for issue in rt.plan.active_issues)
    assert rt.validate_evidence()['status'] == 'rejected'


def test_succeeded_status_without_runtime_evidence_cannot_exempt_queries():
    rt = runtime()
    rt.plan.steps[0].status = 'succeeded'
    issues = query(rt, 'SELECT SUM(amount) FROM items', ['total'], [[180]])
    assert '查询未体现计划要求的聚合粒度' in issues
    assert rt.plan.evidence[-1]['role'] == 'result'
    assert rt.validate_evidence()['status'] == 'rejected'


def test_explicit_primary_reexecution_remains_strict():
    rt = runtime()
    primary(rt)
    issues = query(rt, 'SELECT SUM(amount) FROM items', ['total'], [[180]], plan_step_id='q')
    assert '查询未体现计划要求的聚合粒度' in issues
    assert '查询未体现计划要求的排序' in issues
    assert rt.validate_evidence()['status'] == 'rejected'
