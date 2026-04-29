#!/usr/bin/env python3
"""
scripts/arxiv_alerts_check.py — Daily poll of arxiv-mcp watched topics.

Reads queries from ~/.arxiv-mcp-server/papers/watched_topics.json, runs each
against the arXiv export API, dedups against already-downloaded papers, and
appends new hits to logs/arxiv_alerts.jsonl.

Designed for invocation by systemd/claude-arxiv-alerts.timer. Stdlib only.

Important: this does NOT touch the MCP server's last_checked timestamps —
those are reserved for interactive `check_alerts` calls. We use a fixed
LOOKBACK_DAYS window and dedup against the local downloaded-papers set.

Usage:
  python3 scripts/arxiv_alerts_check.py
  python3 scripts/arxiv_alerts_check.py --lookback-days 14 --max-per-query 30
  python3 scripts/arxiv_alerts_check.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ARXIV_API = "http://export.arxiv.org/api/query"
ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}
DEFAULT_WATCHES = Path.home() / ".arxiv-mcp-server" / "papers" / "watched_topics.json"
DEFAULT_PAPERS_DIR = Path.home() / ".arxiv-mcp-server" / "papers"
DEFAULT_LOG = Path("logs/arxiv_alerts.jsonl")


def load_watches(path: Path) -> list[dict]:
    if not path.exists():
        print(f"[error] watches file not found: {path}", file=sys.stderr)
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"[error] watches file invalid JSON: {e}", file=sys.stderr)
        return []
    return data.get("topics", []) if isinstance(data, dict) else []


def downloaded_ids(papers_dir: Path) -> set[str]:
    if not papers_dir.exists():
        return set()
    return {f.stem for f in papers_dir.glob("*.md")}


def build_query(topic: str, categories: list[str]) -> str:
    if categories:
        cat_clause = " OR ".join(f"cat:{c}" for c in categories)
        return f"({topic}) AND ({cat_clause})"
    return topic


def fetch_arxiv(query: str, max_results: int, timeout: float) -> list[dict]:
    params = {
        "search_query": query,
        "start": "0",
        "max_results": str(max_results),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    url = f"{ARXIV_API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "obi-engine-cron/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except Exception as e:
        print(f"[warn] arxiv fetch failed: {e}", file=sys.stderr)
        return []

    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        print(f"[warn] arxiv response parse failed: {e}", file=sys.stderr)
        return []

    results = []
    for entry in root.findall("atom:entry", ATOM_NS):
        id_url = entry.findtext("atom:id", default="", namespaces=ATOM_NS)
        # id is "http://arxiv.org/abs/2604.20949v1"
        if "/abs/" not in id_url:
            continue
        arxiv_id = id_url.rsplit("/abs/", 1)[-1].split("v")[0]
        published = entry.findtext("atom:published", default="", namespaces=ATOM_NS)
        title = (entry.findtext("atom:title", default="", namespaces=ATOM_NS) or "").strip()
        summary = (entry.findtext("atom:summary", default="", namespaces=ATOM_NS) or "").strip()
        authors = [
            a.findtext("atom:name", default="", namespaces=ATOM_NS)
            for a in entry.findall("atom:author", ATOM_NS)
        ]
        cats = [
            c.attrib.get("term", "")
            for c in entry.findall("atom:category", ATOM_NS)
        ]
        results.append({
            "id": arxiv_id,
            "title": " ".join(title.split()),
            "authors": authors,
            "categories": cats,
            "published": published,
            "abstract": " ".join(summary.split())[:600],
        })
    return results


def within_window(published_iso: str, cutoff: datetime) -> bool:
    if not published_iso:
        return False
    try:
        # arxiv format: "2026-04-22T17:47:52Z"
        dt = datetime.fromisoformat(published_iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    return dt >= cutoff


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--watches", type=Path, default=DEFAULT_WATCHES)
    ap.add_argument("--papers-dir", type=Path, default=DEFAULT_PAPERS_DIR)
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG)
    ap.add_argument("--lookback-days", type=int, default=7)
    ap.add_argument("--max-per-query", type=int, default=15)
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    watches = load_watches(args.watches)
    if not watches:
        print("[info] no watches registered; nothing to do", file=sys.stderr)
        return 0

    seen = downloaded_ids(args.papers_dir)
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.lookback_days)
    now_iso = datetime.now(timezone.utc).isoformat()

    new_hits = []
    for w in watches:
        topic = w.get("topic", "")
        cats = w.get("categories", []) or []
        max_results = int(w.get("max_results") or args.max_per_query)
        if not topic:
            continue
        q = build_query(topic, cats)
        results = fetch_arxiv(q, min(max_results, args.max_per_query), args.timeout)
        for r in results:
            if r["id"] in seen:
                continue
            if not within_window(r["published"], cutoff):
                continue
            new_hits.append({"topic": topic, "categories": cats, **r})

    summary = {
        "checked_at": now_iso,
        "n_watches": len(watches),
        "n_new": len(new_hits),
        "lookback_days": args.lookback_days,
    }

    if args.dry_run:
        json.dump({"summary": summary, "hits": new_hits}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    args.log.parent.mkdir(parents=True, exist_ok=True)
    with args.log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": "arxiv_alerts_summary", **summary}) + "\n")
        for hit in new_hits:
            fh.write(json.dumps({"event": "arxiv_alerts_hit", "checked_at": now_iso, **hit}) + "\n")
    print(
        f"[arxiv_alerts] {summary['n_new']} new hits across {summary['n_watches']} watches -> {args.log}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
