# Website accessibility crawl

Crawl all same-domain pages via hyperlinks, then report common a11y gaps.

## Checks

**HTML pages**
- `<html lang>` present
- at least one `<h1>`
- `<img>` missing / empty `alt`
- CSS `background-image` (inline `style` or `<style>`) — flagged because there is no `alt`

**PDFs linked from the site**
- metadata Title / Subject (description)
- tagged/marked PDF hint
- `/Alt` alternative text when present in the PDF structure

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

Outputs in the `-o` folder: `summary.txt`, `issues.csv`, `report.json`
