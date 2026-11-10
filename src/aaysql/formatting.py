def format_result(result) -> str:
    if result.get("status") != "ok":
        return str(result)
    if "message" in result:
        return result["message"]
    if "rows" not in result:
        return f"OK — {result.get('rows_affected', 0)} row(s) affected"
    columns = result["columns"]
    rows = result["rows"]
    if not rows:
        return "(0 rows)\n" + " | ".join(columns)
    matrix = [["NULL" if row[c] is None else str(row[c]) for c in columns] for row in rows]
    widths = [max(len(columns[i]), *(len(r[i]) for r in matrix)) for i in range(len(columns))]
    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    lines = [sep, "| " + " | ".join(columns[i].ljust(widths[i]) for i in range(len(columns))) + " |", sep]
    for r in matrix:
        lines.append("| " + " | ".join(r[i].ljust(widths[i]) for i in range(len(columns))) + " |")
    lines.append(sep)
    lines.append(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")
    return "\n".join(lines)
