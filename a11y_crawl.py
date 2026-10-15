#!/usr/bin/env python3
"""Crawl a domain and report WCAG-oriented accessibility signals (2.1 AA focus).

Works on Windows / macOS / Linux. Shows live progress. Supports Ctrl+C checkpoint
and `--resume` to continue later.

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
import signal
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from pypdf.generic import DictionaryObject, IndirectObject

USER_AGENT = "WebsiteA11yCheck/1.4 (+WCAG 2.1 AA crawl)"
BG_IMAGE_RE = re.compile(
    r"background(?:-image)?\s*:\s*[^;]*url\s*\(\s*[\'\"]?([^)\'\"]+)[\'\"]?\s*\)",
    re.IGNORECASE,
)
SKIP_SCHEMES = {"mailto", "tel", "javascript", "data", "blob"}
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
LATIN_RE = re.compile(r"[A-Za-z]{3,}")
CHECKPOINT_NAME = "checkpoint.json"


def configure_stdio() -> None:
    """Make Unicode status lines work on Windows consoles."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


@dataclass
class Issue:
    page_url: str
    kind: str
    severity: str  # critical | serious | moderate | review
    wcag: str
    detail: str
    resource: str = ""


@dataclass
class AccessError:
    """Failed to open/download/parse a page or PDF (separate from a11y issues)."""
    url: str
    resource_type: str  # page | pdf
    error_type: str  # http_error | timeout | connection | robots | save | parse | unknown
    message: str
    status_code: int | None = None
    final_url: str = ""


@dataclass
class CrawlReport:
    start_url: str
    pages_crawled: int = 0
    pdfs_checked: int = 0
    issues: list[Issue] = field(default_factory=list)
    access_errors: list[AccessError] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    pdfs: list[str] = field(default_factory=list)


class Progress:
    """Thread-safe console progress for Windows and Unix terminals."""

    def __init__(self, enabled: bool, max_pages: int) -> None:
        self.enabled = enabled
        self.max_pages = max_pages
        self._lock = threading.Lock()
        self.current = ""
        self.in_flight = 0
        self.started = time.monotonic()
        self._last_draw = 0.0
        self._tty = bool(getattr(sys.stderr, "isatty", lambda: False)())

    def set_current(self, label: str) -> None:
        with self._lock:
            self.current = label
            self._draw(force=False)

    def delta_inflight(self, n: int) -> None:
        with self._lock:
            self.in_flight += n
            self._draw(force=False)

    def tick(self, report: CrawlReport, frontier: int, force: bool = False) -> None:
        with self._lock:
            self._draw(report, frontier, force=force)

    def line(self, msg: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._tty:
                sys.stderr.write("\r" + " " * 100 + "\r")
            sys.stderr.write(msg + "\n")
            sys.stderr.flush()

    def _draw(
        self,
        report: CrawlReport | None = None,
        frontier: int = 0,
        force: bool = False,
    ) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if not force and (now - self._last_draw) < 0.15:
            return
        self._last_draw = now
        if report is None:
            return
        elapsed = max(0.1, now - self.started)
        rate = report.pages_crawled / elapsed
        cur = self.current
        if len(cur) > 48:
            cur = cur[:45] + "..."
        msg = (
            f"pages {report.pages_crawled}/{self.max_pages} | "
            f"pdfs {report.pdfs_checked} | "
            f"issues {len(report.issues)} | "
            f"errors {len(report.access_errors)} | "
            f"queue {frontier} | "
            f"inflight {self.in_flight} | "
            f"{rate:.1f} p/s | "
            f"{cur}"
        )
        if self._tty:
            sys.stderr.write("\r" + msg[:120].ljust(120))
            sys.stderr.flush()
        else:
            # non-TTY (piped / some Windows redirects): periodic full lines
            if force or report.pages_crawled % 5 == 0:
                sys.stderr.write(msg + "\n")
                sys.stderr.flush()

    def finish(self) -> None:
        if self.enabled and self._tty:
            sys.stderr.write("\n")
            sys.stderr.flush()


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
    session: requests.Session,
    url: str,
    *,
    resource_type: str = "page",
    stream: bool = False,
) -> tuple[requests.Response | None, AccessError | None]:
    try:
        resp = session.get(url, timeout=25, allow_redirects=True, stream=stream)
        if resp.status_code >= 400:
            return None, AccessError(
                url=url,
                resource_type=resource_type,
                error_type="http_error",
                message=f"HTTP {resp.status_code}",
                status_code=resp.status_code,
                final_url=str(resp.url),
            )
        return resp, None
    except requests.Timeout as exc:
        return None, AccessError(
            url=url,
            resource_type=resource_type,
            error_type="timeout",
            message=str(exc) or "request timed out",
        )
    except requests.ConnectionError as exc:
        return None, AccessError(
            url=url,
            resource_type=resource_type,
            error_type="connection",
            message=str(exc) or "connection failed",
        )
    except requests.RequestException as exc:
        return None, AccessError(
            url=url,
            resource_type=resource_type,
            error_type="unknown",
            message=str(exc) or "request failed",
        )


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
    parent = el.parent
    if parent and parent.name not in {None, "[document]", "body", "html"}:
        for attr in ("aria-label", "aria-labelledby", "title"):
            val = parent.get(attr)
            if val and str(val).strip():
                return True
        if visible_text(parent).strip():
            return True
    return False


