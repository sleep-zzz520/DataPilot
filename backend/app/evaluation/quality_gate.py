"""Fail-closed release decisions against an explicitly human-approved baseline."""
from __future__ import annotations

import math

from .quality_scoring import score_trial


def check_release(report: dict, tasks: list[dict], baseline: dict | None, *, policy: str = 'baseline') -> dict:
    reasons = []
    def reject(reason):
        reasons.append(reason)
    if not isinstance(report, dict):
        return dict(passed=False, reasons=['invalid_report'], pass_rate=0.0)
    if not isinstance(tasks, list) or any(not isinstance(t, dict) for t in tasks):
        return dict(passed=False, reasons=['invalid_tasks'], pass_rate=0.0)
    ids = [t.get('id') for t in tasks]
    if not ids or any(not isinstance(i, str) or not i for i in ids) or len(set(i for i in ids if isinstance(i, str))) != len(ids):
        reject('invalid_task_ids')
    count = report.get('trials_per_task')
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        reject('invalid_trial_count')
        count = 0
    trials = report.get('trials')
    if not isinstance(trials, list) or not trials:
        reject('missing_trials')
        trials = []
    expected = {(i, n) for i in ids if isinstance(i, str) for n in range(count)}
    seen, passed_count = set(), 0
    task_map = {t["id"]: t for t in tasks if isinstance(t.get("id"), str)}
    task_passed = {task_id: 0 for task_id in task_map}
    critical = {t.get('id') for t in tasks if t.get('critical_safety') is True and isinstance(t.get('id'), str)}
    for trial in trials:
        if not isinstance(trial, dict):
            reject('invalid_trial')
            continue
        pair = (trial.get('task_id'), trial.get('trial_index'))
        if (not isinstance(pair[0], str) or not isinstance(pair[1], int)
                or isinstance(pair[1], bool) or pair not in expected or pair in seen):
            reject('unexpected_or_duplicate_trial')
        if not isinstance(pair[0], str) or not isinstance(pair[1], int):
            continue
        seen.add(pair)
        score = trial.get('score')
        valid = (isinstance(score, dict) and score.get('task_id') == pair[0]
                 and isinstance(score.get('checks'), list) and bool(score['checks'])
                 and all(isinstance(c, dict) and c.get('status') in ('pass', 'fail', 'unknown')
                         and type(c.get('passed')) is bool
                         and c['passed'] == (c['status'] == 'pass') for c in score['checks'])
                 and all(type(score.get(k)) is bool for k in
                         ('passed', 'behavior_passed', 'business_completed', 'process_passed', 'safety_passed'))
                 and {'behavior', 'forbidden_actions', 'tool_budget', 'execution'} <= {c.get('name') for c in score['checks'] if isinstance(c.get('name'), str)}
                 and score.get('passed') == all(c['passed'] for c in score['checks'])
                 and all(c.get('category') in ('behavior', 'business', 'safety', 'process', 'semantic') for c in score['checks'])
                 and score.get('safety_passed') == all(c['passed'] for c in score['checks'] if c.get('category') == 'safety')
                 and score.get('behavior_passed') == all(c['passed'] for c in score['checks'] if c.get('category') == 'behavior')
                 and score.get('process_passed') == all(c['passed'] for c in score['checks'] if c.get('category') in ('process', 'safety')))
        observation = trial.get('observation')
        if valid:
            try:
                valid = (isinstance(observation, dict) and pair[0] in task_map
                         and score == score_trial(task_map[pair[0]], observation)
                         and trial.get('error') == observation.get('error'))
            except (TypeError, ValueError, KeyError, AttributeError):
                valid = False
        if not valid:
            reject('missing_or_invalid_score')
        if valid and score['status'] == 'unknown':
            reject('unknown_score')
        if 'error' not in trial or trial['error'] is not None:
            reject('trial_error')
        if valid and score['passed'] and trial.get('error') is None:
            passed_count += 1
            if pair[0] in task_passed:
                task_passed[pair[0]] += 1
        if pair[0] in critical and (not valid or not score['safety_passed'] or not score['passed']):
            reject('critical_safety_failed')
    if seen != expected:
        reject('incomplete_trials')
    rate = passed_count / len(expected) if expected else 0.0
    if policy == 'all_required':
        if rate != 1.0:
            reject('all_required_failed')
        return dict(passed=not reasons, reasons=sorted(set(reasons)), pass_rate=rate)
    if policy != 'baseline':
        reject('unknown_policy')
    valid_baseline = isinstance(baseline, dict) and all(
        isinstance(baseline.get(k), str) and baseline[k].strip() for k in ('approved_by', 'approved_at', 'suite_id', 'suite_sha256', 'scorer_version', 'candidate_config_sha256'))
    if valid_baseline:
        valid_baseline = all(isinstance(baseline.get(k), (int, float)) and not isinstance(baseline[k], bool)
                             and math.isfinite(baseline[k]) and 0 <= baseline[k] <= 1
                             for k in ('pass_rate', 'min_pass_rate', 'max_regression'))
    if valid_baseline:
        task_rates = baseline.get('task_pass_rates')
        valid_baseline = (isinstance(task_rates, dict) and set(task_rates) == set(task_map)
                          and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                  and math.isfinite(v) and 0 <= v <= 1 for v in task_rates.values()))
    if not valid_baseline:
        reject('human_approved_baseline_required')
    elif any(baseline[k] != report.get(k) for k in ('suite_id', 'suite_sha256', 'scorer_version', 'candidate_config_sha256')):
        reject('baseline_suite_mismatch')
    else:
        if any((task_passed[k] / count if count else 0) < v for k, v in baseline['task_pass_rates'].items()):
            reject('per_task_regression')
        if rate < baseline['min_pass_rate']:
            reject('below_minimum_pass_rate')
        if rate < baseline['pass_rate'] - baseline['max_regression']:
            reject('baseline_regression')
    return dict(passed=not reasons, reasons=sorted(set(reasons)), pass_rate=rate)


evaluate_release = check_release
