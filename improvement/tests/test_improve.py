"""Unit tests for the Oracle's deterministic machinery.

No real model: the model calls (name a file, grow a bite of code, summarise) are
driven by small scripted fakes so the orchestration — issue shuffling,
issue/source interleaving, the write→continue→fix progression, and accumulating
MULTIPLE files across inspirations — is exercised directly and fast. Real
generation is covered in test_integration.py.
"""
from __future__ import annotations

import re

from oracle import improve


def model(route):
    """Wrap route(messages)->str into a fake generate(), recording calls."""
    calls = []

    def generate(messages, *, num_predict=None, temperature=None):
        calls.append(messages)
        return route(messages)

    generate.calls = calls
    return generate


BIG = 10 ** 9  # effectively no deadline for the fast unit loops


# --- issue shuffling: ALL issues surface, in a wandering order ---------------
def test_collect_issue_list_includes_every_issue(monkeypatch):
    fake = [{"number": i, "title": f"t{i}", "body": "", "labels": []} for i in range(6)]
    monkeypatch.setattr(improve, "_fetch_issues", lambda: fake)
    items = improve.collect_issue_list()
    assert len(items) == 6
    for i in range(6):
        assert any(f"#{i} t{i}" in it for it in items)   # not just one issue — all of them


def test_collect_issue_list_actually_shuffles(monkeypatch):
    fake = [{"number": i, "title": f"t{i}", "body": "", "labels": []} for i in range(6)]
    monkeypatch.setattr(improve, "_fetch_issues", lambda: fake)
    orders = set()
    for seed in range(6):
        improve._RNG.seed(seed)
        items = improve.collect_issue_list()
        orders.add(tuple(int(re.search(r"#(\d+)", it).group(1)) for it in items))
    assert len(orders) > 1   # different seeds → different orders → it really shuffles


def test_interleave_alternates_then_trails():
    assert improve._interleave([1, 3], [2, 4]) == [1, 2, 3, 4]
    assert improve._interleave([1], [2, 4, 6]) == [1, 2, 4, 6]
    assert improve._interleave([], [2]) == [2]


# --- path coercion -----------------------------------------------------------
def test_bare_directory_becomes_a_file(scratch):
    scratch()
    t = improve.coerce_to_src("src/", "print('x')")
    assert t is not None and t != improve.SRC_DIR and t.suffix == ".py"


def test_traversal_is_contained(scratch):
    scratch()
    assert improve.coerce_to_src("../../etc/passwd", "data").is_relative_to(improve.SRC_DIR)


# --- content validation + code extraction ------------------------------------
def test_broken_python_is_flagged(scratch):
    scratch()
    probs = improve.content_problems(improve.SRC_DIR / "m.py", "def f():\n    return +\n")
    assert len(probs) == 1 and "syntax error" in probs[0].lower()


def test_strip_code_fenced_and_dangling():
    assert improve._strip_code("```python\nX = 1\n```") == "X = 1"
    assert improve._strip_code("```python\nX = 1") == "X = 1"
    assert improve._strip_code("X = 1\n") == "X = 1"


def test_truncation_vs_logic_error():
    assert improve._is_truncation("x = [1, 2,", improve._syntax_error("x = [1, 2,"))
    broke = "def f(:\n    return 1"
    assert not improve._is_truncation(broke, improve._syntax_error(broke))


# --- choosing the target (informed by one inspiration) -----------------------
def test_choose_target_extracts_path(scratch):
    scratch({"src/mechanism.py": ""})
    t = improve.choose_target(lambda m, **k: "src/mechanism.py", "tree", ("issue", "#1 do x"), [])
    assert t.name == "mechanism.py" and t.is_relative_to(improve.SRC_DIR)


def test_choose_target_can_create_new_file(scratch):
    scratch({"src/mechanism.py": ""})
    t = improve.choose_target(lambda m, **k: "src/orrery.py", "tree", ("issue", "#1"), [])
    assert t.name == "orrery.py" and not t.exists() and t.is_relative_to(improve.SRC_DIR)


def test_choose_target_forces_py_and_defaults_on_junk(scratch):
    scratch()
    assert improve.choose_target(lambda m, **k: "src/notes", "t", ("issue", "x"), []).suffix == ".py"
    assert improve.choose_target(lambda m, **k: "garbage", "t", ("issue", "x"), []).suffix == ".py"


