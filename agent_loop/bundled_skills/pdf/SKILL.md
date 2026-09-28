---
name: pdf
description: Read this first whenever a task involves a PDF. Covers text, tables, metadata, embedded images, page renders, scanned pages, and merging, splitting, rotating, or creating a PDF.
---

# PDF

The read tool does not extract PDF text. If the file is a `.pdf`, or read says "does not extract PDF text", follow this file and use bash.

bash uses the machine `python3`, not perm's virtualenv. Install missing libraries into `~/.permanent/pdf-venv`. Do not `pip install` them into the system Python.

## Interpreter

```bash
VENV="$HOME/.permanent/pdf-venv"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
fi
if ! "$VENV/bin/python" -c "import pdfplumber, pypdf" >/dev/null 2>&1; then
  "$VENV/bin/python" -m pip install -q pdfplumber pypdf
fi
```

Use `"$VENV/bin/python"` for the Python below.

`pdftotext` is a system command, not a Python package. If `command -v pdftotext` succeeds, use it. Otherwise use `pdfplumber` from this venv.

## Text

For multi-column pages use `pdftotext -layout`, or `pdfplumber`'s `extract_text()`. Do not use `pypdf`'s `extract_text()` for body text. It joins strings in file order and splits columns apart.

```bash
pdftotext -layout -f 1 -l 5 "/path/to/file.pdf" -
```

When `pdftotext` is missing:

```bash
"$VENV/bin/python" - <<'PY'
import pdfplumber
path = "/path/to/file.pdf"
with pdfplumber.open(path) as pdf:
    for i, page in enumerate(pdf.pages, 1):
        print(f"--- page {i} ---")
        print(page.extract_text() or "")
PY
```

On a long file, extract the pages you need. Do not dump the whole document into the conversation.

## Tables

```bash
"$VENV/bin/python" - <<'PY'
import pdfplumber
with pdfplumber.open("/path/to/file.pdf") as pdf:
    for i, page in enumerate(pdf.pages, 1):
        for table in page.extract_tables() or []:
            print(f"--- page {i} table ---")
            for row in table:
                print("\t".join("" if cell is None else str(cell) for cell in row))
PY
```

## Metadata

Use `pypdf` for page count, title, and author. That is not the body text.

```bash
"$VENV/bin/python" - <<'PY'
from pypdf import PdfReader
reader = PdfReader("/path/to/file.pdf")
print("pages", len(reader.pages))
print(reader.metadata)
PY
```

## Images

Keep these apart:

- An image embedded in the file: extract that image itself.
- What the page looks like: render the page to a picture, then `read` that picture.

Render a page:

```bash
if command -v pdftoppm >/dev/null 2>&1; then
  pdftoppm -png -f 1 -l 1 "/path/to/file.pdf" /tmp/pdf-page
else
  "$VENV/bin/python" - <<'PY'
import pdfplumber
with pdfplumber.open("/path/to/file.pdf") as pdf:
    pdf.pages[0].to_image(resolution=120).save("/tmp/pdf-page-1.png")
PY
fi
```

Then `read` the png. When the extracted text does not match the page, trust the picture.

## Scanned pages

Empty body text usually means a scan. Render the page, then run OCR on the image. If this machine has no OCR program, say the text could not be extracted. Do not treat empty text as "this PDF has no content".

## Editing and new files

Merge, split, and rotate with `pypdf`. Create a new PDF with `reportlab`. Install it into the same venv: `"$VENV/bin/python" -m pip install -q reportlab`.

After creating or modifying any PDF, render the pages you touched and `read` those images. Text must sit next to its heading. It must not overlap or get clipped.
