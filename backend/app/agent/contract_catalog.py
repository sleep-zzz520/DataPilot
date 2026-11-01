"""Read source metadata for enabled contracts, using bound information-schema parameters."""
from sqlalchemy import text


def load_contract_catalog(engine, sources):
    result = []
    for source in sorted(set(sources)):
        parts = source.split(".")
        if len(parts) != 2:
            continue
        schema, table = parts
        with engine.connect() as connection:
            columns = connection.execute(text(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA=:schema AND TABLE_NAME=:table ORDER BY ORDINAL_POSITION"
            ), {"schema": schema, "table": table}).fetchall()
            keys = connection.execute(text(
                "SELECT INDEX_NAME,COLUMN_NAME FROM INFORMATION_SCHEMA.STATISTICS "
                "WHERE TABLE_SCHEMA=:schema AND TABLE_NAME=:table AND NON_UNIQUE=0 ORDER BY INDEX_NAME,SEQ_IN_INDEX"
            ), {"schema": schema, "table": table}).fetchall()
        grouped = {}
        for key, column in keys:
            grouped.setdefault(key, []).append(column)
        result.append({"name": source, "columns": [row[0] for row in columns],
                       "unique_keys": list(grouped.values())})
    return result