# --- growing a file: write → continue (truncated) / fix (broken) -------------
def test_grow_file_valid_on_first_write(scratch):
    scratch()
    g = model(lambda m: "VALUE = 42\n")
    code, last = improve.grow_file(g, improve.SRC_DIR / "m.py", "t", ("issue", "x"), "",
                                   deadline=BIG, max_rounds=4)
    assert improve.content_problems(improve.SRC_DIR / "m.py", code) == [] and len(g.calls) == 1


def test_grow_file_continues_a_truncated_draft(scratch):
    scratch()

    def route(m):
        if "continues from exactly where" in m[-1]["content"]:
            return " 1)\n    return total\n"
        return "def tooth_count(start=13):\n    total = (start +"

    code, _ = improve.grow_file(model(route), improve.SRC_DIR / "m.py", "t", ("issue", "x"), "",
                                deadline=BIG, max_rounds=4)
    assert improve.content_problems(improve.SRC_DIR / "m.py", code) == []
    assert "return total" in code


def test_grow_file_fixes_a_broken_draft(scratch):
    scratch()

    def route(m):
        return "def f():\n    return 1\n" if "does not parse" in m[-1]["content"] else "def f(:\n    return 1"

    g = model(route)
    code, _ = improve.grow_file(g, improve.SRC_DIR / "m.py", "t", ("issue", "x"), "",
                                deadline=BIG, max_rounds=4)
    assert improve.content_problems(improve.SRC_DIR / "m.py", code) == []
    assert any("does not parse" in m[-1]["content"] for m in g.calls)   # took the FIX path


# --- orchestration: MANY files across MANY inspirations ----------------------
def test_generates_multiple_files_across_inspirations(scratch):
    scratch({"src/mech.py": "X = 1\n"})

    def route(m):
        u = m[-1]["content"]
        if "electrifying sentence" in u:
            return "A vision spanning several files"
        if "Pick the ONE file" in u:
            return "src/from_issue.py" if "An open issue" in u else "src/from_source.py"
        return "VALUE = 1\n"   # every write/fix produces valid code

    g = model(route)
    reason, files, last, valid = improve.generate_improvement(
        g, "tree", [("src/mech.py", "X = 1\n")], ["#7 forge a gear", "#9 wind the dial"],
        deadline_seconds=999, max_files=5)
    rels = {t.name for t, _ in files}
    assert {"from_issue.py", "from_source.py"} <= rels   # both issue- and source-driven files
    assert len(files) >= 2 and valid
    assert reason == "A vision spanning several files"


def test_respects_max_files(scratch):
    scratch()
    route = lambda m: ("one" if "electrifying" in m[-1]["content"]
                       else f"src/f{len(m)}.py" if "Pick the ONE file" in m[-1]["content"]
                       else "V = 1\n")
    # many inspirations, but cap at 1 file
    _, files, _, _ = improve.generate_improvement(
        model(route), "t", [], ["#1", "#2", "#3", "#4"], deadline_seconds=999, max_files=1)
    assert len(files) == 1


def test_reports_invalid_when_unrepairable(scratch):
    scratch()

    def route(m):
        u = m[-1]["content"]
        if "Pick the ONE file" in u: return "src/broken.py"
        if "electrifying sentence" in u: return "a flawed vision"
        return "def f(:"   # never parses, in any mode

    _, files, _, valid = improve.generate_improvement(
        model(route), "t", [], ["#1"], deadline_seconds=999, max_files=1, max_rounds=3)
    assert files and valid is False   # ships best effort, flagged invalid


def test_deadline_guard_makes_no_model_calls():
    def boom(*a, **k):
        raise AssertionError("model called past deadline")
    clock = iter([0, 100])   # now()=0 sets deadline=50; next now()=100 is past it
    _, files, _, valid = improve.generate_improvement(
        boom, "t", [("src/a.py", "x")], ["#1"], deadline_seconds=50, now=lambda: next(clock))
    assert files == [] and valid is False


# --- ollama_generate request construction (mocked socket) --------------------
def test_ollama_generate_builds_request_and_parses(monkeypatch):
    import json
    captured = {}

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"message": {"content": "ok"}}).encode()

    monkeypatch.setattr(improve.urllib.request, "urlopen",
                        lambda req, timeout=None: captured.update(body=json.loads(req.data),
                                                                  url=req.full_url) or FakeResp())
    assert improve.ollama_generate([{"role": "user", "content": "hi"}], num_predict=7) == "ok"
    assert captured["url"].endswith("/api/chat") and captured["body"]["options"]["num_predict"] == 7
