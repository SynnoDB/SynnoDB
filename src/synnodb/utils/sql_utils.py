import re


def extract_order_by_columns(sql_query: str) -> list[tuple[str, str]]:
    """Extract columns used in ORDER BY clause of the SQL query.

    Returns:
        List of tuples (column_name, ordering) where ordering is 'ASC' or 'DESC'.
        Default ordering is 'ASC' if not specified.
    """
    order_by_pattern = re.compile(
        r"ORDER BY\s+(.+?)(?:LIMIT|;|$)", re.IGNORECASE | re.DOTALL
    )
    match = order_by_pattern.search(sql_query)
    if match:
        columns_str = match.group(1).strip()
        columns = []
        for col in columns_str.split(","):
            col = col.strip()
            # Check for ASC or DESC
            if re.search(r"\bDESC\b", col, re.IGNORECASE):
                col_name = re.sub(r"\s+DESC\b", "", col, flags=re.IGNORECASE).strip()
                columns.append((col_name, "DESC"))
            elif re.search(r"\bASC\b", col, re.IGNORECASE):
                col_name = re.sub(r"\s+ASC\b", "", col, flags=re.IGNORECASE).strip()
                columns.append((col_name, "ASC"))
            else:
                # Default is ASC
                columns.append((col, "ASC"))
        return columns
    return []
