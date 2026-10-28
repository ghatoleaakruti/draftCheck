# DraftCheck — rules for Claude Code

## Project
DraftCheck is an AI agent that reads engineering drawings (PDF/image), finds dimensioning,
GD&T, and manufacturability errors, proposes corrections, verifies them, and outputs a
redlined drawing. Built solo for the Meta Global AI Developer Hackathon (Oct 26 – Nov 4, 2026).

## Hackathon rules (must follow)
- All code must be written during the event. Never copy in code from other projects.
- Only use code, data, and assets we have the rights to. Never add drawings or files
  from employers or copyrighted sources.
- Never copy text from standards (e.g., ASME Y14.5) into code. Rules are written in our
  own words, with a `source` field citing the reference.

## How we work
- I (Aakruti) am new to this stack. Explain each change in plain language before making it.
- Work on ONE step at a time. Stop and wait for my approval before the next step.
- NEVER run `git commit`, `git push`, `git merge`, or `git rebase`. I do all git operations myself.
- NEVER put API keys or secrets in code. Read them from `.env` via python-dotenv.
  Never print, log, or display the contents of `.env`.
- Ask before installing any new library; if approved, add it to requirements.txt.

## Engineering principles
- The LLM reads and reasons; deterministic Python decides pass/fail. Never let the LLM
  decide whether a rule passes.
- Every new function with logic gets a pytest test in `tests/`.
- Run `pytest` and `ruff check .` before telling me a step is done.
- Update `docs/ARCHITECTURE.md` when the structure or data flow changes.

## Stack
Python 3.12 in `.venv`, FastAPI, pydantic, reportlab, pypdfium2, pillow, pytest, ruff.
LLM: Muse Spark Open on Baseten (OpenAI-compatible API), model id from `.env`.