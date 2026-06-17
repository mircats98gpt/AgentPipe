"""Integration tests that drive the ACTUAL local Ollama model.

Kept fast and bounded: tiny ``num_predict`` (the file is grown iteratively),
a short internal deadline, ``max_files=1`` so a test touches one file, and a
hard 60s per-test timeout. The multi-file / interleave / shuffle orchestration
is covered deterministically in test_improve.py. The ``model`` fixture pulls the
model if absent; if no server is reachable these skip.

    pdm run pytest -m integration -v
"""
from __future__ import annotations

import ast

import pytest

from oracle import improve

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]

DEADLINE = 45   # internal soft budget — stays under the 60s hard timeout


def test_real_choose_target_returns_a_py_path(model, quick_budget, scratch):
    scratch({"src/mechanism.py": "", "src/back_dial.py": ""})
    target = improve.choose_target(
        improve.ollama_generate, improve.repo_tree(), ("issue", "#1 add a gear counter"), [])
    assert target.suffix == ".py" and target.is_relative_to(improve.SRC_DIR)


def test_real_generates_valid_python(model, quick_budget, scratch):
    scratch({"src/mechanism.py": "", "src/main.py": ""})
    reason, files, last, valid = improve.generate_improvement(
        improve.ollama_generate, improve.repo_tree(), improve.source_files(), [],
        deadline_seconds=DEADLINE, max_files=1)
    assert files, f"no file produced; last response:\n{last[:400]}"
    target, code = files[0]
    assert target.suffix == ".py" and target.is_relative_to(improve.SRC_DIR)
    assert valid, f"{target.name} did not parse:\n{code[:400]}"
    ast.parse(code)


def test_real_answers_an_issue_with_valid_python(model, quick_budget, scratch):
    scratch({"src/mechanism.py": ""})
    reason, files, last, valid = improve.generate_improvement(
        improve.ollama_generate, improve.repo_tree(), improve.source_files(),
        ["#1 Add a function that counts the teeth on a gear"],
        deadline_seconds=DEADLINE, max_files=1)
    assert files, f"no file produced; last:\n{last[:400]}"
    target, code = files[0]
    assert target.suffix == ".py" and valid, f"{target.name} invalid:\n{code[:400]}"
