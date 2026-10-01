#!/usr/bin/env python3
"""Report which documents are missing or stale translations.

Reads the language list straight out of _config.yml, so adding a third
language needs no change here.

Three states per (document, target language) pair:

  missing  the source exists, the translation does not
  stale    both exist, but the source was committed more recently
  skipped  `translate: false` — deliberately not translated

"stale" is the state a plain file-existence check cannot see, and it is the
one that actually bites: edit a paragraph of the Chinese source and the
English copy silently keeps serving the old text forever.

Only `translate: false` is honoured as an opt-out. Polyglot's `lang-exclusive`
is a different mechanism (it removes a document from a language build, making
that url 404) and the repo does not use it; see AGENTS.md § 多语言.

Usage:
    tools/i18n-status.py              # posts only
    tools/i18n-status.py --all        # include _brief/
    tools/i18n-status.py --json       # machine-readable, for a translate skill
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Collections laid out as <dir>/<lang>/<name>.md. `report_by_default` is what
# keeps the daily brief cadence from burying the handful of posts that matter:
# ~25 briefs would otherwise drown 9 posts in the same list.
COLLECTIONS = [("_posts", True), ("_brief", False)]


@dataclass
class Doc:
    """One document name, and the languages it exists in."""

    name: str
    collection: str
    paths: dict[str, Path] = field(default_factory=dict)
    translate: dict[str, bool] = field(default_factory=dict)


def read_config() -> tuple[list[str], str]:
    """Pull `languages` and `default_lang` out of _config.yml.

    Hand-rolled rather than via PyYAML so the script runs on a bare Python 3
    with no virtualenv — it is meant to be a one-command check. Both keys are
    flat scalars written on one line, which is the only shape handled here;
    anything else should fail loudly rather than guess.
    """
    text = (REPO / "_config.yml").read_text(encoding="utf-8")

    langs = re.search(r"^languages:\s*\[(.+?)\]\s*$", text, re.M)
    default = re.search(r"^default_lang:\s*[\"']?([\w-]+)[\"']?\s*$", text, re.M)
    if not langs or not default:
        sys.exit("error: could not read `languages` / `default_lang` from _config.yml")

    return [l.strip().strip("\"'") for l in langs.group(1).split(",")], default.group(1)


def front_matter(path: Path) -> dict[str, str]:
    """Return the document's YAML front matter as flat key -> raw string.

    Only top-level `key: value` lines are needed (this script reads exactly
    one key, `translate`), so nested structures are skipped rather than
    parsed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}

    if not text.startswith("---"):
        return {}

    _, _, rest = text.partition("---\n")
    body, sep, _ = rest.partition("\n---")
    if not sep:
        return {}

    out = {}
    for line in body.splitlines():
        if not line or line[0].isspace() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip()] = value.strip().strip("\"'")
    return out


def config_translate_overrides() -> dict[str, bool]:
    """Read per-path `translate:` values out of _config.yml's `defaults:`.

    Keeping the flags in _config.yml means a document needs no front-matter
    key of its own, which is the whole point — the author writes a post and
    nothing else. Matching mirrors Jekyll's own `scope.path` semantics: an
    exact file path, or a directory prefix.
    """
    text = (REPO / "_config.yml").read_text(encoding="utf-8")
    overrides: dict[str, bool] = {}

    # Each `- scope:` block pairs a path with the values applied to it.
    for block in re.split(r"\n\s*-\s+scope:", text)[1:]:
        path = re.search(r"^\s*path:\s*[\"']?([^\"'\n]*)[\"']?\s*$", block, re.M)
        value = re.search(r"^\s*translate:\s*(true|false)\s*$", block, re.M)
        if path and value:
            overrides[path.group(1).strip()] = value.group(1) == "true"

    return overrides


def translate_flag(rel_path: str, fm: dict[str, str], overrides: dict[str, bool]) -> bool:
    """Resolve `translate` for one document: front matter wins over config.

    Among config scopes the longest matching path wins, so a per-file entry
    overrides the directory default it sits inside — same precedence Jekyll
    applies.
    """
    if "translate" in fm:
        return fm["translate"].lower() != "false"

    best = None
    for scope, value in overrides.items():
        if not scope:
            continue
        if rel_path == scope or rel_path.startswith(scope.rstrip("/") + "/"):
            if best is None or len(scope) > len(best[0]):
                best = (scope, value)

    return best[1] if best else True


