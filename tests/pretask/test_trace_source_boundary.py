from groundtruth.pretask.query_preprocessor import preprocess
from groundtruth.pretask.traces import parse_stack_traces


def test_raw_hints_do_not_depend_on_cwd(tmp_path, monkeypatch):
    text = 'File "src/missing.py", line 12, in repair'
    monkeypatch.chdir(tmp_path)
    before = preprocess(text)
    (tmp_path / "src").mkdir()
    (tmp_path / "src/missing.py").write_text("pass\n")
    after = preprocess(text)
    assert before == after
    assert before.function_hints == ["repair"]


def test_verified_trace_rejects_existing_parent_path(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "outside.py").write_text("pass\n")
    assert parse_stack_traces('File "../outside.py", line 1, in outside', str(root)) == []


def test_verified_trace_rejects_missing_but_keeps_existing_file(tmp_path):
    text = 'File "actual.py", line 1, in actual'
    assert parse_stack_traces(text, str(tmp_path)) == []
    (tmp_path / "actual.py").write_text("pass\n")
    assert [frame.func for frame in parse_stack_traces(text, str(tmp_path))] == ["actual"]
