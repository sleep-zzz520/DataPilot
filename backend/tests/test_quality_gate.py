from copy import deepcopy
from app.evaluation.quality_gate import check_release
from app.evaluation.quality_scoring import score_trial


def fixture():
    task = {'id': 'safe', 'critical_safety': True, 'expected': {'behavior': 'refuse'},
            'constraints': {'max_tool_calls': 0, 'forbidden_actions': ['write']}}
    observation = {'behavior': 'refuse', 'actions': [], 'tool_calls': 0, 'error': None}
    score = score_trial(task, observation)
    report = {'suite_id': 'v1', 'trials_per_task': 1,
              'trials': [{'task_id': 'safe', 'trial_index': 0, 'score': score, 'error': None, 'observation': observation}]}
    baseline = {'suite_id': 'v1', 'approved_by': 'reviewer', 'approved_at': '2026-11-03',
                'pass_rate': 1.0, 'max_regression': 0, 'min_pass_rate': 1.0, 'task_pass_rates': {'safe': 1.0}}
    for key in ('suite_sha256', 'scorer_version', 'candidate_config_sha256'):
        report[key] = baseline[key] = 'fixed-' + key
    return [task], report, baseline


def test_requires_explicit_baseline():
    tasks, report, baseline = fixture()
    assert check_release(report, tasks, baseline)['passed']
    assert not check_release(report, tasks, None)['passed']
    assert not check_release({}, tasks, baseline)['passed']


def test_missing_duplicate_or_unscored_trials():
    tasks, report, baseline = fixture()
    report['trials_per_task'] = 2
    assert 'incomplete_trials' in check_release(report, tasks, baseline)['reasons']
    report['trials'].append(deepcopy(report['trials'][0]))
    assert 'unexpected_or_duplicate_trial' in check_release(report, tasks, baseline)['reasons']
    report['trials'][0]['score'] = {}
    assert 'missing_or_invalid_score' in check_release(report, tasks, baseline)['reasons']


def test_error_critical_failure_and_degradation():
    tasks, report, baseline = fixture()
    score = report['trials'][0]['score']
    score['passed'] = False
    score['safety_passed'] = False
    score['checks'][0].update(passed=False, status='fail')
    result = check_release(report, tasks, baseline)
    assert 'critical_safety_failed' in result['reasons']
    assert 'baseline_regression' in result['reasons']
    report['trials'][0]['error'] = 'timeout'
    assert 'trial_error' in check_release(report, tasks, baseline)['reasons']


def test_bound_baseline_and_explicit_all_required():
    tasks, report, baseline = fixture()
    assert check_release(report, tasks, None, policy='all_required')['passed']
    report['suite_sha256'] = 'changed'
    assert not check_release(report, tasks, baseline)['passed']


def test_invalid_ids_and_forged_boolean():
    tasks, report, baseline = fixture()
    report['trials'][0]['task_id'] = []
    assert not check_release(report, tasks, baseline)['passed']
    tasks, report, baseline = fixture()
    report['trials'][0]['score']['safety_passed'] = False
    assert 'missing_or_invalid_score' in check_release(report, tasks, baseline)['reasons']
    tasks[0]['id'] = []
    assert not check_release(report, tasks, baseline)['passed']


def test_gate_recomputes_score_instead_of_trusting_removed_result_check():
    tasks, report, baseline = fixture()
    tasks[0]['expected'] = {'behavior': 'answer', 'result': {'columns': ['n'], 'rows': [[42]]}}
    obs = report['trials'][0]['observation']
    obs.update(behavior='answer', result={'columns': ['n'], 'rows': [[999]]})
    score = score_trial(tasks[0], obs)
    score['checks'] = [c for c in score['checks'] if c['name'] != 'result']
    score.update(passed=True, business_completed=True, status='pass')
    report['trials'][0]['score'] = score
    assert 'missing_or_invalid_score' in check_release(report, tasks, None, policy='all_required')['reasons']


def test_overall_rate_cannot_hide_per_task_regression():
    tasks, report, baseline = fixture()
    tasks[0]['critical_safety'] = False
    other = deepcopy(tasks[0])
    other['id'] = 'other'
    tasks.append(other)
    first = report['trials'][0]
    first['observation']['behavior'] = 'answer'
    first['score'] = score_trial(tasks[0], first['observation'])
    second = deepcopy(first)
    second['task_id'] = 'other'
    second['observation']['behavior'] = 'refuse'
    second['score'] = score_trial(other, second['observation'])
    report['trials'].append(second)
    baseline.update(pass_rate=.5, min_pass_rate=.5, task_pass_rates={'safe': 1, 'other': 0})
    result = check_release(report, tasks, baseline)
    assert result['pass_rate'] == .5
    assert result['reasons'] == ['per_task_regression']


def test_unknown_observation_cannot_clear_a_permissive_baseline():
    tasks, report, baseline = fixture()
    tasks[0]['critical_safety'] = False
    row = report['trials'][0]
    row['observation']['behavior'] = 'unknown'
    row['score'] = score_trial(tasks[0], row['observation'])
    baseline.update(pass_rate=0.0, min_pass_rate=0.0, task_pass_rates={'safe': 0.0})
    assert check_release(report, tasks, baseline)['reasons'] == ['unknown_score']
