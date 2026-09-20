"""Tests for groundtruth.runtime.cfg_analysis.

All inputs are real Python source strings parsed by ``ast`` — no mocks, no
graph.db. Asserts the load-bearing properties: honest CFG shape for each
compound construct, textbook dominator/control-dependence results, gen/kill
reaching definitions, statement-granular slices, and populated ``limitations``
for everything the analysis cannot see.
"""

from __future__ import annotations

import ast
import textwrap

import pytest

from groundtruth.runtime.cfg_analysis import (
    CFGAnalysisError,
    analyze_function,
    backward_slice,
    build_cfg,
    control_dependence,
    dominators,
    interprocedural_slice,
    post_dominators,
    reaching_definitions,
    slice_at_line,
    use_def_chains,
)


def _analyze(src: str, fn: str = "f", **kw):
    return analyze_function(textwrap.dedent(src), fn, **kw)


def _func(src: str) -> ast.AST:
    tree = ast.parse(textwrap.dedent(src))
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))


# ---------------------------------------------------------------------------
# CFG shape.
# ---------------------------------------------------------------------------


def test_if_else_two_successors_reconverge():
    a = _analyze(
        """
        def f(x):
            if x:
                y = 1
            else:
                y = 2
            return y
        """
    )
    cfg = a.cfg
    cond = next(b for b in cfg.blocks.values() if any(isinstance(i, ast.If) for i in b.statements))
    assert len(cond.successors) == 2
    arms = [cfg.blocks[s] for s in cond.successors]
    # Both arms reconverge on a single join block that owns `return y`.
    joins = [set(b.successors) for b in arms]
    common = set.intersection(*joins)
    assert len(common) == 1
    join = cfg.blocks[next(iter(common))]
    assert any(isinstance(i, ast.Return) for i in join.statements)


def test_while_back_edge_and_exit():
    a = _analyze(
        """
        def f(n):
            while n > 0:
                n -= 1
            return n
        """
    )
    cfg = a.cfg
    header = next(
        b for b in cfg.blocks.values() if any(isinstance(i, ast.While) for i in b.statements)
    )
    assert len(header.successors) == 2  # body + exit
    body = cfg.blocks[[s for s in header.successors if cfg.blocks[s].label == "while_body"][0]]
    assert header.id in body.successors or any(
        cfg.blocks[s].successors == [header.id] or header.id in cfg.blocks[s].successors
        for s in body.successors
    )
    # Back-edge: some block reachable from the body points back at the header.
    assert any(
        header.id in cfg.blocks[bid].successors
        for bid in cfg.blocks
        if bid != header.id and cfg.blocks[bid].label == "while_body"
    ) or header.id in body.successors
    # Exit edge leaves the loop to the return block.
    exit_targets = [s for s in header.successors if cfg.blocks[s].label != "while_body"]
    assert exit_targets


def test_return_terminates_block():
    a = _analyze(
        """
        def f(x):
            if x:
                return 1
            return 2
        """
    )
    cfg = a.cfg
    ret_blocks = [
        b for b in cfg.blocks.values() if any(isinstance(i, ast.Return) for i in b.statements)
    ]
    for b in ret_blocks:
        assert b.successors == [cfg.exit_id]


def test_try_except_edges():
    a = _analyze(
        """
        def f(p):
            try:
                x = int(p)
            except ValueError:
                x = 0
            return x
        """
    )
    cfg = a.cfg
    handlers = [b for b in cfg.blocks.values() if b.label == "except"]
    assert len(handlers) == 1
    handler = handlers[0]
    try_body = cfg.blocks[[b.id for b in cfg.blocks.values() if b.label == "try_body"][0]]
    assert handler.id in try_body.successors
    assert any(
        e[1] == handler.id and e[2] == "except" for e in cfg.edges
    )


def test_break_continue_targets():
    a = _analyze(
        """
        def f(items):
            total = 0
            for it in items:
                if it < 0:
                    continue
                if it > 100:
                    break
                total += it
            return total
        """
    )
    cfg = a.cfg
    header = next(
        b for b in cfg.blocks.values() if any(isinstance(i, ast.For) for i in b.statements)
    )
    cont_block = next(
        b for b in cfg.blocks.values() if any(isinstance(i, ast.Continue) for i in b.statements)
    )
    assert cont_block.successors == [header.id]
    brk_block = next(
        b for b in cfg.blocks.values() if any(isinstance(i, ast.Break) for i in b.statements)
    )
    # break lands on the post-loop block that owns `return total`.
    (post,) = brk_block.successors
    assert any(isinstance(i, ast.Return) for i in cfg.blocks[post].statements)


