"""简历 PDF：按技能用 pdfplumber 抽字，工作经历要跟在对应公司下面。"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

RESUME = Path("/Users/wangyin/Desktop/resume(1).pdf")
VENV_PYTHON = Path.home() / ".permanent" / "pdf-venv" / "bin" / "python"


def test_resume_work_history_stays_with_its_company():
    if not RESUME.is_file():
        pytest.skip(f"resume not on this machine: {RESUME}")
    if not VENV_PYTHON.is_file():
        pytest.skip(f"pdf venv not installed: {VENV_PYTHON}")
    if shutil.which("pdftotext"):
        proc = subprocess.run(
            ["pdftotext", "-layout", str(RESUME), "-"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        text = proc.stdout or ""
        if proc.returncode != 0 or "工作经历" not in text:
            text = ""
    else:
        text = ""
    if not text:
        proc = subprocess.run(
            [
                str(VENV_PYTHON),
                "-c",
                (
                    "import pdfplumber\n"
                    f"with pdfplumber.open({str(RESUME)!r}) as pdf:\n"
                    "    print('\\n'.join((p.extract_text() or '') for p in pdf.pages))\n"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        text = proc.stdout
    job = text.find("工作经历")
    react = text.find("Plan-and-Execute")
    company = text.find("量化派")
    papers = text.find("学术成果")
    assert job >= 0 and react > job and company > react and papers > company
