"""Local document extraction and source-local lesson coverage matching."""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path

import frontmatter
import tfidf

RUBRIC_VERSION = "m4-2026-11-01"
GATE_PROMPT = '''You are matching a lesson from a coding-session retrospective against ONE candidate document (a GitHub issue, a memory note or a written rule). Decide:

"covers": true only if someone following the candidate would NOT need the lesson written down again. The candidate states the same rule, even in different words or for a different project.
"covers": false if it is a different rule, even on the same topic (for example both about testing, but different rules), or only mentions the topic in passing.

Examples:
1. Lesson: "Write ~/ro.sh at the first router refusal in a session." Candidate (memory): "Router override: on the first refusal, write the per-session ~/ro-<id4>.sh script once and tell the user." -> {"covers": true} (same rule).
2. Lesson: "Filing a follow-up issue should include the failing command output." Candidate (issue, open): "Bug issues must include the exact failing command and its output in the body." -> {"covers": true} (the issue already requires it).
3. Lesson: "Verify every file:line citation in a fix spec before dispatching." Candidate (rule): "Mutation-test every new guard: delete it and expect red." -> {"covers": false} (both about verification, different rules).

LESSON:
<<A>>

CANDIDATE:
(<<S>>) <<C>>

Reply with ONLY a JSON object, no prose and no code fences:
{"covers": true|false}'''


def lesson_text(name: str, member_texts: list[str]) -> str:
    return " ".join([name, *member_texts])


def anchor_text(name: str, member_texts: list[str]) -> str:
    return "\n".join([name, *(f"- {text}" for text in member_texts[:5])])


def memory_docs(paths: list[str], errors: list) -> list[dict]:
    docs = []
    for filename in paths:
        path = Path(filename)
        if path.name == "MEMORY.md":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            errors.append({"path": str(path), "error": str(exc)})
            continue
        _, metadata, _, body, error = frontmatter.split_frontmatter(
            frontmatter.split_lines_lf_crlf(text))
        if error is None:
            values = {}
            for line in metadata:
                found = re.match(r"^(name|description):\s*(.*?)\s*$", line)
                if found:
                    value = found[2]
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1]
                    values[found[1]] = value
            text = " ".join(part for part in (
                values.get("name", ""), values.get("description", ""),
                "".join(body).strip()) if part)
        docs.append({"source": "memory", "ref": path.stem,
                     "text": text[:2000], "path": str(path)})
    return docs


def _outside_fences(path: Path):
    """Yield line numbers and text, with boundaries at blank lines and fences."""
    fenced = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            yield number, ""
        elif not fenced:
            yield number, line


def rule_bullets(path: Path) -> list[tuple[int, str]]:
    bullets = []
    start, parts = 0, []
    for number, line in _outside_fences(path):
        if line.startswith("- "):
            if parts:
                bullets.append((start, " ".join(parts)))
            start, parts = number, [line[2:].strip()]
        elif line.strip() and line[:1].isspace() and parts:
            parts.append(line.strip())
        else:
            if parts:
                bullets.append((start, " ".join(parts)))
            parts = []
    if parts:
        bullets.append((start, " ".join(parts)))
    return bullets


def rule_paragraphs(path: Path) -> list[tuple[int, str]]:
    paragraphs = []
    start, parts = 0, []
    for number, line in _outside_fences(path):
        if line.strip():
            if not parts:
                start = number
            parts.append(line.strip())
        else:
            text = " ".join(parts)
            if len(text) >= 80:
                paragraphs.append((start, text))
            parts = []
    text = " ".join(parts)
    if len(text) >= 80:
        paragraphs.append((start, text))
    return paragraphs


def rule_docs(home: Path, projects: list[str], workspace: Path,
              errors: list) -> list[dict]:
    files = [(home / ".claude/CLAUDE.md", rule_bullets)]
    for project in projects:
        if "/" in project or ".." in project or "\\" in project:
            continue
        path = workspace / project / "CLAUDE.md"
        if path.resolve().is_relative_to(workspace.resolve()):
            files.append((path, rule_bullets))
    files.extend((p, rule_paragraphs) for p in sorted(
        (home / ".claude/skills").glob("*/SKILL.md")))
    cache = home / ".claude/plugins/cache"
    for plugin in sorted(cache.glob("*/*")):
        if not plugin.is_dir():
            continue
        try:
            versions = [p for p in plugin.iterdir()
                        if re.fullmatch(r"\d+(\.\d+)*", p.name) and p.is_dir()]
        except OSError as exc:
            errors.append({"path": str(plugin), "error": str(exc)})
            continue
        if versions:
            latest = max(versions, key=lambda p: (tuple(map(int, p.name.split("."))), p.name))
            files.extend((p, rule_paragraphs) for p in sorted(
                latest.glob("skills/*/SKILL.md")))
    docs = []
    for path, reader in files:
        try:
            entries = reader(path)
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError) as exc:
            errors.append({"path": str(path), "error": str(exc)})
            continue
        try:
            ref = "~/" + str(path.relative_to(home))
        except ValueError:
            ref = str(path)
        docs.extend({"source": "rule", "ref": ref, "line": line,
                     "text": text[:2000]} for line, text in entries)
    return docs