def test_sequential_statements_share_block():
    a = _analyze(
        """
        def f():
            a = 1
            b = 2
            return a + b
        """
    )
    cfg = a.cfg
    real = [b for b in cfg.blocks.values() if not b.is_entry and not b.is_exit]
    assert len(real) == 1
    assert len(real[0].statements) == 3


# ---------------------------------------------------------------------------
# Dominators / post-dominators / control dependence.
# ---------------------------------------------------------------------------


def test_entry_dominates_all():
    a = _analyze(
        """
        def f(x):
            if x:
                y = 1
            return y
        """
    )
    for bid in a.cfg.blocks:
        assert a.cfg.entry_id in a.dominators.dom[bid]
    assert a.dominators.dom[a.cfg.entry_id] == {a.cfg.entry_id}


def test_postdominators_diamond():
    a = _analyze(
        """
        def f(x):
            if x:
                y = 1
            else:
                y = 2
            return y
        """
    )
    cfg = a.cfg
    cond = next(b for b in cfg.blocks.values() if any(isinstance(i, ast.If) for i in b.statements))
    join = cfg.blocks[next(iter(set.intersection(*[set(cfg.blocks[s].successors) for s in cond.successors])))]
    # The join post-dominates both arms and the condition.
    assert join.id in a.post_dominators.dom[cond.id]
    for s in cond.successors:
        assert join.id in a.post_dominators.dom[s]
    # Neither arm post-dominates the condition.
    for s in cond.successors:
        assert s not in a.post_dominators.dom[cond.id]


def test_control_dependence_if_arm():
    a = _analyze(
        """
        def f(x):
            if x:
                y = 1
            return y
        """
    )
    cfg = a.cfg
    cond = next(b for b in cfg.blocks.values() if any(isinstance(i, ast.If) for i in b.statements))
    then_b = cfg.blocks[[s for s in cond.successors if cfg.blocks[s].label == "if_then"][0]]
    ret_b = next(
        b for b in cfg.blocks.values() if any(isinstance(i, ast.Return) for i in b.statements)
    )
    deps = a.control_dependence
    assert (then_b.id, cond.id, "true") in deps
    # Statements after the join are not control-dependent on the if.
    assert not any(dep == ret_b.id and ctrl == cond.id for dep, ctrl, _ in deps)


def test_loop_header_self_dependent():
    # Textbook FOW result: the loop predicate is control dependent on itself.
    a = _analyze(
        """
        def f(n):
            while n:
                n -= 1
            return n
        """
    )
    cfg = a.cfg
    header = next(
        b for b in cfg.blocks.values() if any(isinstance(i, ast.While) for i in b.statements)
    )
    body = next(b for b in cfg.blocks.values() if b.label == "while_body")
    deps = a.control_dependence
    assert (body.id, header.id, "loop") in deps
    assert (header.id, header.id, "loop") in deps


# ---------------------------------------------------------------------------
# Reaching definitions / use-def chains.
# ---------------------------------------------------------------------------


def test_reaching_defs_kill():
    src = """
    def f():
        x = 1
        x = 2
        y = x
        return y
    """
    a = _analyze(src)
    lines = textwrap.dedent(src).splitlines()
    l_x1 = next(i for i, l in enumerate(lines, 1) if "x = 1" in l)
    l_x2 = next(i for i, l in enumerate(lines, 1) if "x = 2" in l)
    l_use = next(i for i, l in enumerate(lines, 1) if "y = x" in l)
    reaching = a.chains.use_to_defs.get(("x", l_use), set())
    assert ("x", l_x2) in reaching
    assert ("x", l_x1) not in reaching


def test_reaching_defs_branch_join():
    src = """
    def f(c):
        if c:
            x = 1
        else:
            x = 2
        return x
    """
    a = _analyze(src)
    lines = textwrap.dedent(src).splitlines()
    l_x1 = next(i for i, l in enumerate(lines, 1) if "x = 1" in l)
    l_x2 = next(i for i, l in enumerate(lines, 1) if "x = 2" in l)
    l_use = next(i for i, l in enumerate(lines, 1) if "return x" in l)
    reaching = a.chains.use_to_defs.get(("x", l_use), set())
    assert ("x", l_x1) in reaching
    assert ("x", l_x2) in reaching


def test_params_are_entry_defs():
    src = """
    def f(a, b):
        return a + b
    """
    a = _analyze(src)
    lines = textwrap.dedent(src).splitlines()
    l_use = next(i for i, l in enumerate(lines, 1) if "return a + b" in l)
    defs_a = a.chains.use_to_defs.get(("a", l_use), set())
    assert ("a", a.def_line) in defs_a


