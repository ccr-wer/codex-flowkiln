#!/usr/bin/env python3
"""Codex Flowkiln - skeleton.

A local, standard-library-only engine that turns repeated Codex prompts into
installable skill candidates. Three stages, no business workflow hard-coded:

    collect  : stdin JSON (Codex hook) -> jsonl, with secret redaction
    mine     : jsonl -> candidate clusters (rules + TF-IDF cosine, offline)
    promote  : one candidate -> SKILL.md (DRY-RUN by default; --apply writes)
    selftest : synthetic end-to-end run, exits non-zero on failure

No third-party dependencies. Windows / macOS / Linux. Python 3.8+.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VERSION = "0.1.1-skeleton"

# ---------------------------------------------------------------------------
# Paths (user scope by default, so the worktree stays clean)
# ---------------------------------------------------------------------------

def data_dir() -> Path:
    override = os.environ.get("WFM_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return (Path.home() / ".codex-flowkiln").expanduser()


def prompts_path() -> Path:
    return data_dir() / "prompts.jsonl"


def candidates_path() -> Path:
    return data_dir() / "candidates.json"


def dismissed_path() -> Path:
    return data_dir() / "dismissed.json"


# ---------------------------------------------------------------------------
# Redaction - never persist secrets even from a trusted local hook
# ---------------------------------------------------------------------------

SECRET_RULES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "<redacted:openai-key>"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "<redacted:github-pat>"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{12,}"), "Bearer <redacted>"),
    (re.compile(r"(?i)\b(api[-_]?key|secret|token)\s*[:=]\s*\S+"), r"\1=<redacted>"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<redacted:email>"),
]


def redact(text: str) -> str:
    for pattern, replacement in SECRET_RULES:
        text = pattern.sub(replacement, text)
    return text


# ---------------------------------------------------------------------------
# collect : hook stdin -> jsonl
# ---------------------------------------------------------------------------

def collect(raw: str, event: Optional[str] = None) -> Dict[str, Any]:
    """Parse one Codex hook payload from stdin and append a redacted record.

    Returns a hook stdout payload. Codex hook contract: stdout JSON is parsed
    as directives; plain text is added as context. We stay silent by default
    (suppressOutput) so mining never pollutes the model context.
    """
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}

    hook_event = event or payload.get("hook_event_name") or "unknown"
    text = ""
    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input")

    if isinstance(tool_input, dict):
        # UserPromptSubmit carries the prompt here or in a prompt field.
        for key in ("prompt", "message", "input", "command", "query"):
            if isinstance(tool_input.get(key), str):
                text = tool_input[key]
                break
        if not text:
            text = json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
    elif isinstance(tool_input, str):
        text = tool_input
    if not text and isinstance(payload.get("prompt"), str):
        text = payload["prompt"]
    if not text and isinstance(payload.get("text"), str):
        text = payload["text"]

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": hook_event,
        "session_id": payload.get("session_id", ""),
        "cwd": payload.get("cwd", ""),
        "tool": tool_name,
        "text": redact(text)[:4000],
    }

    target = prompts_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    return {"continue": True, "suppressOutput": True}


def read_prompts(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    path = path or prompts_path()
    out: List[Dict[str, Any]] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # tolerate a torn append
    return out


# ---------------------------------------------------------------------------
# review : the human decides. list -> user says keep/dismiss -> act.
# A dismissed cluster is fingerprinted so the same workflow does not come back.
# ---------------------------------------------------------------------------

def read_dismissed() -> List[Dict[str, Any]]:
    path = dismissed_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def write_dismissed(entries: List[Dict[str, Any]]) -> Path:
    out = dismissed_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def is_dismissed(terms: List[str], dismissed: List[Dict[str, Any]]) -> bool:
    """A cluster is dismissed if its skeleton terms largely overlap a rejection.

    Fuzzy on purpose: the same workflow may pick up one extra term next time,
    so we require >=75% of the smaller term set to match.
    """
    cur = {t for t in terms if t}
    for entry in dismissed:
        seen = set(entry.get("fingerprint_terms", []))
        if not seen:
            continue
        overlap = len(cur & seen)
        if overlap >= max(1, int(0.75 * min(len(cur), len(seen)))):
            return True
    return False


def filter_dismissed(candidates: List[Dict[str, Any]],
                     dismissed: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    kept, skipped = [], 0
    for c in candidates:
        if is_dismissed(c.get("skeleton_terms", []), dismissed):
            skipped += 1
        else:
            kept.append(c)
    return kept, skipped


def dismiss(candidates: List[Dict[str, Any]], wanted: str,
            reason: str = "") -> Dict[str, Any]:
    """Remove one candidate from the list and remember the rejection.

    The fingerprint (skeleton terms) is what keeps it from reappearing on the
    next mine run - simply deleting it would just bring it back.
    """
    hit = next((c for c in candidates if c.get("id") == wanted), None)
    if hit is None:
        raise SystemExit(f"candidate not found: {wanted}")
    kept = [c for c in candidates if c.get("id") != wanted]
    kept_path = candidates_path()
    kept_path.parent.mkdir(parents=True, exist_ok=True)
    kept_path.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")

    entries = read_dismissed()
    entries.append({
        "id": hit["id"],
        "fingerprint_terms": hit.get("skeleton_terms", []),
        "count": hit.get("count", 0),
        "reason": reason,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    write_dismissed(entries)
    return {
        "dismissed": hit["id"],
        "count": hit.get("count", 0),
        "reason": reason or "(none)",
        "remaining": len(kept),
        "note": "removed from the list; fingerprinted so it will not come back",
    }


def render_list(candidates: List[Dict[str, Any]]) -> str:
    """Human-readable review list. Meant to be pasted into the chat verbatim."""
    if not candidates:
        return "No candidates awaiting review."
    lines = [f"{len(candidates)} candidate(s) awaiting review:", ""]
    for i, c in enumerate(candidates, 1):
        terms = ", ".join(c.get("skeleton_terms", [])[:6]) or "(none)"
        slots = ", ".join(c.get("slots", [])) or "none detected"
        sample = (c.get("samples") or ["(no sample)"])[0]
        lines.append(f"[{i}] id={c['id']}  count={c['count']}")
        lines.append(f"    terms: {terms}")
        lines.append(f"    slots: {slots}")
        lines.append(f"    sample: {sample}")
        lines.append("")
    lines.append("Keep some? Promote those. Say no to any other -> dismiss it.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# mine : offline clustering, no model call, no network
# ---------------------------------------------------------------------------

TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]")
NOISE_RE = re.compile(
    r"(?i)(sk-[a-z0-9_-]+|ghp_[a-z0-9]+|https?://\S+|\b[0-9a-f]{8,}\b|"
    r"[a-z]:[\\/][^\s]+|/[^\s]+|\b\d+\b|\{[^}]*\})"
)


def tokenize(text: str) -> List[str]:
    """Normalize away paths/ids/numbers, keep stable signal tokens."""
    cleaned = NOISE_RE.sub(" ", text.lower())
    tokens = TOKEN_RE.findall(cleaned)
    # CJK single chars are weak signal: also emit adjacent bigrams.
    bigrams = [
        a + b for a, b in zip(tokens, tokens[1:])
        if "\u4e00" <= a <= "\u9fff" and "\u4e00" <= b <= "\u9fff"
    ]
    return tokens + bigrams


def _tfidf_vectors(docs: List[List[str]]):
    df: Counter = Counter()
    for toks in docs:
        df.update(set(toks))
    n = max(len(docs), 1)
    vectors: List[Dict[str, float]] = []
    for toks in docs:
        tf = Counter(toks)
        vec: Dict[str, float] = {}
        for token, count in tf.items():
            idf = math.log((n + 1) / (df[token] + 1)) + 1.0
            vec[token] = (1.0 + math.log(count)) * idf
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        vectors.append({k: v / norm for k, v in vec.items()})
    return vectors


def _cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def slots_from(text: str) -> List[str]:
    """Extract variable slots so a candidate is a workflow, not a transcript."""
    found = []
    for pattern, label in (
        (re.compile(r"[a-z]:[\\/][^\s]+", re.I), "path"),
        (re.compile(r"https?://\S+"), "url"),
        (re.compile(r"\b\d+\b"), "number"),
        (re.compile(r"[\w.-]+@[\w.-]+"), "email"),
    ):
        for _ in pattern.finditer(text):
            found.append(label)
    return sorted(set(found))


# Stopwords that carry no workflow signal in either language.
STOPWORDS = set("""
a an the and or to of in on for with is are please 把 和 与 在 给 把 这 那 我 你 他 的 了 并 然后 然后 按 把 用
""".split())

LATIN_RE = re.compile(r"[a-z][a-z0-9_+#.-]{2,}")
CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")


def keywords(text: str) -> set:
    """Extract content-bearing terms: latin words + CJK runs, minus stopwords."""
    found = set(LATIN_RE.findall(text.lower()))
    for run in CJK_RUN_RE.findall(text):
        # Use 2-grams as the common signal. A whole long CJK run
        # almost never repeats verbatim, so including it only inflates the union.
        for n in (2, 3):
            for i in range(len(run) - n + 1):
                found.add(run[i:i + n])
    return {w for w in found if w not in STOPWORDS and len(w) >= 2}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def mine(records: List[Dict[str, Any]], threshold: float = 0.72,
         min_count: int = 3, shared_keywords: int = 3) -> List[Dict[str, Any]]:
    """Cluster repeated prompts into skill candidates.

    Two independent similarity signals; either one linking two prompts is enough:
      1. TF-IDF cosine over normalized tokens (good for English / long prompts)
      2. Count of shared content keywords >= shared_keywords (good for short
         Chinese prompts, where ratio-based metrics get diluted by n-grams)
    Deterministic: same input -> same clusters -> same candidate names.
    """
    texts = [r.get("text", "") for r in records]
    docs = [tokenize(t) for t in texts]
    kws = [keywords(t) for t in texts]
    usable = [i for i in range(len(texts)) if len(docs[i]) >= 2 or len(kws[i]) >= 2]
    if len(usable) < min_count:
        return []

    vecs = _tfidf_vectors([docs[i] for i in usable])
    uf = _UnionFind(len(usable))
    for ai in range(len(usable)):
        for bi in range(ai + 1, len(usable)):
            cos = _cosine(vecs[ai], vecs[bi])
            shared = len(kws[usable[ai]] & kws[usable[bi]])
            if cos >= threshold or shared >= shared_keywords:
                uf.union(ai, bi)

    groups: Dict[int, List[int]] = defaultdict(list)
    for idx, real_i in enumerate(usable):
        groups[uf.find(idx)].append(real_i)

    candidates: List[Dict[str, Any]] = []
    for members in groups.values():
        if len(members) < min_count:
            continue
        samples = [texts[i] for i in members]
        # Shared content keywords become the skeleton (readable in zh and en).
        tf: Counter = Counter()
        for i in members:
            tf.update(kws[i])
        top = [w for w, _ in tf.most_common(8) if len(w) > 1]
        if not top:  # fallback for very short prompts
            for i in members:
                tf.update(docs[i])
            top = [w for w, _ in tf.most_common(8) if len(w) > 1]
        signatures = set()
        for i in members:
            signatures.add(records[i].get("tool", ""))
        candidates.append({
            "id": "wf-" + "_".join(top[:3]).replace("/", "-")[:40],
            "count": len(members),
            "signatures": sorted(s for s in signatures if s),
            "skeleton_terms": top,
            "slots": sorted({s for t in samples for s in slots_from(t)}),
            "samples": samples[:5],
            "first_ts": records[members[0]].get("ts", ""),
            "last_ts": records[members[-1]].get("ts", ""),
        })
    candidates.sort(key=lambda c: (-c["count"], c["id"]))
    return candidates


# ---------------------------------------------------------------------------
# promote : candidate -> SKILL.md, dry-run unless --apply
# ---------------------------------------------------------------------------

SKILL_TEMPLATE = """---
name: {name}
description: {description}
---

