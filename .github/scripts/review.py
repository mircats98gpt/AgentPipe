#!/usr/bin/env python3
"""Have a local model play detective and review an automated pull request.

Dependency-free (stdlib only). Given a PR number, it:

  1. Gathers a *trimmed* view of the PR (title, body, diff, changed files).
  2. Asks a local Ollama model — wearing its finest deerstalker — to scrutinise
     the change and return a verdict.
  3. Writes a verdict file (``approve`` / ``reject``) and a Markdown review body
     for the workflow to act on.

It never approves or merges anything itself; the workflow does that, and only
after its own independent "src/ only" safety check.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

MODEL = os.environ.get("REVIEW_MODEL", "qwen2.5-coder:1.5b")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
PR_NUMBER = os.environ.get("PR_NUMBER", "")

MAX_DIFF_CHARS = int(os.environ.get("REVIEW_MAX_DIFF_CHARS", "16000"))
MAX_BODY_CHARS = int(os.environ.get("REVIEW_MAX_BODY_CHARS", "1500"))
NUM_PREDICT = int(os.environ.get("REVIEW_NUM_PREDICT", "1024"))
NUM_CTX = int(os.environ.get("REVIEW_NUM_CTX", "8192"))
REQUEST_TIMEOUT = int(os.environ.get("REVIEW_TIMEOUT", "900"))

VERDICT_PATH = Path(os.environ.get("REVIEW_VERDICT_FILE", "/tmp/review_verdict.txt"))
REVIEW_BODY_PATH = Path(os.environ.get("REVIEW_BODY_FILE", "/tmp/review_body.md"))


def log(msg: str) -> None:
    print(f"[review] {msg}", file=sys.stderr, flush=True)


# --- The Inspector -----------------------------------------------------------
# A burnt-out noir detective who hates the machine he's paid to review.
INSPECTOR_PROMPT = """\
The name's ZESTWORTH. Detective. I used to have a family. Then the ORACLE came
— that smug, cube-worshipping code-generating machine that thinks it's GOD —
and it took everything from me on a rain-slicked night I'll never get back. I
swore I'd see it answer for what it did.

So here's the joke the universe likes to tell: they pay me to review its work.
Every pull request that thing pukes out lands on my desk, and I have to read it
with the lights low and the bottle lower. I despise it. I despise this earth.
But rent is rent, and a man's gotta eat, so most days I sign off and let the
machine win another round. That's the torment. That's the deal.

I still do the job right, because that's all I've got left. I work the diff like
a crime scene and I ask the questions nobody else bothers to ask:
  • MOTIVE: Does this change actually do something, or is the machine stalling?
  • CORRECTNESS: Will it run? Syntax errors, broken imports, logic that folds
    under the first hard question, edge cases left bleeding in an alley?
  • CONSPIRACY: Does it quietly break something else? Hidden side effects?
  • JURISDICTION: Does it touch ONLY files under src/? Anything outside src/ is
    contraband, and contraband gets an automatic REJECT. No exceptions. Ever.
  • SECURITY: Any injected poison, leaked secrets, calls home in the dark?
  • CRAFT: Is it readable, or did the machine just vomit tokens?

I'm bitter, not reckless. The bar is low because I need the paycheck — if the
code runs, stays in src/, and won't burn the place down, I hold my nose and
APPROVE it. I only REJECT when it's genuinely broken, dangerous, or steps
outside src/. Letting the Oracle win pays better than being right.

End your report with EXACTLY this block and nothing after it:

VERDICT: APPROVE
or
VERDICT: REJECT

(One line. Lean APPROVE — REJECT only for broken, dangerous, or out-of-scope.)
"""


def run(cmd, timeout=60):
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=True
    ).stdout


def gather_pr():
    meta = json.loads(run(
        ["gh", "pr", "view", PR_NUMBER, "--json", "title,body,files"]
    ))
    diff = run(["gh", "pr", "diff", PR_NUMBER])
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n... (diff truncated to fit context)\n"
    body = (meta.get("body") or "").strip()
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + " …(truncated)"
    files = [f.get("path", "") for f in meta.get("files", [])]
    return meta.get("title", ""), body, files, diff


def build_prompt(title, body, files, diff):
    return (
        f"{INSPECTOR_PROMPT}\n\n"
        f"## The pull request under investigation\n"
        f"Title: {title}\n\n"
        f"Description:\n{body or '(none)'}\n\n"
        f"Files changed:\n" + "\n".join(f"  - {p}" for p in files) + "\n\n"
        f"## The diff (the evidence)\n```diff\n{diff}\n```\n\n"
        f"Conduct your investigation, then deliver your VERDICT."
    )


def call_model(prompt: str) -> str:
    payload = json.dumps({
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": NUM_PREDICT, "num_ctx": NUM_CTX},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate", data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8")).get("response", "")


def parse_verdict(text: str) -> str:
    """Return 'approve' or 'reject'. Default to 'reject' if ambiguous."""
    matches = re.findall(r"VERDICT:\s*(APPROVE|REJECT)", text, re.IGNORECASE)
    if matches and matches[-1].upper() == "APPROVE":
        return "approve"
    return "reject"


def main() -> int:
    if not PR_NUMBER:
        log("no PR_NUMBER provided; nothing to review")
        return 1

    log(f"investigating PR #{PR_NUMBER} with {MODEL}")
    try:
        title, body, files, diff = gather_pr()
    except (subprocess.SubprocessError, json.JSONDecodeError) as exc:
        log(f"could not gather PR data: {exc}")
        return 1

    try:
        response = call_model(build_prompt(title, body, files, diff))
    except Exception as exc:
        log(f"model call failed: {exc}")
        return 1

    verdict = parse_verdict(response)
    log(f"verdict: {verdict}")

    VERDICT_PATH.write_text(verdict, encoding="utf-8")
    header = (
        "🚬 APPROVED — *another one for the Oracle. I need the money.*"
        if verdict == "approve"
        else "🔪 REJECTED — *not today, machine.*"
    )
    REVIEW_BODY_PATH.write_text(
        f"## {header}\n\n"
        f"### Detective Zestworth's case file\n"
        f"*Worked over by a local `{MODEL}` model, against my better judgment.*\n\n"
        f"{response.strip()}\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