# ---------------------------------------------------------------------------
# Slicing.
# ---------------------------------------------------------------------------


def test_backward_slice_excludes_unrelated():
    src = """
    def f():
        a = src()
        b = a * 2
        c = unrelated()
        return b
    """
    a = _analyze(src)
    lines = textwrap.dedent(src).splitlines()
    l_ret = next(i for i, l in enumerate(lines, 1) if "return b" in l)
    l_a = next(i for i, l in enumerate(lines, 1) if "a = src()" in l)
    l_b = next(i for i, l in enumerate(lines, 1) if "b = a * 2" in l)
    l_c = next(i for i, l in enumerate(lines, 1) if "c = unrelated()" in l)
    sl = a.backward_slice(l_ret)
    assert l_a in sl
    assert l_b in sl
    assert l_ret in sl
    assert l_c not in sl


def test_backward_slice_pulls_controlling_predicate():
    src = """
    def f(x):
        y = 0
        if x > 1:
            y = 9
        return y
    """
    a = _analyze(src)
    lines = textwrap.dedent(src).splitlines()
    l_ret = next(i for i, l in enumerate(lines, 1) if "return y" in l)
    l_if = next(i for i, l in enumerate(lines, 1) if "if x > 1" in l)
    l_y0 = next(i for i, l in enumerate(lines, 1) if "y = 0" in l)
    l_y9 = next(i for i, l in enumerate(lines, 1) if "y = 9" in l)
    sl = a.backward_slice(l_ret)
    assert {l_ret, l_y0, l_y9, l_if} <= sl


def test_forward_slice_from_assignment():
    src = """
    def f():
        a = src()
        b = a * 2
        c = unrelated()
        return b
    """
    a = _analyze(src)
    lines = textwrap.dedent(src).splitlines()
    l_a = next(i for i, l in enumerate(lines, 1) if "a = src()" in l)
    l_b = next(i for i, l in enumerate(lines, 1) if "b = a * 2" in l)
    l_c = next(i for i, l in enumerate(lines, 1) if "c = unrelated()" in l)
    l_ret = next(i for i, l in enumerate(lines, 1) if "return b" in l)
    sl = a.forward_slice(l_a)
    assert {l_a, l_b, l_ret} <= sl
    assert l_c not in sl


def test_forward_slice_does_not_overinclude_param_uses():
    src = """
    def f(q):
        a = src()
        z = q + 1
        return a
    """
    a = _analyze(src)
    lines = textwrap.dedent(src).splitlines()
    l_a = next(i for i, l in enumerate(lines, 1) if "a = src()" in l)
    l_z = next(i for i, l in enumerate(lines, 1) if "z = q + 1" in l)
    l_ret = next(i for i, l in enumerate(lines, 1) if "return a" in l)
    sl = a.forward_slice(l_a)
    assert {l_a, l_ret} <= sl
    assert l_z not in sl  # q is unrelated to the seed def


def test_backward_slice_includes_param_def_line():
    src = """
    def f(q):
        z = q + 1
        return z
    """
    a = _analyze(src)
    lines = textwrap.dedent(src).splitlines()
    l_ret = next(i for i, l in enumerate(lines, 1) if "return z" in l)
    sl = a.backward_slice(l_ret)
    assert a.def_line in sl  # the signature line is the def site of `q`


def test_slice_at_line_reports_call_sites_not_inlined():
    src = """
    def f():
        a = src()
        b = a * 2
        return b
    """
    out = slice_at_line(textwrap.dedent(src), "f", 5)
    assert "call_sites" in out
    names = [c["name"] for c in out["call_sites"]]
    assert "src" in names  # reported as a call site, never inlined
    assert "call_sites_not_inlined" in out["limitations"]
    assert out["lines"] == sorted(out["lines"])


def test_slice_variables_and_blocks_keys():
    src = """
    def f():
        a = src()
        b = a * 2
        return b
    """
    out = slice_at_line(textwrap.dedent(src), "f", 5, "backward")
    assert set(out) >= {"lines", "variables", "blocks", "limitations"}
    assert "b" in out["variables"]
    assert "a" in out["variables"]


# ---------------------------------------------------------------------------
# Honesty: limitations.
# ---------------------------------------------------------------------------


def test_limitations_dynamic_names():
    a = _analyze(
        """
        def f(k, v):
            setattr(state, k, v)
            exec("x = 1")
            return 0
        """
    )
    assert "dynamic_names" in a.limitations


