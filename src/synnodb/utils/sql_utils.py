def extract_order_by_columns(sql_query: str) -> list[tuple[str, str]]:
    """Extract columns used in the outermost ORDER BY clause of the SQL query.

    Only the top-level ORDER BY is considered. An ORDER BY nested inside a window
    function (``... OVER (ORDER BY ...)``) or a subquery is deliberately ignored,
    since it does not determine the row order of the final result set.

    Returns:
        List of tuples (column_expr, ordering) where ordering is 'ASC' or 'DESC'.
        Default ordering is 'ASC' if not specified. Returns an empty list when the
        query has no top-level ORDER BY (or cannot be parsed).
    """
    import sqlglot
    from sqlglot import expressions as exp
    from sqlglot.errors import ParseError

    try:
        tree = sqlglot.parse_one(sql_query, read="duckdb")
    except ParseError:
        return []

    if tree is None:
        return []

    # The top-level ORDER BY attaches to the root node, whether that is a SELECT or
    # a set operation (UNION/EXCEPT/INTERSECT). find_all would also reach nested
    # ORDER BYs inside window functions and subqueries, so read the root's arg only.
    order = tree.args.get("order")
    if not isinstance(order, exp.Order):
        return []

    columns: list[tuple[str, str]] = []
    for ordered in order.expressions:
        col_expr = ordered.this.sql(dialect="duckdb")
        ordering = "DESC" if ordered.args.get("desc") else "ASC"
        columns.append((col_expr, ordering))
    return columns