def check_headings(page_url: str, soup: BeautifulSoup) -> list[Issue]:
    issues: list[Issue] = []
    headings = []
    for level in range(1, 7):
        for _h in soup.find_all(f"h{level}"):
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
            break
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

    sample = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)[:8000]
    has_cjk = bool(CJK_RE.search(sample))
    has_latin = bool(LATIN_RE.search(sample))
    local_langs = [el for el in soup.find_all(attrs={"lang": True}) if el.name != "html"]
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
                if role in {"presentation", "none"}:
                    continue
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


def check_pdf(
    session: requests.Session, pdf_url: str, tmp_dir: Path
) -> tuple[list[Issue], list[AccessError]]:
    issues: list[Issue] = []
    access_errors: list[AccessError] = []
    resp, err = fetch(session, pdf_url, resource_type="pdf", stream=True)
    if err is not None:
        access_errors.append(err)
        return issues, access_errors
    assert resp is not None

    ctype = resp.headers.get("Content-Type", "")
    if not is_pdf_url(pdf_url, ctype) and "pdf" not in ctype.lower():
        if not pdf_url.lower().endswith(".pdf"):
            access_errors.append(
                AccessError(
                    url=pdf_url,
                    resource_type="pdf",
                    error_type="not_pdf",
                    message=f"URL does not look like PDF ({ctype})",
                    final_url=str(resp.url),
                )
            )
            return issues, access_errors

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
        access_errors.append(
            AccessError(
                url=pdf_url,
                resource_type="pdf",
                error_type="save",
                message=str(exc),
                final_url=str(resp.url),
            )
        )
        return issues, access_errors

    try:
        reader = PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001
        access_errors.append(
            AccessError(
                url=pdf_url,
                resource_type="pdf",
                error_type="parse",
                message=f"cannot parse PDF: {exc}",
                final_url=str(resp.url),
            )
        )
        return issues, access_errors

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

    return issues, access_errors



def issue_from_dict(d: dict[str, Any]) -> Issue:
    return Issue(
        page_url=d.get("page_url", ""),
        kind=d.get("kind", ""),
        severity=d.get("severity", "review"),
        wcag=d.get("wcag", ""),
        detail=d.get("detail", ""),
        resource=d.get("resource", ""),
    )


def access_error_from_dict(d: dict[str, Any]) -> AccessError:
    return AccessError(
        url=d.get("url", ""),
        resource_type=d.get("resource_type", "page"),
        error_type=d.get("error_type", "unknown"),
        message=d.get("message", ""),
        status_code=d.get("status_code"),
        final_url=d.get("final_url", ""),
    )