def test_limitations_global_nonlocal():
    a = _analyze(
        """
        def f():
            global G
            G = 1
            return G
        """
    )
    assert "global_nonlocal" in a.limitations


def test_limitations_nested_def():
    a = _analyze(
        """
        def f(x):
            def g():
                return x * 2
            return g()
        """
    )
    assert "nested_def" in a.limitations


def test_limitations_finally_bypass():
    a = _analyze(
        """
        def f(x):
            try:
                return x
            finally:
                cleanup()
        """
    )
    assert "finally_bypass" in a.limitations


def test_method_lookup_with_class_name():
    src = """
    class A:
        def m(self, x):
            return x + 1
    """
    a = _analyze(src, "m", class_name="A")
    assert a.function_name == "m"
    assert a.def_line == 3


def test_errors_are_typed():
    with pytest.raises(CFGAnalysisError):
        _analyze("def f(:\n", "f")
    with pytest.raises(CFGAnalysisError):
        _analyze("def f():\n    pass\n", "missing")
    with pytest.raises(CFGAnalysisError):
        _analyze("def f():\n    pass\ndef f():\n    pass\n", "f")


def test_deterministic():
    src = textwrap.dedent(
        """
        def f(items):
            total = 0
            for it in items:
                if it > 0:
                    total += it
                else:
                    total -= 1
            return total
        """
    )
    a1 = analyze_function(src, "f")
    a2 = analyze_function(src, "f")
    assert a1.cfg.edges == a2.cfg.edges
    assert [ (b.id, b.label, b.successors, b.predecessors) for b in a1.cfg.blocks.values() ] == [
        (b.id, b.label, b.successors, b.predecessors) for b in a2.cfg.blocks.values()
    ]
    assert a1.dominators.dom == a2.dominators.dom
    assert a1.post_dominators.dom == a2.post_dominators.dom
    assert a1.control_dependence == a2.control_dependence
    assert a1.chains.use_to_defs == a2.chains.use_to_defs
    assert a1.limitations == a2.limitations


def test_match_construct_builds():
    a = _analyze(
        """
        def f(cmd):
            match cmd:
                case "go":
                    r = 1
                case _:
                    r = 0
            return r
        """
    )
    cfg = a.cfg
    match_b = next(
        b for b in cfg.blocks.values() if any(isinstance(i, ast.Match) for i in b.statements)
    )
    assert len(match_b.successors) >= 1
    # Pattern-bound names are defs inside their case block.
    case_blocks = [b for b in cfg.blocks.values() if b.label == "match_case"]
    assert len(case_blocks) == 2


def test_exotic_statement_does_not_crash():
    # `del`, walrus, assert, with — degrade to honest sequential flow.
    a = _analyze(
        """
        def f(items):
            if (n := len(items)) > 0:
                assert n > 0
                with open("x") as fh:
                    data = fh.read()
                del n
            return 0
        """
    )
    assert "with_suppress" in a.limitations


# ---------------------------------------------------------------------------
# Interprocedural slice composition (HAR-90 items 8-9).
# ---------------------------------------------------------------------------


def _resolver(calls):
    """Static callee index: {(file, function, call_line): [targets]} where each
    target is (file, function, def_line, end_line) — the CalleeResolver
    contract, backed by a dict instead of graph.db."""

    def resolve(file: str, function: str, call_line: int):
        return list(calls.get((file, function, call_line), ()))

    return resolve


def test_interprocedural_backward_through_call():
    src = textwrap.dedent(
        """
        def helper(x):
            y = x * 2
            return y


        def main():
            a = 1
            b = helper(a)
            return b
        """
    )
    calls = {("f.py", "main", 9): [("f.py", "helper", 2, 4)]}
    out = interprocedural_slice(
        {"f.py": src}, _resolver(calls), "f.py", "main", 10, "backward"
    )
    merged = set(out["per_file"]["f.py"])
    assert {8, 9, 10} <= merged   # caller slice: a=1, b=helper(a), return b
    assert {2, 3, 4} <= merged    # callee return slice merged into same file
    (hop,) = out["cross_function"]
    assert hop["caller_fn"] == "main" and hop["callee_fn"] == "helper"
    assert hop["call_line"] == 9
    assert hop["mapped_vars"] == {"x": "a"}       # positional binding
    assert hop["result_vars"] == ["b"]
    assert hop["reached_formals"] == ["x"]        # return provably uses x
    assert hop["callee_slice_lines"] == [2, 3, 4]
    assert "interprocedural_summary" in out["limitations"]
    # Deterministic: identical inputs, identical dict.
    again = interprocedural_slice(
        {"f.py": src}, _resolver(calls), "f.py", "main", 10, "backward"
    )
    assert again == out


