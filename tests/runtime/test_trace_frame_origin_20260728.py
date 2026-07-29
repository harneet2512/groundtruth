"""trace_frame must deliver a REPOSITORY frame or nothing (2026-07-28).

Live defect (run 30390877219): ``_produce_trace`` labelled the first non-leaky
parsed frame verbatim as "deepest in-repo frame" and delivered it. Three of the
five frames GT shipped in that run were NOT in-repo::

    deepest in-repo frame: yaml/constructor.py:427 in construct_undefined
    deepest in-repo frame: regex/regex.py:254 in match
    deepest in-repo frame: <string>:18 in test_without_return_cmd

ROOT CAUSE (read, not guessed): ``pretask.traces._is_in_repo`` DOES screen for
repo membership, but for a RELATIVE frame path it calls ``os.path.realpath(path)``
— which resolves against the CURRENT PROCESS CWD, not against ``repo_root``. In
the container the gateway runs WITH cwd == repo_root, so ``realpath("yaml/
constructor.py")`` == ``<repo_root>/yaml/constructor.py`` and the prefix test
returns True BEFORE the existence fallback is ever reached. ``site-packages/``
has already been stripped by ``parse_stack_traces`` normalization, so the
bad-marker screen cannot see it either, and ``<string>`` resolves the same way.

This test reproduces that CWD condition explicitly (``monkeypatch.chdir``) and
pins the gateway-side law: the delivery site classifies every frame's ORIGIN and
only ``FrameOrigin.REPOSITORY`` may be delivered; every suppression emits an
auditable control row naming the origin.
"""
from __future__ import annotations

import textwrap

import pytest

from groundtruth.runtime.gateway import (
    FrameOrigin,
    GatewayState,
    ToolEvent,
    _produce_trace,
    classify_frame_origin,
)


_TRACE = textwrap.dedent('''\
    Traceback (most recent call last):
      File "pkg/mod_a.py", line 12, in load
        return parse(text)
      File "yaml/constructor.py", line 427, in construct_undefined
        raise ConstructorError(None, None, "could not determine a constructor")
      File "regex/regex.py", line 254, in match
        return _compile(pattern, flags, kwargs).match(string, pos, endpos)
      File "<string>", line 18, in test_without_return_cmd
        run()
    ValueError: boom
''')