def last_commit_ts(paths: list[Path]) -> dict[Path, int]:
    """Last-commit unix timestamp for each path, in one git call.

    Uncommitted files map to 0, which reads as "older than everything" and so
    never produces a false stale report for a translation that was just
    written but not yet committed.
    """
    if not paths:
        return {}

    rels = [str(p.relative_to(REPO)) for p in paths]
    proc = subprocess.run(
        ["git", "-C", str(REPO), "log", "--format=%ct", "--name-only", "--no-merges", "--", *rels],
        capture_output=True,
        text=True,
    )

    seen: dict[str, int] = {}
    ts = 0
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.isdigit() and len(line) >= 9:
            ts = int(line)
        elif line not in seen:
            # name-only lines follow their commit's timestamp, newest first,
            # so the first sighting of a path is its latest commit.
            seen[line] = ts

    return {p: seen.get(str(p.relative_to(REPO)), 0) for p in paths}


def collect(languages: list[str], collections: list[str]) -> list[Doc]:
    docs: dict[tuple[str, str], Doc] = {}
    overrides = config_translate_overrides()

    for collection in collections:
        for lang in languages:
            directory = REPO / collection / lang
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.md")):
                key = (collection, path.name)
                doc = docs.setdefault(key, Doc(name=path.stem, collection=collection))
                doc.paths[lang] = path
                rel = str(path.relative_to(REPO))
                doc.translate[lang] = translate_flag(rel, front_matter(path), overrides)

    return [docs[k] for k in sorted(docs)]


def analyse(docs: list[Doc], languages: list[str], default_lang: str) -> dict:
    stamps = last_commit_ts([p for d in docs for p in d.paths.values()])
    report = {lang: {"missing": [], "stale": [], "skipped": []} for lang in languages}

    for doc in docs:
        # The source is whichever language the document was actually written
        # in. Usually the default, but two posts here were drafted in English,
        # and for those the translation owed runs the other way.
        source_lang = default_lang if default_lang in doc.paths else next(iter(doc.paths))
        source = doc.paths[source_lang]

        for lang in languages:
            if lang == source_lang:
                continue

            entry = {
                "collection": doc.collection,
                "name": doc.name,
                "source_lang": source_lang,
                "source": str(source.relative_to(REPO)),
            }

            if not doc.translate.get(source_lang, True):
                report[lang]["skipped"].append({**entry, "reason": "translate: false"})
            elif lang not in doc.paths:
                report[lang]["missing"].append(entry)
            elif stamps.get(source, 0) > stamps.get(doc.paths[lang], 0):
                report[lang]["stale"].append(
                    {
                        **entry,
                        "translation": str(doc.paths[lang].relative_to(REPO)),
                        "source_ts": stamps.get(source, 0),
                        "translation_ts": stamps.get(doc.paths[lang], 0),
                    }
                )

    return report


def render(report: dict, show_skipped: bool) -> int:
    import datetime

    def day(ts: int) -> str:
        return datetime.date.fromtimestamp(ts).isoformat() if ts else "未提交"

    outstanding = 0
    chunks = []

    for lang, buckets in report.items():
        outstanding += len(buckets["missing"]) + len(buckets["stale"])
        lines = []

        if buckets["missing"]:
            lines.append(f"  缺失 ({len(buckets['missing'])})")
            for e in buckets["missing"]:
                lines.append(f"    {e['collection']:<7} {e['name']}")

        if buckets["stale"]:
            lines.append(f"  过期 ({len(buckets['stale'])})")
            for e in buckets["stale"]:
                lines.append(
                    f"    {e['collection']:<7} {e['name']}"
                    f"   源 {day(e['source_ts'])} / 译 {day(e['translation_ts'])}"
                )

        if show_skipped and buckets["skipped"]:
            lines.append(f"  跳过 ({len(buckets['skipped'])})")
            for e in buckets["skipped"]:
                lines.append(f"    {e['collection']:<7} {e['name']}   {e['reason']}")

        if lines:
            chunks.append(f"{lang}\n" + "\n".join(lines))

    print("\n\n".join(chunks) if chunks else "所有翻译都是最新的。")
    return outstanding


def main() -> int:
    parser = argparse.ArgumentParser(description="Report missing and stale translations.")
    parser.add_argument("--all", action="store_true", help="include _brief/ (hidden by default)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--skipped", action="store_true", help="also list translate: false docs")
    args = parser.parse_args()

    languages, default_lang = read_config()
    collections = [name for name, by_default in COLLECTIONS if by_default or args.all]

    docs = collect(languages, collections)
    report = analyse(docs, languages, default_lang)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    outstanding = render(report, args.skipped)

    if not args.all:
        hidden = [name for name, by_default in COLLECTIONS if not by_default]
        print(f"\n（未统计 {', '.join(hidden)}；加 --all 查看）")

    # Exit 0 either way: outstanding translations are the normal state of this
    # repo, not an error. A caller that wants to gate on it can read the count.
    return 0


if __name__ == "__main__":
    sys.exit(main())
