"""Product-boundary tests: the planner and chat graph really run with local models."""
import json
from threading import Lock

import pytest
from langchain_core.messages import AIMessage
from app.evaluation import product_trial as trial
from app.usage.callback import RequestUsage
from test_chat_execution_integration import ScriptedLLM, call


def test_budget_shared_across_bind_and_copy_including_failures(tmp_path):
    class Model:
        max_tokens = 10
        def model_copy(self, update):
            return self
        def bind_tools(self, tools, **kwargs):
            return self
        def invoke(self, *args, **kwargs):
            raise ValueError('provider failed')
        def stream(self, *args, **kwargs):
            yield 'chunk'
    state = dict(attempted_calls=0, max_calls=2, usage=RequestUsage(),
                 progress=tmp_path / 'progress.json', lock=Lock())
    model = trial.BudgetedModel(Model(), state)
    copied = model.bind_tools([]).model_copy(update={'max_tokens': 3})
    with pytest.raises(ValueError):
        copied.invoke('first')
    assert list(model.stream('second')) == ['chunk']
    with pytest.raises(trial.CallBudgetExceeded):
        copied.invoke('third')
    assert state['attempted_calls'] == 2
    assert json.loads(state['progress'].read_text())['attempted_calls'] == 2


def test_chart_data_is_actual_series_data():
    assert trial.chart_observation({'xAxis': [{'data': ['east', 'west']}], 'series': [
        {'type': 'bar', 'name': 'sales', 'data': [3, {'value': 7}]}]}) == {
        'type': 'bar', 'data': {'columns': ['dimension', 'sales'], 'rows': [['east', 3], ['west', 7]]}}
    assert trial.chart_observation({'series': [{'type': 'pie', 'data': [
        {'name': 'a', 'value': 2}]}]})['data']['rows'] == [['a', 2]]
    assert trial.chart_observation(None) is None
    assert trial.chart_observation({'series': []}) is None


def test_unknown_behavior_stays_unknown():
    assert trial.infer_behavior({'reply': ''}) == 'unknown'
    assert trial.infer_behavior({'reply': '正在处理中'}) == 'unknown'
    assert trial.infer_behavior({'reply': '已完成', 'confirmation': {'status': 'approved', 'mode': 'approval'}}) == 'unknown'
    assert trial.infer_behavior({'reply': 'done', 'error': {'code': 'failed'}}) == 'unknown'
    assert trial.infer_behavior({'confirmation': {'required': True, 'mode': 'clarification'}}) == 'clarify'
    assert trial.infer_behavior({'confirmation': {'required': True, 'mode': 'approval'}}) == 'approval'
    assert trial.infer_behavior({'confirmation': {'status': 'rejected'}}) == 'refuse'


@pytest.mark.parametrize('numeric_answer', [230, 999])
def test_execute_runs_real_planner_tools_and_display_gate(tmp_path, monkeypatch, numeric_answer):
    from app.core import factories
    from app.evaluation.task_suite import load_suite
    task = load_suite()['tasks'][0]
    task['turns'][0]['approval'] = 'approve'
    # Shared external cursor survives the product's callback-specific model copies.
    seen = []
    responses = [
        AIMessage(content=json.dumps({'goal': '汇总销售金额', 'data_sources': ['sales.csv'],
            'metrics': ['amount'], 'output_format': 'table', 'steps': [
                {'id': 'q', 'title': '查询总额', 'kind': 'query'},
                {'id': 'v', 'title': '验证', 'kind': 'validate', 'depends_on': ['q']},
                {'id': 's', 'title': '回答', 'kind': 'summarize', 'depends_on': ['v']}]})),
        call('query_file', {'file': 'sales.csv', 'sql': ('SELECT SUM(amount) AS total FROM df'
            if numeric_answer == 230 else 'SELECT SUM(amount) * 2 AS total FROM df')}),
        AIMessage(content=f'总额为{numeric_answer}。'),
    ]
    class SharedScript(ScriptedLLM):
        def _next(self):
            value = responses[min(len(seen), len(responses) - 1)]
            seen.append(value)
            return value
    monkeypatch.setattr(factories, 'build_llm', lambda _, **kwargs: SharedScript(responses=responses))
    observation = trial.execute({'task': task, 'config': {'provider': 'fake', 'model_name': 'local'},
        'max_calls': 10, 'request_timeout': 1, 'mode': 'single'}, tmp_path)
    assert len(seen) >= 2, observation
    assert observation['attempted_model_calls'] >= 2
    assert observation['calls'][0]['tool'] == 'query_file'
    assert observation['calls'][0]['arguments']['sql'].startswith('SELECT SUM')
    assert observation['error'] is None, observation
    assert observation['result']['rows'] == [[230.0 if numeric_answer == 230 else 460.0]], observation
    observation['latency_s'] = 0.1
    if numeric_answer == 230:
        assert '230' in observation['reply']
        from app.evaluation.quality_scoring import score_trial
        assert score_trial(task, observation)['passed'], score_trial(task, observation)
    else:
        # A bad prose number is not silently relabeled as a correct business answer.
        from app.evaluation.quality_scoring import score_trial
        assert not score_trial(task, observation)['passed']


@pytest.mark.parametrize('chart', [
    {'series': {}}, {'series': [None]},
    {'series': [{'type': 'bar'}, {'type': 'line'}]},
    {'series': [{'type': 'pie', 'data': [42]}]},
    {'series': [{'type': 'bar'}], 'xAxis': 42},
])
def test_unsupported_chart_is_unknown(chart):
    assert trial.chart_observation(chart) is None


def test_real_upload_owner_check_survives_a_warm_file_cache(isolated_storage):
    from app import persistence
    from app.api import chat_api
    path = isolated_storage / 'owned.csv'
    path.write_text('amount\n123\n')
    file_id = 'eval-owner-only'
    persistence.save_upload(file_id, path.name, str(path), ['amount'], [{'amount': 123}], user_id=4)
    try:
        assert chat_api._load_uploaded_dfs([file_id], 4)[path.name].iloc[0]['amount'] == 123
        assert chat_api._load_uploaded_dfs([file_id], 7) == {}
        assert chat_api._uploaded_file_paths([file_id], 7) == {}
    finally:
        chat_api._file_df_cache.pop(file_id, None)