def _repo(tmp_path):
    """A repo tree where ONLY pkg/mod_a.py actually exists."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod_a.py").write_text(
        "def load(text):\n    return parse(text)\n", encoding="utf-8")
    return tmp_path


def _rows(state):
    captured: list[dict] = []

    def _recorder(feature_id, decision_site, decision, **extra):
        captured.append(dict(
            feature_id=feature_id, decision_site=decision_site,
            decision=decision, **extra))

    state.control_recorder = _recorder
    return captured


def test_only_the_repository_frame_is_delivered(tmp_path, monkeypatch):
    """The three live non-repo frames are suppressed; the one real repo frame ships."""
    repo = _repo(tmp_path)
    # The live condition: the gateway process runs WITH cwd == repo_root, which is
    # exactly what makes realpath() launder a relative dependency path into "in-repo".
    monkeypatch.chdir(repo)
    state = GatewayState(graph_db=None, repo_root=str(repo))
    event = ToolEvent(kind="test", command="python -m pytest -q", output=_TRACE)

    out = _produce_trace(event, state)

    assert len(out) == 1, [a.target for a in out]
    env = out[0]
    assert env.target == "pkg/mod_a.py"
    body = " ".join(env.payload)
    assert "pkg/mod_a.py:12" in body
    for poison in ("yaml/constructor.py", "regex/regex.py", "<string>"):
        assert poison not in body, body
        assert poison not in env.target


def test_every_suppressed_frame_emits_an_auditable_origin_row(tmp_path, monkeypatch):
    """A suppression the GATEWAY makes is a recorded decision, not an invisible one.

    RE-POINTED 2026-07-28.  This used ``_TRACE``, whose three foreign frames name
    files that do not exist under the repo.  Those frames no longer REACH the
    gateway: ``pretask/traces.py:_is_in_repo`` used to resolve a relative path
    with ``realpath`` against the CWD, and this test chdir'd into the repo, so
    they were laundered in and arrived here to be suppressed by origin.  With the
    laundering closed they are dropped upstream, ``_produce_trace`` sees an empty
    frame list, and there is nothing for it to record -- which is why this test
    began asserting against an empty set.

    So it now exercises a frame that genuinely reaches the classifier: one that
    EXISTS under the repo root and is suppressed on a segment rule.  ``.venv`` is
    the right choice because ``_is_leaky`` does NOT pre-empt it (unlike
    ``site-packages`` / ``node_modules`` / ``vendor``, which are dropped by the
    path policy before ``classify_frame_origin`` runs, via the bare ``continue``
    at ``gateway.py:2884``).

    NOTE the residual gap this move exposes, tracked separately: the upstream
    ``traces.py`` drop is now the SILENT one.  The gateway records its decisions;
    the parser does not record its own.
    """
    repo = _repo(tmp_path)
    # A real file under the root, inside a virtualenv directory -- installed code
    # that physically lives in the checkout.
    # NOT ``.venv/lib/...``: ``traces.py``'s bad_markers include ``/lib/``, so such
    # a frame is dropped upstream and never reaches the gateway either -- measured,
    # this test's first version asserted against an empty row list because of it.
    (repo / ".venv" / "pkgs").mkdir(parents=True)
    (repo / ".venv" / "pkgs" / "dep.py").write_text("def f():\n    pass\n", encoding="utf-8")
    trace = textwrap.dedent('''\
        Traceback (most recent call last):
          File "pkg/mod_a.py", line 12, in load
            return parse(text)
          File ".venv/pkgs/dep.py", line 3, in f
            raise ValueError
        ValueError: boom
    ''')
    monkeypatch.chdir(repo)
    state = GatewayState(graph_db=None, repo_root=str(repo))
    rows = _rows(state)
    event = ToolEvent(kind="test", command="python -m pytest -q", output=trace)

    out = _produce_trace(event, state)

    # The repository frame still ships; the vendored one does not.
    assert [a.target for a in out] == ["pkg/mod_a.py"]
    suppressed = [r for r in rows if r["decision"] == "SUPPRESSED"]
    assert [r["reason"] for r in suppressed] == ["frame_origin:DEPENDENCY"], suppressed
    for row in suppressed:
        assert row["fact_class"] == "localization"
        assert row["candidate_id"]


def test_a_nonexistent_foreign_frame_never_reaches_the_gateway(tmp_path, monkeypatch):
    """The upstream half of the same property, pinned so the two cannot drift.

    ``traces.py`` drops a relative frame that names no real file under the root,
    so the gateway is never asked about it and delivers nothing.  Suppression
    here is correct-or-quiet; it is simply someone else's decision.
    """
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    state = GatewayState(graph_db=None, repo_root=str(repo))
    event = ToolEvent(kind="test", command="python -m pytest -q", output=_TRACE)

    out = _produce_trace(event, state)

    assert [a.target for a in out] == ["pkg/mod_a.py"]
    body = " ".join(out[0].payload)
    for poison in ("yaml/constructor.py", "regex/regex.py", "<string>"):
        assert poison not in body, body


@pytest.mark.parametrize(
    "path,expected",
    [
        ("<string>", FrameOrigin.GENERATED),
        ("<stdin>", FrameOrigin.GENERATED),
        ("<frozen importlib._bootstrap>", FrameOrigin.GENERATED),
        ("proto/service_pb2.py", FrameOrigin.GENERATED),
        ("build/lib/pkg/mod_a.py", FrameOrigin.GENERATED),
        ("dist/pkg/mod_a.py", FrameOrigin.GENERATED),
        ("lib/site-packages/yaml/constructor.py", FrameOrigin.DEPENDENCY),
        ("x/dist-packages/regex/regex.py", FrameOrigin.DEPENDENCY),
        ("node_modules/left-pad/index.js", FrameOrigin.DEPENDENCY),
        ("vendor/github.com/x/y.go", FrameOrigin.DEPENDENCY),
        (".venv/lib/python3.11/json/decoder.py", FrameOrigin.DEPENDENCY),
        ("/root/go/pkg/mod/github.com/x@v1.2.3/y.go", FrameOrigin.DEPENDENCY),
        ("/usr/lib/python3.11/json/decoder.py", FrameOrigin.DEPENDENCY),
        ("yaml/constructor.py", FrameOrigin.UNRESOLVED),
        ("", FrameOrigin.UNRESOLVED),
        ("pkg/mod_a.py", FrameOrigin.REPOSITORY),
    ],
)
def test_classifier_table(tmp_path, path, expected):
    repo = _repo(tmp_path)
    assert classify_frame_origin(path, str(repo)) is expected


def test_no_repo_root_can_never_be_repository(tmp_path):
    """Correct-or-quiet: without a root there is nothing to be a member OF."""
    assert classify_frame_origin("pkg/mod_a.py", "") is FrameOrigin.UNRESOLVED


# ---------------------------------------------------------------------------
# The ROOT-RESOLUTION sentinel.
#
# `_root()` never returns "" -- its failure sentinel is "/", emitted with a
# GT_ROOT_MISSING marker and a `gt_root_missing` ledger row, and the code there
# documents the condition as LIVE ("the ro /opt/gt mount shadows it"). Under
# that sentinel EVERY frame classifies UNRESOLVED, including an absolute
# /testbed/... path, because `_to_repo_rel` strips /testbed/ first and the
# existence probe then asks about /src/app.py. trace_frame goes 100% dark.
#
# Failing closed is right -- with no root there is nothing to be a member OF.
# Being INDISTINGUISHABLE from ordinary correct-or-quiet suppression is not.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("root", ["/", "\\", "", "  "])
def test_an_unresolved_repo_root_is_named_once_not_silently_suppressed(root, monkeypatch,
                                                                       tmp_path):
    """A reader must be able to tell "GT could not locate the repo" from "no frame
    was in-repo". Those have completely different fixes."""
    monkeypatch.chdir(tmp_path)
    state = GatewayState(graph_db=None, repo_root=root)
    rows = _rows(state)
    event = ToolEvent(kind="test", command="python -m pytest -q", output=_TRACE)

    out = _produce_trace(event, state)

    assert out == [], "nothing may be delivered without a resolved repository root"
    reasons = [r["reason"] for r in rows if r["decision"] == "SUPPRESSED"]
    assert reasons == ["repo_root_unresolved_sentinel"], reasons
