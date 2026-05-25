"""Tests for Track A preprocessor."""
from __future__ import annotations

from groundtruth.pretask.query_preprocessor import preprocess
from groundtruth.pretask.v2_types import QueryObject


def test_empty_string_returns_empty_query_object() -> None:
    q = preprocess("")
    assert isinstance(q, QueryObject)
    assert q.raw_text == ""
    assert q.file_hints == []
    assert q.function_hints == []
    assert q.class_hints == []
    assert q.high_signal_tokens == []
    assert q.code_blocks == []


def test_simple_python_stack_trace() -> None:
    text = 'Traceback:\n  File "src/foo.py", line 42, in bar\n    do_thing()'
    q = preprocess(text)
    assert "src/foo.py" in q.file_hints
    assert "bar" in q.function_hints
    sources = {(t.token, t.source) for t in q.high_signal_tokens}
    assert ("bar", "stack_trace") in sources
    assert ("foo", "stack_trace") in sources


def test_backtick_path() -> None:
    text = "The file `src/utils.py` is wrong."
    q = preprocess(text)
    assert "src/utils.py" in q.file_hints


def test_backtick_identifier() -> None:
    text = "`MyClass.serialize_payload` raises something."
    q = preprocess(text)
    sources = {(t.token, t.source) for t in q.high_signal_tokens}
    assert ("MyClass.serialize_payload", "backtick") in sources
    assert ("MyClass", "backtick") in sources
    assert ("serialize_payload", "backtick") in sources


def test_fenced_code_block() -> None:
    text = "before\n```python\nx = 1\n```\nafter"
    q = preprocess(text)
    assert len(q.code_blocks) == 1
    assert "x = 1" in q.code_blocks[0]


def test_title_extraction() -> None:
    text = "Bug in HelperFunction\n\nWhen calling foo it breaks."
    q = preprocess(text)
    title_entries = [
        t for t in q.high_signal_tokens if t.source == "title"
    ]
    tokens = {t.token: t.weight for t in title_entries}
    assert "HelperFunction" in tokens
    assert tokens["HelperFunction"] == 1.5


def test_camel_case_error_class() -> None:
    text = "TypeError when serializing the result."
    q = preprocess(text)
    assert "TypeError" in q.class_hints
    err = [t for t in q.high_signal_tokens if t.token == "TypeError"]
    assert any(t.source == "error_class" and t.weight == 3.0 for t in err)


def test_snake_case_in_prose() -> None:
    text = "Please set the cache_dir to None when running."
    q = preprocess(text)
    snake = {
        (t.token, t.source) for t in q.high_signal_tokens
        if t.source == "snake_case"
    }
    assert ("cache_dir", "snake_case") in snake


def test_stopword_filter_drops_common_words() -> None:
    text = "fix the bug in the function"
    q = preprocess(text)
    bad = {"fix", "bug", "function", "the"}
    bad_hits = [t for t in q.high_signal_tokens if t.token.lower() in bad]
    assert bad_hits == []


def test_real_swebench_issue_spotcheck() -> None:
    text = (
        "Title: Wrong unit conversion in astropy.units.Quantity\n\n"
        "When calling `Quantity.to('m')` on a value with unit `cm`,\n"
        "the conversion silently drops the prefix. Stack:\n"
        '  File "astropy/units/quantity.py", line 1234, in to\n'
        "    return self._convert(unit)\n"
        "Raises UnitConversionError. The cache_unit flag is also wrong."
    )
    q = preprocess(text)
    assert "astropy/units/quantity.py" in q.file_hints
    assert "to" in q.function_hints
    assert "UnitConversionError" in q.class_hints
    snake = {t.token for t in q.high_signal_tokens if t.source == "snake_case"}
    assert "cache_unit" in snake
    bt = {t.token for t in q.high_signal_tokens if t.source == "backtick"}
    assert "Quantity" in bt or any(t.token == "Quantity" for t in q.high_signal_tokens)


def test_token_dedup_keeps_max_weight() -> None:
    text = (
        'File "x.py", line 1, in handler\n'
        "  do()\n"
        "Also `handler` is buggy."
    )
    q = preprocess(text)
    handler_entries = [t for t in q.high_signal_tokens if t.token == "handler"]
    sources = {t.source for t in handler_entries}
    assert "stack_trace" in sources
    assert "backtick" in sources
    st = [t for t in handler_entries if t.source == "stack_trace"]
    assert st and st[0].weight == 4.0


def test_raw_text_preserved() -> None:
    text = "Title\n\nBody with `code` and  weird  whitespace.\n"
    q = preprocess(text)
    assert q.raw_text == text


def test_lists_are_deduplicated() -> None:
    text = (
        'File "src/a.py", line 1, in foo\n'
        '  call()\n'
        'File "src/a.py", line 5, in foo\n'
        '  call2()\n'
    )
    q = preprocess(text)
    assert q.file_hints.count("src/a.py") == 1
    assert q.function_hints.count("foo") == 1


def test_codeblock_idents_mined_as_backtick_weight() -> None:
    text = (
        "Issue summary.\n\n"
        "```python\n"
        "from foo import process_request\n"
        "process_request(MyHandler())\n"
        "```\n"
    )
    q = preprocess(text)
    assert len(q.code_blocks) == 1
    bt = {t.token: t.weight for t in q.high_signal_tokens if t.source == "backtick"}
    assert bt.get("process_request") == 3.5
    assert bt.get("MyHandler") == 3.5


def test_codeblock_idents_skip_stopwords() -> None:
    text = "Repro:\n```\nimport sys\nfor i in range(10):\n    print(i)\n```"
    q = preprocess(text)
    bt_tokens = {t.token for t in q.high_signal_tokens if t.source == "backtick"}
    for stopword in {"for", "import", "print"}:
        assert stopword not in bt_tokens