# {title}

Use this skill when the task matches the trigger below. Otherwise ignore it.

## When to use
{when}

## Skeleton steps
{steps}

## Variables
{variables}

## Verify
{verify}

<!--
Generated by codex-flowkiln {version} on {generated}.
Source: {count} similar prompts clustered from local Codex history.
REVIEW BEFORE USE: this is a skeleton distilled from observed prompts, not a
vetted procedure. Fill in the real steps, fix the trigger, delete anything wrong.
-->"""


def sanitize_name(raw: str) -> str:
    name = re.sub(r"[^a-z0-9-]", "-", raw.lower()).strip("-")
    name = re.sub(r"-{2,}", "-", name)[:64].strip("-")
    return name or "repeated-workflow"


def render_skill(candidate: Dict[str, Any]) -> Tuple[str, str]:
    name = sanitize_name(candidate["id"])
    terms = candidate.get("skeleton_terms", [])
    trigger = " / ".join(terms[:4]) if terms else "repeated task"
    description = (
        "Reusable workflow for repeated tasks involving " + trigger + ". "
        "Use when the request matches these terms and the listed tool calls; "
        "do not use for unrelated one-off tasks."
    )
    steps = "\n".join(
        f"{i}. [TODO: describe step observed around '{t}' in the source prompts]"
        for i, t in enumerate(terms[:6], 1)
    ) or "1. [TODO: describe the repeated operation]"
    variables = ", ".join(candidate.get("slots", [])) or "none detected"
    tools = ", ".join(candidate.get("signatures", [])) or "unspecified"
    when = f"Task mentions {trigger} and would otherwise be re-explained each time."
    verify = f"Re-run the workflow and confirm the same outcome. Observed tools: {tools}."
    body = SKILL_TEMPLATE.format(
        name=name,
        description=description.replace("\n", " ")[:1024],
        title=name,
        when=when,
        steps=steps,
        variables=variables,
        verify=verify,
        version=VERSION,
        generated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        count=candidate.get("count", 0),
    )
    return name, body


def promote(candidates: List[Dict[str, Any]], wanted: Optional[str],
            apply: bool, dest: Optional[Path]) -> List[Dict[str, Any]]:
    dest_root = dest or (Path.home() / ".agents" / "skills")
    selected = [c for c in candidates if wanted is None or c["id"] == wanted]
    if wanted and not selected:
        raise SystemExit(f"candidate not found: {wanted}")

    results = []
    for candidate in selected:
        name, body = render_skill(candidate)
        target_dir = dest_root / name
        target_file = target_dir / "SKILL.md"
        results.append({
            "id": candidate["id"],
            "name": name,
            "target": str(target_file),
            "applied": False,
            "reason": "dry-run (pass --apply to write)" if not apply else "",
        })
        if apply:
            if target_file.exists():
                results[-1]["reason"] = "skipped: target already exists"
                continue
            target_dir.mkdir(parents=True, exist_ok=True)
            target_file.write_text(body, encoding="utf-8")
            results[-1]["applied"] = True
            results[-1]["reason"] = "written; REVIEW before use"
    return results


# ---------------------------------------------------------------------------
# selftest : synthetic end-to-end, no external state
# ---------------------------------------------------------------------------

def selftest() -> int:
    checks: List[Tuple[str, bool]] = []

    def check(label: str, ok: bool) -> None:
        checks.append((label, bool(ok)))

    synthetic = [
        "rewrite the changelog for release v2 and push the tag",
        "rewrite the changelog for release v3 and push the tag",
        "please rewrite the changelog for release v4 and push the tag",
        "rewrite the changelog for release v5 and push the tag",
        "fix the flaky test in CI then run the full build suite",
        "fix the flaky test in CI then run the full build suite again",
        "fix that flaky CI test and run the full build suite",
        "fix the flaky test in CI then run the full build suite",
        "one-off: what is the weather in Tokyo right now",
    ]
    with tempfile.TemporaryDirectory() as td:
        os.environ["WFM_DATA_DIR"] = td
        for i, text in enumerate(synthetic):
            payload = json.dumps({
                "hook_event_name": "UserPromptSubmit",
                "session_id": f"test-{i}",
                "cwd": td,
                "tool_input": {"prompt": text},
            })
            out = collect(payload)
            check(f"collect[{i}] suppressed", out.get("suppressOutput") is True)

        records = read_prompts()
        check("collect wrote all records", len(records) == len(synthetic))
        check("redaction keeps clean text", "weather" in records[-1]["text"])

        secrets_payload = json.dumps({
            "hook_event_name": "UserPromptSubmit",
            "tool_input": {"prompt": "use key sk-abcdefghijklmnopqrstuvwxyz and ghp_abcdefghijklmnopqrstuvwxyz"},
        })
        collect(secrets_payload)
        last = read_prompts()[-1]["text"]
        check("redacts openai key", "sk-abcdefghijklmnopqrstuvwxyz" not in last)
        check("redacts github pat", "ghp_abcdefghijklmnopqrstuvwxyz" not in last)

        records = read_prompts()
        candidates = mine(records, threshold=0.70, min_count=3)
        check("found at least one cluster", len(candidates) >= 1)
        check("candidate has count", bool(candidates) and candidates[0]["count"] >= 3)
        check("candidate has skeleton", bool(candidates) and bool(candidates[0]["skeleton_terms"]))

        dest = Path(td) / "out" / "skills"
        preview = promote(candidates, None, apply=False, dest=dest)
        check("dry-run writes nothing", not any(r["applied"] for r in preview))
        check("dry-run reports target", bool(preview) and preview[0]["target"].endswith("SKILL.md"))

        applied = promote(candidates[:1], None, apply=True, dest=dest)
        check("apply writes file", bool(applied) and applied[0]["applied"])
        written = Path(applied[0]["target"])
        check("file exists", written.exists())
        if written.exists():
            content = written.read_text(encoding="utf-8")
            check("frontmatter name matches dir", f"name: {written.parent.name}" in content)
            check("has description", "description:" in content)
            check("has TODO markers", "[TODO:" in content)
        again = promote(candidates[:1], None, apply=True, dest=dest)
        check("no overwrite on re-apply", again[0]["reason"].startswith("skipped"))

        second = mine(records, threshold=0.70, min_count=3)
        check("deterministic mining", [c["id"] for c in second] == [c["id"] for c in candidates])

        # review loop: the list goes to the user; a "no" must erase and stay erased
        check("render_list names every id",
              bool(candidates) and all(c["id"] in render_list(candidates) for c in candidates))
        target = candidates[0]["id"]
        result = dismiss(candidates, target, reason="user said no")
        check("dismiss reports removal", result["dismissed"] == target)
        check("dismiss shrinks the list", result["remaining"] == len(candidates) - 1)
        left = read_candidates()
        check("dismissed id gone from list", all(c["id"] != target for c in left))
        entries = read_dismissed()
        check("dismiss records fingerprint",
              bool(entries) and entries[-1]["id"] == target and entries[-1]["fingerprint_terms"])
        filtered, skipped = filter_dismissed(candidates, entries)
        check("filter drops dismissed cluster", skipped == 1)
        check("filter keeps the rest", len(filtered) == len(candidates) - 1)
        resurrected, skipped2 = filter_dismissed(
            mine(records, threshold=0.70, min_count=3), read_dismissed())
        check("dismissed cluster stays gone", skipped2 == 1 and all(c["id"] != target for c in resurrected))

        os.environ.pop("WFM_DATA_DIR", None)

    passed = sum(1 for _, ok in checks if ok)
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="wfminer", description=__doc__)
    subs = parser.add_subparsers(dest="cmd", required=True)

    p_collect = subs.add_parser("collect", help="read one Codex hook payload from stdin")
    p_collect.add_argument("--event", help="override hook_event_name")

    p_mine = subs.add_parser("mine", help="cluster prompts into candidates")
    p_mine.add_argument("--threshold", type=float, default=0.72,
                        help="TF-IDF cosine threshold (English/long prompts)")
    p_mine.add_argument("--min-count", type=int, default=3,
                        help="min prompts per cluster")
    p_mine.add_argument("--shared-keywords", type=int, default=3,
                        help="shared content keywords needed to link (short/zh prompts)")
    p_mine.add_argument("--json", action="store_true", help="print candidates as JSON")

    p_promote = subs.add_parser("promote", help="render a candidate as SKILL.md")
    p_promote.add_argument("id", nargs="?", help="candidate id; omit for all")
    p_promote.add_argument("--apply", action="store_true", help="actually write (default: dry-run)")
    p_promote.add_argument("--dest", help="skills root (default: ~/.agents/skills)")

    p_list = subs.add_parser("list", help="print the review list for the chat")
    p_list.add_argument("--json", action="store_true", help="print as JSON")

    p_dismiss = subs.add_parser("dismiss", help="drop a candidate after the user says no")
    p_dismiss.add_argument("id", help="candidate id to remove")
    p_dismiss.add_argument("--reason", default="", help="why the user rejected it")

    subs.add_parser("selftest", help="synthetic end-to-end verification")
    subs.add_parser("version")

    args = parser.parse_args(argv)

    if args.cmd == "version":
        print(VERSION)
        return 0
    if args.cmd == "collect":
        print(json.dumps(collect(sys.stdin.read(), args.event), ensure_ascii=False))
        return 0
    if args.cmd == "selftest":
        return selftest()
    if args.cmd == "mine":
        records = read_prompts()
        raw = mine(records, args.threshold, args.min_count, args.shared_keywords)
        kept, skipped = filter_dismissed(raw, read_dismissed())
        out = candidates_path()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.json:
            print(json.dumps(kept, ensure_ascii=False, indent=2))
        else:
            print(f"{len(records)} prompts -> {len(kept)} candidate(s)")
            if skipped:
                print(f"skipped {skipped} dismissed cluster(s)")
            for c in kept:
                print(f"  {c['id']}  count={c['count']}  terms={','.join(c['skeleton_terms'][:4])}")
        return 0
    if args.cmd == "list":
        candidates = read_candidates()
        if args.json:
            print(json.dumps(candidates, ensure_ascii=False, indent=2))
        else:
            print(render_list(candidates))
        return 0
    if args.cmd == "dismiss":
        result = dismiss(read_candidates(), args.id, args.reason)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "promote":
        candidates = read_candidates()
        dest = Path(args.dest) if args.dest else None
        results = promote(candidates, args.id, args.apply, dest)
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    return 2


def read_candidates() -> List[Dict[str, Any]]:
    path = candidates_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


if __name__ == "__main__":
    sys.exit(main())
