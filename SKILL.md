---
name: codex-flowkiln
description: Mine repeated Codex prompts into reusable skill candidates. Use when the user asks to find repeated workflows, distill a habit into a skill, or review what tasks keep recurring. Never auto-promotes a skill; always requires review. Do not use for one-off tasks.
---

# Codex Flowkiln

Turn repeating requests in local Codex history into DRAFT skills you can review.
This skill is a skeleton: it knows HOW to mine and promote, but contains NO
business-specific workflow. You (the model) supply judgment on what is worth
keeping.

## Where things live

- Engine: `scripts/wfminer.py` (Python standard library only, no pip install)
- Data: `~/.codex-flowkiln/` (prompts.jsonl, candidates.json)
- Default promotion target: `~/.agents/skills/<name>/SKILL.md`
  NOTE: Codex discovers skills from `.agents/skills/`, NOT `.codex/skills/`.

## Stage 1 - collect

Optional. A hook template is provided at `hooks/hooks.json` to record prompts
automatically. It redacts secrets and writes to `prompts.jsonl`. If no hook is
installed, you can seed the file manually in the same JSONL shape:

    {"ts":"2026-01-01T00:00:00Z","event":"UserPromptSubmit","session_id":"s1","cwd":"...","tool":"","text":"the user prompt"}

Never store raw secrets. The engine redacts on write, but you should too.

## Stage 2 - mine

Run offline. Deterministic. No model tokens, no network.

    python scripts/wfminer.py mine --threshold 0.72 --min-count 3

This writes `~/.codex-flowkiln/candidates.json`. Inspect with:

    python scripts/wfminer.py mine --json

A candidate is a cluster of similar prompts with: count, shared terms,
variable slots, tool signatures, and up to 5 sample prompts.

## Stage 3 - promote (ALWAYS review first)

Dry-run by default - prints where a SKILL.md WOULD go, writes nothing:

    python scripts/wfminer.py promote <candidate-id>

Only after you have read the candidate and decided it is genuinely reusable:

    python scripts/wfminer.py promote <candidate-id> --apply

Generated skills contain `[TODO: ...]` markers and a provenance comment. A
generated skill is a SKELETON, not a vetted procedure. Before telling the user
it is ready:

1. Read the generated SKILL.md in full.
2. Replace every TODO with the real, verified steps.
3. Check the `description` states both what it does and when to trigger.
4. Confirm the frontmatter `name` matches the directory name.
5. Tell the user a file was written and where (as-is disclosure).

Existing target files are never overwritten.

## Guardrails

- Never run `--apply` without the user explicitly asking to create the skill.
- Never promote a candidate whose samples contain secrets or private data.
- Repeated does not mean important: a frequent one-liner may not deserve a skill.
- If a candidate is vague, ask the user instead of guessing the workflow.

## Verify the engine itself

    python scripts/wfminer.py selftest

Expect "25/25 checks passed". If it fails, do not trust the output.
