#!/usr/bin/env python3
"""Update an AcademicPages publications page from NASA ADS."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ADS_SEARCH_URL = "https://api.adsabs.harvard.edu/v1/search/query"
ADS_LIBRARY_URL = "https://api.adsabs.harvard.edu/v1/biblib/libraries/{library_id}"
ADS_BIBCODE_QUERY_CHUNK_SIZE = 20
ADS_LIBRARY_PAGE_SIZE = 200


def load_yaml(path: Path) -> dict:
    try:
        import yaml
    except ImportError as exc:
        raise SystemExit("Missing dependency: pip install pyyaml") from exc
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def ads_search(token: str, query: str, rows: int, sort: str) -> list[dict]:
    fields = [
        "bibcode",
        "title",
        "author",
        "year",
        "pub",
        "volume",
        "page",
        "pubdate",
        "citation_count",
        "bibstem",
    ]
    params = urllib.parse.urlencode(
        {
            "q": query,
            "fl": ",".join(fields),
            "rows": rows,
            "sort": sort,
        }
    )
    req = urllib.request.Request(
        f"{ADS_SEARCH_URL}?{params}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"ADS API error {exc.code}: {detail}") from exc
    return payload.get("response", {}).get("docs", [])


def ads_library_page(token: str, library_id: str, rows: int, start: int) -> dict:
    url = ADS_LIBRARY_URL.format(library_id=urllib.parse.quote(library_id))
    url += "?" + urllib.parse.urlencode({"raw": "true", "rows": rows, "start": start})
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"ADS library API error {exc.code}: {detail}") from exc


def ads_library_bibcodes(token: str, library_id: str, rows: int) -> list[str]:
    page_size = max(1, min(rows, ADS_LIBRARY_PAGE_SIZE))
    bibcodes: list[str] = []
    seen: set[str] = set()
    expected = 0
    start = 0
    while True:
        payload = ads_library_page(token, library_id, page_size, start)
        expected = int(payload.get("num_documents") or payload.get("number_of_documents") or expected or 0)
        documents = payload.get("documents", [])
        page_bibcodes = [item["bibcode"] if isinstance(item, dict) else item for item in documents]
        for bibcode in page_bibcodes:
            if bibcode not in seen:
                bibcodes.append(bibcode)
                seen.add(bibcode)
        if not expected or len(bibcodes) >= expected:
            break
        if not page_bibcodes:
            break
        start += page_size

    if expected and len(bibcodes) < expected:
        raise SystemExit(
            f"ADS library returned {len(bibcodes)} of {expected} documents; "
            "check ADS API pagination."
        )
    return bibcodes


def ads_records_for_bibcodes(token: str, bibcodes: list[str], sort: str) -> list[dict]:
    if not bibcodes:
        return []
    records_by_bibcode: dict[str, dict] = {}
    for idx in range(0, len(bibcodes), ADS_BIBCODE_QUERY_CHUNK_SIZE):
        chunk = bibcodes[idx : idx + ADS_BIBCODE_QUERY_CHUNK_SIZE]
        query = " OR ".join(f'bibcode:"{bibcode}"' for bibcode in chunk)
        for doc in ads_search(token, query, len(chunk), sort):
            if doc.get("bibcode"):
                records_by_bibcode[doc["bibcode"]] = doc

    missing = [bibcode for bibcode in bibcodes if bibcode not in records_by_bibcode]
    if missing:
        raise SystemExit(
            f"ADS returned metadata for {len(records_by_bibcode)} of {len(bibcodes)} "
            "library records. Missing bibcodes: "
            + ", ".join(missing)
        )

    docs = list(records_by_bibcode.values())
    if sort.strip().lower() in {"date desc", "pubdate desc"}:
        docs.sort(key=lambda doc: (str(doc.get("pubdate", "")), str(doc.get("year", "")), doc["bibcode"]), reverse=True)
    return docs


def normalize_name(name: str) -> str:
    cleaned = re.sub(r"[{}~]", "", name)
    cleaned = re.sub(r"\s+", " ", cleaned.replace(".", "")).strip().lower()
    if "," in cleaned:
        last, rest = [part.strip() for part in cleaned.split(",", 1)]
        initials = "".join(part[0] for part in rest.split() if part)
        return f"{last},{initials}"
    parts = cleaned.split()
    if len(parts) >= 2:
        return f"{parts[-1]},{''.join(part[0] for part in parts[:-1])}"
    return cleaned


def author_matches(name: str, configured_names: list[str]) -> bool:
    normalized = normalize_name(name)
    return normalized in {normalize_name(candidate) for candidate in configured_names}


def format_author(name: str) -> str:
    name = re.sub(r"[{}]", "", name).replace("~", " ")
    if "," not in name:
        return name
    last, rest = [part.strip() for part in name.split(",", 1)]
    parts = []
    for token in rest.split():
        if "-" in token:
            parts.append("-".join(piece[0].upper() + "." for piece in token.split("-") if piece))
        else:
            parts.append(token[0].upper() + ".")
    return f"{last}, {' '.join(parts)}"


def emphasize_author(name: str, match_names: list[str]) -> str:
    escaped = html.escape(format_author(name))
    if author_matches(name, match_names):
        return f"<strong>{escaped}</strong>"
    return escaped


def format_authors(authors: list[str], match_names: list[str], max_before_ellipsis: int) -> str:
    if not authors:
        return ""
    target_index = next(
        (idx for idx, author in enumerate(authors) if author_matches(author, match_names)),
        None,
    )
    formatted = [emphasize_author(author, match_names) for author in authors]
    if len(formatted) <= max_before_ellipsis + 1:
        if len(formatted) == 1:
            return formatted[0]
        return ", ".join(formatted[:-1]) + f", & {formatted[-1]}"
    if target_index is not None and target_index >= max_before_ellipsis:
        head = ", ".join(formatted[: max_before_ellipsis - 1])
        return f"{head}, ..., {formatted[target_index]}, et al."
    return ", ".join(formatted[:max_before_ellipsis]) + ", et al."


def page_value(doc: dict) -> str:
    pages = doc.get("page") or []
    if isinstance(pages, list) and pages:
        return str(pages[0])
    if isinstance(pages, str):
        return pages
    return ""


def journal_name(doc: dict) -> str:
    bibstems = doc.get("bibstem") or []
    if bibstems:
        return str(bibstems[0])
    pub = str(doc.get("pub", ""))
    if pub == "arXiv e-prints":
        return "arXiv"
    return pub


def publication_line(doc: dict, cfg: dict) -> str:
    authors = format_authors(
        doc.get("author") or [],
        cfg["author"]["match_names"],
        cfg["formatting"]["max_authors_before_ellipsis"],
    )
    title = html.escape(html.unescape((doc.get("title") or ["Untitled"])[0]))
    pub = journal_name(doc)
    pieces = [authors, str(doc.get("year", "")), html.escape(pub)]
    if doc.get("volume"):
        pieces.append(html.escape(str(doc["volume"])))
    page = page_value(doc)
    if page:
        pieces.append(html.escape(page))
    citation = ", ".join(piece for piece in pieces if piece)
    bibcode = doc["bibcode"]
    url = f"https://ui.adsabs.harvard.edu/abs/{urllib.parse.quote(bibcode)}/abstract"
    return f'{citation}, <a href="{url}">{title}</a>'


def is_first_or_second_author(doc: dict, match_names: list[str]) -> bool:
    authors = doc.get("author") or []
    return any(author_matches(author, match_names) for author in authors[:2])


def publication_stats(docs: list[dict]) -> dict:
    citation_counts = sorted((int(doc.get("citation_count") or 0) for doc in docs), reverse=True)
    h_index = sum(1 for idx, citation_count in enumerate(citation_counts, 1) if citation_count >= idx)
    total_citations = sum(citation_counts)
    return {
        "h_index": h_index,
        "total_citations": total_citations,
        "publication_count": len(docs),
        "citation_floor": (total_citations // 100) * 100,
    }


def render_publications(docs: list[dict], cfg: dict) -> str:
    match_names = cfg["author"]["match_names"]
    first_second = [doc for doc in docs if is_first_or_second_author(doc, match_names)]
    other = [doc for doc in docs if doc not in first_second]
    stats = publication_stats(docs)

    lines: list[str] = []
    if cfg["formatting"].get("include_stats", True):
        library_id = cfg["ads"].get("library_id")
        library_url = (
            f"https://ui.adsabs.harvard.edu/public-libraries/{library_id}"
            if library_id
            else "https://ui.adsabs.harvard.edu/"
        )
        lines.extend(
            [
                '{% if site.author.googlescholar %}',
                f'  <div class="wordwrap"> h index: {stats["h_index"]}</div>',
                f'  <div class="wordwrap"> Total citations: >{stats["citation_floor"]}</div>',
                f'  <div class="wordwrap"> Number of publications: {stats["publication_count"]} </div> '
                f'(Please find my publications on <a href="{library_url}">my ADS library</a>)',
                "{% endif %}",
                "",
            ]
        )

    lines.extend(['<div class="wordwrap">', f'  <h3>{html.escape(cfg["formatting"]["first_second_heading"])}</h3>', "  <ol>"])
    for idx, doc in enumerate(first_second, 1):
        lines.append(f"    <li>{publication_line(doc, cfg)}</li>")
    lines.extend(["  </ol>", "</div>", ""])

    lines.extend(['<div class="wordwrap">', f'  <h3>{html.escape(cfg["formatting"]["other_heading"])}</h3>', "  <ol>"])
    for doc in other:
        lines.append(f"    <li>{publication_line(doc, cfg)}</li>")
    lines.extend(["  </ol>", "</div>", ""])
    return "\n".join(lines)


def render_cv_stats(docs: list[dict]) -> str:
    stats = publication_stats(docs)
    return "\n".join(
        [
            f' * h index: {stats["h_index"]}',
            f' * Total citations: >{stats["citation_floor"]}',
            f' * Number of publications: {stats["publication_count"]}',
            "",
        ]
    )


def replace_managed_region(original: str, rendered: str, start: str, end: str) -> str:
    block = f"{start}\n{rendered}{end}"
    if start in original and end in original:
        pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.S)
        return pattern.sub(lambda _: block, original)
    raise SystemExit(f"Could not find managed markers: {start} / {end}")


def update_page(original: str, rendered: str, cfg: dict) -> str:
    start = cfg["site"]["managed_region_start"]
    end = cfg["site"]["managed_region_end"]
    if start in original and end in original:
        return replace_managed_region(original, rendered, start, end)
    frontmatter = re.match(r"(?s)\A---\n.*?\n---\n", original)
    if frontmatter:
        block = f"{start}\n{rendered}{end}"
        return original[: frontmatter.end()].rstrip() + "\n\n" + block + "\n"
    raise SystemExit("Could not find managed markers or YAML front matter.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/publications-agent.yaml")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--output", help="Write rendered publications block to this file.")
    args = parser.parse_args()

    cfg = load_yaml(Path(args.config))
    token = os.environ.get("ADS_TOKEN")
    if not token:
        raise SystemExit("Set ADS_TOKEN to a NASA ADS API token.")

    if cfg["ads"].get("library_id"):
        bibcodes = ads_library_bibcodes(token, cfg["ads"]["library_id"], int(cfg["ads"].get("rows", 200)))
        docs = ads_records_for_bibcodes(token, bibcodes, cfg["ads"].get("sort", "date desc"))
    else:
        docs = ads_search(
            token,
            cfg["ads"]["query"],
            int(cfg["ads"].get("rows", 200)),
            cfg["ads"].get("sort", "date desc"),
        )
    rendered = render_publications(docs, cfg)
    rendered_cv_stats = render_cv_stats(docs)

    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")

    changed_paths: list[Path] = []
    page_path = Path(cfg["site"]["publications_page"])
    if not page_path.exists():
        print(rendered)
        return 0

    original = page_path.read_text(encoding="utf-8")
    updated = update_page(original, rendered, cfg)
    if updated != original:
        changed_paths.append(page_path)
    if args.write and updated != original:
        page_path.write_text(updated, encoding="utf-8")

    cv_page = cfg["site"].get("cv_page")
    if cv_page:
        cv_path = Path(cv_page)
        cv_original = cv_path.read_text(encoding="utf-8")
        cv_updated = replace_managed_region(
            cv_original,
            rendered_cv_stats,
            cfg["site"]["cv_stats_region_start"],
            cfg["site"]["cv_stats_region_end"],
        )
        if cv_updated != cv_original:
            changed_paths.append(cv_path)
        if args.write and cv_updated != cv_original:
            cv_path.write_text(cv_updated, encoding="utf-8")

    if not changed_paths:
        print("No publication changes.")
        return 0
    if args.write:
        changed = ", ".join(str(path) for path in changed_paths)
        print(f"Updated {changed} on {dt.date.today().isoformat()}.")
    else:
        print(updated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