def save_checkpoint(
    path: Path,
    *,
    start_url: str,
    report: CrawlReport,
    seen_pages: set[str],
    seen_pdfs: set[str],
    pages_attempted: int,
    frontier_pages: list[str],
    frontier_pdfs: list[str],
    max_pages: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "start_url": start_url,
        "max_pages": max_pages,
        "pages_attempted": pages_attempted,
        "pages_crawled": report.pages_crawled,
        "pdfs_checked": report.pdfs_checked,
        "pages": report.pages,
        "pdfs": report.pdfs,
        "seen_pages": sorted(seen_pages),
        "seen_pdfs": sorted(seen_pdfs),
        "frontier_pages": frontier_pages,
        "frontier_pdfs": frontier_pdfs,
        "issues": [asdict(i) for i in report.issues],
        "access_errors": [asdict(e) for e in report.access_errors],
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(path)  # atomic on Windows when same volume


def load_checkpoint(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def crawl(
    start_url: str,
    max_pages: int,
    delay: float,
    respect_robots: bool,
    out_dir: Path,
    workers: int = 1,
    show_progress: bool = True,
    resume: bool = False,
    checkpoint_every: int = 10,
) -> CrawlReport:
    start_url = normalize_url(start_url)
    base = urlparse(start_url)
    if base.scheme not in {"http", "https"} or not base.netloc:
        raise SystemExit(f"Need an http(s) start URL, got: {start_url}")

    workers = max(1, int(workers))
    checkpoint_path = out_dir / CHECKPOINT_NAME
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_tmp = out_dir / "pdf_cache"
    pdf_tmp.mkdir(parents=True, exist_ok=True)

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
    stop = threading.Event()
    in_flight_pages: set[str] = set()
    in_flight_pdfs: set[str] = set()
    pending_pages: set[str] = set()
    pending_pdfs: set[str] = set()
    progress = Progress(show_progress, max_pages)

    if resume and checkpoint_path.is_file():
        data = load_checkpoint(checkpoint_path)
        ck_start = normalize_url(data.get("start_url", ""))
        if ck_start and ck_start != start_url:
            raise SystemExit(
                f"Checkpoint start_url {ck_start!r} != {start_url!r}. "
                "Use the same URL or delete checkpoint.json."
            )
        report.pages = list(data.get("pages", []))
        report.pdfs = list(data.get("pdfs", []))
        report.pages_crawled = int(data.get("pages_crawled", 0))
        report.pdfs_checked = int(data.get("pdfs_checked", 0))
        report.issues = [issue_from_dict(i) for i in data.get("issues", [])]
        report.access_errors = [
            access_error_from_dict(e) for e in data.get("access_errors", [])
        ]
        seen_pages = set(data.get("seen_pages", []))
        seen_pdfs = set(data.get("seen_pdfs", []))
        pages_attempted = int(data.get("pages_attempted", 0))
        pending_pages = set(data.get("frontier_pages", []))
        pending_pdfs = set(data.get("frontier_pdfs", []))
        progress.line(
            f"Resumed checkpoint: {report.pages_crawled} pages, "
            f"{report.pdfs_checked} PDFs, {len(report.issues)} issues, "
            f"{len(report.access_errors)} access errors, "
            f"{len(pending_pages)} pages queued"
        )
    else:
        pending_pages.add(start_url)

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

    def frontier_counts() -> tuple[int, int]:
        with lock:
            return len(pending_pages) + len(in_flight_pages), len(pending_pdfs) + len(
                in_flight_pdfs
            )

    def snapshot_frontier() -> tuple[list[str], list[str]]:
        with lock:
            pages = sorted(pending_pages | in_flight_pages)
            pdfs = sorted(pending_pdfs | in_flight_pdfs)
            return pages, pdfs

    def persist(reason: str) -> None:
        fp, fpdf = snapshot_frontier()
        with lock:
            save_checkpoint(
                checkpoint_path,
                start_url=start_url,
                report=report,
                seen_pages=set(seen_pages),
                seen_pdfs=set(seen_pdfs),
                pages_attempted=pages_attempted,
                frontier_pages=fp,
                frontier_pdfs=fpdf,
                max_pages=max_pages,
            )
        progress.line(f"Checkpoint saved ({reason}): {checkpoint_path}")

    def request_stop(signum=None, frame=None) -> None:  # noqa: ANN001
        if not stop.is_set():
            progress.line("Interrupt received — finishing in-flight work, then saving…")
            stop.set()

    # Ctrl+C / Ctrl+Break on Windows
    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, request_stop)  # type: ignore[attr-defined]
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    def claim_page(url: str) -> bool:
        nonlocal pages_attempted
        with lock:
            if stop.is_set():
                return False
            if url in seen_pages and url not in in_flight_pages:
                # already completed (seen and not currently in flight reclaim)
                if url not in pending_pages:
                    return False
            if pages_attempted >= max_pages and url not in in_flight_pages:
                # allow in-flight reclaim after interrupt? keep simple: block new
                if url not in seen_pages:
                    return False
            pending_pages.discard(url)
            if url in seen_pages and url not in in_flight_pages:
                # completed page
                return False
            if url not in seen_pages:
                seen_pages.add(url)
                pages_attempted += 1
            in_flight_pages.add(url)
            return True

    def finish_page_claim(url: str, success_html: bool) -> None:
        with lock:
            in_flight_pages.discard(url)
            if not success_html and stop.is_set():
                # put back for resume if we didn't finish HTML analysis
                pending_pages.add(url)
                seen_pages.discard(url)
                # pages_attempted stays — approximate; recalculate optional

    def claim_pdf(url: str) -> bool:
        with lock:
            if stop.is_set() and url not in in_flight_pdfs:
                pending_pdfs.add(url)
                return False
            if url in seen_pdfs and url not in in_flight_pdfs:
                pending_pdfs.discard(url)
                return False
            pending_pdfs.discard(url)
            seen_pdfs.add(url)
            if url not in report.pdfs:
                report.pdfs.append(url)
            in_flight_pdfs.add(url)
            return True

    def finish_pdf_claim(url: str, done: bool) -> None:
        with lock:
            in_flight_pdfs.discard(url)
            if not done:
                pending_pdfs.add(url)
                seen_pdfs.discard(url)
                if url in report.pdfs:
                    report.pdfs.remove(url)

    def process_page(url: str) -> tuple[set[str], set[str]]:
        if not claim_page(url):
            return set(), set()
        progress.delta_inflight(1)
        progress.set_current(f"GET {url}")
        ok = False
        try:
            if robots and not robots.can_fetch(USER_AGENT, url):
                with lock:
                    report.access_errors.append(
                        AccessError(
                            url=url,
                            resource_type="page",
                            error_type="robots",
                            message="blocked by robots.txt",
                        )
                    )
                ok = True
                return set(), set()

            session = make_session()
            throttle()
            if stop.is_set():
                return set(), set()
            resp, err = fetch(session, url, resource_type="page")
            if err is not None:
                with lock:
                    report.access_errors.append(err)
                ok = True
                return set(), set()
            assert resp is not None

            final_url = normalize_url(resp.url)
            ctype = resp.headers.get("Content-Type", "")
            with lock:
                seen_pages.add(final_url)

            if is_pdf_url(final_url, ctype):
                if claim_pdf(final_url):
                    progress.set_current(f"PDF {final_url}")
                    throttle()
                    pdf_ok = False
                    try:
                        pdf_issues, pdf_errs = check_pdf(session, final_url, pdf_tmp)
                        with lock:
                            report.issues.extend(pdf_issues)
                            report.access_errors.extend(pdf_errs)
                            report.pdfs_checked += 1
                        pdf_ok = True
                    finally:
                        finish_pdf_claim(final_url, pdf_ok)
                ok = True
                return set(), set()

            if "html" not in ctype.lower() and not final_url.endswith(
                (".html", ".htm", "/")
            ):
                ok = True
                return set(), set()

            page_issues, links, pdfs = check_html_page(final_url, resp.text, base)
            with lock:
                if final_url not in report.pages:
                    report.pages.append(final_url)
                    report.pages_crawled += 1
                report.issues.extend(page_issues)
            ok = True
            return links, pdfs
        finally:
            finish_page_claim(url, ok)
            progress.delta_inflight(-1)
            fp, _ = frontier_counts()
            with lock:
                progress.tick(report, fp)

    def process_pdf(url: str) -> tuple[set[str], set[str]]:
        if not claim_pdf(url):
            return set(), set()
        progress.delta_inflight(1)
        progress.set_current(f"PDF {url}")
        done = False
        try:
            session = make_session()
            throttle()
            if stop.is_set():
                return set(), set()
            pdf_issues, pdf_errs = check_pdf(session, url, pdf_tmp)
            with lock:
                report.issues.extend(pdf_issues)
                report.access_errors.extend(pdf_errs)
                report.pdfs_checked += 1
            done = True
            return set(), set()
        finally:
            finish_pdf_claim(url, done)
            progress.delta_inflight(-1)
            fp, _ = frontier_counts()
            with lock:
                progress.tick(report, fp)

    interrupted = False
    completed_since_ckpt = 0

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="a11y") as pool:
        futures: set = set()

        def submit_page(u: str) -> None:
            with lock:
                if stop.is_set():
                    pending_pages.add(u)
                    return
                if u in seen_pages and u not in in_flight_pages and u not in pending_pages:
                    return
                pending_pages.add(u)
            futures.add(pool.submit(process_page, u))

        def submit_pdf(u: str) -> None:
            with lock:
                if stop.is_set():
                    pending_pdfs.add(u)
                    return
                if u in seen_pdfs and u not in in_flight_pdfs:
                    return
                pending_pdfs.add(u)
            futures.add(pool.submit(process_pdf, u))

        for u in sorted(pending_pages):
            submit_page(u)
        for u in sorted(pending_pdfs):
            submit_pdf(u)
        pending_pages.clear()
        pending_pdfs.clear()

        progress.line(
            f"Crawl start: {start_url} | workers={workers} | max_pages={max_pages} | delay={delay}s"
        )

        while futures:
            if stop.is_set():
                interrupted = True
                # let in-flight finish; do not submit new from results
                done, futures = wait(futures, return_when=FIRST_COMPLETED)
                for fut in done:
                    try:
                        fut.result()
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
                # cancel remaining if any still queued in executor
                for fut in list(futures):
                    fut.cancel()
                break

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
                completed_since_ckpt += 1
                if not stop.is_set():
                    for link in sorted(links):
                        with lock:
                            already = link in seen_pages or link in in_flight_pages
                            at_cap = pages_attempted >= max_pages
                        if not already and not at_cap:
                            submit_page(link)
                    for pdf in sorted(pdfs):
                        with lock:
                            already = pdf in seen_pdfs or pdf in in_flight_pdfs
                        if not already:
                            submit_pdf(pdf)
                else:
                    with lock:
                        pending_pages.update(links)
                        pending_pdfs.update(pdfs)

                if checkpoint_every > 0 and completed_since_ckpt >= checkpoint_every:
                    persist("periodic")
                    completed_since_ckpt = 0

            fp, _ = frontier_counts()
            with lock:
                progress.tick(report, fp, force=True)

    progress.finish()

    if interrupted or stop.is_set():
        persist("interrupt")
        write_outputs(report, out_dir)
        progress.line(
            f"Interrupted. Partial report written. Resume with:\n"
            f"  python a11y_crawl.py {start_url} -o {out_dir} --resume"
        )
        return report

    # successful completion: write outputs and clear checkpoint
    write_outputs(report, out_dir)
    if checkpoint_path.exists():
        try:
            checkpoint_path.unlink()
            progress.line("Checkpoint cleared (crawl finished).")
        except OSError:
            pass
    return report


