#!/usr/bin/env python3
"""Crawl a domain and report WCAG-oriented accessibility signals (2.1 AA focus).

HTML pages:
  - html[lang] (3.1.1); note when mixed scripts lack local lang (3.1.2)
  - headings: missing h1, multiple h1, skipped levels (1.3.1 / 2.4.6)
  - img alt: missing attr = critical; empty alt = review (decorative only) (1.1.1)
  - CSS background-image: check aria-label / nearby text (1.1.1)

PDFs:
  - Title/Subject metadata is advisory only (not a11y alt text)
  - tagged/marked PDF + /Alt on figures (1.1.1 / 1.3.1)
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from pypdf.generic import DictionaryObject, IndirectObject

USER_AGENT = "WebsiteA11yCheck/1.2 (+WCAG 2.1 AA crawl)"
BG_IMAGE_RE = re.compile(
    r"background(?:-image)?\s*:\s*[^;]*url\s*\(\s*[\'\"]?([^)\'\"]+)[\'\"]?\s*\)",
    re.IGNORECASE,
)
SKIP_SCHEMES = {"mailto", "tel", "javascript", "data", "blob"}
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
LATIN_RE = re.compile(r"[A-Za-z]{3,}")


@dataclass
class Issue:
    page_url: str
    kind: str
    severity: str  # critical | serious | moderate | review
    wcag: str
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


def visible_text(el) -> str:
    return " ".join(el.stripped_strings) if el else ""


def has_accessible_name(el) -> bool:
    if not el:
        return False
    for attr in ("aria-label", "aria-labelledby", "title"):
        val = el.get(attr)
        if val and str(val).strip():
            return True
    if visible_text(el).strip():
        return True
    # parent with aria-label often wraps icon backgrounds
    parent = el.parent
    if parent and parent.name not in {None, "[document]", "body", "html"}:
        for attr in ("aria-label", "aria-labelledby", "title"):
            val = parent.get(attr)
            if val and str(val).strip():
                return True
        # short nearby text on parent
        if visible_text(parent).strip():
            return True
    return False


def check_headings(page_url: str, soup: BeautifulSoup) -> list[Issue]:
    issues: list[Issue] = []
    headings = []
    for level in range(1, 7):
        for h in soup.find_all(f"h{level}"):
            headings.append(level)

    h1_count = sum(1 for lv in headings if lv == 1)
    if h1_count == 0:
        issues.append(
            Issue(
                page_url,
                "missing_h1",
                "serious",
                "1.3.1 / 2.4.6",
                "page has no <h1>",
            )
        )
    elif h1_count > 1:
        issues.append(
            Issue(
                page_url,
                "multiple_h1",
                "moderate",
                "1.3.1 / 2.4.6",
                f"page has {h1_count} <h1> elements (prefer one main heading)",
            )
        )

    # skipped levels in document order
    prev = 0
    for lv in headings:
        if prev and lv > prev + 1:
            issues.append(
                Issue(
                    page_url,
                    "heading_level_skip",
                    "moderate",
                    "1.3.1 / 2.4.6",
                    f"heading jumps from h{prev} to h{lv}",
                )
            )
            break  # one skip note per page is enough signal
        prev = lv
    return issues


def check_lang(page_url: str, soup: BeautifulSoup, html: str) -> list[Issue]:
    issues: list[Issue] = []
    html_tag = soup.find("html")
    lang = (html_tag.get("lang") if html_tag else None) or ""
    if not str(lang).strip():
        issues.append(
            Issue(
                page_url,
                "missing_html_lang",
                "critical",
                "3.1.1",
                "<html> has no lang attribute",
            )
        )

    # mixed CJK + Latin without any local lang attributes
    sample = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)[:8000]
    has_cjk = bool(CJK_RE.search(sample))
    has_latin = bool(LATIN_RE.search(sample))
    local_langs = soup.find_all(attrs={"lang": True})
    # exclude the html root itself
    local_langs = [el for el in local_langs if el.name != "html"]
    if has_cjk and has_latin and not local_langs:
        issues.append(
            Issue(
                page_url,
                "mixed_language_no_local_lang",
                "review",
                "3.1.2",
                "page appears to mix Chinese and English without local lang attributes",
            )
        )
    return issues


def check_html_page(
    page_url: str, html: str, base: urlparse
) -> tuple[list[Issue], set[str], set[str]]:
    issues: list[Issue] = []
    links: set[str] = set()
    pdfs: set[str] = set()
    soup = BeautifulSoup(html, "html.parser")

    issues.extend(check_lang(page_url, soup, html))
    issues.extend(check_headings(page_url, soup))

    for img in soup.find_all("img"):
        src = (img.get("src") or img.get("data-src") or "").strip()
        abs_src = urljoin(page_url, src) if src else ""
        role = (img.get("role") or "").lower()
        if img.has_attr("alt"):
            alt = str(img.get("alt", ""))
            if alt.strip() == "":
                if role == "presentation" or role == "none":
                    continue  # explicitly decorative
                issues.append(
                    Issue(
                        page_url,
                        "empty_img_alt",
                        "review",
                        "1.1.1",
                        "img alt is empty — OK only if decorative; informative images need meaningful text",
                        abs_src,
                    )
                )
        else:
            issues.append(
                Issue(
                    page_url,
                    "missing_img_alt",
                    "critical",
                    "1.1.1",
                    "img missing alt attribute",
                    abs_src,
                )
            )

    # inline style background-image
    for el in soup.find_all(style=True):
        bgs = extract_bg_urls_from_css(el.get("style", ""))
        if not bgs:
            continue
        tag = el.name or "element"
        if has_accessible_name(el):
            issues.append(
                Issue(
                    page_url,
                    "css_background_image_ok_name",
                    "review",
                    "1.1.1",
                    f"<{tag}> has CSS background-image and an accessible name nearby — confirm equivalence",
                    urljoin(page_url, bgs[0]),
                )
            )
        else:
            for bg in bgs:
                issues.append(
                    Issue(
                        page_url,
                        "css_background_image_missing_text",
                        "serious",
                        "1.1.1",
                        f"<{tag}> CSS background-image may lack equivalent text / aria-label",
                        urljoin(page_url, bg),
                    )
                )

    # <style> blocks — cannot bind to one element; flag as review
    for style_tag in soup.find_all("style"):
        for bg in extract_bg_urls_from_css(style_tag.string or ""):
            issues.append(
                Issue(
                    page_url,
                    "css_background_image_in_stylesheet",
                    "review",
                    "1.1.1",
                    "<style> declares background-image — verify visible/accessible equivalent on the page",
                    urljoin(page_url, bg),
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
        issues.append(
            Issue(pdf_url, "pdf_fetch_failed", "moderate", "", "could not download PDF", pdf_url)
        )
        return issues

    ctype = resp.headers.get("Content-Type", "")
    if not is_pdf_url(pdf_url, ctype) and "pdf" not in ctype.lower():
        if not pdf_url.lower().endswith(".pdf"):
            issues.append(
                Issue(
                    pdf_url,
                    "pdf_not_pdf",
                    "review",
                    "",
                    f"URL does not look like PDF ({ctype})",
                    pdf_url,
                )
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
        issues.append(
            Issue(pdf_url, "pdf_save_failed", "moderate", "", str(exc), pdf_url)
        )
        return issues

    try:
        reader = PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001
        issues.append(
            Issue(
                pdf_url,
                "pdf_parse_failed",
                "moderate",
                "",
                f"cannot parse PDF: {exc}",
                pdf_url,
            )
        )
        return issues

    # Metadata is advisory only — not a substitute for accessible alternative text
    meta = reader.metadata or {}
    title = str(meta.get("/Title") or "").strip()
    subject = str(meta.get("/Subject") or "").strip()
    if not title and not subject:
        issues.append(
            Issue(
                pdf_url,
                "pdf_missing_metadata_description",
                "review",
                "",
                "PDF has no Title/Subject metadata (helpful, but NOT WCAG alternative text)",
                pdf_url,
            )
        )

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
                "serious",
                "1.3.1",
                "PDF does not appear tagged/marked — structure may be inaccessible",
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
                "serious",
                "1.1.1",
                "no /Alt alternative text found on PDF figures/structure",
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
    workers: int = 1,
) -> CrawlReport:
    start_url = normalize_url(start_url)
    base = urlparse(start_url)
    if base.scheme not in {"http", "https"} or not base.netloc:
        raise SystemExit(f"Need an http(s) start URL, got: {start_url}")

    workers = max(1, int(workers))
    bootstrap = requests.Session()
    bootstrap.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
    robots = resolve_robots(bootstrap, base) if respect_robots else None

    report = CrawlReport(start_url=start_url)
    lock = threading.Lock()
    rate_lock = threading.Lock()
    last_request = 0.0
    seen_pages: set[str] = set()
    seen_pdfs: set[str] = set()
    pages_attempted = 0
    pdf_tmp = out_dir / "pdf_cache"
    pdf_tmp.mkdir(parents=True, exist_ok=True)

    def throttle() -> None:
        nonlocal last_request
        with rate_lock:
            now = time.monotonic()
            wait_for = delay - (now - last_request)
            if wait_for > 0:
                time.sleep(wait_for)
            last_request = time.monotonic()

    def make_session() -> requests.Session:
        s = requests.Session()
        s.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
        return s

    def claim_page(url: str) -> bool:
        """Return True if this worker should fetch the URL as a page candidate."""
        nonlocal pages_attempted
        with lock:
            if url in seen_pages:
                return False
            if pages_attempted >= max_pages:
                return False
            seen_pages.add(url)
            pages_attempted += 1
            return True

    def claim_pdf(url: str) -> bool:
        with lock:
            if url in seen_pdfs:
                return False
            seen_pdfs.add(url)
            report.pdfs.append(url)
            return True

    def enqueue_links(links: set[str], pdfs: set[str], submit_page, submit_pdf) -> None:
        for link in sorted(links):
            with lock:
                already = link in seen_pages
                at_cap = pages_attempted >= max_pages
            if not already and not at_cap:
                submit_page(link)
        for pdf in sorted(pdfs):
            with lock:
                already = pdf in seen_pdfs
            if not already:
                submit_pdf(pdf)

    def process_page(url: str) -> tuple[set[str], set[str]]:
        if not claim_page(url):
            return set(), set()

        if robots and not robots.can_fetch(USER_AGENT, url):
            with lock:
                report.issues.append(
                    Issue(url, "robots_blocked", "review", "", "blocked by robots.txt")
                )
            return set(), set()

        session = make_session()
        throttle()
        resp = fetch(session, url)
        if resp is None:
            with lock:
                report.issues.append(
                    Issue(url, "fetch_failed", "moderate", "", "could not fetch page")
                )
            return set(), set()

        final_url = normalize_url(resp.url)
        ctype = resp.headers.get("Content-Type", "")
        with lock:
            seen_pages.add(final_url)

        if is_pdf_url(final_url, ctype):
            # redirected to PDF
            if claim_pdf(final_url):
                throttle()
                pdf_issues = check_pdf(session, final_url, pdf_tmp)
                with lock:
                    report.issues.extend(pdf_issues)
                    report.pdfs_checked += 1
            return set(), set()

        if "html" not in ctype.lower() and not final_url.endswith((".html", ".htm", "/")):
            return set(), set()

        page_issues, links, pdfs = check_html_page(final_url, resp.text, base)
        with lock:
            report.pages.append(final_url)
            report.pages_crawled += 1
            report.issues.extend(page_issues)
        return links, pdfs

    def process_pdf(url: str) -> tuple[set[str], set[str]]:
        if not claim_pdf(url):
            return set(), set()
        session = make_session()
        throttle()
        pdf_issues = check_pdf(session, url, pdf_tmp)
        with lock:
            report.issues.extend(pdf_issues)
            report.pdfs_checked += 1
        return set(), set()

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="a11y") as pool:
        futures = set()

        def submit_page(u: str) -> None:
            futures.add(pool.submit(process_page, u))

        def submit_pdf(u: str) -> None:
            futures.add(pool.submit(process_pdf, u))

        submit_page(start_url)
        while futures:
            done, futures = wait(futures, return_when=FIRST_COMPLETED)
            for fut in done:
                try:
                    links, pdfs = fut.result()
                except Exception as exc:  # noqa: BLE001
                    with lock:
                        report.issues.append(
                            Issue(
                                start_url,
                                "worker_error",
                                "moderate",
                                "",
                                f"worker failed: {exc}",
                            )
                        )
                    continue
                enqueue_links(links, pdfs, submit_page, submit_pdf)

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

    fields = ["page_url", "kind", "severity", "wcag", "detail", "resource"]
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for i in report.issues:
            w.writerow(asdict(i))

    by_sev: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for i in report.issues:
        by_sev[i.severity] = by_sev.get(i.severity, 0) + 1
        by_kind[i.kind] = by_kind.get(i.kind, 0) + 1

    order = ["critical", "serious", "moderate", "review"]
    lines = [
        f"Start URL: {report.start_url}",
        f"Pages crawled: {report.pages_crawled}",
        f"PDFs checked: {report.pdfs_checked}",
        f"Issues: {len(report.issues)}",
        "",
        "By severity:",
    ]
    for sev in order:
        if sev in by_sev:
            lines.append(f"  {sev}: {by_sev[sev]}")
    lines.append("")
    lines.append("By kind:")
    for k, v in sorted(by_kind.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"  {k}: {v}")
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Crawl a site and check WCAG-oriented a11y signals"
    )
    p.add_argument("start_url", help="https://example.com/")
    p.add_argument("--max-pages", type=int, default=200)
    p.add_argument("--delay", type=float, default=0.3, help="min seconds between requests (global)")
    p.add_argument(
        "--workers",
        type=int,
        default=1,
        help="parallel fetch workers (default 1 = sequential)",
    )
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
        workers=args.workers,
    )
    write_outputs(report, out_dir)
    print(f"Crawled {report.pages_crawled} pages, {report.pdfs_checked} PDFs")
    print(f"Found {len(report.issues)} issues")
    print(f"Wrote {out_dir / 'summary.txt'}, issues.csv, report.json")
    return 0



if __name__ == "__main__":
    sys.exit(main())
