"""Expression engine tests for AayushSQL v0.9.0.

Covers the parser precedence table, the shared expression AST, and the canonical
evaluator semantics for ``IS NULL``, ``LIKE``, ``IN``, ``BETWEEN``, arithmetic,
``CASE``, ``DISTINCT``, and ``OFFSET``.

Truth-table tests are written out literally (``UNKNOWN is None``) so the SQL
three-valued logic contract is visible in the test, not inferred from code.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from aaysql.ast import (
    Between,
    BinaryExpr,
    Identifier,
    InList,
    IsNull,
    Like,
    Literal,
    UnaryExpr,
)
from aaysql.engine import Engine
from aaysql.errors import ExecutionError, LexerError, ParserError
from aaysql.lexer import Lexer
from aaysql.parser import Parser

UNKNOWN = None


# --- helpers --------------------------------------------------------------


def _parse(sql: str):
    return Parser(Lexer().tokenize(sql)).parse()


def _expr(text: str):
    """Parse a bare expression by selecting it with an alias."""
    stmt = _parse(f"AAYUSH SELECT {text} AS r FROM t")
    return stmt.columns[0].expr


def _engine(rows_sql: str = "", columns: str = "id INT, name TEXT, dept TEXT, score INT, price FLOAT"):
    e = Engine()
    e.execute(f"AAYUSH CREATE TABLE t ({columns})")
    if rows_sql:
        e.execute(f"AAYUSH INSERT INTO t VALUES {rows_sql}")
    return e


def _rows(engine: Engine, sql: str):
    return engine.execute(sql)["rows"]


def _value(engine: Engine, text: str):
    return engine.execute(f"AAYUSH SELECT {text} AS r FROM t WHERE id = 1")["rows"][0]["r"]


@pytest.fixture()
def one_row():
    """A single row so ``SELECT <expr>`` yields exactly the expression value."""
    return _engine("(1, 'Alex', 'Eng', 90, 10.5)")


# --- lexer / parser: precedence and shape ---------------------------------


def test_multiplication_binds_tighter_than_addition():
    e = _engine("(1, 'a', 'x', 1, 1.0)")
    assert _rows(e, "AAYUSH SELECT 2 + 3 * 4 AS r FROM t")[0]["r"] == 14
    assert _rows(e, "AAYUSH SELECT (2 + 3) * 4 AS r FROM t")[0]["r"] == 20


def test_subtraction_is_left_associative_and_spaced_or_not():
    e = _engine("(1, 'a', 'x', 1, 1.0)")
    assert _rows(e, "AAYUSH SELECT 1 - 2 AS r FROM t")[0]["r"] == -1
    assert _rows(e, "AAYUSH SELECT 1-2 AS r FROM t")[0]["r"] == -1
    assert _rows(e, "AAYUSH SELECT 8-3-2 AS r FROM t")[0]["r"] == 3
    expr = _expr("8 - 3 - 2")
    assert expr == BinaryExpr(BinaryExpr(Literal(8), "-", Literal(3)), "-", Literal(2))


def test_division_is_left_associative():
    e = _engine("(1, 'a', 'x', 1, 1.0)")
    assert _rows(e, "AAYUSH SELECT 8 / 4 / 2 AS r FROM t")[0]["r"] == 1.0


def test_unary_minus_and_addition():
    e = _engine("(1, 'a', 'x', 1, 1.0)")
    assert _rows(e, "AAYUSH SELECT -5 AS r FROM t")[0]["r"] == -5
    assert _rows(e, "AAYUSH SELECT - 5 AS r FROM t")[0]["r"] == -5
    assert _rows(e, "AAYUSH SELECT 1 - -2 AS r FROM t")[0]["r"] == 3
    assert _rows(e, "AAYUSH SELECT +7 AS r FROM t")[0]["r"] == 7
    assert _rows(e, "AAYUSH SELECT -3 * 2 AS r FROM t")[0]["r"] == -6


def test_negative_number_stays_a_literal_for_insert_and_default():
    e = Engine()
    e.execute("AAYUSH CREATE TABLE neg (v INT, d INT DEFAULT -4)")
    e.execute("AAYUSH INSERT INTO neg (v) VALUES (-7), (-2)")
    e.execute("AAYUSH INSERT INTO neg VALUES (3, -9)")
    assert _rows(e, "AAYUSH SELECT v, d FROM neg") == [
        {"v": -7, "d": -4},
        {"v": -2, "d": -4},
        {"v": 3, "d": -9},
    ]


def test_select_star_is_still_a_wildcard_not_multiplication():
    e = _engine("(1, 'a', 'x', 1, 1.0)")
    assert _rows(e, "AAYUSH SELECT * FROM t") == [
        {"id": 1, "name": "a", "dept": "x", "score": 1, "price": 1.0}
    ]
    assert _rows(e, "AAYUSH SELECT id * 2 AS doubled FROM t") == [{"doubled": 2}]


def test_not_binds_looser_than_comparison():
    assert _expr("NOT 1 = 2") == UnaryExpr("NOT", BinaryExpr(Literal(1), "=", Literal(2)))
    assert _expr("NOT 1 = 2 AND 3 = 3") == BinaryExpr(
        UnaryExpr("NOT", BinaryExpr(Literal(1), "=", Literal(2))), "AND", BinaryExpr(Literal(3), "=", Literal(3))
    )


def test_between_consumes_only_its_own_and():
    expr = _expr("1 BETWEEN 1 AND 5 AND 2 = 2")
    assert expr.op == "AND"
    assert expr.left == Between(Literal(1), Literal(1), Literal(5))


def test_not_between_and_not_in_and_not_like_shapes():
    assert _expr("1 NOT BETWEEN 2 AND 3") == Between(Literal(1), Literal(2), Literal(3), True)
    assert _expr("1 NOT IN (2, 3)") == InList(Literal(1), (Literal(2), Literal(3)), True)
    assert _expr("'a' NOT LIKE 'b'") == Like(Literal("a"), Literal("b"), True)


def test_is_null_shapes():
    assert _expr("1 IS NULL") == IsNull(Literal(1), False)
    assert _expr("1 IS NOT NULL") == IsNull(Literal(1), True)


def test_parentheses_override_operator_precedence():
    assert _expr("2 * (3 + 4)") == BinaryExpr(
        Literal(2), "*", BinaryExpr(Literal(3), "+", Literal(4))
    )


@pytest.mark.parametrize(
    "sql, message",
    [
        ("AAYUSH SELECT * FROM t WHERE id IN ()", "IN list must not be empty"),
        ("AAYUSH SELECT * FROM t WHERE id IN (SELECT id FROM t)", "Subqueries inside IN"),
        ("AAYUSH SELECT * FROM t WHERE id BETWEEN 1", "Expected AND in BETWEEN"),
        ("AAYUSH SELECT * FROM t WHERE name LIKE", "Expected a pattern after LIKE"),
        ("AAYUSH SELECT CASE WHEN id THEN 1 FROM t", "Expected END to close CASE"),
        ("AAYUSH SELECT CASE id WHEN 1 2 END FROM t", "Expected THEN in CASE"),
        ("AAYUSH SELECT CASE END FROM t", "CASE requires at least one WHEN"),
        ("AAYUSH SELECT DISTINCT DISTINCT id FROM t", "Duplicate DISTINCT keyword"),
        ("AAYUSH SELECT * FROM t LIMIT -1", "LIMIT must be a non-negative integer"),
        ("AAYUSH SELECT * FROM t OFFSET -2", "OFFSET must be a non-negative integer"),
        ("AAYUSH SELECT * FROM t LIMIT 1.5", "LIMIT must be a non-negative integer"),
        ("AAYUSH SELECT * FROM t OFFSET 1.5", "OFFSET must be a non-negative integer"),
        ("AAYUSH SELECT * FROM t WHERE id IS NOT TRUE", "only IS NULL and IS NOT NULL"),
        ("AAYUSH SELECT * FROM t OFFSET 1 LIMIT 1", "Unexpected token 'LIMIT'"),
        ("AAYUSH SELECT id FROM t WHERE 1 +", "Expected literal"),
        ("AAYUSH SELECT 1 + 2 3 AS r FROM t", "Expected FROM"),
    ],
)
def test_invalid_expression_syntax_is_rejected(sql, message):
    e = _engine("(1, 'a', 'x', 1, 1.0)")
    with pytest.raises(ParserError, match=message):
        e.execute(sql)


# --- IS NULL / IS NOT NULL ------------------------------------------------


@pytest.mark.parametrize(
    "expr, expected",
    [
        ("NULL IS NULL", True),
        ("5 IS NULL", False),
        ("NULL IS NOT NULL", False),
        ("5 IS NOT NULL", True),
        ("NOT (NULL IS NULL)", False),
        ("NOT (5 IS NULL)", True),
    ],
)
def test_is_null_truth_table(one_row, expr, expected):
    assert _value(one_row, expr) == expected


def test_is_null_never_returns_unknown(one_row):
    # A NULL comparison is UNKNOWN; IS NULL deliberately is not.
    assert _value(one_row, "NULL = NULL") is UNKNOWN
    assert _value(one_row, "NULL IS NULL") is True


def test_is_null_filters_rows_and_ignores_indexes():
    e = _engine("(1, 10), (2, NULL), (3, 30)", "id INT, score INT")
    e.execute("AAYUSH CREATE INDEX idx_score ON t (score)")
    assert _rows(e, "AAYUSH SELECT id FROM t WHERE score IS NULL") == [{"id": 2}]
    assert _rows(e, "AAYUSH SELECT id FROM t WHERE score IS NOT NULL") == [{"id": 1}, {"id": 3}]
    plan = e.execute("AAYUSH EXPLAIN SELECT id FROM t WHERE score IS NULL")["message"]
    assert "INDEX SCAN" not in plan


def test_is_null_combines_with_other_predicates():
    e = _engine("(1, 10), (2, NULL), (3, 30)", "id INT, score INT")
    assert _rows(e, "AAYUSH SELECT id FROM t WHERE score IS NULL OR score > 20") == [
        {"id": 2},
        {"id": 3},
    ]
    assert _rows(e, "AAYUSH SELECT id FROM t WHERE score IS NOT NULL AND score > 20") == [{"id": 3}]
    # A NULL comparison must not sneak in through a rewritten IS NOT NULL.
    assert _rows(e, "AAYUSH SELECT id FROM t WHERE NOT (score = NULL)") == []


# --- LIKE -----------------------------------------------------------------


@pytest.mark.parametrize(
    "value, pattern, expected",
    [
        ("Alex", "A%", True),
        ("A", "A%", True),
        ("Alex", "B%", False),
        ("Jackson", "%son", True),
        ("Wilson", "%son", True),
        ("cat", "_at", True),
        ("hat", "_at", True),
        ("at", "_at", False),
        ("abc", "a_c", True),
        ("a-c", "a_c", True),
        ("aXc", "a_c", True),
        ("ac", "a_c", False),
        ("anything", "%", True),
        ("", "%", True),
        ("abc", "abc", True),
        ("abc", "ABC", False),
        ("a.c", "a.c", True),
        ("abc", "a.c", False),
        ("a*b", "a*b", True),
        ("axb", "a*b", False),
        ("a[b", "a[b", True),
        ("a^b$", "a^b$", True),
    ],
)
def test_like_pattern_semantics(one_row, value, pattern, expected):
    assert _value(one_row, f"'{value}' LIKE '{pattern}'") is expected


def test_like_is_case_sensitive(one_row):
    assert _value(one_row, "'Alex' LIKE 'alex'") is False
    assert _value(one_row, "'alex' LIKE 'alex'") is True


@pytest.mark.parametrize(
    "expr, expected",
    [
        ("NULL LIKE 'A%'", UNKNOWN),
        ("'Alex' LIKE NULL", UNKNOWN),
        ("NOT (NULL LIKE 'A%')", UNKNOWN),
        ("'Alex' NOT LIKE 'A%'", False),
        ("'Alex' NOT LIKE 'B%'", True),
        ("NULL NOT LIKE 'A%'", UNKNOWN),
    ],
)
def test_like_null_semantics(one_row, expr, expected):
    assert _value(one_row, expr) is expected


def test_like_rejects_non_text_operands(one_row):
    with pytest.raises(ExecutionError, match="LIKE requires a TEXT operand"):
        _value(one_row, "5 LIKE '5%'")
    with pytest.raises(ExecutionError, match="LIKE requires a TEXT pattern"):
        _value(one_row, "'5' LIKE 5")


def test_like_in_every_expression_context():
    e = _engine(
        "(1, 'Alex', 'Eng', 90, 1.0), (2, 'Bob', 'Sales', 70, 2.0), (3, NULL, 'Eng', 50, 3.0)"
    )
    assert _rows(e, "AAYUSH SELECT id FROM t WHERE name LIKE 'A%'") == [{"id": 1}]
    assert _rows(e, "AAYUSH SELECT id FROM t WHERE name NOT LIKE 'A%'") == [{"id": 2}]
    assert _rows(e, "AAYUSH SELECT dept, COUNT(*) AS c FROM t WHERE name LIKE '%o%' GROUP BY dept") == [
        {"dept": "Sales", "c": 1}
    ]
    assert _rows(e, "AAYUSH SELECT dept FROM t GROUP BY dept HAVING COUNT(*) = 2") == [{"dept": "Eng"}]
    assert _rows(
        e, "AAYUSH SELECT id, CASE WHEN name LIKE 'A%' THEN 'yes' ELSE 'no' END AS r FROM t ORDER BY id"
    )[0] == {"id": 1, "r": "yes"}


def test_like_in_join_and_update_and_delete():
    e = Engine()
    e.execute("AAYUSH CREATE TABLE u (id INT, name TEXT, pattern TEXT)")
    e.execute("AAYUSH CREATE TABLE o (id INT, user_id INT, tag TEXT)")
    e.execute("AAYUSH INSERT INTO u VALUES (1, 'Alex', 'b%'), (2, 'Bob', 'z%')")
    e.execute("AAYUSH INSERT INTO o VALUES (10, 1, 'big'), (11, 1, 'small'), (12, 2, 'big')")
    assert _rows(
        e, "AAYUSH SELECT o.id FROM u JOIN o ON u.id = o.user_id AND o.tag LIKE u.pattern ORDER BY o.id"
    ) == [{"o.id": 10}]
    # String concatenation is outside this release; the statement must fail and
    # change nothing rather than silently writing a partial result.
    with pytest.raises(LexerError):
        e.execute("AAYUSH UPDATE o SET tag = tag || '!' WHERE tag LIKE '_ig'")
    assert _rows(e, "AAYUSH SELECT tag FROM o ORDER BY id") == [
        {"tag": "big"},
        {"tag": "small"},
        {"tag": "big"},
    ]
    e.execute("AAYUSH UPDATE o SET tag = 'x' WHERE tag LIKE 's%'")
    assert _rows(e, "AAYUSH SELECT id, tag FROM o ORDER BY id") == [
        {"id": 10, "tag": "big"},
        {"id": 11, "tag": "x"},
        {"id": 12, "tag": "big"},
    ]
    e.execute("AAYUSH DELETE FROM o WHERE tag NOT LIKE 'b%'")
    assert _rows(e, "AAYUSH SELECT id FROM o ORDER BY id") == [{"id": 10}, {"id": 12}]


# --- IN / NOT IN ----------------------------------------------------------


@pytest.mark.parametrize(
    "expr, expected",
    [
        ("2 IN (1, 2, 3)", True),
        ("2 IN (1, 3)", False),
        ("2 IN (1, NULL, 3)", UNKNOWN),
        ("2 IN (2, NULL)", True),
        ("NULL IN (1, 2)", UNKNOWN),
        ("2 NOT IN (1, 3)", True),
        ("2 NOT IN (1, NULL, 3)", UNKNOWN),
        ("2 NOT IN (2, NULL)", False),
        ("NULL NOT IN (1, 2)", UNKNOWN),
        ("'b' IN ('a', 'b')", True),
        ("'B' IN ('a', 'b')", False),
    ],
)
def test_in_truth_table(one_row, expr, expected):
    assert _value(one_row, expr) is expected


def test_in_accepts_expressions_in_the_list(one_row):
    assert _value(one_row, "2 IN (1 + 1, 9)") is True
    assert _value(one_row, "3 IN (1 + 1, 9)") is False


def test_in_list_three_valued_logic_interacts_with_and():
    e = _engine("(1, 10, TRUE), (2, 20, FALSE), (3, NULL, TRUE)", "id INT, x INT, active BOOL")
    # UNKNOWN AND TRUE is UNKNOWN, so row 3 is not admitted.
    assert _rows(e, "AAYUSH SELECT id FROM t WHERE (x IN (1, NULL)) AND active = TRUE") == []
    assert _rows(e, "AAYUSH SELECT id FROM t WHERE x IN (10, NULL)") == [{"id": 1}]
    assert _rows(e, "AAYUSH SELECT id FROM t WHERE x NOT IN (10)") == [{"id": 2}]


def test_in_and_not_in_on_a_column():
    e = _engine("(1, 'Eng'), (2, 'Sales'), (3, NULL), (4, 'HR')", "id INT, dept TEXT")
    assert _rows(e, "AAYUSH SELECT id FROM t WHERE dept IN ('Eng', 'HR')") == [
        {"id": 1},
        {"id": 4},
    ]
    # NOT IN with no NULL in the list excludes the NULL department.
    assert _rows(e, "AAYUSH SELECT id FROM t WHERE dept NOT IN ('Eng')") == [
        {"id": 2},
        {"id": 4},
    ]


def test_in_composes_with_group_by_and_having():
    e = _engine("(1, 'Eng', 10), (2, 'Eng', 20), (3, 'HR', 30)", "id INT, dept TEXT, score INT")
    assert _rows(
        e,
        "AAYUSH SELECT dept, SUM(score) AS s FROM t GROUP BY dept "
        "HAVING SUM(score) IN (30, 99) ORDER BY dept",
    ) == [{"dept": "Eng", "s": 30}, {"dept": "HR", "s": 30}]


# --- BETWEEN / NOT BETWEEN ------------------------------------------------


@pytest.mark.parametrize(
    "expr, expected",
    [
        ("5 BETWEEN 1 AND 5", True),
        ("5 BETWEEN 5 AND 5", True),
        ("5 BETWEEN 6 AND 10", False),
        ("5 NOT BETWEEN 1 AND 5", False),
        ("5 NOT BETWEEN 6 AND 10", True),
        ("NULL BETWEEN 1 AND 5", UNKNOWN),
        ("5 BETWEEN NULL AND 10", UNKNOWN),
        ("5 BETWEEN 1 AND NULL", UNKNOWN),
        ("5 BETWEEN 10 AND 1", False),
        ("'b' BETWEEN 'a' AND 'c'", True),
    ],
)
def test_between_truth_table(one_row, expr, expected):
    assert _value(one_row, expr) is expected


def test_not_between_is_logical_negation_preserving_unknown():
    assert _value(_engine("(1, 'a', 'x', 1, 1.0)"), "NOT (NULL BETWEEN 1 AND 5)") is UNKNOWN
    assert _value(_engine("(1, 'a', 'x', 1, 1.0)"), "NULL NOT BETWEEN 1 AND 5") is UNKNOWN


def test_between_in_where_update_and_delete():
    e = _engine("(1, 15000), (2, 30000), (3, 60000), (4, NULL)", "id INT, salary INT")
    assert _rows(e, "AAYUSH SELECT id FROM t WHERE salary BETWEEN 30000 AND 60000") == [
        {"id": 2},
        {"id": 3},
    ]
    assert _rows(e, "AAYUSH SELECT id FROM t WHERE salary NOT BETWEEN 1 AND 29999") == [
        {"id": 2},
        {"id": 3},
    ]
    e.execute("AAYUSH UPDATE t SET salary = salary + 1 WHERE salary BETWEEN 30000 AND 60000")
    assert _rows(e, "AAYUSH SELECT id, salary FROM t ORDER BY id") == [
        {"id": 1, "salary": 15000},
        {"id": 2, "salary": 30001},
        {"id": 3, "salary": 60001},
        {"id": 4, "salary": None},
    ]
    # NOT BETWEEN is UNKNOWN for the NULL salary, so that row survives.
    e.execute("AAYUSH DELETE FROM t WHERE salary NOT BETWEEN 20000 AND 70000")
    assert _rows(e, "AAYUSH SELECT id FROM t ORDER BY id") == [{"id": 2}, {"id": 3}, {"id": 4}]


def test_between_and_not_combine_without_confusing_the_and_parser():
    e = _engine("(1, 70, 10), (2, 90, 5), (3, 50, 20)", "id INT, score INT, age INT")
    assert _rows(e, "AAYUSH SELECT id FROM t WHERE score BETWEEN 60 AND 90 AND age BETWEEN 1 AND 10") == [
        {"id": 1},
        {"id": 2},
    ]
    assert _rows(e, "AAYUSH SELECT id FROM t WHERE NOT score BETWEEN 60 AND 90") == [{"id": 3}]


# --- arithmetic -----------------------------------------------------------

@pytest.mark.parametrize(
    "expr, expected",
    [
        ("5 + 3", 8),
        ("5 - 3", 2),
        ("5 * 3", 15),
        ("5 / 2", 2.5),
        ("5 % 2", 1),
        ("-5 * -3", 15),
        ("-(5 + 3)", -8),
        ("2 + 3 * 4 - 1", 13),
        ("(2 + 3) * (4 - 1)", 15),
        ("5 + 2.5", 7.5),
        ("5 * 2.0", 10.0),
        ("7 % 3", 1),
        ("-7 % 3", 2),
        ("7 % -3", -2),
        ("7.5 % 2", 1.5),
    ],
)
def test_arithmetic_results(one_row, expr, expected):
    assert _value(one_row, expr) == expected


def test_integer_arithmetic_stays_integer_and_division_is_float(one_row):
    result = _value(one_row, "5 * 3")
    assert isinstance(result, int)
    assert isinstance(_value(one_row, "5 / 5"), float)


@pytest.mark.parametrize(
    "expr",
    ["5 + NULL", "NULL + 5", "NULL * 10", "-NULL", "NULL / 2", "2 - NULL", "NULL % 2"],
)
def test_arithmetic_null_propagation(one_row, expr):
    assert _value(one_row, expr) is None


@pytest.mark.parametrize(
    "expr, message",
    [
        ("5 / 0", "Division by zero"),
        ("5 % 0", "Modulo by zero"),
        ("5.0 / 0", "Division by zero"),
        ("'a' + 1", "requires numeric operands"),
        ("1 - 'a'", "requires numeric operands"),
        ("TRUE + 1", "requires numeric operands"),
        ("TRUE * 2", "requires numeric operands"),
        ("-TRUE", "requires numeric operands"),
    ],
)
def test_arithmetic_errors(one_row, expr, message):
    with pytest.raises(ExecutionError, match=message):
        _value(one_row, expr)


def test_boolean_column_is_not_numeric(one_row):
    e = _engine("(1, TRUE)", "id INT, active BOOL")
    with pytest.raises(ExecutionError, match="requires numeric operands, got bool"):
        _rows(e, "AAYUSH SELECT active + 1 AS r FROM t")


def test_non_finite_result_is_rejected_before_it_is_produced(one_row):
    huge = "9" * 400 + ".0"
    with pytest.raises(ExecutionError, match="not a finite number"):
        _value(one_row, f"{huge} * 10.0")


def test_arithmetic_uses_null_per_row():
    e = _engine("(1, 10, 2), (2, NULL, 2), (3, 10, NULL)", "id INT, a INT, b INT")
    assert _rows(e, "AAYUSH SELECT id, a * b AS product FROM t ORDER BY id") == [
        {"id": 1, "product": 20},
        {"id": 2, "product": None},
        {"id": 3, "product": None},
    ]


def test_arithmetic_in_where_and_order_by():
    e = _engine("(1, 10), (2, 20), (3, 30)", "id INT, price INT")
    assert _rows(e, "AAYUSH SELECT id FROM t WHERE price * 2 > 40 ORDER BY price DESC") == [
        {"id": 3}
    ]
    assert _rows(e, "AAYUSH SELECT id FROM t WHERE price * 2 >= 40 ORDER BY price DESC") == [
        {"id": 3},
        {"id": 2},
    ]
    assert _rows(e, "AAYUSH SELECT id FROM t ORDER BY price * -1 LIMIT 1") == [{"id": 3}]


def test_arithmetic_composes_with_aggregates():
    e = _engine("(1, 10, 2), (2, 20, 3), (3, 30, 4)", "id INT, price INT, quantity INT")
    assert _rows(e, "AAYUSH SELECT SUM(price * quantity) AS revenue FROM t") == [{"revenue": 200}]
    assert _rows(e, "AAYUSH SELECT AVG(price) * 2 AS doubled FROM t") == [{"doubled": 40.0}]


def test_nested_aggregates_remain_rejected():
    e = _engine("(1, 10), (2, 20)", "id INT, price INT")
    with pytest.raises(ExecutionError, match="Nested aggregate functions"):
        e.execute("AAYUSH SELECT SUM(COUNT(price)) AS r FROM t")


def test_update_arithmetic_is_statement_atomic_on_error():
    e = _engine("(1, 10.0), (2, 0.0), (3, 20.0)", "id INT, bal FLOAT")
    with pytest.raises(ExecutionError, match="Division by zero"):
        e.execute("AAYUSH UPDATE t SET bal = 100 / bal")
    # Row 1 evaluates fine, row 2 raises: the whole statement must be a no-op.
    assert _rows(e, "AAYUSH SELECT id, bal FROM t ORDER BY id") == [
        {"id": 1, "bal": 10.0},
        {"id": 2, "bal": 0.0},
        {"id": 3, "bal": 20.0},
    ]


def test_update_arithmetic_is_statement_atomic_on_type_error():
    e = _engine("(1, 10), (2, 20)", "id INT, bal INT")
    with pytest.raises(ExecutionError, match="expects"):
        e.execute("AAYUSH UPDATE t SET bal = bal / 2")
    assert _rows(e, "AAYUSH SELECT bal FROM t ORDER BY id") == [{"bal": 10}, {"bal": 20}]


def test_update_arithmetic_inside_a_transaction_keeps_previous_statements():
    e = _engine("(1, 10.0), (2, 0.0)", "id INT, bal FLOAT")
    e.execute("AAYUSH BEGIN")
    e.execute("AAYUSH UPDATE t SET bal = bal + 1 WHERE id = 1")
    with pytest.raises(ExecutionError, match="Division by zero"):
        e.execute("AAYUSH UPDATE t SET bal = 10 / bal")
    assert _rows(e, "AAYUSH SELECT bal FROM t ORDER BY id") == [{"bal": 11.0}, {"bal": 0.0}]
    e.execute("AAYUSH COMMIT")
    assert _rows(e, "AAYUSH SELECT bal FROM t ORDER BY id") == [{"bal": 11.0}, {"bal": 0.0}]


def test_arithmetic_result_is_coerced_on_update():
    e = _engine("(1, 10)", "id INT, bal INT")
    with pytest.raises(ExecutionError, match="expects INT"):
        e.execute("AAYUSH UPDATE t SET bal = bal / 4")
    e.execute("AAYUSH UPDATE t SET bal = bal * 2 + 1")
    assert _rows(e, "AAYUSH SELECT bal FROM t") == [{"bal": 21}]


# --- CASE ---------------------------------------------------------------


def test_searched_case_first_true_branch_wins():
    e = _engine("(1, 95), (2, 80), (3, 70), (4, 10)", "id INT, score INT")
    assert _rows(
        e,
        "AAYUSH SELECT id, CASE WHEN score >= 90 THEN 'A' WHEN score >= 75 THEN 'B' "
        "WHEN score >= 60 THEN 'C' ELSE 'F' END AS grade FROM t ORDER BY id",
    ) == [
        {"id": 1, "grade": "A"},
        {"id": 2, "grade": "B"},
        {"id": 3, "grade": "C"},
        {"id": 4, "grade": "F"},
    ]


def test_case_without_else_returns_null():
    e = _engine("(1, 95), (2, 10)", "id INT, score INT")
    assert _rows(
        e, "AAYUSH SELECT id, CASE WHEN score >= 90 THEN 'A' END AS g FROM t ORDER BY id"
    ) == [{"id": 1, "g": "A"}, {"id": 2, "g": None}]


def test_case_treats_unknown_condition_as_no_match():
    e = _engine("(1, NULL)", "id INT, score INT")
    rows = _rows(
        e,
        "AAYUSH SELECT CASE WHEN score > 90 THEN 'high' WHEN score IS NULL THEN 'missing' ELSE 'other' END AS g FROM t",
    )
    assert rows == [{"g": "missing"}]
    rows = _rows(e, "AAYUSH SELECT CASE WHEN score > 90 THEN 'high' ELSE 'other' END AS g FROM t")
    assert rows == [{"g": "other"}]


def test_simple_case_compares_with_sql_equality():
    e = _engine("(1, 'Eng'), (2, 'Finance'), (3, 'Other')", "id INT, dept TEXT")
    assert _rows(
        e,
        "AAYUSH SELECT id, CASE dept WHEN 'Eng' THEN 'Technical' WHEN 'Finance' THEN 'Business' "
        "ELSE 'Other' END AS g FROM t ORDER BY id",
    ) == [
        {"id": 1, "g": "Technical"},
        {"id": 2, "g": "Business"},
        {"id": 3, "g": "Other"},
    ]


def test_simple_case_with_null_base_never_matches_even_when_null():
    e = _engine("(1, NULL)", "id INT, dept TEXT")
    rows = _rows(
        e,
        "AAYUSH SELECT CASE dept WHEN NULL THEN 'null-branch' WHEN 'Eng' THEN 'eng' ELSE 'fallback' END AS g FROM t",
    )
    assert rows == [{"g": "fallback"}]


def test_nested_case():
    e = _engine("(1, 10), (2, 30), (3, 70)", "id INT, age INT")
    assert _rows(
        e,
        "AAYUSH SELECT id, CASE WHEN age >= 18 THEN "
        "CASE WHEN age >= 65 THEN 'Senior' ELSE 'Adult' END "
        "ELSE 'Minor' END AS g FROM t ORDER BY id",
    ) == [
        {"id": 1, "g": "Minor"},
        {"id": 2, "g": "Adult"},
        {"id": 3, "g": "Senior"},
    ]


def test_case_with_arithmetic_and_aggregates():
    e = _engine("(1, 10, 2), (2, 20, 3)", "id INT, price INT, qty INT")
    assert _rows(
        e,
        "AAYUSH SELECT SUM(CASE WHEN id = 1 THEN price * qty ELSE 0 END) AS first_total FROM t",
    ) == [{"first_total": 20}]


def test_case_in_where_having_and_order_by():
    e = _engine("(1, 'Eng', 10), (2, 'HR', 20)", "id INT, dept TEXT, score INT")
    assert _rows(
        e, "AAYUSH SELECT id FROM t WHERE CASE WHEN dept = 'Eng' THEN score ELSE 0 END = 10"
    ) == [{"id": 1}]
    assert _rows(
        e, "AAYUSH SELECT dept, SUM(score) AS s FROM t GROUP BY dept "
        "HAVING CASE WHEN SUM(score) > 15 THEN TRUE ELSE FALSE END",
    ) == [{"dept": "HR", "s": 20}]
    assert _rows(
        e, "AAYUSH SELECT id FROM t ORDER BY CASE WHEN dept = 'HR' THEN 0 ELSE 1 END LIMIT 1"
    ) == [{"id": 2}]


def test_case_in_update_passes_through_column_coercion():
    e = _engine("(1, 'a'), (2, 'b')", "id INT, name TEXT")
    with pytest.raises(ExecutionError, match="expects INT"):
        e.execute("AAYUSH UPDATE t SET id = CASE WHEN id = 1 THEN 'x' ELSE id END")
    assert _rows(e, "AAYUSH SELECT id, name FROM t ORDER BY id") == [
        {"id": 1, "name": "a"},
        {"id": 2, "name": "b"},
    ]
    e.execute("AAYUSH UPDATE t SET name = CASE WHEN id = 1 THEN 'z' ELSE name END")
    assert _rows(e, "AAYUSH SELECT name FROM t ORDER BY id") == [{"name": "z"}, {"name": "b"}]


def test_case_respects_not_null_constraints_on_update():
    e = Engine()
    e.execute("AAYUSH CREATE TABLE t (id INT, name TEXT NOT NULL)")
    e.execute("AAYUSH INSERT INTO t VALUES (1, 'a')")
    with pytest.raises(Exception, match="cannot be NULL"):
        e.execute("AAYUSH UPDATE t SET name = CASE WHEN id = 1 THEN NULL ELSE 'x' END")


# --- DISTINCT -----------------------------------------------------------


def test_distinct_removes_duplicate_scalars_in_first_occurrence_order():
    e = _engine("(1, 'Eng'), (2, 'Sales'), (3, 'Eng'), (4, NULL), (5, NULL)", "id INT, dept TEXT")
    assert _rows(e, "AAYUSH SELECT DISTINCT dept FROM t") == [
        {"dept": "Eng"},
        {"dept": "Sales"},
        {"dept": None},
    ]


def test_distinct_composite_key():
    e = _engine(
        "(1, 'Pune', 'Eng'), (2, 'Pune', 'Eng'), (3, 'Pune', 'HR'), (4, 'Delhi', 'Eng')",
        "id INT, city TEXT, dept TEXT",
    )
    assert _rows(e, "AAYUSH SELECT DISTINCT city, dept FROM t") == [
        {"city": "Pune", "dept": "Eng"},
        {"city": "Pune", "dept": "HR"},
        {"city": "Delhi", "dept": "Eng"},
    ]


def test_distinct_keeps_boolean_and_integer_apart():
    e = Engine()
    e.execute("AAYUSH CREATE TABLE t (id INT, b BOOL, i INT)")
    e.execute("AAYUSH INSERT INTO t VALUES (1, TRUE, 1), (2, FALSE, 0)")
    assert _rows(e, "AAYUSH SELECT DISTINCT b FROM t") == [{"b": True}, {"b": False}]
    assert _rows(e, "AAYUSH SELECT DISTINCT CASE WHEN id = 1 THEN 1 ELSE 0 END AS v FROM t") == [
        {"v": 1},
        {"v": 0},
    ]


def test_distinct_after_join():
    e = Engine()
    e.execute("AAYUSH CREATE TABLE u (id INT, name TEXT)")
    e.execute("AAYUSH CREATE TABLE o (id INT, user_id INT)")
    e.execute("AAYUSH INSERT INTO u VALUES (1, 'Alex'), (2, 'Bob')")
    e.execute("AAYUSH INSERT INTO o VALUES (10, 1), (11, 1), (12, 2)")
    assert _rows(
        e, "AAYUSH SELECT DISTINCT u.name FROM u JOIN o ON u.id = o.user_id ORDER BY u.name"
    ) == [{"u.name": "Alex"}, {"u.name": "Bob"}]


def test_distinct_after_aggregation_and_with_having():
    e = _engine("(1, 'a', 10), (2, 'a', 20), (3, 'b', 1)", "id INT, g TEXT, score INT")
    assert _rows(
        e,
        "AAYUSH SELECT DISTINCT CASE WHEN SUM(score) > 25 THEN 'big' ELSE 'small' END AS bucket "
        "FROM t GROUP BY g ORDER BY bucket",
    ) == [{"bucket": "big"}, {"bucket": "small"}]


def test_distinct_with_order_limit_offset():
    e = _engine("(1, 'a'), (2, 'b'), (3, 'a'), (4, 'c'), (5, 'b')", "id INT, v TEXT")
    assert _rows(e, "AAYUSH SELECT DISTINCT v FROM t ORDER BY v LIMIT 2 OFFSET 1") == [
        {"v": "b"},
        {"v": "c"},
    ]
    assert _rows(e, "AAYUSH SELECT DISTINCT v FROM t ORDER BY v DESC LIMIT 1") == [{"v": "c"}]


def test_distinct_requires_order_by_expressions_in_the_select_list():
    e = _engine("(1, 'a'), (2, 'b')", "id INT, v TEXT")
    with pytest.raises(ExecutionError, match="ORDER BY expressions must appear in the select list"):
        e.execute("AAYUSH SELECT DISTINCT v FROM t ORDER BY id")


def test_distinct_allows_order_by_alias_and_selected_expression():
    e = _engine("(1, 'a'), (2, 'b'), (3, 'a')", "id INT, v TEXT")
    assert _rows(e, "AAYUSH SELECT DISTINCT v AS label FROM t ORDER BY label DESC") == [
        {"label": "b"},
        {"label": "a"},
    ]
    assert _rows(e, "AAYUSH SELECT DISTINCT v FROM t ORDER BY v") == [{"v": "a"}, {"v": "b"}]


def test_distinct_star_deduplicates_whole_rows():
    e = _engine("(1, 'a'), (1, 'a'), (2, 'b')", "id INT, v TEXT")
    assert _rows(e, "AAYUSH SELECT DISTINCT * FROM t") == [
        {"id": 1, "v": "a"},
        {"id": 2, "v": "b"},
    ]
    assert _rows(e, "AAYUSH SELECT DISTINCT * FROM t ORDER BY id") == [
        {"id": 1, "v": "a"},
        {"id": 2, "v": "b"},
    ]


def test_distinct_duplicate_output_labels_are_still_rejected():
    e = _engine("(1, 'a')", "id INT, v TEXT")
    with pytest.raises(ExecutionError, match="Duplicate output column name"):
        e.execute("AAYUSH SELECT DISTINCT id, id FROM t")


# --- OFFSET -------------------------------------------------------------


def test_offset_applies_after_ordering_and_before_limit():
    e = _engine("(1, 10), (2, 20), (3, 30), (4, 40), (5, 50)", "id INT, v INT")
    assert _rows(e, "AAYUSH SELECT id FROM t ORDER BY id LIMIT 2 OFFSET 1") == [
        {"id": 2},
        {"id": 3},
    ]
    assert _rows(e, "AAYUSH SELECT id FROM t ORDER BY v DESC OFFSET 3") == [{"id": 2}, {"id": 1}]
    assert _rows(e, "AAYUSH SELECT id FROM t ORDER BY id LIMIT 2 OFFSET 0") == [
        {"id": 1},
        {"id": 2},
    ]


def test_offset_past_the_end_returns_no_rows():
    e = _engine("(1, 10), (2, 20)", "id INT, v INT")
    assert _rows(e, "AAYUSH SELECT id FROM t ORDER BY id OFFSET 5") == []
    assert _rows(e, "AAYUSH SELECT id FROM t ORDER BY id LIMIT 1 OFFSET 5") == []


def test_offset_does_not_skip_rows_before_grouping():
    e = _engine("(1, 'b', 10), (2, 'a', 20), (3, 'a', 30)", "id INT, g TEXT, v INT")
    # Grouping must see all rows: group 'a' has 2 rows even with OFFSET 1.
    assert _rows(
        e,
        "AAYUSH SELECT g, COUNT(*) AS c FROM t GROUP BY g ORDER BY g OFFSET 1",
    ) == [{"g": "b", "c": 1}]
    assert _rows(e, "AAYUSH SELECT g, COUNT(*) AS c FROM t GROUP BY g ORDER BY g LIMIT 5 OFFSET 0") == [
        {"g": "a", "c": 2},
        {"g": "b", "c": 1},
    ]


# --- shared traversal / validation coverage ------------------------------


def test_unknown_columns_inside_new_expressions_are_rejected():
    e = _engine("(1, 'a')", "id INT, v TEXT")
    for sql in [
        "AAYUSH SELECT id FROM t WHERE missing IS NULL",
        "AAYUSH SELECT id FROM t WHERE missing LIKE 'a%'",
        "AAYUSH SELECT id FROM t WHERE id IN (1, missing)",
        "AAYUSH SELECT id FROM t WHERE id BETWEEN missing AND 5",
        "AAYUSH SELECT id FROM t WHERE CASE WHEN missing = 1 THEN 1 ELSE 0 END",
        "AAYUSH SELECT missing * 2 AS r FROM t",
        "AAYUSH SELECT CASE WHEN id = 1 THEN missing ELSE 1 END AS r FROM t",
        "AAYUSH SELECT id FROM t ORDER BY missing * 2",
        "AAYUSH UPDATE t SET id = id + missing",
        "AAYUSH DELETE FROM t WHERE missing BETWEEN 1 AND 2",
    ]:
        with pytest.raises(ExecutionError, match="Unknown column"):
            e.execute(sql)


def test_aggregates_in_where_join_on_and_update_stay_rejected():
    e = _engine("(1, 'a', 10)", "id INT, v TEXT, score INT")
    for sql in [
        "AAYUSH SELECT id FROM t WHERE SUM(score) > 1",
        "AAYUSH SELECT id FROM t WHERE id IN (COUNT(*))",
        "AAYUSH SELECT id FROM t WHERE score BETWEEN SUM(score) AND 10",
        "AAYUSH UPDATE t SET score = SUM(score)",
        "AAYUSH DELETE FROM t WHERE COUNT(*) > 0",
    ]:
        with pytest.raises(ExecutionError, match="Aggregate functions are not allowed"):
            e.execute(sql)


def test_grouped_validation_sees_inside_every_new_node():
    e = _engine("(1, 'a', 10), (2, 'b', 20)", "id INT, g TEXT, score INT")
    for sql in [
        "AAYUSH SELECT g, CASE WHEN id > 0 THEN 1 ELSE 0 END AS r FROM t GROUP BY g",
        "AAYUSH SELECT g, CASE WHEN score IS NULL THEN SUM(score) ELSE 0 END AS r FROM t GROUP BY g",
        "AAYUSH SELECT g, COUNT(*) AS c FROM t GROUP BY g HAVING id > 0",
        "AAYUSH SELECT g FROM t GROUP BY g ORDER BY score + 1",
        "AAYUSH SELECT g FROM t GROUP BY g HAVING CASE WHEN score > 0 THEN TRUE ELSE FALSE END",
    ]:
        with pytest.raises(ExecutionError, match="must appear in GROUP BY"):
            e.execute(sql)


def test_expression_nodes_are_all_covered_by_the_shared_traversal():
    """Every expression class in the AST must yield children or be a known leaf."""
    from aaysql import ast as ast_module
    from aaysql.expressions import EXPRESSION_TYPES, iter_expr_children

    sample = {
        "Literal": Literal(1),
        "Identifier": Identifier("x"),
        "AggregateCall": _expr("SUM(x)"),
        "UnaryExpr": _expr("-x"),
        "BinaryExpr": _expr("x + 1"),
        "IsNull": _expr("x IS NULL"),
        "Like": _expr("x LIKE 'a'"),
        "InList": _expr("x IN (1, 2)"),
        "Between": _expr("x BETWEEN 1 AND 2"),
        "Case": _expr("CASE WHEN x THEN 1 ELSE 2 END"),
    }
    for name in sample:
        node_type = getattr(ast_module, name)
        assert node_type in EXPRESSION_TYPES, name
    for name, node in sample.items():
        if name in {"Literal", "Identifier"}:
            assert list(iter_expr_children(node)) == []
        else:
            assert list(iter_expr_children(node)), f"{name} yields no children"
    # One representative deep walk must reach every identifier.
    from aaysql.expressions import collect_identifiers

    deep = _expr("CASE WHEN a IS NULL THEN 1 WHEN b LIKE 'x%' THEN c IN (d, 2) "
                 "WHEN e BETWEEN f AND g THEN h + i ELSE j * k END")
    assert [ident.name for ident in collect_identifiers(deep)] == [
        "a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k",
    ]
