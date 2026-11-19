---
name: codex-flowkiln
description: Mine repeated Codex prompts into reusable skill candidates and present them for in-chat review. Use when the user asks to find repeated workflows, distill a habit into a skill, or review what tasks keep recurring. The user must approve every promotion and every dismissal; never auto-promote or auto-dismiss. Do not use for one-off tasks.
---

# Codex Flowkiln

Turn repeating requests in local Codex history into DRAFT skills, then let the
human decide — in chat, not in a file. This skill is a skeleton: it knows HOW to
mine, list, dismiss and promote, but contains NO business-specific workflow.

## Where things live

- Engine: `scripts/wfminer.py` (Python standard library only, no pip install)
- Data: `~/.codex-flowkiln/` (prompts.jsonl, candidates.json, dismissed.json)
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

This writes `~/.codex-flowkiln/candidates.json`. Clusters the user has already
dismissed are skipped automatically and reported as
`skipped N dismissed cluster(s)`.

## Stage 3 - put the list in chat (MANDATORY)

    python scripts/wfminer.py list

Paste this list into the conversation for the user to judge. Requirements:

- Show every candidate: id, count, keywords, sample. Do not pre-filter.
- Present in the user's language, but keep `id` and `count` values verbatim.
- Say plainly what each cluster looks like and what a skill from it would do.
- Then ASK which ones to keep and which to drop. Stop and wait.

Do not promote, dismiss, or edit anything before the user answers. Silence is
not approval.

## Stage 4 - act on the verdict

If the user wants a candidate kept:

    python scripts/wfminer.py promote <id>              # dry-run: prints target path
    python scripts/wfminer.py promote <id> --apply      # only after explicit approval

If the user says no to a candidate, DROP IT from the list:

    python scripts/wfminer.py dismiss <id> --reason "why the user rejected it"

Then tell the user it is removed and will not reappear. Do not quietly skip
dismissal — a rejection must be recorded, or the same cluster returns on the
next mine run.

Mixed answers are normal: promote some, dismiss others, leave undecided ones
alone. Never replace a user verdict with your own judgement.

## After promotion

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
- Never dismiss a candidate without the user saying no to it.
- Never skip Stage 3: the list lives in the chat, not in a file the user must dig up.
- Repeated does not mean important: a frequent one-liner may not deserve a skill.
- If a candidate is vague, ask the user instead of guessing the workflow.
- Never promote a candidate whose samples contain secrets or private data.

## Verify the engine itself

    python scripts/wfminer.py selftest

Expect every check to report PASS (currently 33/33). If any FAIL, do not trust
the output.
