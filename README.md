# Website accessibility checker

Crawl all same-domain pages via hyperlinks and report WCAG 2.1 AA–oriented gaps.

## Checks

**HTML pages**
| Check | Severity | WCAG |
| --- | --- | --- |
| `<html lang>` missing | critical | 3.1.1 |
| Mixed Chinese/English with no local `lang` | review | 3.1.2 |
| No `<h1>` | serious | 1.3.1 / 2.4.6 |
| Multiple `<h1>` | moderate | 1.3.1 / 2.4.6 |
| Heading level skip (e.g. h2 → h4) | moderate | 1.3.1 / 2.4.6 |
| `<img>` missing `alt` | critical | 1.1.1 |
| `<img alt="">` (only OK if decorative) | review | 1.1.1 |
| CSS `background-image` without accessible name / nearby text | serious | 1.1.1 |

**PDFs** (linked from the site)
| Check | Severity | WCAG |
| --- | --- | --- |
| Not tagged/marked | serious | 1.3.1 |
| No `/Alt` on figures/structure | serious | 1.1.1 |
| Missing Title/Subject metadata | review | — (helpful, **not** alternative text) |

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python a11y_crawl.py https://example.com/ -o a11y_report
```

Options: `--max-pages 200`, `--delay 0.3`, `--no-robots`

Outputs: `summary.txt` (by severity), `issues.csv`, `report.json`
