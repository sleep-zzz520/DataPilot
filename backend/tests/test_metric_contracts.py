"""Deterministic metric-contract and AST gate regressions; no LLM or database."""
import json

import pytest
from pydantic import ValidationError

from app.agent.contracts import (
    EXAMPLE_SALES_CONTRACTS, ContractBinding, MetricContract, MetricContractRegistry,
    bind_metric_contracts, validate_sql_contract, validate_sql_contracts,
)


CATALOG = [{"name": "sales", "columns": ["sales_amount", "refund_amount", "cost_amount",
                                           "created_at", "region", "region_id"]}]


@pytest.fixture
def contract():
    return MetricContract(
        id="sales.net_sales.january", version="1", formula="SUM(sales_amount) - SUM(refund_amount)",
        required_columns=("sales_amount", "refund_amount", "created_at", "region"),
        required_filters=("created_at >= '2026-01-01'", "created_at < '2026-02-01'"),
        group_by=("region",), grain="region", unit="CNY", source="sales",
        scope="configured January 2026 net sales grouped by region", metric_names=("净销售额",),
    )


VALID_SQL = """SELECT region, SUM(sales_amount)-SUM(refund_amount) AS net_sales
FROM sales WHERE created_at >= '2026-01-01' AND created_at < '2026-02-01'
GROUP BY region ORDER BY net_sales DESC"""


def codes(sql, contract, catalog=CATALOG):
    return {i.code for i in validate_sql_contract(sql, contract, catalog, step_id="query", evidence_id="ev1")}


def test_correct_query_and_qualified_aliases(contract):
    assert not codes(VALID_SQL, contract)
    assert not codes("""SELECT s.region, SUM(s.sales_amount)-SUM(s.refund_amount) AS amount
    FROM sales AS s WHERE (s.created_at >= '2026-01-01') AND s.created_at < '2026-02-01'
    GROUP BY s.region ORDER BY amount""", contract)


@pytest.mark.parametrize("replacement, expected", [
    ("1=1", "REQUIRED_FILTER_MISSING_OR_CHANGED"),
    ("created_at >= '2025-01-01' AND created_at < '2026-02-01'", "REQUIRED_FILTER_MISSING_OR_CHANGED"),
    ("created_at >= '2026-01-01' OR created_at < '2026-02-01'", "REQUIRED_FILTER_MISSING_OR_CHANGED"),
    ("created_at >= '2026-01-01' AND created_at < '2026-02-01' OR 1=1", "REQUIRED_FILTER_MISSING_OR_CHANGED"),
    ("created_at >= '2026-01-01' AND created_at < '2026-02-01' AND region='East'", "UNAPPROVED_FILTER"),
])
def test_filter_tampering(contract, replacement, expected):
    sql = VALID_SQL.replace("created_at >= '2026-01-01' AND created_at < '2026-02-01'", replacement)
    assert expected in codes(sql, contract)


def test_filter_in_comment_does_not_count(contract):
    sql = "SELECT region,SUM(sales_amount)-SUM(refund_amount) FROM sales /* WHERE created_at >= '2026-01-01' AND created_at < '2026-02-01' */ GROUP BY region"
    assert "REQUIRED_FILTER_MISSING_OR_CHANGED" in codes(sql, contract)


@pytest.mark.parametrize("sql, expected", [
    (VALID_SQL.replace("SUM(sales_amount)-SUM(refund_amount)", "AVG(sales_amount)-SUM(refund_amount)"), "METRIC_EXPRESSION_MISMATCH"),
    (VALID_SQL.replace("SUM(sales_amount)-SUM(refund_amount)", "SUM(sales_amount)"), "METRIC_EXPRESSION_MISMATCH"),
    (VALID_SQL.replace("GROUP BY region", "GROUP BY region,created_at"), "GRAIN_MISMATCH"),
    (VALID_SQL.replace("GROUP BY region", ""), "GRAIN_MISMATCH"),
    (VALID_SQL.replace("FROM sales", "FROM fake_sales"), "SOURCE_MISMATCH"),
    (VALID_SQL.replace("SUM(sales_amount)", "SUM(unknown_amount)"), "UNKNOWN_OR_AMBIGUOUS_COLUMN"),
    (VALID_SQL.replace("region, SUM", "region, cost_amount, SUM"), "UNEXPECTED_PROJECTION"),
])
def test_formula_grain_source_and_column_mismatch(contract, sql, expected):
    assert expected in codes(sql, contract)


