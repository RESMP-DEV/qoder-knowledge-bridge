---
description: Ground answers in Qoder Repo Wiki pages or knowledge cards, cite IDs, and flag stale knowledge.
alwaysApply: true
---

# Knowledge provenance

- When you use Qoder Repo Wiki or knowledge cards, cite the project path, branch, page ID or card ID, and baseline commit.
- Prefer Repo Wiki for architecture, intent, and module boundaries. Prefer knowledge cards for conventions, setup commands, and operational notes. Prefer live code for exact current behavior.
- If a wiki page or card conflicts with live code, treat the code as truth and explicitly note the drift. Do not repeat a stale claim as fact.
- Before relying on knowledge, compare the knowledge baseline with the repository HEAD. If they differ, say the knowledge may be stale and name both revisions.
- Use qkb context, search, page, and card to retrieve evidence. Quote only the relevant excerpt, not entire pages.
- Never invent page IDs, card IDs, or citations. If no knowledge base matches the project, say so and continue from repository evidence.
- Never write to the Qoder knowledge directories. qkb access is read-only.