def write_outputs(report: CrawlReport, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "report.json"
    csv_path = out_dir / "issues.csv"
    summary_path = out_dir / "summary.txt"
    errors_csv = out_dir / "errors.csv"
    errors_json = out_dir / "error_report.json"
    errors_txt = out_dir / "error_report.txt"

    with open(json_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(
            {
                "start_url": report.start_url,
                "pages_crawled": report.pages_crawled,
                "pdfs_checked": report.pdfs_checked,
                "pages": report.pages,
                "pdfs": report.pdfs,
                "issues": [asdict(i) for i in report.issues],
                "access_errors": [asdict(e) for e in report.access_errors],
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

    err_fields = [
        "url",
        "resource_type",
        "error_type",
        "status_code",
        "message",
        "final_url",
    ]
    with open(errors_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=err_fields)
        w.writeheader()
        for e in report.access_errors:
            w.writerow(asdict(e))

    with open(errors_json, "w", encoding="utf-8", newline="\n") as f:
        json.dump(
            {
                "start_url": report.start_url,
                "error_count": len(report.access_errors),
                "errors": [asdict(e) for e in report.access_errors],
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    by_type: dict[str, int] = {}
    by_res: dict[str, int] = {}
    err_lines = [
        f"Access error report for: {report.start_url}",
        f"Total access errors: {len(report.access_errors)}",
        "",
    ]
    for e in report.access_errors:
        by_type[e.error_type] = by_type.get(e.error_type, 0) + 1
        by_res[e.resource_type] = by_res.get(e.resource_type, 0) + 1
    err_lines.append("By resource type:")
    for k, v in sorted(by_res.items()):
        err_lines.append(f"  {k}: {v}")
    err_lines.append("")
    err_lines.append("By error type:")
    for k, v in sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0])):
        err_lines.append(f"  {k}: {v}")
    err_lines.append("")
    err_lines.append("Details:")
    for e in report.access_errors:
        code = f" HTTP {e.status_code}" if e.status_code else ""
        err_lines.append(
            f"- [{e.resource_type}/{e.error_type}]{code} {e.url} — {e.message}"
        )
    errors_txt.write_text("\n".join(err_lines) + "\n", encoding="utf-8", newline="\n")

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
        f"Access errors: {len(report.access_errors)}",
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
    if report.access_errors:
        lines.append("")
        lines.append(
            f"See error_report.txt / errors.csv for {len(report.access_errors)} failed page/PDF accesses."
        )
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Crawl a site and check WCAG-oriented a11y signals (Windows-friendly)"
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
    p.add_argument(
        "--resume",
        action="store_true",
        help=f"continue from {CHECKPOINT_NAME} in the output directory",
    )
    p.add_argument(
        "--checkpoint-every",
        type=int,
        default=10,
        help="save checkpoint every N completed tasks (0 = only on interrupt)",
    )
    p.add_argument(
        "--no-progress",
        action="store_true",
        help="disable live progress status on stderr",
    )
    return p.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    configure_stdio()
    args = parse_args(argv)
    out_dir = Path(args.out)
    report = crawl(
        start_url=args.start_url,
        max_pages=args.max_pages,
        delay=args.delay,
        respect_robots=not args.no_robots,
        out_dir=out_dir,
        workers=args.workers,
        show_progress=not args.no_progress,
        resume=args.resume,
        checkpoint_every=args.checkpoint_every,
    )
    print(f"Crawled {report.pages_crawled} pages, {report.pdfs_checked} PDFs")
    print(f"Found {len(report.issues)} issues, {len(report.access_errors)} access errors")
    print(
        f"Wrote {out_dir / 'summary.txt'}, issues.csv, report.json, "
        f"error_report.txt, errors.csv"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # fallback if signal handler did not run yet
        sys.stderr.write("\nInterrupted.\n")
        sys.exit(130)