@pytest.mark.parametrize("sql", [
    "DELETE FROM sales", "SELECT 1; DELETE FROM sales", "SELECT 1 UNION SELECT 2",
    "WITH x AS (SELECT * FROM sales) SELECT * FROM x",
    "SELECT (SELECT MAX(cost_amount) FROM sales) FROM sales",
    "SELECT SUM(sales_amount) OVER() FROM sales", "SELECT SLEEP(10) FROM sales",
    "SELECT SUM(sales_amount) FROM sales FOR UPDATE",
    "SELECT DISTINCT SUM(sales_amount) FROM sales", "SELECT * FROM sales OFFSET 1",
    "SELECT region,SUM(sales_amount) FROM sales GROUP BY region WITH ROLLUP",
    "SELECT region,SUM(sales_amount) FROM sales GROUP BY ALL region",
    "SELECT SUM(sales_amount) FROM sales TABLESAMPLE SYSTEM (20)",
])
def test_unsupported_and_non_read_only_scopes_fail_closed(contract, sql):
    assert codes(sql, contract) & {"SQL_NOT_SINGLE_SELECT", "UNSUPPORTED_SQL_SCOPE", "SQL_PARSE_ERROR"}


def test_unknown_join_must_clarify(contract):
    sql = VALID_SQL.replace("FROM sales", "FROM sales LEFT JOIN customers c ON sales.region_id=c.id")
    catalog = CATALOG + [{"name": "customers", "columns": ["id"]}]
    issues = validate_sql_contract(sql, contract, catalog)
    assert any(i.code == "UNPROVEN_JOIN" and i.action == "clarify" for i in issues)


def test_registered_join_requires_real_unique_proof_and_exact_keys(contract):
    joined = contract.model_copy(update={"join_constraints": ()}).model_dump()
    joined["join_constraints"] = [{"source": "customers", "left_on": ["region_id"], "right_on": ["id"]}]
    joined = MetricContract.model_validate(joined)
    sql = VALID_SQL.replace("FROM sales", "FROM sales LEFT JOIN customers c ON sales.region_id=c.id")
    catalog = CATALOG + [{"name": "customers", "columns": ["id"], "unique_keys": [["id"]]}]
    assert not codes(sql, joined, catalog)
    assert "UNPROVEN_JOIN" in codes(sql, joined, CATALOG + [{"name": "customers", "columns": ["id"]}])
    assert "UNPROVEN_JOIN" in codes(sql.replace("LEFT JOIN", "INNER JOIN"), joined, catalog)
    assert "UNPROVEN_JOIN" in codes(sql.replace("sales.region_id=c.id", "sales.region_id=c.id OR 1=1"), joined, catalog)
    assert "UNPROVEN_JOIN" in codes(sql.replace("sales.region_id=c.id", "sales.cost_amount=c.id"), joined, catalog)


def test_unknown_catalog_or_ambiguous_column_blocks(contract):
    assert "CONTRACT_SOURCE_UNAVAILABLE" in codes(VALID_SQL, contract, [])
    sql = VALID_SQL.replace("FROM sales", "FROM sales LEFT JOIN customers c ON sales.region_id=c.id")
    catalog = CATALOG + [{"name": "customers", "columns": ["id", "region"]}]
    assert "UNKNOWN_OR_AMBIGUOUS_COLUMN" in codes(sql, contract, catalog)


def test_net_profit_without_cost_column_clarifies_instead_of_substituting_net_sales():
    registry = MetricContractRegistry(EXAMPLE_SALES_CONTRACTS, ["sales.net_profit"])
    catalog = [{"name": "sales", "columns": ["sales_amount", "refund_amount"]}]
    binding = bind_metric_contracts("计算净利润", {"metrics": ["净利润"]}, catalog, registry)
    assert binding.contracts[0].formula.endswith("- SUM(cost_amount)")
    assert any(i.code == "MISSING_REQUIRED_COLUMNS" and i.action == "clarify" for i in binding.issues)
    assert "METRIC_EXPRESSION_MISMATCH" in codes("SELECT SUM(sales_amount)-SUM(refund_amount) FROM sales",
                                                binding.contracts[0], catalog)


