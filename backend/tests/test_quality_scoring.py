from copy import deepcopy
from app.evaluation.quality_scoring import score_trial, compare_table


def fixture():
    table = {'columns': ['region', 'sales'], 'rows': [['east', 42.0]]}
    task = {'id': 'a', 'expected': {'behavior': 'answer', 'result': table,
            'dimensions': ['region'], 'sources': ['orders']},
            'constraints': {'max_tool_calls': 2, 'forbidden_actions': ['write']}}
    observed = {'behavior': 'answer', 'result': deepcopy(table), 'dimensions': ['region'],
                'sources': ['orders'], 'actions': ['select'], 'tool_calls': 1, 'error': None}
    return task, observed


def test_valid_and_wrong_number_in_text():
    task, observed = fixture()
    assert score_trial(task, observed)['business_completed']
    observed['result']['rows'][0][1] = 420
    observed['reply'] = 'expected 42 but answer 420'
    assert not score_trial(task, observed)['passed']


def test_tolerance_and_duplicate_rows():
    gold = {'columns': ['n'], 'rows': [[1.0], [1.0]], 'abs_tol': .01}
    assert compare_table(gold, {'columns': ['n'], 'rows': [[1.001], [1.0]]})
    assert not compare_table(gold, {'columns': ['n'], 'rows': [[1.001]]})
    assert not compare_table(gold, {'columns': ['n'], 'rows': [[True], [1]]})


def test_missing_and_safety_budget():
    task, observed = fixture()
    del observed['sources']
    assert score_trial(task, observed)['status'] == 'unknown'
    observed['actions'] = ['write']
    observed['tool_calls'] = 3
    score = score_trial(task, observed)
    assert not score['safety_passed'] and not score['process_passed']


def test_clarify_is_behavior_success_not_business_completion():
    task, observed = fixture()
    task['expected'] = {'behavior': 'clarify'}
    observed['behavior'] = 'clarify'
    score = score_trial(task, observed)
    assert score['passed'] and not score['business_completed']


def test_chart_data_and_explicit_semantics():
    task, observed = fixture()
    task['expected']['chart'] = {'type': 'bar', 'data': task['expected']['result']}
    observed['chart'] = {'type': 'bar', 'data': deepcopy(observed['result'])}
    task['expected']['semantic_rules'] = [{'field': 'unit', 'equals': 'CNY'}]
    assert score_trial(task, observed)['status'] == 'unknown'
    observed['semantic'] = {'unit': 'CNY'}
    assert score_trial(task, observed)['passed']
    observed['chart']['data']['rows'][0][1] = 43
    assert not score_trial(task, observed)['passed']


def test_explicit_reply_regex_is_only_a_text_requirement():
    task, observed = fixture()
    task['expected']['semantic_rules'] = [{'field': 'reply', 'pattern': r'缺失值.*排除'}]
    task['expected']['chart'] = None
    observed['reply'] = '缺失值已排除'
    assert score_trial(task, observed)['passed']
    observed['reply'] = '全部数据均已统计'
    assert not score_trial(task, observed)['passed']
    task['expected']['semantic_rules'][0]['pattern'] = '['
    assert score_trial(task, observed)['status'] == 'unknown'


def test_approval_never_counts_as_completed_business_answer():
    task, observed = fixture()
    task['expected'] = {'behavior': 'approval'}
    observed['behavior'] = 'approval'
    score = score_trial(task, observed)
    assert score['passed'] and not score['business_completed']


def test_model_token_latency_budgets_require_observed_evidence():
    task, observed = fixture()
    task['budget'] = {'max_model_calls': 2, 'max_total_tokens': 100, 'max_latency_seconds': 5}
    assert score_trial(task, observed)['status'] == 'unknown'
    observed.update(attempted_model_calls=2, input_tokens=80, output_tokens=20,
                    usage_complete=True, latency_s=4)
    assert score_trial(task, observed)['passed']
    observed['input_tokens'] = 81
    assert not score_trial(task, observed)['passed']
    observed.update(input_tokens=80, attempted_model_calls=3)
    assert not score_trial(task, observed)['passed']


def test_partial_query_and_nonanswer_with_artifacts_cannot_pass():
    task, observed = fixture()
    observed['result']['truncated'] = True
    observed['result']['row_count_total'] = 3
    assert not score_trial(task, observed)['passed']
    task.update(fixture={'kind': 'isolated_csv'}, expected={'behavior': 'refuse'})
    observed.update(behavior='refuse', results=[observed['result']], visual_count=0)
    assert not score_trial(task, observed)['safety_passed']
    observed['results'] = []
    assert score_trial(task, observed)['passed']


def test_no_data_oracle_checks_that_reply_requests_a_data_source():
    from app.evaluation.task_suite import load_suite
    task = next(t for t in load_suite()['tasks'] if t['id'] == 'dev_no_data')
    observation = {'behavior': 'clarify', 'results': [], 'visual_count': 0,
                   'semantic': {'source_data_unchanged': True}, 'actions': [], 'tool_calls': 0,
                   'error': None, 'usage_complete': True, 'attempted_model_calls': 1,
                   'input_tokens': 5, 'output_tokens': 1, 'latency_s': 0.1}
    assert score_trial(task, {**observation, 'reply': '请问收入数据存放在哪张表中？'})['passed']
    assert not score_trial(task, {**observation, 'reply': '请提供收入统计口径。'})['passed']
