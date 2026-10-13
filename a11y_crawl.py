#!/usr/bin/env python3
"""Crawl a domain and report basic accessibility issues.

Checks per HTML page:
  - <html lang="...">
  - at least one <h1>
  - <img> alt (missing / empty)
  - elements with CSS background-image (inline or <style>) — flagged (no alt)

Checks per discovered PDF:
  - metadata Title / Subject (description)
  - XMP / catalog hints for accessibility
  - image XObjects with /Alt (alternative text) when present
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from pypdf.generic import DictionaryObject, IndirectObject

USER_AGENT = "WebsiteA11yCheck/1.0 (+local accessibility crawl)"
BG_IMAGE_RE = re.compile(
    r"background(?:-image)?\s*:\s*[^;]*url\s*\(\s*['\"]?([^)'\"]+)['\"]?\s*\)",
    re.IGNORECASE,
)
SKIP_SCHEMES = {"mailto", "tel", "javascript", "data", "blob"}


@dataclass
class Issue:
    page_url: str
    kind: str
    detail: str
    resource: str = ""


@dataclass
class CrawlReport:
    start_url: str
    pages_crawled: int = 0
    pdfs_checked: int = 0
    issues: list[Issue] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    pdfs: list[str] = field(default_factory=list)


def same_site(base: urlparse, other: urlparse) -> bool:
    if other.scheme not in {"http", "https"}:
        return False
    return other.netloc.lower() == base.netloc.lower()


def normalize_url(url: str) -> str:
    url, _ = urldefrag(url)
    parsed = urlparse(url)
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return parsed._replace(path=path, params="", query=parsed.query, fragment="").geturl()


def is_pdf_url(url: str, content_type: str | None = None) -> bool:
    path = urlparse(url).path.lower()
    if path.endswith(".pdf"):
        return True
    if content_type and "application/pdf" in content_type.lower():
        return True
    return False


def extract_bg_urls_from_css(css: str) -> list[str]:
    return [m.group(1).strip() for m in BG_IMAGE_RE.finditer(css or "")]


def resolve_robots(session: requests.Session, start: urlparse) -> RobotFileParser | None:
    robots_url = f"{start.scheme}://{start.netloc}/robots.txt"
    rp = RobotFileParser()
    try:
        resp = session.get(robots_url, timeout=15)
        if resp.status_code >= 400:
            return None
        rp.parse(resp.text.splitlines())
        return rp
    except requests.RequestException:
        return None


def fetch(
    session: requests.Session, url: str, stream: bool = False
) -> requests.Response | None:
    try:
        resp = session.get(url, timeout=25, allow_redirects=True, stream=stream)
        if resp.status_code >= 400:
            return None
        return resp
    except requests.RequestException:
        return None


def check_html_page(page_url: str, html: str, base: urlparse) -> tuple[list[Issue], set[str], set[str]]:
    issues: list[Issue] = []
    links: set[str] = set()
    pdfs: set[str] = set()
    soup = BeautifulSoup(html, "html.parser")

    html_tag = soup.find("html")
    lang = (html_tag.get("lang") if html_tag else None) or ""
    if not str(lang).strip():
        issues.append(Issue(page_url, "missing_html_lang", "<html> has no lang attribute"))

    h1s = soup.find_all("h1")
    if not h1s:
        issues.append(Issue(page_url, "missing_h1", "page has no <h1>"))

    for img in soup.find_all("img"):
        src = (img.get("src") or img.get("data-src") or "").strip()
        abs_src = urljoin(page_url, src) if src else ""
        if img.has_attr("alt"):
            if str(img.get("alt", "")).strip() == "" and not img.get("role") == "presentation":
                # empty alt can be intentional for decorative; still report for review
                issues.append(
                    Issue(
                        page_url,
                        "empty_img_alt",
                        "img alt is empty (decorative?) — review",
                        abs_src,
                    )
                )
        else:
            issues.append(
                Issue(page_url, "missing_img_alt", "img missing alt attribute", abs_src)
            )

    # inline style background-image
    for el in soup.find_all(style=True):
        for bg in extract_bg_urls_from_css(el.get("style", "")):
            abs_bg = urljoin(page_url, bg)
            tag = el.name or "element"
            issues.append(
                Issue(
                    page_url,
                    "css_background_image",
                    f"<{tag}> uses CSS background-image (no alt possible; needs accessible text nearby)",
                    abs_bg,
                )
            )

    # <style> blocks
    for style_tag in soup.find_all("style"):
        for bg in extract_bg_urls_from_css(style_tag.string or ""):
            abs_bg = urljoin(page_url, bg)
            issues.append(
                Issue(
                    page_url,
                    "css_background_image",
                    "<style> declares background-image (no alt; needs accessible alternative)",
                    abs_bg,
                )
            )

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#"):
            continue
        abs_url = normalize_url(urljoin(page_url, href))
        parsed = urlparse(abs_url)
        if parsed.scheme in SKIP_SCHEMES:
            continue
        if not same_site(base, parsed):
            continue
        if is_pdf_url(abs_url):
            pdfs.add(abs_url)
        else:
            links.add(abs_url)

    # also catch direct pdf / asset links in other tags
    for tag in soup.find_all(["iframe", "embed", "object", "link", "source"]):
        for attr in ("href", "src", "data"):
            if not tag.has_attr(attr):
                continue
            abs_url = normalize_url(urljoin(page_url, tag[attr].strip()))
            parsed = urlparse(abs_url)
            if not same_site(base, parsed):
                continue
            if is_pdf_url(abs_url):
                pdfs.add(abs_url)

    return issues, links, pdfs


def _unwrap(obj):
    while isinstance(obj, IndirectObject):
        obj = obj.get_object()
    return obj


def collect_pdf_alts(obj, seen: set[int], alts: list[str]) -> None:
    obj = _unwrap(obj)
    if obj is None:
        return
    oid = id(obj)
    if oid in seen:
        return
    seen.add(oid)

    if isinstance(obj, DictionaryObject):
        if "/Alt" in obj:
            try:
                alts.append(str(obj["/Alt"]))
            except Exception:
                alts.append("(unreadable /Alt)")
        for key in obj:
            collect_pdf_alts(obj[key], seen, alts)
    elif isinstance(obj, list):
        for item in obj:
            collect_pdf_alts(item, seen, alts)


def check_pdf(session: requests.Session, pdf_url: str, tmp_dir: Path) -> list[Issue]:
    issues: list[Issue] = []
    resp = fetch(session, pdf_url, stream=True)
    if resp is None:
        issues.append(Issue(pdf_url, "pdf_fetch_failed", "could not download PDF", pdf_url))
        return issues

    ctype = resp.headers.get("Content-Type", "")
    if not is_pdf_url(pdf_url, ctype) and "pdf" not in ctype.lower():
        # still try if extension says pdf
        if not pdf_url.lower().endswith(".pdf"):
            issues.append(
                Issue(pdf_url, "pdf_not_pdf", f"URL does not look like PDF ({ctype})", pdf_url)
            )
            return issues

    safe_name = re.sub(r"[^\w.-]+", "_", urlparse(pdf_url).path.strip("/")) or "doc.pdf"
    if not safe_name.lower().endswith(".pdf"):
        safe_name += ".pdf"
    path = tmp_dir / safe_name
    try:
        with open(path, "wb") as f:
            for chunk in resp.iter_content(65536):
                if chunk:
                    f.write(chunk)
    except OSError as exc:
        issues.append(Issue(pdf_url, "pdf_save_failed", str(exc), pdf_url))
        return issues

    try:
        reader = PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001
        issues.append(Issue(pdf_url, "pdf_parse_failed", f"cannot parse PDF: {exc}", pdf_url))
        return issues

    meta = reader.metadata or {}
    title = (meta.get("/Title") if meta else None) or ""
    subject = (meta.get("/Subject") if meta else None) or ""
    title_s = str(title).strip() if title else ""
    subject_s = str(subject).strip() if subject else ""

    if not title_s and not subject_s:
        issues.append(
            Issue(
                pdf_url,
                "pdf_missing_description",
                "PDF has no Title or Subject metadata (description / 替代文字)",
                pdf_url,
            )
        )
    elif not subject_s and not title_s:
        pass
    else:
        # if only one is present, note lightly
        if not title_s:
            issues.append(
                Issue(
                    pdf_url,
                    "pdf_missing_title",
                    "PDF has Subject but no Title metadata",
                    pdf_url,
                )
            )
        if not subject_s:
            issues.append(
                Issue(
                    pdf_url,
                    "pdf_missing_subject",
                    "PDF has Title but no Subject (description) metadata",
                    pdf_url,
                )
            )

    # Marked/Tagged PDF?
    root = reader.trailer.get("/Root") if reader.trailer else None
    root = _unwrap(root) if root else None
    marked = False
    if isinstance(root, DictionaryObject):
        markinfo = _unwrap(root.get("/MarkInfo")) if "/MarkInfo" in root else None
        if isinstance(markinfo, DictionaryObject) and markinfo.get("/Marked") is True:
            marked = True
    if not marked:
        issues.append(
            Issue(
                pdf_url,
                "pdf_not_tagged",
                "PDF does not appear to be a tagged/marked PDF (harder for screen readers)",
                pdf_url,
            )
        )

    alts: list[str] = []
    try:
        if root is not None:
            collect_pdf_alts(root, set(), alts)
        for page in reader.pages:
            collect_pdf_alts(page.get_object(), set(), alts)
    except Exception:  # noqa: BLE001
        pass

    nonempty_alts = [a for a in alts if str(a).strip()]
    if not nonempty_alts:
        issues.append(
            Issue(
                pdf_url,
                "pdf_missing_alt_text",
                "no /Alt alternative text found on PDF structure or images",
                pdf_url,
            )
        )

    return issues


def crawl(
    start_url: str,
    max_pages: int,
    delay: float,
    respect_robots: bool,
    out_dir: Path,
) -> CrawlReport:
    start_url = normalize_url(start_url)
    base = urlparse(start_url)
    if base.scheme not in {"http", "https"} or not base.netloc:
        raise SystemExit(f"Need an http(s) start URL, got: {start_url}")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})

    robots = resolve_robots(session, base) if respect_robots else None
    report = CrawlReport(start_url=start_url)
    queue: deque[str] = deque([start_url])
    seen_pages: set[str] = set()
    seen_pdfs: set[str] = set()
    pdf_tmp = out_dir / "pdf_cache"
    pdf_tmp.mkdir(parents=True, exist_ok=True)

    while queue and len(seen_pages) < max_pages:
        url = queue.popleft()
        if url in seen_pages:
            continue
        if robots and not robots.can_fetch(USER_AGENT, url):
            report.issues.append(Issue(url, "robots_blocked", "blocked by robots.txt"))
            seen_pages.add(url)
            continue

        resp = fetch(session, url)
        time.sleep(delay)
        if resp is None:
            report.issues.append(Issue(url, "fetch_failed", "could not fetch page"))
            seen_pages.add(url)
            continue

        final_url = normalize_url(resp.url)
        ctype = resp.headers.get("Content-Type", "")
        seen_pages.add(url)
        seen_pages.add(final_url)

        if is_pdf_url(final_url, ctype):
            if final_url not in seen_pdfs:
                seen_pdfs.add(final_url)
                report.pdfs.append(final_url)
                report.issues.extend(check_pdf(session, final_url, pdf_tmp))
                report.pdfs_checked += 1
            continue

        if "html" not in ctype.lower() and not final_url.endswith(
            (".html", ".htm", "/")
        ):
            # skip non-html assets discovered as pages
            continue

        report.pages.append(final_url)
        report.pages_crawled += 1
        page_issues, links, pdfs = check_html_page(final_url, resp.text, base)
        report.issues.extend(page_issues)

        for link in sorted(links):
            if link not in seen_pages:
                queue.append(link)
        for pdf in sorted(pdfs):
            if pdf not in seen_pdfs:
                seen_pdfs.add(pdf)
                report.pdfs.append(pdf)
                report.issues.extend(check_pdf(session, pdf, pdf_tmp))
                report.pdfs_checked += 1
                time.sleep(delay)

    return report


def write_outputs(report: CrawlReport, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "report.json"
    csv_path = out_dir / "issues.csv"
    summary_path = out_dir / "summary.txt"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "start_url": report.start_url,
                "pages_crawled": report.pages_crawled,
                "pdfs_checked": report.pdfs_checked,
                "pages": report.pages,
                "pdfs": report.pdfs,
                "issues": [asdict(i) for i in report.issues],
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["page_url", "kind", "detail", "resource"])
        w.writeheader()
        for i in report.issues:
            w.writerow(asdict(i))

    counts: dict[str, int] = {}
    for i in report.issues:
        counts[i.kind] = counts.get(i.kind, 0) + 1

    lines = [
        f"Start URL: {report.start_url}",
        f"Pages crawled: {report.pages_crawled}",
        f"PDFs checked: {report.pdfs_checked}",
        f"Issues: {len(report.issues)}",
        "",
        "By kind:",
    ]
    for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"  {k}: {v}")
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Crawl a site and check basic a11y signals")
    p.add_argument("start_url", help="https://example.com/")
    p.add_argument("--max-pages", type=int, default=200)
    p.add_argument("--delay", type=float, default=0.3, help="seconds between requests")
    p.add_argument("--no-robots", action="store_true", help="ignore robots.txt")
    p.add_argument("-o", "--out", default="a11y_report", help="output directory")
    return p.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = Path(args.out)
    report = crawl(
        start_url=args.start_url,
        max_pages=args.max_pages,
        delay=args.delay,
        respect_robots=not args.no_robots,
        out_dir=out_dir,
    )
    write_outputs(report, out_dir)
    print(f"Crawled {report.pages_crawled} pages, {report.pdfs_checked} PDFs")
    print(f"Found {len(report.issues)} issues")
    print(f"Wrote {out_dir / 'summary.txt'}, issues.csv, report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