def issue_docs(repo: str, issues: list[dict]) -> list[dict]:
    docs = []
    for issue in issues:
        if type(issue.get("number")) is not int:
            continue
        created = issue.get("createdAt") or issue.get("created_at")
        docs.append({"source": "issue", "ref": f"{repo}#{issue['number']}",
                     "text": ((issue.get("title") or "") + "\n" +
                              (issue.get("body") or ""))[:2000],
                     "created": created[:10] if created else None,
                     "state": issue.get("state", "")})
    return docs


def _vector(tokens: list[str], df: Counter, count: int) -> dict[str, float]:
    weights = {term: (1 + math.log(tf)) * (1 + math.log((count + 1) / (df[term] + 1)))
               for term, tf in Counter(tokens).items()}
    norm = math.sqrt(sum(weight * weight for weight in weights.values()))
    return {term: weight / norm for term, weight in weights.items()} if norm else {}


class Index:
    """One source's documents: idf over all of them, L2-normalised vectors and an inverted index.

    Build it once and rank many lessons against it. Ranking walks only the postings of the lesson's own
    terms, so a run over hundreds of lessons does not re-tokenise every document each time. ``view`` limits
    a lesson to some of the documents (its own repos, its own projects' rules) while idf stays that of the
    whole source.
    """

    def __init__(self, docs: list[dict]):
        self.docs = docs
        tokens = [tfidf._tokenize_for_tfidf(doc["text"]) for doc in docs]
        self.df = Counter(term for document in tokens for term in set(document))
        self.postings: dict[str, list[tuple[int, float]]] = {}
        for i, document in enumerate(tokens):
            for term, weight in _vector(document, self.df, len(docs)).items():
                self.postings.setdefault(term, []).append((i, weight))

    def view(self, allowed: "set[int]") -> "_View":
        return _View(self, allowed)

    def top(self, query_tokens: list[str], allowed: "set[int] | None" = None) -> tuple[dict, float] | None:
        """The highest-scoring document (lowest ref on a tie), or None when every score is 0."""
        scores: dict[int, float] = {}
        for term, weight in _vector(query_tokens, self.df, len(self.docs)).items():
            for i, doc_weight in self.postings.get(term, ()):
                if allowed is None or i in allowed:
                    scores[i] = scores.get(i, 0.0) + weight * doc_weight
        ranked = [(score, i) for i, score in scores.items() if score > 0]
        if not ranked:
            return None
        score, i = min(ranked, key=lambda r: (-r[0], self.docs[r[1]]["ref"], r[1]))
        return self.docs[i], score


class _View:
    def __init__(self, index: Index, allowed: "set[int]"):
        self.index, self.allowed = index, allowed

    def top(self, query_tokens: list[str]) -> tuple[dict, float] | None:
        return self.index.top(query_tokens, self.allowed)


def best_candidate(query: str, sources: "dict[str, list[dict] | Index | _View]") -> tuple[dict, float] | None:
    """The highest-cosine document across sources; a source may be a list of documents, an Index or a view."""
    query_tokens = tfidf._tokenize_for_tfidf(query)
    best, best_score, best_key = None, 0.0, None
    for source, docs in sources.items():
        found = (docs if hasattr(docs, "top") else Index(docs)).top(query_tokens)
        if found is None:
            continue
        doc, score = found
        key = (source, doc["ref"])
        if score > best_score or (score == best_score and key < best_key):
            best, best_score, best_key = doc, score, key
    return (best, best_score) if best is not None else None


def parse_gate(stdout: str | None) -> bool | None:
    if stdout is None or "{" not in stdout:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(stdout[stdout.index("{"):])
    except ValueError:
        return None
    covers = value.get("covers") if isinstance(value, dict) else None
    return covers if isinstance(covers, bool) else None


def gate(anchor: str, candidate: dict, runner) -> bool | None:
    source = candidate["source"]
    if source == "issue" and candidate.get("state") in ("OPEN", "CLOSED"):
        source += ", " + candidate["state"].lower()
    fill = {"<<A>>": anchor, "<<S>>": source, "<<C>>": candidate["text"][:800]}
    # One pass: text that looks like a placeholder inside the lesson or candidate stays as it is.
    prompt = re.sub("<<[ASC]>>", lambda m: fill[m.group()], GATE_PROMPT)
    for _ in range(2):
        verdict = parse_gate(runner(prompt))
        if verdict is not None:
            return verdict
    return None


def short_hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:12]
