"""Experiment validity and preservation, without network calls."""
import json
import subprocess
from pathlib import Path
import pytest
from app.evaluation import quality_runner as runner


def test_secret_metadata_allowlist():
    safe = runner.safe_model_config({'api_key': 'secret', 'password': 'secret',
        'base_url': 'https://secret-endpoint/key', 'model_name': 'example'})
    assert 'secret' not in json.dumps(safe)
    assert 'base_url' not in safe


def test_provenance_and_repetition_are_separate():
    tasks = [{'id': 'a', 'split': 'dev', 'expected': {'behavior': 'answer'}}]
    def row(index, passed):
        return {'task_id': 'a', 'trial_index': index, 'score': {'passed': passed, 'business_completed': passed},
                'observation': {'latency_s': 1+index, 'input_tokens': 10, 'output_tokens': 5, 'usage_complete': True}}
    result = runner.summarize([row(0, False), row(1, True)], tasks)
    assert result['unique_tasks'] == 1 and result['runs'] == 2
    assert result['first_trial']['passed'] == 0
    assert result['all_repeats_pass_tasks'] == 0
    assert result['by_outcome']['failure']['runs'] == 1


def test_missing_worker_artifact_fails_and_temp_directory_is_removed(monkeypatch):
    directories = []
    class Process:
        def __init__(self, *args, **kwargs):
            assert kwargs['start_new_session']
            assert kwargs['env']['LANGSMITH_TRACING'] == 'false'
        def communicate(self, payload, timeout):
            parsed = json.loads(payload)
            directories.append(Path(parsed['root']))
    monkeypatch.setattr(subprocess, 'Popen', Process)
    obs = runner.run_trial({'budget': {'max_model_calls': 2, 'max_latency_seconds': 5}}, {'api_key': 'private'})
    assert obs['error'] == 'WORKER_EXIT_WITHOUT_RESULT'
    assert not directories[0].exists()


def test_cli_rejects_duplicate_or_outside_split_tasks_before_reading_credentials():
    with pytest.raises(SystemExit):
        runner.main(['run', '--tasks', 'dev_sales_total,dev_sales_total', '--out', '/tmp/unused-quality-output.json'])


def test_gate_does_not_let_the_report_choose_its_own_tasks_or_repeat(tmp_path):
    suite = runner.load_suite()
    path = tmp_path / 'omitted.json'
    path.write_text(json.dumps({'suite_sha256': runner.suite_digest(suite), 'dataset_snapshot': suite,
                                'selected_task_ids': ['dev_sales_total'], 'trials_per_task': 1,
                                'expected_runs': 1, 'scorer_version': runner.SCORER_VERSION,
                                'evaluation_kind': 'product_chain_synthetic', 'settings': {
                                    'mode': 'auto', 'workers': 1, 'max_calls': 12, 'request_timeout': 30,
                                    'trial_timeout': 180, 'provider_max_retries': 0}}))
    assert runner.main(['gate', '--split', 'all', '--repeat', '2', '--out', str(path)]) == 1


def test_timeout_preserves_tool_prefix_without_claiming_completion(monkeypatch):
    calls = [{"tool": "query_file", "arguments": {"file": "one.csv", "sql": "SELECT 1"},
              "status": "ok", "table_observed": True}]

    class Process:
        pid = 42
        def communicate(self, payload=None, timeout=None):
            if payload:
                root = Path(json.loads(payload)["root"])
                (root / "tools-progress.json").write_text(json.dumps(calls))
                raise subprocess.TimeoutExpired("worker", timeout)

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(runner.os, "killpg", lambda *a: None)
    obs = runner.run_trial({'budget': {'max_model_calls': 2, 'max_latency_seconds': 5}}, {'api_key': 'private'})
    assert obs["error"] == "TRIAL_TIMEOUT"
    assert obs["calls"] == calls and obs["calls_complete"] is False