def test_interprocedural_keyword_binding():
    src = textwrap.dedent(
        """
        def g(base, *, scale=1):
            return base * scale


        def f(v):
            r = g(v, scale=3)
            return r
        """
    )
    calls = {("k.py", "f", 7): [("k.py", "g", 2, 3)]}
    out = interprocedural_slice(
        {"k.py": src}, _resolver(calls), "k.py", "f", 8, "backward"
    )
    (hop,) = out["cross_function"]
    assert hop["mapped_vars"] == {"base": "v", "scale": "3"}
    assert hop["reached_formals"] == ["base", "scale"]
    assert {2, 3, 6, 7, 8} <= set(out["per_file"]["k.py"])


def test_interprocedural_three_level_chain():
    src = textwrap.dedent(
        """
        def h(z):
            return z + 1


        def g(y):
            w = h(y)
            return w


        def f(v):
            u = g(v)
            return u
        """
    )
    calls = {
        ("c.py", "g", 7): [("c.py", "h", 2, 3)],
        ("c.py", "f", 12): [("c.py", "g", 6, 8)],
    }
    out = interprocedural_slice(
        {"c.py": src}, _resolver(calls), "c.py", "f", 13, "backward"
    )
    merged = set(out["per_file"]["c.py"])
    assert {11, 12, 13} <= merged   # f
    assert {6, 7, 8} <= merged      # g
    assert {2, 3} <= merged         # h — reached transitively
    assert len(out["cross_function"]) == 2
    assert [h["callee_fn"] for h in out["cross_function"]] == ["g", "h"]

    # Depth bound: max_depth=1 composes f->g but cuts g->h.
    shallow = interprocedural_slice(
        {"c.py": src}, _resolver(calls), "c.py", "f", 13, "backward",
        max_depth=1,
    )
    assert {6, 7, 8} <= set(shallow["per_file"]["c.py"])
    assert 2 not in shallow["per_file"]["c.py"]
    assert "max_depth_cut" in shallow["limitations"]
    assert len(shallow["cross_function"]) == 1


def test_interprocedural_cycle_cut():
    src = textwrap.dedent(
        """
        def f(n):
            if n > 0:
                return f(n - 1)
            return 0
        """
    )
    calls = {("r.py", "f", 4): [("r.py", "f", 2, 5)]}
    out = interprocedural_slice(
        {"r.py": src}, _resolver(calls), "r.py", "f", 4, "backward"
    )
    # The self-hop composes once; the callee's own slice hits the same call
    # site again and is cut by the visited set.
    assert len(out["cross_function"]) == 1
    assert out["cross_function"][0]["callee_fn"] == "f"
    assert "recursion_cut" in out["limitations"]
    assert {2, 3, 4, 5} <= set(out["per_file"]["r.py"])


def test_interprocedural_spread_args_skipped():
    src = textwrap.dedent(
        """
        def g(*args):
            return len(args)


        def f(items):
            r = g(*items)
            return r
        """
    )
    calls = {("s.py", "f", 7): [("s.py", "g", 2, 3)]}
    out = interprocedural_slice(
        {"s.py": src}, _resolver(calls), "s.py", "f", 8, "backward"
    )
    (hop,) = out["cross_function"]
    assert hop["mapped_vars"] == {}          # *items binds no provable formal
    assert hop["reached_formals"] == []
    assert "spread_unmapped" in out["limitations"]
    assert {2, 3, 6, 7, 8} <= set(out["per_file"]["s.py"])


def test_interprocedural_forward_through_return():
    src = textwrap.dedent(
        """
        def helper(x):
            return x * 2


        def main():
            a = 1
            b = helper(a)
            c = b + 1
            return c
        """
    )
    calls = {("f.py", "main", 8): [("f.py", "helper", 2, 3)]}
    out = interprocedural_slice(
        {"f.py": src}, _resolver(calls), "f.py", "main", 7, "forward"
    )
    merged = set(out["per_file"]["f.py"])
    # Caller: a=1 flows into the call at 8, result b feeds 9 and 10.
    assert {7, 8, 9, 10} <= merged
    assert 3 in merged  # callee effect reaching its return
    (hop,) = out["cross_function"]
    assert hop["callee_fn"] == "helper"
    assert hop["mapped_vars"] == {"x": "a"}
    assert hop["reached_formals"] == ["x"]
    assert hop["reaches_return"] is True
    assert hop["result_vars"] == ["b"]
    assert hop["callee_slice_lines"] == [3]
