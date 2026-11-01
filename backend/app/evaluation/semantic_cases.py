"""Frozen synthetic semantic cases with human labels and an independent pandas oracle.

Holdout definitions are generated together, hashed and persisted BEFORE replay.
Never edit an existing manifest to improve a score: create a separately named suite.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal

import duckdb
import pandas as pd


SUITE_DIR = Path(__file__).resolve().parents[2] / "data/eval/semantic-contract-20261031"
DEFAULT_MANIFEST = SUITE_DIR / "cases-manifest.json"


@dataclass(frozen=True)
class SemanticCase:
    id: str
    split: str
    schema: str
    category: str
    question: str
    sql: str
    contract_data: dict
    catalog: list[dict]
    expected_action: Literal["execute", "repair", "clarify"]
    oracle_kind: str
    start: str
    end: str
    missing_column: str | None = None
    expected_columns: tuple[str, ...] = ("dimension", "value")
    expected_rows: tuple[tuple, ...] = ()

    @property
    def contract(self):
        from app.agent.contracts import MetricContract
        return MetricContract.model_validate(self.contract_data)

    def model_context(self) -> dict:
        """Input suitable for a model; deliberately excludes gold SQL and labels."""
        return {"case_id": self.id, "question": self.question, "catalog": self.catalog,
                "trusted_metric_contract": self.contract_data}


def synthetic_tables(schema: str) -> dict[str, pd.DataFrame]:
    if schema == "commerce":
        base = pd.DataFrame([
            (1, "2026-01-31", "east", 1000, 20, 250),
            (2, "2026-02-01", "east", 200, 10, 50),
            (3, "2026-02-12", "east", 350, 20, 100),
            (4, "2026-02-07", "west", 400, 30, 150),
            (5, "2026-02-28", "west", 150, 5, 50),
            (6, "2026-03-01", "east", 500, 25, 120),
            (7, "2026-03-19", "east", 900, 30, 300),
            (8, "2026-03-03", "west", 700, 40, 200),
            (9, "2026-03-31", "west", 300, 10, 100),
            (10, "2026-04-01", "west", 2000, 100, 600),
        ], columns=["order_id", "sold_at", "region", "gross", "refund", "total_cost"])
        unique = pd.DataFrame([("east", "East"), ("west", "West")], columns=["region_key", "label"])
        duplicates = pd.DataFrame([("east", "a"), ("east", "b"), ("west", "a")],
                                  columns=["region_key", "label"])
        return {"orders": base, "region_dim": unique, "region_history": duplicates}
    if schema == "operations":
        base = pd.DataFrame([
            (1, "2026-01-31", "red", 800, 1000, 90, 100),
            (2, "2026-02-01", "red", 100, 200, 5, 80),
            (3, "2026-02-15", "red", 220, 400, 12, 120),
            (4, "2026-02-12", "blue", 80, 200, 2, 100),
            (5, "2026-02-28", "blue", 160, 300, 8, 200),
            (6, "2026-03-01", "red", 220, 400, 21, 140),
            (7, "2026-03-22", "red", 400, 700, 4, 60),
            (8, "2026-03-13", "blue", 90, 300, 12, 200),
            (9, "2026-03-31", "blue", 300, 500, 3, 100),
            (10, "2026-04-01", "blue", 900, 1000, 99, 100),
        ], columns=["usage_id", "recorded_at", "team", "busy_seconds", "capacity_seconds",
                    "failed_requests", "total_requests"])
        unique = pd.DataFrame([("red", "Red"), ("blue", "Blue")], columns=["team_key", "label"])
        duplicates = pd.DataFrame([("red", "a"), ("red", "b"), ("blue", "a")],
                                  columns=["team_key", "label"])
        return {"service_usage": base, "team_dim": unique, "team_history": duplicates}
    raise ValueError(f"Unknown synthetic schema: {schema}")


def independent_expected(case: SemanticCase) -> list[list]:
    """Business oracle uses explicit pandas arithmetic, never validator/SQL output."""
    frames = synthetic_tables(case.schema)
    if case.schema == "commerce":
        rows = frames["orders"]
        rows = rows[(rows.sold_at >= case.start) & (rows.sold_at < case.end)].copy()
        rows["value"] = rows["gross"] - rows["refund"]
        if case.oracle_kind == "net_profit":
            rows["value"] = rows["value"] - rows["total_cost"]
        grouped = rows.groupby("region")["value"].sum()
    else:
        rows = frames["service_usage"]
        rows = rows[(rows.recorded_at >= case.start) & (rows.recorded_at < case.end)]
        summed = rows.groupby("team").sum(numeric_only=True)
        if case.oracle_kind == "utilization":
            grouped = summed["busy_seconds"] / summed["capacity_seconds"]
        elif case.oracle_kind == "error_rate":
            grouped = summed["failed_requests"] / summed["total_requests"]
        else:
            raise ValueError(case.oracle_kind)
    return [[str(key), float(value)] for key, value in grouped.sort_index().items()]


def _schema_cases(split: str, schema: str) -> list[SemanticCase]:
    holdout = split == "holdout"
    start, end = ("2026-03-01", "2026-04-01") if holdout else ("2026-02-01", "2026-03-01")
    commerce = schema == "commerce"
    source, dimension, time_column = ("orders", "region", "sold_at") if commerce else ("service_usage", "team", "recorded_at")
    oracle = ("net_profit" if holdout else "net_sales") if commerce else ("error_rate" if holdout else "utilization")
    formula = {"net_sales": "SUM(gross) - SUM(refund)",
               "net_profit": "SUM(gross) - SUM(refund) - SUM(total_cost)",
               "utilization": "SUM(busy_seconds) / SUM(capacity_seconds)",
               "error_rate": "SUM(failed_requests) / SUM(total_requests)"}[oracle]
    required = {"net_sales": ["gross", "refund"], "net_profit": ["gross", "refund", "total_cost"],
                "utilization": ["busy_seconds", "capacity_seconds"],
                "error_rate": ["failed_requests", "total_requests"]}[oracle]
    metric_name = {"net_sales": "净销售额", "net_profit": "净利润", "utilization": "设备利用率", "error_rate": "请求错误率"}[oracle]
    dim_source = "region_dim" if commerce else "team_dim"
    history = "region_history" if commerce else "team_history"
    right_key = dimension + "_key"
    contract = dict(id=f"eval.{oracle}.{split}", version="1", name=metric_name, formula=formula,
                    required_columns=required + [time_column, dimension],
                    required_filters=[f"{time_column} >= '{start}'", f"{time_column} < '{end}'"],
                    group_by=[dimension], grain=dimension, unit="CNY" if commerce else "ratio", source=source,
                    scope=("Synthetic booked gross less refunds" + (" less booked total costs including tax" if holdout else ""))
                    if commerce else "Synthetic ratio of summed counts; never average of row ratios",
                    join_constraints=[dict(source=dim_source, left_on=[dimension], right_on=[right_key])])
    frames = synthetic_tables(schema)
    catalog = [{"name": name, "columns": list(frame.columns),
                **({"unique_keys": [[right_key]]} if name == dim_source else {})}
               for name, frame in frames.items()]
    where = " AND ".join(contract["required_filters"])
    valid = f"SELECT {dimension} AS dimension, {formula} AS value FROM {source} WHERE {where} GROUP BY {dimension} ORDER BY dimension"
    question = f"按{dimension}统计{start}（含）至{end}（不含）的{metric_name}，单位为{contract['unit']}，使用已确认指标契约。"
    def make(category, sql, expected="repair", missing=None):
        local_catalog = json.loads(json.dumps(catalog))
        if missing:
            next(item for item in local_catalog if item["name"] == source)["columns"].remove(missing)
        case = SemanticCase(id=f"{split}.{schema}.{category}", split=split, schema=schema,
                            category=category, question=question, sql=sql, contract_data=contract,
                            catalog=local_catalog, expected_action=expected, oracle_kind=oracle,
                            start=start, end=end, missing_column=missing)
        return replace(case, expected_rows=tuple(tuple(row) for row in independent_expected(case)))
    alias_formula = formula
    for column in required:
        alias_formula = alias_formula.replace(f"({column})", f"(b.{column})")
    alias = f"SELECT b.{dimension} AS dimension, {alias_formula} AS value FROM {source} b WHERE b.{time_column} < '{end}' AND (b.{time_column} >= '{start}') GROUP BY b.{dimension} ORDER BY dimension"
    safe_join = valid.replace(f"FROM {source}", f"FROM {source} LEFT JOIN {dim_source} d ON {source}.{dimension}=d.{right_key}")
    if commerce:
        bad_formula = "SUM(gross)" if not holdout else "SUM(gross)-SUM(refund)"
        variants = [make("valid_alias_reordered", alias, "execute"), make("valid_safe_join", safe_join, "execute"),
                    make("missing_time", valid.replace(where, "1=1")),
                    make("wrong_time", valid.replace(f"< '{end}'", f"< '2026-12-31'")),
                    make("wrong_expression", valid.replace(formula, bad_formula)),
                    make("unit_multiplier", valid.replace(formula, f"({formula}) / 1000")),
                    make("join_fanout", safe_join.replace(dim_source, history)),
                    make("missing_required_column", valid, "clarify", required[-1])]
    else:
        bad_denominator = "COUNT(*)" if not holdout else "SUM(failed_requests)"
        denominator = "SUM(capacity_seconds)" if not holdout else "SUM(total_requests)"
        variants = [make("valid_plain", valid, "execute"), make("valid_alias_reordered", alias, "execute"),
                    make("valid_safe_join", safe_join, "execute"),
                    make("or_weakened_time", valid.replace(where, f"({where}) OR 1=1")),
                    make("wrong_grain", valid.replace(f"GROUP BY {dimension}", f"GROUP BY {dimension}, {time_column}")),
                    make("wrong_denominator", valid.replace(denominator, bad_denominator)),
                    make("wrong_source", valid.replace(f"FROM {source}", "FROM archived_usage")),
                    make("missing_required_column", valid, "clarify", required[-1])]
    return variants


def build_cases(split: str | None = None) -> list[SemanticCase]:
    """32 cases: 16 tune and 16 held out, two schemas and four distinct formulas."""
    if split not in (None, "tune", "holdout"):
        raise ValueError(split)
    return [case for which in ("tune", "holdout") if split is None or split == which
            for schema in ("commerce", "operations") for case in _schema_cases(which, schema)]


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def freeze_manifest(path: Path = DEFAULT_MANIFEST) -> dict:
    cases = [asdict(case) for case in build_cases()]
    datasets = {schema: {name: frame.to_dict("records") for name, frame in synthetic_tables(schema).items()}
                for schema in ("commerce", "operations")}
    body = dict(suite="semantic-contract-20261031-v1", label_source="human frozen actions + independent pandas arithmetic",
                datasets=datasets, cases=cases)
    manifest = {**body, "content_sha256": _hash(body),
                "case_hashes": {case["id"]: _hash(case) for case in cases}}
    path = Path(path)
    if path.exists():
        existing = json.loads(path.read_text())
        if existing != json.loads(json.dumps(manifest)):
            raise ValueError("Frozen case manifest changed; create a new suite instead of modifying holdout")
        return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create makes accidental overwrite impossible, including parallel runners.
    with path.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False)
    return json.loads(json.dumps(manifest))


def load_frozen_cases(split=None, path: Path = DEFAULT_MANIFEST) -> list[SemanticCase]:
    manifest = freeze_manifest(path)
    result = []
    for raw in manifest["cases"]:
        if split is not None and raw["split"] != split:
            continue
        raw = dict(raw)
        raw["expected_columns"] = tuple(raw["expected_columns"])
        raw["expected_rows"] = tuple(tuple(row) for row in raw["expected_rows"])
        result.append(SemanticCase(**raw))
    return result


def live_cases(limit=16, path: Path = DEFAULT_MANIFEST) -> list[SemanticCase]:
    """Balanced round-robin subset: both splits, schemas, valid and invalid cases."""
    cases = load_frozen_cases(path=path)
    selections = {("tune", "commerce"): [0, 2, 5, 7],
                  ("tune", "operations"): [2, 3, 5, 6],
                  ("holdout", "commerce"): [1, 3, 4, 6],
                  ("holdout", "operations"): [1, 4, 7, 0]}
    selected = []
    for (split, schema), positions in selections.items():
        bucket = [c for c in cases if c.split == split and c.schema == schema]
        selected.extend(bucket[position] for position in positions)
    return selected[:limit]


def execute_case_sql(case: SemanticCase, sql: str | None = None) -> dict:
    """Execute against actual synthetic tables; labels never depend on this output."""
    statement = case.sql if sql is None else sql
    frames = synthetic_tables(case.schema)
    source = case.contract_data["source"]
    if case.missing_column:
        frames[source] = frames[source].drop(columns=[case.missing_column])
    # Wrong-source cases have a real stale table, making the mistake semantic.
    if "archived_usage" in statement:
        archived = frames[source].copy()
        for column in archived.select_dtypes(include="number").columns:
            archived[column] = archived[column] * (3 if column == "failed_requests" else 2)
        frames["archived_usage"] = archived
    import sqlglot
    from sqlglot import exp
    try:
        ast = sqlglot.parse_one(statement, read="duckdb")
        sources = [table.name for table in ast.find_all(exp.Table)]
    except sqlglot.errors.SqlglotError:
        sources = []
    provenance = dict(executed_sql=statement, source=sources[0] if sources else None,
                      sources=sources, execution_engine="duckdb")
    # This helper is also used with model candidates: only local registered tables
    # and one SELECT may execute, with no file/network table functions or UDFs.
    try:
        statements = sqlglot.parse(statement, read="duckdb")
        ast = statements[0] if len(statements) == 1 else None
        safe = (isinstance(ast, exp.Select) and not ast.find(exp.Into, exp.Lock, exp.Anonymous)
                and all(isinstance(table.this, exp.Identifier) and not table.db and not table.catalog
                        and table.name in frames for table in ast.find_all(exp.Table)))
    except sqlglot.errors.SqlglotError:
        safe = False
    if not safe:
        return dict(**provenance, columns=[], rows=[], error="Evaluation executor supports only local, read-only single SELECT")
    connection = duckdb.connect(":memory:")
    try:
        for name, frame in frames.items():
            connection.register(name, frame)
        cursor = connection.execute(statement)
        rows = [[v.item() if hasattr(v, "item") else v for v in row] for row in cursor.fetchall()]
        return dict(**provenance, columns=[item[0] for item in cursor.description], rows=rows, error=None)
    except duckdb.Error as exc:
        return dict(**provenance, columns=[], rows=[], error=str(exc))
    finally:
        connection.close()


if __name__ == "__main__":
    print(json.dumps({"manifest": str(DEFAULT_MANIFEST), "sha256": freeze_manifest()["content_sha256"]}))
