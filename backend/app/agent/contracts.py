"""Trusted metric definitions and a conservative, AST based SQL semantic gate.

Scope: one SELECT over physical tables; no CTEs/subqueries/windows/set operations.
This gate proves structural agreement with configured definitions, not arbitrary SQL
equivalence or database correctness. Unsupported constructs fail closed. Registry
configuration belongs to the application owner; never load it from LLM output.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping, Optional

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ValidationIssue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    code: str
    step_id: Optional[str] = None
    evidence_id: Optional[str] = None
    expected: Any = None
    actual: Any = None
    action: Literal["repair", "clarify", "stop"] = "repair"


class JoinConstraint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source: str
    left_on: tuple[str, ...]
    right_on: tuple[str, ...]
    cardinality: Literal["many_to_one", "one_to_one"] = "many_to_one"
    # LEFT preserves the metric's base population; INNER needs a separate coverage proof.
    kind: Literal["left"] = "left"

    @model_validator(mode="after")
    def validate_keys(self):
        if not self.left_on or len(self.left_on) != len(self.right_on):
            raise ValueError("JOIN keys must be nonempty and have equal length")
        return self


class MetricContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    name: Optional[str] = None
    formula: str = Field(min_length=1)
    required_columns: tuple[str, ...]
    required_filters: tuple[str, ...] = ()
    group_by: tuple[str, ...] = ()
    grain: str = "total"
    unit: str
    source: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    join_constraints: tuple[JoinConstraint, ...] = ()
    metric_names: tuple[str, ...] = ()
    question_matches: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_expressions(self):
        for text in (self.formula, *self.required_filters, *self.group_by):
            statements = sqlglot.parse(text, read="mysql")
            if len(statements) != 1 or statements[0] is None:
                raise ValueError("Contract expressions must contain exactly one expression")
            tree = statements[0]
            if isinstance(tree, (exp.Query, exp.DDL, exp.DML, exp.Command)):
                raise ValueError("Contract expressions cannot contain statements")
        return self


class ContractBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    contracts: tuple[MetricContract, ...] = ()
    issues: tuple[ValidationIssue, ...] = ()
    question: str = ""
    plan_version: Optional[int] = None


# Examples describe ONLY the demo sales schema and accounting convention below.
# They are disabled by default and do not claim a universal net-profit definition.
EXAMPLE_SALES_CONTRACTS = (
    MetricContract(
        id="sales.net_sales", version="1", name="净销售额",
        formula="SUM(sales_amount) - SUM(refund_amount)",
        required_columns=("sales_amount", "refund_amount"), unit="CNY", source="sales",
        scope="demo sales only: gross sales less refunds, tax-inclusive; no currency conversion",
        metric_names=("净销售额",),
    ),
    MetricContract(
        id="sales.net_profit", version="1", name="净利润",
        formula="SUM(sales_amount) - SUM(refund_amount) - SUM(cost_amount)",
        required_columns=("sales_amount", "refund_amount", "cost_amount"), unit="CNY", source="sales",
        scope="demo sales only: net sales less booked total costs; cost_amount includes all expenses and tax",
        metric_names=("净利润",),
    ),
)


class MetricContractRegistry:
    """Immutable application-owned registry. Only explicitly enabled IDs can bind."""

    def __init__(self, contracts=(), enabled_contract_ids=()):
        entries = {}
        for entry in contracts:
            contract = entry if isinstance(entry, MetricContract) else MetricContract.model_validate(entry)
            if contract.id in entries:
                raise ValueError(f"Duplicate metric contract: {contract.id}")
            entries[contract.id] = contract
        enabled = frozenset(enabled_contract_ids)
        if enabled - entries.keys():
            raise ValueError("Enabled contract IDs are not defined")
        self._contracts = MappingProxyType(entries)
        self.enabled_contract_ids = enabled

    @classmethod
    def from_config(cls, config: Mapping[str, Any]):
        entries = list(config.get("contracts", []))
        if config.get("include_sales_examples", False):
            entries.extend(EXAMPLE_SALES_CONTRACTS)
        return cls(entries, config.get("enabled_contract_ids", []))

    @classmethod
    def from_json(cls, path: str | Path):
        """Read an owner-configured local file, never a user/LLM supplied path."""
        return cls.from_config(json.loads(Path(path).read_text(encoding="utf-8")))

    def get(self, contract_id: str) -> Optional[MetricContract]:
        return self._contracts.get(contract_id) if contract_id in self.enabled_contract_ids else None

    def enabled(self) -> tuple[MetricContract, ...]:
        return tuple(value for key, value in self._contracts.items() if key in self.enabled_contract_ids)


def _plan_dict(plan) -> dict:
    return plan.model_dump() if isinstance(plan, BaseModel) else dict(plan or {})


def _catalog_tables(catalog) -> dict[str, dict]:
    result = {}
    for item in catalog or []:
        name = item.get("name") or item.get("table_name") or item.get("table")
        if not name:
            continue
        columns = item.get("columns", [])
        if isinstance(columns, Mapping):
            columns = list(columns)
        result[str(name).lower()] = {
            "columns": {str(c.get("name", c.get("column_name", "")) if isinstance(c, Mapping) else c).lower()
                        for c in columns},
            "unique_keys": item.get("unique_keys", []),
        }
    return result


def _contract_catalog_issues(contract, catalog, step_id=None, evidence_id=None):
    tables = _catalog_tables(catalog)
    source = contract.source.lower()
    common = {"step_id": step_id, "evidence_id": evidence_id, "action": "clarify"}
    if source not in tables:
        return [ValidationIssue(code="CONTRACT_SOURCE_UNAVAILABLE", expected=contract.source,
                                actual=sorted(tables), **common)]
    required = {c.lower() for c in contract.required_columns}
    for text in (contract.formula, *contract.required_filters, *contract.group_by):
        tree = sqlglot.parse_one(text, read="mysql")
        required.update(c.name.lower() for c in tree.find_all(exp.Column) if not c.table)
    missing = required - tables[source]["columns"]
    return ([ValidationIssue(code="MISSING_REQUIRED_COLUMNS", expected=sorted(required),
                             actual={"missing": sorted(missing), "source": contract.source}, **common)]
            if missing else [])


def bind_metric_contracts(question, plan, catalog, registry: MetricContractRegistry,
                          contract_ids=None) -> ContractBinding:
    """Bind trusted IDs, then exact configured metric names/question selectors.

    A plan may select an existing ID but cannot define or override its semantics.
    Generic prose is never searched for substrings such as 'sales' or 'profit'.
    """
    data = _plan_dict(plan)
    issues, selected = [], []
    references = list(contract_ids) if contract_ids is not None else list(data.get("contract_ids", []))
    metrics = data.get("metrics", [])
    if contract_ids is None and not references:
        references.extend(metric["contract_id"] for metric in metrics
                          if isinstance(metric, Mapping) and metric.get("contract_id"))
    if not references:
        exact_names = {m if isinstance(m, str) else m.get("name", "") for m in metrics if isinstance(m, (str, Mapping))}
        for name in exact_names:
            matches = [c for c in registry.enabled() if name in c.metric_names or name == c.id]
            if len(matches) > 1:
                issues.append(ValidationIssue(code="AMBIGUOUS_METRIC_CONTRACT", expected="one explicit contract_id",
                                              actual=[c.id for c in matches], action="clarify"))
            elif matches:
                references.append(matches[0].id)
        if not references:
            references.extend(c.id for c in registry.enabled() if question in c.question_matches)
    for ref in dict.fromkeys(references):
        contract = registry.get(ref)
        if contract is None:
            issues.append(ValidationIssue(code="UNKNOWN_CONTRACT", expected="enabled trusted contract_id",
                                          actual=ref, action="clarify"))
            continue
        selected.append(contract)
        issues.extend(_contract_catalog_issues(contract, catalog))
        for metric in metrics:
            if isinstance(metric, Mapping) and metric.get("contract_id") == ref:
                supplied_version = metric.get("contract_version", metric.get("version"))
                if supplied_version is not None and str(supplied_version) != contract.version:
                    issues.append(ValidationIssue(code="CONTRACT_VERSION_MISMATCH", expected=contract.version,
                                                  actual=supplied_version, action="clarify"))
                # Definitions only originate from registry; an attempted model override blocks binding.
                for key in ("formula", "required_columns", "required_filters", "group_by", "source", "scope"):
                    if key in metric:
                        issues.append(ValidationIssue(code="CONTRACT_OVERRIDE_FORBIDDEN", expected="registry definition",
                                                      actual=key, action="stop"))
        for key in ("unit", "grain"):
            if data.get(key) and data[key] != getattr(contract, key):
                issues.append(ValidationIssue(code=f"CONTRACT_{key.upper()}_MISMATCH",
                                              expected=getattr(contract, key), actual=data[key], action="clarify"))
    if not selected and (references or metrics):
        issues.append(ValidationIssue(code="METRIC_CONTRACT_REQUIRED", expected="explicit configured metric definition",
                                      actual=metrics, action="clarify"))
    return ContractBinding(contracts=tuple(selected), issues=tuple(issues), question=question,
                           plan_version=data.get("version"))


def _conjuncts(tree):
    tree = tree.unnest()
    if isinstance(tree, exp.And):
        return _conjuncts(tree.this) + _conjuncts(tree.expression)
    return [tree]


def validate_sql_contract(sql: str, contract, catalog, *, step_id=None, evidence_id=None,
                          dialect="mysql", source=None, _allowed_formulas=()) -> list[ValidationIssue]:
    """Reject unsupported SQL and report structural disagreement with a trusted contract.

    Required WHERE predicates must occur as mandatory AND conjuncts. This bounded
    proof intentionally rejects equivalent BETWEEN/OR rewrites and narrower periods;
    repair should emit the configured expression, not guess SQL equivalence.
    """
    contract = contract if isinstance(contract, MetricContract) else MetricContract.model_validate(contract)
    issues = []

    def issue(code, expected, actual, action="repair"):
        issues.append(ValidationIssue(code=code, step_id=step_id, evidence_id=evidence_id,
                                      expected=expected, actual=actual, action=action))

    if source is not None and source.lower() != contract.source.lower():
        issue("SOURCE_MISMATCH", contract.source, source)
    issues.extend(_contract_catalog_issues(contract, catalog, step_id, evidence_id))
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except (SqlglotError, ValueError, RecursionError) as exc:
        issue("SQL_PARSE_ERROR", "one supported SELECT", str(exc), "stop")
        return issues
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        issue("SQL_NOT_SINGLE_SELECT", "one read-only SELECT", sql, "stop")
        return issues
    tree = statements[0]
    unsupported = [k for k, v in tree.args.items() if v and k not in {
        "expressions", "from_", "joins", "where", "group", "order", "limit"}]
    group_node = tree.args.get("group")
    if group_node:
        unsupported.extend(f"group.{k}" for k, v in group_node.args.items() if v and k != "expressions")
    if (unsupported or len(list(tree.find_all(exp.Select))) != 1
            or tree.find(exp.Subquery, exp.Window, exp.CTE, exp.SetOperation, exp.Into, exp.Lock)
            or tree.find(exp.Anonymous)):
        issue("UNSUPPORTED_SQL_SCOPE", "flat SELECT without side effects, UDFs, CTE, windows or set operations",
              unsupported or tree.sql(dialect=dialect), "stop")
        return issues
    from_node = tree.args.get("from_")
    base = from_node.this if from_node else None
    if not isinstance(base, exp.Table):
        issue("SOURCE_MISMATCH", contract.source, str(base))
        return issues

    def table_name(node):
        return ".".join(part.name.lower() for part in node.parts)

    base_name = table_name(base)
    if base_name != contract.source.lower():
        issue("SOURCE_MISMATCH", contract.source, base_name)
    tables = _catalog_tables(catalog)
    aliases = {}
    for table in tree.find_all(exp.Table):
        name = table_name(table)
        alias = table.alias_or_name.lower()
        if alias in aliases:
            issue("AMBIGUOUS_SOURCE_ALIAS", "distinct source aliases", alias, "stop")
        aliases[alias] = name
        aliases[name] = name
        if name not in tables:
            issue("UNKNOWN_SOURCE", sorted(tables), name)
        if any(v for k, v in table.args.items() if k not in {"this", "db", "catalog", "alias"}):
            issue("UNSUPPORTED_SQL_SCOPE", "physical tables without sampling or hints", table.sql(), "stop")
    physical = set(aliases.values())
    select_aliases = {e.alias.lower() for e in tree.expressions if e.alias}

    def resolve_column(column, expected=False):
        name = column.name.lower()
        qualifier = ".".join(part.name.lower() for part in column.parts[:-1])
        if qualifier:
            resolved = aliases.get(qualifier, qualifier if expected else None)
            if resolved in tables and name in tables[resolved]["columns"]:
                return f"{resolved}.{name}"
        else:
            if expected and name in tables.get(contract.source.lower(), {}).get("columns", set()):
                return f"{contract.source.lower()}.{name}"
            candidates = [p for p in physical if name in tables.get(p, {}).get("columns", set())]
            if len(candidates) == 1:
                return f"{candidates[0]}.{name}"
        return None

    for column in tree.find_all(exp.Column):
        # SELECT aliases are only legal in ORDER BY within this subset.
        if not column.table and column.name.lower() in select_aliases and column.find_ancestor(exp.Order):
            continue
        if resolve_column(column) is None:
            issue("UNKNOWN_OR_AMBIGUOUS_COLUMN", "one catalog-backed source column", column.sql())
    if tree.find(exp.Star):
        # COUNT(*) is explicit, safe and can be a registered formula; SELECT * is not.
        for star in tree.find_all(exp.Star):
            if not isinstance(star.parent, exp.Count):
                issue("UNSUPPORTED_PROJECTION", "explicit contract columns and metric", star.sql())

    def canonical(node, expected=False):
        node = node.copy()

        def transform(part):
            if isinstance(part, exp.Column):
                resolved = resolve_column(part, expected)
                if resolved:
                    return exp.column(resolved, quoted=True)
            if isinstance(part, exp.Identifier):
                return exp.to_identifier(part.name.lower(), quoted=False)
            if isinstance(part, exp.Paren):
                return part.this
            return part

        return node.transform(transform).sql(dialect=dialect)

    expected_formula = canonical(sqlglot.parse_one(contract.formula, read=dialect), True)
    projections = [e.this if isinstance(e, exp.Alias) else e for e in tree.expressions]
    actual_projections = [canonical(e) for e in projections]
    if expected_formula not in actual_projections:
        issue("METRIC_EXPRESSION_MISMATCH", contract.formula, [e.sql(dialect=dialect) for e in projections])
    group = tree.args.get("group")
    actual_groups = [canonical(e) for e in group.expressions] if group else []
    expected_groups = [canonical(sqlglot.parse_one(e, read=dialect), True) for e in contract.group_by]
    if sorted(actual_groups) != sorted(expected_groups):
        issue("GRAIN_MISMATCH", {"grain": contract.grain, "group_by": contract.group_by},
              [e.sql() for e in group.expressions] if group else [])
    allowed_formulas = {canonical(sqlglot.parse_one(text, read=dialect), True) for text in _allowed_formulas}
    for actual in actual_projections:
        if actual not in {expected_formula, *expected_groups, *allowed_formulas}:
            issue("UNEXPECTED_PROJECTION", [contract.formula, *contract.group_by], actual)

    where = tree.args.get("where")
    actual_filters = _conjuncts(where.this) if where else []
    actual_keys = {canonical(e) for e in actual_filters}
    expected_keys = {canonical(sqlglot.parse_one(e, read=dialect), True) for e in contract.required_filters}
    for predicate, key in zip(contract.required_filters,
                              [canonical(sqlglot.parse_one(e, read=dialect), True) for e in contract.required_filters]):
        if key not in actual_keys:
            issue("REQUIRED_FILTER_MISSING_OR_CHANGED", predicate, where.sql() if where else None)
    for predicate in actual_filters:
        if canonical(predicate) not in expected_keys:
            # Extra filtering changes the metric population, even when it narrows a period.
            issue("UNAPPROVED_FILTER", list(contract.required_filters), predicate.sql())

    for join in tree.args.get("joins", []):
        if not isinstance(join.this, exp.Table):
            issue("UNPROVEN_JOIN", "configured LEFT JOIN to unique dimension key", join.sql(), "clarify")
            continue
        right_source = table_name(join.this)
        candidates = [c for c in contract.join_constraints if c.source.lower() == right_source]
        constraint = candidates[0] if len(candidates) == 1 else None
        right = tables.get(right_source, {})
        right_keys = tuple(k.lower() for k in constraint.right_on) if constraint else ()
        unique_keys = {tuple(str(c).lower() for c in key) for key in right.get("unique_keys", [])
                       if isinstance(key, (tuple, list))}
        if (constraint is None or join.side.upper() != "LEFT" or join.args.get("kind")
                or join.args.get("method") or join.args.get("using") or right_keys not in unique_keys):
            issue("UNPROVEN_JOIN", "trusted LEFT JOIN constraint and catalog unique_keys proof", join.sql(), "clarify")
            continue
        on = join.args.get("on")
        predicates = _conjuncts(on) if on else []
        actual_pairs = set()
        for predicate in predicates:
            if isinstance(predicate, exp.EQ) and isinstance(predicate.this, exp.Column) and isinstance(predicate.expression, exp.Column):
                actual_pairs.add(frozenset((resolve_column(predicate.this), resolve_column(predicate.expression))))
        expected_pairs = {frozenset((f"{contract.source.lower()}.{left.lower()}", f"{right_source}.{right_col.lower()}"))
                          for left, right_col in zip(constraint.left_on, constraint.right_on)}
        if len(predicates) != len(expected_pairs) or actual_pairs != expected_pairs:
            issue("UNPROVEN_JOIN", "exact registered equality keys", join.sql(), "clarify")
    return issues


def validate_sql_contracts(sql, binding: ContractBinding, catalog, **kwargs) -> list[ValidationIssue]:
    issues = list(binding.issues)
    for contract in binding.contracts:
        allowed = tuple(c.formula for c in binding.contracts if c.source == contract.source)
        issues.extend(validate_sql_contract(sql, contract, catalog, _allowed_formulas=allowed, **kwargs))
    return issues