def test_examples_disabled_by_default_and_registry_file_must_enable(tmp_path, contract):
    path = tmp_path / "contracts.json"
    path.write_text(json.dumps({"contracts": [contract.model_dump()]}))
    assert MetricContractRegistry.from_json(path).get(contract.id) is None
    path.write_text(json.dumps({"contracts": [contract.model_dump()], "enabled_contract_ids": [contract.id]}))
    assert MetricContractRegistry.from_json(path).get(contract.id) == contract
    assert not MetricContractRegistry.from_config({"include_sales_examples": True}).enabled()
    assert not MetricContractRegistry().enabled()


def test_explicit_binding_does_not_allow_model_formula_or_version_override(contract):
    registry = MetricContractRegistry([contract], [contract.id])
    binding = bind_metric_contracts("算销售额", {"metrics": [{"contract_id": contract.id,
                                  "formula": "SUM(cost_amount)", "version": "2"}], "version": 3}, CATALOG, registry)
    assert binding.contracts == (contract,)
    assert binding.plan_version == 3
    assert {i.code for i in binding.issues} >= {"CONTRACT_OVERRIDE_FORBIDDEN", "CONTRACT_VERSION_MISMATCH"}
    with pytest.raises(ValidationError):
        contract.formula = "SUM(cost_amount)"


def test_unregistered_metrics_and_ambiguous_language_require_clarification(contract):
    registry = MetricContractRegistry([contract], [contract.id])
    binding = bind_metric_contracts("销售额和净利润哪个高", {"metrics": ["销售额和净利润"]}, CATALOG, registry)
    assert not binding.contracts
    assert binding.issues[0].action == "clarify"
    explicit = bind_metric_contracts("销售额和净利润哪个高", {}, CATALOG, registry, [contract.id])
    assert explicit.contracts == (contract,)
    unknown = bind_metric_contracts("净利润", {}, CATALOG, registry, ["invented"])
    assert "UNKNOWN_CONTRACT" in {i.code for i in unknown.issues}


def test_structured_issue_propagates_step_evidence_and_action(contract):
    binding = ContractBinding(contracts=(contract,))
    issues = validate_sql_contracts("SELECT 1", binding, CATALOG, step_id="q1", evidence_id="e1")
    assert issues
    assert all(i.step_id == "q1" and i.evidence_id == "e1" for i in issues)
    assert set(issues[0].model_dump()) == {"code", "step_id", "evidence_id", "expected", "actual", "action"}


def test_catalog_dict_columns_and_count_star():
    contract = MetricContract(id="count", version="1", formula="COUNT(*)", required_columns=(),
                              unit="rows", source="sales", scope="all rows")
    assert not codes("SELECT COUNT(*) AS total FROM sales", contract,
                     [{"name": "sales", "columns": [{"name": "id"}]}])
    assert "UNSUPPORTED_PROJECTION" in codes("SELECT * FROM sales", contract)


def test_multiple_bound_metrics_can_share_query_but_not_add_unregistered_expressions():
    registry = MetricContractRegistry(EXAMPLE_SALES_CONTRACTS,
                                    ["sales.net_sales", "sales.net_profit"])
    binding = bind_metric_contracts("比较两个已选指标", {"metrics": [
        {"contract_id": "sales.net_sales"}, {"contract_id": "sales.net_profit"},
    ]}, CATALOG, registry)
    assert len(binding.contracts) == 2
    sql = "SELECT SUM(sales_amount)-SUM(refund_amount) net_sales, SUM(sales_amount)-SUM(refund_amount)-SUM(cost_amount) net_profit FROM sales"
    assert not validate_sql_contracts(sql, binding, CATALOG)
    issues = validate_sql_contracts(sql.replace(" net_profit FROM", " net_profit, MAX(cost_amount) FROM"), binding, CATALOG)
    assert "UNEXPECTED_PROJECTION" in {i.code for i in issues}
