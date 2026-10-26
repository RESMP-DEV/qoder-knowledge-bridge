#!/usr/bin/env python3
"""Read-only access to Qoder Repo Wiki and knowledge-card databases.

The module is shared by the qkb CLI and the plugin hooks. It never opens a
knowledge database for writing: every SQLite connection uses mode=ro and
PRAGMA query_only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import urllib.parse
from collections import Counter, defaultdict, deque
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

PLUGIN_NAME = "qoder-knowledge-bridge"
VERSION = "0.1.0"
DEFAULT_CONTEXT_BUDGET = 16000
DEFAULT_MAX_CHARS = 30000

WORD_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+\-]{1,}")
QKB_CMD_RE = re.compile(r"(?:^|[\s;&|()])(?:\S*/)?qkb(?:\s|$)")
STOP_WORDS = {
    "the", "and", "for", "with", "from", "that", "this", "into", "over",
    "under", "about", "when", "where", "what", "how", "why", "are", "is",
    "was", "were", "be", "been", "has", "have", "had", "not", "but", "all",
    "any", "can", "could", "should", "would", "will", "just", "use", "using",
    "used", "via", "per", "than", "then", "also", "more", "most", "less",
    "least", "very", "only", "your", "you", "our", "their", "its", "it",
    "they", "them", "there", "here", "which", "while", "does", "did", "get",
}


class QkbError(Exception):
    """User-facing qkb error."""


def qoder_home() -> Path:
    return Path(os.environ.get("QODER_HOME", "~/.qoder")).expanduser()


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def human_chars(count: int) -> str:
    if count >= 1000000:
        return "{:.1f}M".format(count / 1000000.0)
    if count >= 1000:
        return "{:.1f}k".format(count / 1000.0)
    return str(count)


def clean_markdown(text: str) -> str:
    text = re.sub(r"<cite>.*?</cite>", "", text or "", flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def strip_toc(text: str) -> str:
    return re.sub(r"(?ms)^##\s+Table of Contents\s*\n.*?(?=^##\s)", "", text or "").strip()


def demote_headings(text: str, levels: int = 2) -> str:
    def replace(match: "re.Match") -> str:
        depth = min(6, len(match.group(1)) + levels)
        return "#" * depth + " "

    return re.sub(r"(?m)^(#{1,6})\s+", replace, text or "")


def real_path(path: Path) -> str:
    try:
        return str(path.expanduser().resolve())
    except OSError:
        return str(path.expanduser().absolute())


def tokenize(query: str) -> List[str]:
    terms: List[str] = []
    for word in WORD_RE.findall(query or ""):
        word = word.lower().strip(".-")
        if len(word) < 2 or word in STOP_WORDS or word in terms:
            continue
        terms.append(word)
    return terms[:10]


def plain_text(text: str) -> str:
    text = text or ""
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"<cite>.*?</cite>", " ", text, flags=re.S)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^[#>*+\-]+\s*", "", text, flags=re.M)
    text = re.sub(r"[*~|]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def make_snippet(text: str, terms: Sequence[str], width: int = 280) -> str:
    plain = plain_text(text)
    if not plain:
        return ""
    lowered = plain.lower()
    positions = [lowered.find(term) for term in terms]
    positions = [pos for pos in positions if pos >= 0]
    pos = min(positions) if positions else 0
    start = max(0, pos - 80)
    end = min(len(plain), start + width)
    snippet = plain[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(plain):
        snippet = snippet + "..."
    return snippet


def compact_json(value: Any, limit: int = 600) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = str(value)
    return re.sub(r"\s+", " ", text)[:limit]


# ---------------------------------------------------------------------------
# SQLite access


def connect_ro(path: Path) -> sqlite3.Connection:
    quoted = urllib.parse.quote(str(path), safe="/")
    conn = sqlite3.connect("file:{}?mode=ro".format(quoted), uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def db_meta(conn: sqlite3.Connection) -> Dict[str, str]:
    if not table_exists(conn, "db_meta"):
        return {}
    return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM db_meta")}


@dataclass
class Snapshot:
    partition_id: int
    branch: str
    locale: str
    snapshot_id: str
    baseline_commit: str
    published: bool
    count: int
    chars: int
    state: str = ""
    stage: str = ""
    updated_at: str = ""


@dataclass
class KnowledgeBase:
    root: Path
    name: str
    owner: Optional[str]
    wiki_db: Optional[Path]
    cards_db: Optional[Path]
    wiki: Optional[Snapshot] = None
    cards: Optional[Snapshot] = None

    @property
    def primary(self) -> Optional[Snapshot]:
        return self.wiki or self.cards

    @property
    def baseline(self) -> str:
        return (self.wiki.baseline_commit if self.wiki else "") or (
            self.cards.baseline_commit if self.cards else ""
        )


def pick_snapshot(db: Optional[Path], table: str) -> Optional[Snapshot]:
    if not db or not db.exists():
        return None
    with closing(connect_ro(db)) as conn:
        if not table_exists(conn, table) or not table_exists(conn, "partition"):
            return None
        partitions = conn.execute(
            "SELECT id, branch, locale, published_snapshot_id, baseline_commit, "
            "last_accessed_at FROM partition ORDER BY last_accessed_at DESC, id DESC"
        ).fetchall()
        last_runs: Dict[int, sqlite3.Row] = {}
        if table_exists(conn, "last_run"):
            for row in conn.execute(
                "SELECT partition_id, snapshot_id, target_commit, state, stage, updated_at FROM last_run"
            ):
                last_runs[row["partition_id"]] = row
        for part in partitions:
            candidates: List[str] = []
            published = part["published_snapshot_id"] or ""
            if published:
                candidates.append(published)
            last_run = last_runs.get(part["id"])
            if last_run and last_run["snapshot_id"]:
                candidates.append(last_run["snapshot_id"])
            top = conn.execute(
                "SELECT snapshot_id FROM {} WHERE partition_id=? GROUP BY snapshot_id "
                "ORDER BY COUNT(*) DESC LIMIT 1".format(table),
                (part["id"],),
            ).fetchone()
            if top and top["snapshot_id"]:
                candidates.append(top["snapshot_id"])
            seen = set()
            for snapshot_id in candidates:
                if snapshot_id in seen:
                    continue
                seen.add(snapshot_id)
                stats = conn.execute(
                    "SELECT COUNT(*) AS c, COALESCE(SUM(LENGTH(content)), 0) AS chars "
                    "FROM {} WHERE partition_id=? AND snapshot_id=?".format(table),
                    (part["id"], snapshot_id),
                ).fetchone()
                if not stats or stats["c"] == 0:
                    continue
                baseline = part["baseline_commit"] or ""
                state = ""
                stage = ""
                updated = part["last_accessed_at"] or ""
                if last_run and last_run["snapshot_id"] == snapshot_id:
                    baseline = baseline or (last_run["target_commit"] or "")
                    state = last_run["state"] or ""
                    stage = last_run["stage"] or ""
                    updated = last_run["updated_at"] or updated
                return Snapshot(
                    partition_id=part["id"],
                    branch=part["branch"] or "",
                    locale=part["locale"] or "",
                    snapshot_id=snapshot_id,
                    baseline_commit=baseline,
                    published=snapshot_id == published,
                    count=stats["c"],
                    chars=stats["chars"] or 0,
                    state=state,
                    stage=stage,
                    updated_at=updated,
                )
        row = conn.execute(
            "SELECT partition_id, snapshot_id, COUNT(*) AS c, "
            "COALESCE(SUM(LENGTH(content)), 0) AS chars FROM {} "
            "GROUP BY partition_id, snapshot_id ORDER BY c DESC LIMIT 1".format(table)
        ).fetchone()
        if not row:
            return None
        part = conn.execute(
            "SELECT branch, locale, published_snapshot_id, baseline_commit, "
            "last_accessed_at FROM partition WHERE id=?",
            (row["partition_id"],),
        ).fetchone()
        return Snapshot(
            partition_id=row["partition_id"],
            branch=part["branch"] if part else "",
            locale=part["locale"] if part else "",
            snapshot_id=row["snapshot_id"] or "",
            baseline_commit=(part["baseline_commit"] if part else "") or "",
            published=bool(part and part["published_snapshot_id"] == row["snapshot_id"]),
            count=row["c"],
            chars=row["chars"] or 0,
            updated_at=(part["last_accessed_at"] if part else "") or "",
        )


def kb_dir_name(name: str) -> str:
    value = re.sub(r"^knowledge-", "", name)
    value = re.sub(r"-[0-9a-f]{8,}$", "", value)
    return value or name


def read_owner(*dbs: Optional[Path]) -> Optional[str]:
    for db in dbs:
        if not db or not db.exists():
            continue
        try:
            with closing(connect_ro(db)) as conn:
                owner = db_meta(conn).get("owner_tag")
                if owner:
                    return owner
        except sqlite3.Error:
            continue
    return None


def discover(home: Optional[Path] = None) -> List[KnowledgeBase]:
    root = (home or qoder_home()) / "qoder-knowledge"
    if not root.is_dir():
        return []
    kbs: List[KnowledgeBase] = []
    for directory in sorted(root.glob("knowledge-*")):
        if not directory.is_dir():
            continue
        wiki_db = directory / "wiki" / "wiki.v1.db"
        cards_db = directory / "cards" / "cards.v1.db"
        wiki_db = wiki_db if wiki_db.is_file() else None
        cards_db = cards_db if cards_db.is_file() else None
        if not wiki_db and not cards_db:
            continue
        kb = KnowledgeBase(
            root=directory,
            name=kb_dir_name(directory.name),
            owner=read_owner(wiki_db, cards_db),
            wiki_db=wiki_db,
            cards_db=cards_db,
        )
        kb.wiki = pick_snapshot(kb.wiki_db, "section")
        kb.cards = pick_snapshot(kb.cards_db, "card")
        kbs.append(kb)
    return kbs


def kb_has_content(kb: KnowledgeBase) -> bool:
    return bool((kb.wiki and kb.wiki.count) or (kb.cards and kb.cards.count))


def match_kb(kbs: List[KnowledgeBase], project: Optional[str], require: bool = False) -> Optional[KnowledgeBase]:
    target = Path(project).expanduser() if project else Path.cwd()
    root = real_path(Path(git_root(target)))
    scored: List[Tuple[int, KnowledgeBase]] = []
    for kb in kbs:
        if not kb.owner:
            continue
        owner = real_path(Path(kb.owner))
        if root == owner:
            score = 100
        elif root.startswith(owner.rstrip(os.sep) + os.sep):
            score = 80 + owner.count(os.sep)
        elif owner.startswith(root.rstrip(os.sep) + os.sep):
            score = 60 + root.count(os.sep)
        elif Path(owner).name == Path(root).name:
            score = 40
        else:
            continue
        score += 5 if kb.wiki and kb.wiki.count else 0
        score += 3 if kb.cards and kb.cards.count else 0
        scored.append((score, kb))
    if not scored:
        if require:
            available = ", ".join(sorted(kb.name for kb in kbs)) or "none"
            raise QkbError(
                "no knowledge base matches {}. Available: {}. "
                "Use --project to choose one.".format(root, available)
            )
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        names = ", ".join(kb.name for _, kb in scored[:5])
        raise QkbError(
            "ambiguous knowledge base for {}. Candidates: {}. "
            "Use --project with a more specific path.".format(root, names)
        )
    return scored[0][1]


# ---------------------------------------------------------------------------
# Git and memory context


def run_command(args: Sequence[str], cwd: Optional[Path] = None) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            list(args),
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=8,
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def git_root(path: Path) -> str:
    code, out, _ = run_command(["git", "-C", str(path), "rev-parse", "--show-toplevel"])
    return out if code == 0 and out else real_path(path)


def git_info(project: Optional[str]) -> Dict[str, Any]:
    base = Path(project).expanduser() if project else Path.cwd()
    root = Path(git_root(base))
    code, head, _ = run_command(["git", "-C", str(root), "rev-parse", "--short=12", "HEAD"])
    _, branch, _ = run_command(["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"])
    status_code, status, _ = run_command(["git", "-C", str(root), "status", "--porcelain"])
    _, log, _ = run_command(["git", "-C", str(root), "log", "-5", "--oneline"])
    is_git = code == 0 and bool(head)
    return {
        "root": str(root),
        "is_git": is_git,
        "head": head if is_git else "",
        "branch": branch if is_git else "",
        "dirty": len(status.splitlines()) if status_code == 0 else 0,
        "status": status.splitlines()[:20] if status_code == 0 else [],
        "log": log.splitlines() if is_git else [],
    }


def stale_info(kb: KnowledgeBase, project: Optional[str]) -> Dict[str, Any]:
    info = git_info(project or kb.owner)
    baseline = kb.baseline
    head = info["head"]
    if not baseline:
        return {"known": False, "stale": None, "baseline": "", "head": head}
    if not head:
        return {"known": False, "stale": None, "baseline": baseline, "head": ""}
    matches = baseline.startswith(head) or head.startswith(baseline[: len(head)])
    return {"known": True, "stale": not matches, "baseline": baseline, "head": head}


def project_slug(project: Optional[str]) -> str:
    path = real_path(Path(project).expanduser() if project else Path.cwd())
    return path.replace(os.sep, "-")


def memory_indexes(home: Path, project: Optional[str]) -> List[Tuple[str, Path]]:
    result: List[Tuple[str, Path]] = []
    user_index = home / "memory" / "MEMORY.md"
    if user_index.is_file():
        result.append(("user", user_index))
    project_index = home / "projects" / project_slug(project) / "memory" / "MEMORY.md"
    if project_index.is_file():
        result.append(("project", project_index))
    return result


def read_text(path: Path, limit: int = 4000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[:limit]


def memory_lines(
    home: Path, project: Optional[str], terms: Sequence[str], limit: int = 10
) -> List[str]:
    lines: List[str] = []
    for scope, path in memory_indexes(home, project):
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        matches: List[str] = []
        fallback: List[str] = []
        for raw in content.splitlines():
            line = raw.strip()
            if not line.startswith(("-", "*")):
                continue
            fallback.append(line)
            lowered = line.lower()
            if terms and any(term in lowered for term in terms):
                matches.append(line)
        if terms:
            chosen = matches[:limit]
        else:
            chosen = fallback[:5] if scope == "project" else []
        for line in chosen:
            lines.append("- [{}] {}".format(scope, line.lstrip("-* ")))
        if len(lines) >= limit:
            break
    return lines[:limit]


def memory_index_text(home: Path, project: Optional[str], search: str = "") -> str:
    terms = tokenize(search)
    blocks: List[str] = []
    for scope, path in memory_indexes(home, project):
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        sections: Dict[str, List[str]] = {}
        current_heading = "Overview"
        for raw in content.splitlines():
            line = raw.strip()
            if not line or line.startswith("<!--"):
                continue
            if line.startswith("#"):
                current_heading = line.lstrip("# ").strip() or "Overview"
                continue
            if terms and not any(term in line.lower() for term in terms):
                continue
            if line.startswith(("-", "*")):
                sections.setdefault(current_heading, []).append(line)
        if not sections:
            continue
        parts = ["## {} memory".format(scope.capitalize()), "Source: {}".format(path), ""]
        for heading, lines in sections.items():
            parts.append("### {}".format(heading))
            parts.extend(lines[:40])
            parts.append("")
        blocks.append("\n".join(parts).strip())
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Knowledge content


def wiki_sections(kb: KnowledgeBase) -> List[Dict[str, Any]]:
    if not kb.wiki or not kb.wiki_db:
        return []
    with closing(connect_ro(kb.wiki_db)) as conn:
        rows = conn.execute(
            "SELECT id, parent_id, ordinal, title, content, meta FROM section "
            "WHERE partition_id=? AND snapshot_id=? ORDER BY ordinal, title",
            (kb.wiki.partition_id, kb.wiki.snapshot_id),
        )
        return [dict(row) for row in rows]


def card_rows(kb: KnowledgeBase) -> List[Dict[str, Any]]:
    if not kb.cards or not kb.cards_db:
        return []
    with closing(connect_ro(kb.cards_db)) as conn:
        rows = conn.execute(
            "SELECT id, card_type, title, content, meta FROM card "
            "WHERE partition_id=? AND snapshot_id=? ORDER BY card_type, title",
            (kb.cards.partition_id, kb.cards.snapshot_id),
        )
        return [dict(row) for row in rows]


def module_rows(kb: KnowledgeBase) -> List[Dict[str, Any]]:
    if not kb.cards or not kb.cards_db:
        return []
    with closing(connect_ro(kb.cards_db)) as conn:
        if not table_exists(conn, "module"):
            return []
        rows = conn.execute(
            "SELECT m.id, m.parent_id, m.title, m.ordinal, COUNT(mc.card_id) AS card_count "
            "FROM module m LEFT JOIN module_card mc ON mc.partition_id=m.partition_id "
            "AND mc.snapshot_id=m.snapshot_id AND mc.module_id=m.id "
            "WHERE m.partition_id=? AND m.snapshot_id=? "
            "GROUP BY m.id, m.parent_id, m.title, m.ordinal "
            "ORDER BY m.ordinal, m.title",
            (kb.cards.partition_id, kb.cards.snapshot_id),
        )
        return [dict(row) for row in rows]


def card_module(kb: KnowledgeBase, card_id: str) -> Optional[Dict[str, Any]]:
    if not kb.cards or not kb.cards_db:
        return None
    with closing(connect_ro(kb.cards_db)) as conn:
        if not (table_exists(conn, "module_card") and table_exists(conn, "module")):
            return None
        row = conn.execute(
            "SELECT m.id, m.title FROM module_card mc JOIN module m "
            "ON m.id=mc.module_id AND m.partition_id=mc.partition_id "
            "AND m.snapshot_id=mc.snapshot_id "
            "WHERE mc.partition_id=? AND mc.snapshot_id=? AND mc.card_id=? LIMIT 1",
            (kb.cards.partition_id, kb.cards.snapshot_id, card_id),
        ).fetchone()
        return dict(row) if row else None


def card_relations(kb: KnowledgeBase, card_id: str) -> List[Dict[str, Any]]:
    if not kb.cards or not kb.cards_db:
        return []
    with closing(connect_ro(kb.cards_db)) as conn:
        if not table_exists(conn, "relation"):
            return []
        rows = conn.execute(
            "SELECT r.a_id, r.b_id, r.kind, "
            "COALESCE(ta.title, '') AS a_title, COALESCE(tb.title, '') AS b_title "
            "FROM relation r "
            "LEFT JOIN card ta ON ta.id=r.a_id AND ta.partition_id=r.partition_id "
            "AND ta.snapshot_id=r.snapshot_id "
            "LEFT JOIN card tb ON tb.id=r.b_id AND tb.partition_id=r.partition_id "
            "AND tb.snapshot_id=r.snapshot_id "
            "WHERE r.partition_id=? AND r.snapshot_id=? AND (r.a_id=? OR r.b_id=?)",
            (kb.cards.partition_id, kb.cards.snapshot_id, card_id, card_id),
        )
        result = []
        for row in rows:
            other_id = row["b_id"] if row["a_id"] == card_id else row["a_id"]
            other_title = row["b_title"] if row["a_id"] == card_id else row["a_title"]
            result.append({"id": other_id, "title": other_title, "kind": row["kind"]})
        return result


def normalize_heading(value: str) -> str:
    value = (value or "").lower().replace("-", " ").replace("_", " ")
    return re.sub(r"\s+", " ", value).strip()


def display_title(title: str) -> str:
    if "-" in title or "_" in title:
        return title.replace("-", " ").replace("_", " ").title()
    return title


def strip_leading_heading(text: str, title: str, force: bool = False) -> str:
    lines = (text or "").splitlines()
    if lines and lines[0].lstrip().startswith("#"):
        heading = normalize_heading(lines[0].lstrip("# "))
        wanted = normalize_heading(title)
        if force or (wanted and (wanted in heading or heading in wanted)):
            return "\n".join(lines[1:]).lstrip()
    return text or ""


def resolve_item(items: Sequence[Dict[str, Any]], needle: str, kind: str) -> Dict[str, Any]:
    exact = [item for item in items if item["id"] == needle]
    if exact:
        return exact[0]
    prefix = [item for item in items if item["id"].startswith(needle)]
    if len(prefix) == 1:
        return prefix[0]
    if prefix:
        raise QkbError(
            "ambiguous {} id '{}': {}".format(
                kind, needle, ", ".join(item["id"][:12] for item in prefix[:6])
            )
        )
    by_title = [item for item in items if item["title"].lower() == needle.lower()]
    if len(by_title) == 1:
        return by_title[0]
    raise QkbError("{} not found: {}".format(kind, needle))


def section_tree(sections: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
    ids = {section["id"] for section in sections}
    roots: List[Dict[str, Any]] = []
    children: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for section in sections:
        parent = section["parent_id"] or ""
        if parent and parent in ids and parent != section["id"]:
            children[parent].append(section)
        else:
            roots.append(section)
    for values in children.values():
        values.sort(key=lambda item: (item["ordinal"], item["title"]))
    roots.sort(key=lambda item: (item["ordinal"], item["title"]))
    return roots, children


def walk_tree(
    roots: Sequence[Dict[str, Any]], children: Dict[str, List[Dict[str, Any]]]
) -> List[Tuple[int, Dict[str, Any]]]:
    output: List[Tuple[int, Dict[str, Any]]] = []
    seen = set()

    def visit(node: Dict[str, Any], depth: int) -> None:
        if node["id"] in seen:
            return
        seen.add(node["id"])
        output.append((depth, node))
        for child in children.get(node["id"], []):
            visit(child, depth + 1)

    for root in roots:
        visit(root, 0)
    return output


def page_bundle(kb: KnowledgeBase, needle: str, max_chars: int = DEFAULT_MAX_CHARS) -> Dict[str, Any]:
    sections = wiki_sections(kb)
    if not sections:
        raise QkbError("{} has no Repo Wiki pages for branch {}. Run a wiki build first.".format(
            kb.name, kb.primary.branch if kb.primary else "?"
        ))
    target = resolve_item(sections, needle, "page")
    _, children = section_tree(sections)
    parts: List[str] = []
    citations: List[str] = []
    seen = set()

    def visit(section: Dict[str, Any], depth: int) -> None:
        if section["id"] in seen:
            return
        seen.add(section["id"])
        citations.append(section["id"])
        content = strip_leading_heading(section["content"], section["title"])
        if depth == 0:
            if content.strip():
                parts.append(content.strip())
        else:
            parts.append("## " + section["title"].strip())
            if content.strip():
                parts.append(content.strip())
        for child in children.get(section["id"], []):
            visit(child, depth + 1)

    visit(target, 0)
    text = "\n\n".join(part for part in parts if part)
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars].rstrip() + "\n\n...[truncated by qkb]"
    return {
        "page": target,
        "text": text,
        "citations": citations,
        "truncated": truncated,
        "children": [child["id"] for child in children.get(target["id"], [])],
    }


def card_bundle(kb: KnowledgeBase, needle: str) -> Dict[str, Any]:
    cards = card_rows(kb)
    if not cards:
        raise QkbError("{} has no knowledge cards for branch {}. Run a knowledge build first.".format(
            kb.name, kb.cards.branch if kb.cards else "?"
        ))
    card = resolve_item(cards, needle, "card")
    return {
        "card": card,
        "module": card_module(kb, card["id"]),
        "relations": card_relations(kb, card["id"]),
    }


def score_text(title: str, content: str, terms: Sequence[str]) -> int:
    title_lower = (title or "").lower()
    content_lower = (content or "").lower()
    coverage = sum(1 for term in terms if term in title_lower or term in content_lower)
    title_hits = sum(title_lower.count(term) for term in terms)
    content_hits = sum(content_lower.count(term) for term in terms)
    if coverage == 0:
        return 0
    return coverage * 25 + min(title_hits * 8, 80) + min(content_hits, 40)


def search_kb(kb: KnowledgeBase, terms: Sequence[str], limit: int = 6) -> Dict[str, Any]:
    wiki_hits: List[Tuple[int, Dict[str, Any]]] = []
    for section in wiki_sections(kb):
        score = score_text(section["title"], section["content"], terms)
        if score:
            wiki_hits.append((score, section))
    card_hits: List[Tuple[int, Dict[str, Any]]] = []
    for card in card_rows(kb):
        score = score_text(
            "{} {}".format(card["title"], card["card_type"]), card["content"], terms
        )
        if score:
            card_hits.append((score, card))
    wiki_hits.sort(key=lambda item: (-item[0], item[1]["ordinal"], item[1]["title"]))
    card_hits.sort(key=lambda item: (-item[0], item[1]["card_type"], item[1]["title"]))
    return {"wiki": wiki_hits[:limit], "cards": card_hits[:limit]}


# ---------------------------------------------------------------------------
# Formatting


def snapshot_label(snapshot: Optional[Snapshot]) -> str:
    if not snapshot:
        return "no data"
    pieces = [snapshot.branch or "?", snapshot.locale or "?"]
    pieces.append("published" if snapshot.published else "working")
    if snapshot.state:
        pieces.append(snapshot.state)
    return " / ".join(pieces)


def kb_summary(kb: KnowledgeBase) -> Dict[str, Any]:
    snapshot = kb.primary
    stale = stale_info(kb, kb.owner) if kb.owner else {
        "known": False, "stale": None, "baseline": kb.baseline, "head": ""
    }
    return {
        "name": kb.name,
        "path": str(kb.root),
        "owner": kb.owner or "",
        "branch": snapshot.branch if snapshot else "",
        "locale": snapshot.locale if snapshot else "",
        "wiki_pages": kb.wiki.count if kb.wiki else 0,
        "wiki_chars": kb.wiki.chars if kb.wiki else 0,
        "cards": kb.cards.count if kb.cards else 0,
        "baseline": kb.baseline,
        "head": stale.get("head", ""),
        "stale": stale.get("stale"),
        "updated_at": (kb.wiki.updated_at if kb.wiki else "") or (
            kb.cards.updated_at if kb.cards else ""
        ),
    }


def format_projects(kbs: List[KnowledgeBase]) -> str:
    lines = ["# Qoder knowledge bases", ""]
    lines.append("| KB | Owner | Branch | Wiki | Cards | Baseline | Status |")
    lines.append("| --- | --- | --- | ---: | ---: | --- | --- |")
    for kb in kbs:
        summary = kb_summary(kb)
        status = "no baseline"
        if summary["stale"] is True:
            status = "stale vs HEAD"
        elif summary["stale"] is False:
            status = "current"
        lines.append(
            "| {} | {} | {} | {} | {} | {} | {} |".format(
                summary["name"],
                summary["owner"] or "?",
                summary["branch"] or "?",
                summary["wiki_pages"],
                summary["cards"],
                (summary["baseline"][:12] or "-"),
                status,
            )
        )
    return "\n".join(lines)


def format_index(kb: KnowledgeBase, project: Optional[str]) -> str:
    summary = kb_summary(kb)
    lines = [
        "# Qoder knowledge index — {}".format(summary["owner"] or kb.name),
        "",
        "- KB: `{}`".format(kb.name),
        "- Branch / locale: {} / {}".format(summary["branch"] or "?", summary["locale"] or "?"),
        "- Repo Wiki: {} pages ({})".format(summary["wiki_pages"], human_chars(summary["wiki_chars"])),
        "- Knowledge cards: {}".format(summary["cards"]),
        "- Baseline: {}".format(summary["baseline"][:12] or "unknown"),
    ]
    if summary["stale"] is True:
        lines.append("- Status: **may be stale** (baseline {} vs HEAD {})".format(
            summary["baseline"][:12], summary["head"]
        ))
    elif summary["stale"] is False:
        lines.append("- Status: current with HEAD {}".format(summary["head"]))
    lines.append("")

    sections = wiki_sections(kb)
    if sections:
        roots, children = section_tree(sections)
        lines.append("## Repo Wiki pages")
        for depth, section in walk_tree(roots, children):
            indent = "  " * depth
            lines.append("{}- {} [{}] ({})".format(
                indent, section["title"], section["id"][:8], human_chars(len(section["content"] or ""))
            ))
        lines.append("")
    else:
        lines.append("## Repo Wiki pages")
        lines.append("No generated wiki pages yet.")
        lines.append("")

    cards = card_rows(kb)
    if cards:
        counts = Counter(card["card_type"] for card in cards)
        lines.append("## Knowledge cards")
        lines.append(", ".join("{}: {}".format(kind, count) for kind, count in sorted(counts.items())))
        lines.append("")
        modules = module_rows(kb)
        if modules:
            lines.append("### Modules")
            module_ids = {module["id"] for module in modules}
            roots_m = [module for module in modules if module["parent_id"] not in module_ids]
            children_m: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for module in modules:
                if module["parent_id"] in module_ids and module["parent_id"] != module["id"]:
                    children_m[module["parent_id"]].append(module)
            visited = set()

            def visit_module(module: Dict[str, Any], depth: int) -> None:
                if module["id"] in visited:
                    return
                visited.add(module["id"])
                lines.append("{}- {} ({} cards)".format(
                    "  " * depth, module["title"], module["card_count"]
                ))
                for child in sorted(children_m.get(module["id"], []), key=lambda item: (item["ordinal"], item["title"])):
                    visit_module(child, depth + 1)

            for module in sorted(roots_m, key=lambda item: (item["ordinal"], item["title"])):
                visit_module(module, 0)
            lines.append("")
    else:
        lines.append("## Knowledge cards")
        lines.append("No knowledge cards yet.")
        lines.append("")

    info = git_info(project or kb.owner)
    lines.append("## Repo state")
    if info["is_git"]:
        lines.append("- {} @ {} ({})".format(info["root"], info["head"], info["branch"]))
        lines.append("- Dirty files: {}".format(info["dirty"]))
    else:
        lines.append("- {} (not a git repository)".format(info["root"]))
    return "\n".join(lines)


def format_search(kbs: List[KnowledgeBase], terms: Sequence[str], limit: int) -> str:
    lines = ["# qkb search: {}".format(" ".join(terms)), ""]
    for kb in kbs:
        results = search_kb(kb, terms, limit)
        summary = kb_summary(kb)
        lines.append("## {} — {}".format(kb.name, summary["owner"] or "unknown owner"))
        lines.append("Branch {}, baseline {}, {} pages, {} cards".format(
            summary["branch"] or "?", summary["baseline"][:12] or "unknown",
            summary["wiki_pages"], summary["cards"]
        ))
        lines.append("")
        lines.append("### Repo Wiki")
        if not results["wiki"]:
            lines.append("No matching wiki pages.")
        for index, (score, section) in enumerate(results["wiki"], 1):
            lines.append("#### {}. {} [{}] — score {}".format(
                index, section["title"], section["id"][:8], score
            ))
            snippet = make_snippet(section["content"], terms)
            if snippet:
                lines.append(snippet)
            lines.append("`qkb page {}`".format(section["id"][:8]))
            lines.append("")
        lines.append("### Knowledge cards")
        if not results["cards"]:
            lines.append("No matching cards.")
        for index, (score, card) in enumerate(results["cards"], 1):
            lines.append("#### {}. {} [{}] — {} (score {})".format(
                index, card["title"], card["id"][:8], card["card_type"], score
            ))
            snippet = make_snippet(card["content"], terms)
            if snippet:
                lines.append(snippet)
            lines.append("`qkb card {}`".format(card["id"][:8]))
            lines.append("")
    if not kbs:
        lines.append("No knowledge bases found. Run a wiki/knowledge build in Qoder Knowledge Center first.")
    return "\n".join(lines)


def format_page(kb: KnowledgeBase, bundle: Dict[str, Any]) -> str:
    page = bundle["page"]
    summary = kb_summary(kb)
    lines = [
        "# {}".format(display_title(page["title"])),
        "",
        "<!-- qkb source: project={} kb={} branch={} locale={} snapshot={} baseline={} page={} -->".format(
            summary["owner"] or "?", kb.name, summary["branch"] or "?",
            summary["locale"] or "?", (kb.wiki.snapshot_id if kb.wiki else "?"),
            summary["baseline"][:12] or "unknown", page["id"],
        ),
        "",
        bundle["text"],
    ]
    if len(bundle["citations"]) > 1:
        lines.extend(["", "## Sections cited"])
        for section_id in bundle["citations"][1:]:
            lines.append("- `{}`".format(section_id[:8]))
    return "\n".join(lines)


def format_card(kb: KnowledgeBase, bundle: Dict[str, Any]) -> str:
    card = bundle["card"]
    summary = kb_summary(kb)
    lines = [
        "# {}".format(card["title"]),
        "",
        "<!-- qkb source: project={} kb={} branch={} locale={} snapshot={} baseline={} card={} type={} -->".format(
            summary["owner"] or "?", kb.name, summary["branch"] or "?",
            summary["locale"] or "?", (kb.cards.snapshot_id if kb.cards else "?"),
            summary["baseline"][:12] or "unknown", card["id"], card["card_type"],
        ),
        "",
        "Type: `{}`".format(card["card_type"]),
    ]
    if bundle["module"]:
        lines.append("Module: {}".format(bundle["module"]["title"]))
    lines.extend(["", card["content"].strip() or "(empty card)"])
    if bundle["relations"]:
        lines.extend(["", "## Relations"])
        for relation in bundle["relations"]:
            lines.append("- {} — {} [{}]".format(
                relation["kind"], relation["title"] or "untitled", relation["id"][:8]
            ))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Context pack


class Budget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        self.parts: List[str] = []

    def add(self, text: str) -> bool:
        text = (text or "").strip()
        if not text:
            return False
        remaining = self.limit - self.used
        if remaining <= 0:
            return False
        if len(text) > remaining:
            if remaining < 200:
                return False
            text = text[:remaining].rstrip() + "\n...[truncated]"
        self.parts.append(text)
        self.used += len(text) + 2
        return True

    def text(self) -> str:
        return "\n\n".join(self.parts)


def repo_lines(
    project: Optional[str], excerpt_chars: int = 1200, plain: bool = False
) -> Tuple[List[str], Dict[str, Any]]:
    info = git_info(project)
    root = Path(info["root"])
    lines = ["Root: {}".format(info["root"])]
    if info["is_git"]:
        lines.append("Git: {} @ {} — {} dirty file(s)".format(
            info["branch"], info["head"], info["dirty"]
        ))
        if info["log"]:
            lines.append("Recent commits: " + "; ".join(info["log"][:5]))
    else:
        lines.append("Git: not a repository")
    for name in ("AGENTS.md", "README.md", "README", "README.rst"):
        path = root / name
        if path.is_file():
            excerpt = read_text(path, excerpt_chars).strip()
            if plain:
                excerpt = plain_text(excerpt)[:excerpt_chars]
            if excerpt:
                lines.append("{} excerpt:\n{}".format(name, excerpt))
    return lines, info


def build_context(
    home: Path, project: Optional[str], task: str, budget: int
) -> Tuple[str, List[KnowledgeBase]]:
    kbs = discover(home)
    kb: Optional[KnowledgeBase] = None
    kb_note = ""
    try:
        kb = match_kb(kbs, project)
    except QkbError as exc:
        kb_note = str(exc)
    terms = tokenize(task)
    repo_state, info = repo_lines(project, excerpt_chars=700, plain=True)
    content_budget = max(1200, budget - 900)
    body = Budget(content_budget)
    body.add("# Qoder context pack\nGenerated by qkb {} at {}. Read-only knowledge snapshot.".format(
        VERSION, iso_now()
    ))
    body.add("## Task\n{}".format(task.strip()))
    body.add("## Repo state\n" + "\n".join("- " + line for line in repo_state))

    memories = memory_lines(home, project or info["root"], terms, limit=10)
    if memories:
        body.add("## Memory\n" + "\n".join(memories))
    citations: List[str] = []
    if kb and kb_has_content(kb):
        summary = kb_summary(kb)
        body.add("## Knowledge source\n- Project: {}\n- KB: {} (branch {}, locale {})\n- Baseline: {} / HEAD {}\n- {} wiki pages, {} cards".format(
            summary["owner"] or "?", kb.name, summary["branch"] or "?",
            summary["locale"] or "?", summary["baseline"][:12] or "unknown",
            summary["head"] or "unknown", summary["wiki_pages"], summary["cards"],
        ))
        results = search_kb(kb, terms, limit=5)
        if results["wiki"]:
            body.add("## Architecture from Repo Wiki")
            for _, section in results["wiki"][:4]:
                try:
                    bundle = page_bundle(kb, section["id"], max_chars=1800)
                except QkbError:
                    continue
                excerpt = demote_headings(strip_toc(clean_markdown(bundle["text"])), 2)
                excerpt = strip_leading_heading(excerpt, section["title"], force=True)
                if body.add("### {} [{}]\n{}".format(
                    section["title"], section["id"][:8], excerpt
                )):
                    citations.append("wiki `{}` — {}".format(section["id"][:8], section["title"]))
        if results["cards"]:
            body.add("## Knowledge cards")
            for _, card in results["cards"][:6]:
                excerpt = strip_leading_heading(card["content"] or "", card["title"], force=True)
                excerpt = demote_headings(clean_markdown(excerpt), 3)
                if len(excerpt) > 900:
                    excerpt = excerpt[:900].rstrip() + "..."
                if body.add("### {} [{}] — {}\n{}".format(
                    card["title"], card["id"][:8], card["card_type"], excerpt
                )):
                    citations.append("card `{}` — {} ({})".format(
                        card["id"][:8], card["title"], card["card_type"]
                    ))
        preferred = ("coding_conventions", "unique_setup_and_commands", "tech_stack",
                     "build_system", "configuration_system", "overview")
        cards = card_rows(kb)
        added_types = {card["card_type"] for _, card in results["cards"]}
        for card_type in preferred:
            if card_type in added_types:
                continue
            candidate = next((card for card in cards if card["card_type"] == card_type), None)
            if not candidate:
                continue
            excerpt = strip_leading_heading(candidate["content"] or "", candidate["title"], force=True)
            excerpt = demote_headings(clean_markdown(excerpt), 3)[:900]
            if body.add("### {} [{}] — {}\n{}".format(
                candidate["title"], candidate["id"][:8], candidate["card_type"], excerpt
            )):
                citations.append("card `{}` — {} ({})".format(
                    candidate["id"][:8], candidate["title"], candidate["card_type"]
                ))
    else:
        note = kb_note or (
            "No Repo Wiki or knowledge cards match {}.".format(info["root"])
        )
        body.add("## Knowledge source\n{}".format(note))

    text = body.text()
    citation_lines = citations[:20] or ["No knowledge citations were used."]
    text += "\n\n## Citations\n" + "\n".join("- " + line for line in citation_lines)
    text += (
        "\n\n## Grounding rules\n"
        "- Cite page/card IDs in plans and reviews.\n"
        "- Treat live code as implementation truth; Repo Wiki and cards describe intent and conventions.\n"
        "- If the baseline differs from HEAD, say the knowledge may be stale instead of presenting it as current.\n"
        "- Knowledge databases are read-only; qkb never writes to them."
    )
    return text, ([kb] if kb else [])


# ---------------------------------------------------------------------------
# Hooks


def provenance_path(home: Path) -> Path:
    data_dir = os.environ.get("QODER_PLUGIN_DATA")
    base = Path(data_dir).expanduser() if data_dir else home / "plugins" / "data" / (PLUGIN_NAME + "-local")
    return base / "provenance.jsonl"


def write_provenance(home: Path, entry: Dict[str, Any]) -> None:
    if os.environ.get("QKB_PROVENANCE", "").lower() in ("0", "off", "false", "no"):
        return
    path = provenance_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        pass


def read_provenance(home: Path, session_id: str) -> List[Dict[str, Any]]:
    path = provenance_path(home)
    if not path.is_file():
        return []
    entries: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in deque(handle, maxlen=2000):
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if session_id and entry.get("session_id") != session_id:
                    continue
                entries.append(entry)
    except OSError:
        return []
    return entries


def hook_input() -> Dict[str, Any]:
    try:
        value = json.load(sys.stdin)
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def is_qkb_command(command: str) -> bool:
    return bool(QKB_CMD_RE.search(command or ""))


def cmd_hook_session_start(home: Path, _args: argparse.Namespace) -> int:
    data = hook_input()
    cwd = data.get("cwd") or os.environ.get("QODER_CWD") or os.getcwd()
    kbs = discover(home)
    if not kbs:
        return 0
    try:
        kb = match_kb(kbs, cwd)
    except QkbError:
        kb = None
    if not kb:
        return 0
    pages = kb.wiki.count if kb.wiki else 0
    cards = kb.cards.count if kb.cards else 0
    stale = stale_info(kb, cwd)
    if stale.get("stale") is True:
        state = "stale vs HEAD {} (baseline {})".format(
            stale["head"], stale["baseline"][:12]
        )
    elif stale.get("stale") is False:
        state = "current with HEAD {}".format(stale["head"])
    else:
        state = "baseline unknown"
    lines = [
        "Qoder Knowledge Bridge (qkb) is active.",
        "Project KB: {} — {}, {} wiki pages, {} cards.".format(
            kb.owner or kb.name, state, pages, cards
        ),
    ]
    if pages == 0 and cards == 0:
        lines.append("The KB exists but has no generated wiki pages or cards yet.")
    lines.append(
        "Before planning, run `qkb context --task \"<task>\"`; drill in with "
        "`qkb search`, `qkb page <id>`, or `qkb card <id>`. Cite IDs; code is truth."
    )
    context = "\n".join(lines)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }, ensure_ascii=False))
    return 0


def cmd_hook_provenance(home: Path, _args: argparse.Namespace) -> int:
    data = hook_input()
    tool_name = data.get("tool_name") or os.environ.get("QODER_TOOL_NAME") or ""
    tool_input = data.get("tool_input") or {}
    command = ""
    if isinstance(tool_input, dict):
        command = str(tool_input.get("command") or "")
    origin = ""
    entry: Dict[str, Any] = {
        "ts": iso_now(),
        "session_id": data.get("session_id") or "",
        "cwd": data.get("cwd") or os.getcwd(),
    }
    if command and is_qkb_command(command):
        origin = "qkb"
        entry["command"] = command[:500]
    elif tool_name.startswith("mcp__") and any(
        keyword in tool_name.lower() for keyword in ("repowiki", "qmind", "knowledge")
    ):
        origin = "mcp"
        entry["tool"] = tool_name
        entry["input"] = compact_json(tool_input)
    else:
        return 0
    entry["origin"] = origin
    response = data.get("tool_response")
    if response is not None:
        try:
            entry["response_chars"] = len(response) if isinstance(response, str) else len(
                json.dumps(response, ensure_ascii=False)
            )
        except (TypeError, ValueError):
            entry["response_chars"] = 0
    write_provenance(home, entry)
    return 0


def cmd_hook_stop(home: Path, _args: argparse.Namespace) -> int:
    data = hook_input()
    if data.get("stop_hook_active"):
        return 0
    session_id = data.get("session_id") or ""
    cwd = data.get("cwd") or os.getcwd()
    entries = read_provenance(home, session_id)
    lines: List[str] = []
    if entries:
        qkb_entries = [entry for entry in entries if entry.get("origin") == "qkb"]
        mcp_entries = [entry for entry in entries if entry.get("origin") == "mcp"]
        counts: Counter = Counter()
        ids = set()
        for entry in qkb_entries:
            command = entry.get("command", "")
            try:
                parts = shlex.split(command)
            except ValueError:
                parts = command.split()
            subcommand = next(
                (part for part in parts if part in (
                    "projects", "index", "search", "page", "card", "context", "repo", "memory"
                )),
                "qkb",
            )
            counts[subcommand] += 1
            for index, part in enumerate(parts):
                if part in ("page", "card") and index + 1 < len(parts):
                    ids.add(parts[index + 1].strip("'\""))
        mcp_counts = Counter(entry.get("tool", "mcp").split("__")[-1] for entry in mcp_entries)
        summary = ", ".join("{} {}".format(count, name) for name, count in sorted(counts.items()))
        if mcp_counts:
            mcp_summary = ", ".join("{} {}".format(count, name) for name, count in sorted(mcp_counts.items()))
            lines.append("Knowledge bridge: {} qkb call(s) [{}]; {} MCP call(s) [{}].".format(
                len(qkb_entries), summary or "none", len(mcp_entries), mcp_summary
            ))
        else:
            lines.append("Knowledge bridge: {} qkb call(s) [{}].".format(
                len(qkb_entries), summary or "none"
            ))
        if ids:
            lines.append("Pages/cards consulted: {}.".format(
                ", ".join(sorted(ids)[:12])
            ))
    try:
        kb = match_kb(discover(home), cwd)
    except QkbError:
        kb = None
    if kb:
        stale = stale_info(kb, cwd)
        if stale.get("stale") is True:
            lines.append(
                "Repo Wiki may be stale: baseline {} vs HEAD {}. "
                "Consider `qodercli wiki --update --repo {}`.".format(
                    stale["baseline"][:12], stale["head"], kb.owner or cwd
                )
            )
    if not lines:
        return 0
    print(json.dumps({"systemMessage": "\n".join(lines)}, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------------------
# CLI commands


def find_kb_for_item(
    kbs: List[KnowledgeBase], project: Optional[str], needle: str, kind: str
) -> Tuple[KnowledgeBase, Dict[str, Any]]:
    candidates: List[Tuple[KnowledgeBase, Dict[str, Any]]] = []
    if project:
        kb = match_kb(kbs, project, require=True)
        items = wiki_sections(kb) if kind == "page" else card_rows(kb)
        return kb, resolve_item(items, needle, kind)
    try:
        kb = match_kb(kbs, None)
    except QkbError:
        kb = None
    if kb:
        items = wiki_sections(kb) if kind == "page" else card_rows(kb)
        try:
            return kb, resolve_item(items, needle, kind)
        except QkbError:
            pass
    for kb in kbs:
        items = wiki_sections(kb) if kind == "page" else card_rows(kb)
        try:
            item = resolve_item(items, needle, kind)
        except QkbError as exc:
            if "ambiguous" in str(exc):
                raise
            continue
        candidates.append((kb, item))
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise QkbError("{} not found in any knowledge base: {}".format(kind, needle))
    names = ", ".join(kb.name for kb, _ in candidates)
    raise QkbError("{} found in multiple knowledge bases ({}). Use --project.".format(kind, names))


def cmd_projects(home: Path, args: argparse.Namespace) -> int:
    kbs = discover(home)
    if args.json:
        print(json.dumps([kb_summary(kb) for kb in kbs], indent=2, ensure_ascii=False))
        return 0
    if not kbs:
        print("No Qoder knowledge bases found under {}.".format(home / "qoder-knowledge"))
        return 0
    print(format_projects(kbs))
    return 0


def cmd_index(home: Path, args: argparse.Namespace) -> int:
    kbs = discover(home)
    if not kbs:
        raise QkbError("no knowledge bases found under {}".format(home / "qoder-knowledge"))
    kb = match_kb(kbs, args.project)
    if not kb:
        if args.json:
            print(json.dumps([kb_summary(item) for item in kbs], indent=2, ensure_ascii=False))
        else:
            print("No knowledge base matches {}. Available knowledge bases:\n".format(
                real_path(Path(args.project or os.getcwd()))
            ))
            print(format_projects(kbs))
        return 0
    if args.json:
        sections = wiki_sections(kb)
        roots, children = section_tree(sections)
        payload = {
            "kb": kb_summary(kb),
            "wiki_tree": [
                {"depth": depth, "id": section["id"], "title": section["title"],
                 "chars": len(section["content"] or "")}
                for depth, section in walk_tree(roots, children)
            ],
            "card_types": dict(Counter(card["card_type"] for card in card_rows(kb))),
            "modules": module_rows(kb),
            "repo": git_info(args.project or kb.owner),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    print(format_index(kb, args.project))
    return 0


def cmd_search(home: Path, args: argparse.Namespace) -> int:
    query = " ".join(args.query)
    terms = tokenize(query)
    if not terms:
        raise QkbError("search query has no usable terms")
    kbs = discover(home)
    if not kbs:
        raise QkbError("no knowledge bases found under {}".format(home / "qoder-knowledge"))
    if args.all:
        targets = kbs
    else:
        kb = match_kb(kbs, args.project)
        targets = [kb] if kb else kbs
    if args.json:
        payload = []
        for kb in targets:
            results = search_kb(kb, terms, args.limit)
            payload.append({
                "kb": kb_summary(kb),
                "wiki": [
                    {"score": score, "id": section["id"], "title": section["title"],
                     "snippet": make_snippet(section["content"], terms)}
                    for score, section in results["wiki"]
                ],
                "cards": [
                    {"score": score, "id": card["id"], "title": card["title"],
                     "card_type": card["card_type"],
                     "snippet": make_snippet(card["content"], terms)}
                    for score, card in results["cards"]
                ],
            })
        print(json.dumps({"query": query, "terms": terms, "results": payload},
                         indent=2, ensure_ascii=False))
        return 0
    print(format_search(targets, terms, args.limit))
    return 0


def cmd_page(home: Path, args: argparse.Namespace) -> int:
    kbs = discover(home)
    if not kbs:
        raise QkbError("no knowledge bases found")
    kb, _ = find_kb_for_item(kbs, args.project, args.page_id, "page")
    bundle = page_bundle(kb, args.page_id, max(500, args.max_chars))
    if args.json:
        print(json.dumps({
            "kb": kb_summary(kb),
            "page": bundle["page"],
            "text": bundle["text"],
            "citations": bundle["citations"],
            "truncated": bundle["truncated"],
        }, indent=2, ensure_ascii=False))
        return 0
    print(format_page(kb, bundle))
    return 0


def cmd_card(home: Path, args: argparse.Namespace) -> int:
    kbs = discover(home)
    if not kbs:
        raise QkbError("no knowledge bases found")
    kb, _ = find_kb_for_item(kbs, args.project, args.card_id, "card")
    bundle = card_bundle(kb, args.card_id)
    if args.json:
        print(json.dumps({"kb": kb_summary(kb), **bundle}, indent=2, ensure_ascii=False))
        return 0
    print(format_card(kb, bundle))
    return 0


def cmd_context(home: Path, args: argparse.Namespace) -> int:
    task = args.task
    if task == "-":
        task = sys.stdin.read()
    if not task or not task.strip():
        raise QkbError("context requires --task \"<task>\" (or --task - to read stdin)")
    budget = max(2000, args.budget)
    text, kbs = build_context(home, args.project, task, budget)
    if args.json:
        print(json.dumps({
            "task": task,
            "project": real_path(Path(args.project or os.getcwd())),
            "kb": kb_summary(kbs[0]) if kbs else None,
            "text": text,
        }, indent=2, ensure_ascii=False))
        return 0
    print(text)
    return 0


def cmd_repo(home: Path, args: argparse.Namespace) -> int:
    lines, info = repo_lines(args.project, excerpt_chars=1600)
    if args.json:
        print(json.dumps({"git": info, "lines": lines}, indent=2, ensure_ascii=False))
        return 0
    print("# Repo context")
    print()
    for line in lines:
        print("- " + line)
    return 0


def cmd_memory(home: Path, args: argparse.Namespace) -> int:
    indexes = memory_indexes(home, args.project)
    text = memory_index_text(home, args.project, args.search)
    if args.json:
        print(json.dumps({
            "project": real_path(Path(args.project or os.getcwd())),
            "indexes": [
                {"path": str(path), "scope": scope}
                for scope, path in indexes
            ],
            "text": text,
        }, indent=2, ensure_ascii=False))
        return 0
    if not indexes:
        print("No Qoder memory index found for {} or the user scope.".format(
            real_path(Path(args.project or os.getcwd()))
        ))
        return 0
    if not text:
        print("No memory index entries matched {!r}.".format(args.search))
        return 0
    print(text)
    return 0


# ---------------------------------------------------------------------------
# Argument parsing


def add_common(parser: argparse.ArgumentParser, project: bool = True, json_flag: bool = True) -> None:
    if project:
        parser.add_argument("--project", default=None, help="Project path (default: current directory)")
    if json_flag:
        parser.add_argument("--json", action="store_true", help="Emit JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qkb",
        description="Read-only bridge to Qoder Repo Wiki, knowledge cards, repo context, and memory.",
    )
    parser.add_argument("--home", default=None, help="Qoder config root (default: $QODER_HOME or ~/.qoder)")
    parser.add_argument("--version", action="version", version="qkb " + VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    projects = sub.add_parser("projects", help="List knowledge bases")
    add_common(projects, project=False)

    index = sub.add_parser("index", help="Show the wiki/card index for a project")
    add_common(index)

    search = sub.add_parser("search", help="Search wiki pages and knowledge cards")
    search.add_argument("query", nargs="+")
    search.add_argument("--limit", type=int, default=6, help="Results per type per KB (default: 6)")
    search.add_argument("--all", action="store_true", help="Search every knowledge base")
    add_common(search)

    page = sub.add_parser("page", help="Read a Repo Wiki page by id, prefix, or title")
    page.add_argument("page_id")
    page.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    add_common(page)

    card = sub.add_parser("card", help="Read a knowledge card by id, prefix, or title")
    card.add_argument("card_id")
    add_common(card)

    context = sub.add_parser("context", help="Build a cited context pack for an external agent")
    context.add_argument("--task", required=True, help="Task description, or - to read stdin")
    context.add_argument("--budget", type=int, default=DEFAULT_CONTEXT_BUDGET, help="Character budget")
    add_common(context)

    repo = sub.add_parser("repo", help="Show git state and AGENTS.md/README excerpts")
    add_common(repo)

    memory = sub.add_parser("memory", help="Show Qoder memory indexes")
    memory.add_argument("--search", default="", help="Only show matching index lines")
    add_common(memory)

    for name, help_text in (
        ("hook-session-start", "SessionStart hook"),
        ("hook-provenance", "PostToolUse provenance hook"),
        ("hook-stop", "Stop summary hook"),
    ):
        hook = sub.add_parser(name, help=help_text)
        add_common(hook, project=False, json_flag=False)

    return parser


def dispatch(home: Path, args: argparse.Namespace) -> int:
    handlers = {
        "projects": cmd_projects,
        "index": cmd_index,
        "search": cmd_search,
        "page": cmd_page,
        "card": cmd_card,
        "context": cmd_context,
        "repo": cmd_repo,
        "memory": cmd_memory,
        "hook-session-start": cmd_hook_session_start,
        "hook-provenance": cmd_hook_provenance,
        "hook-stop": cmd_hook_stop,
    }
    handler = handlers.get(args.command)
    if not handler:
        raise QkbError("unknown command: {}".format(args.command))
    return handler(home, args)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    home = Path(args.home).expanduser() if args.home else qoder_home()
    try:
        return dispatch(home, args)
    except QkbError as exc:
        print("qkb: {}".format(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
    except Exception as exc:  # keep hooks non-fatal; the CLI reports the error
        print("qkb: unexpected error: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
