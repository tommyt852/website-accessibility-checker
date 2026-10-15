# Website accessibility checker

Crawl all same-domain pages via hyperlinks and report WCAG 2.1 AA–oriented gaps.

**Target OS:** Windows (also works on macOS/Linux). Live progress on the console. Ctrl+C saves a checkpoint; `--resume` continues later.

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
| Missing PDF `/Title` (document title) | serious | 2.4.2 |
| `/Title` is just the filename | serious | 2.4.2 |
| Missing `/Subject` metadata | review | — (helpful, **not** alternative text) |

## Windows one-click

1. **`setup.bat`** — creates `.venv` and installs `requirements.txt`
2. **`run.bat`** — prompts for a URL (or pass it), shows live progress  
   - `run.bat https://example.com/`  
   - Ctrl+C → saves `a11y_report\checkpoint.json` + partial report  
   - `run.bat https://example.com/ --resume` → continue
3. **`shutdown.bat`** — stops running `a11y_crawl.py` processes; optional delete `.venv`

## Manual setup

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python a11y_crawl.py https://example.com/ -o a11y_report
python a11y_crawl.py https://example.com/ --workers 4 --delay 0.2
python a11y_crawl.py https://example.com/ -o a11y_report --resume
```

Options:

- `--max-pages 200`
- `--delay 0.3` — global min gap between requests
- `--workers 1` — parallel fetch workers (default sequential)
- `--resume` — continue from `checkpoint.json` in `-o`
- `--checkpoint-every 10` — periodic save (0 = only on interrupt)
- `--no-progress` — disable stderr progress
- `--no-robots`

Outputs:

- `summary.txt`, `issues.csv`, `report.json` — accessibility findings
- `error_report.txt`, `errors.csv`, `error_report.json` — pages/PDFs that failed to open (HTTP error, timeout, connection, robots, save/parse)
- `checkpoint.json` — only if interrupted / for `--resume`
