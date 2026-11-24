"""PDF output recovers from partial font registration in a fresh process."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from round_robin.reports import exports

CHILD = r'''
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
import sys
import threading
import reportlab
from pypdf import PdfReader
from round_robin.reports import exports

case = sys.argv[1]
assert Path(exports.__file__).resolve().parents[2] == Path(sys.argv[2])
directory = Path(reportlab.__file__).parent / "fonts"
register = exports.pdfmetrics.registerFont
if case in {"regular", "both"}:
    register(exports.TTFont("RRVera", str(directory / "Vera.ttf")))
if case in {"bold", "both"}:
    register(exports.TTFont("RRVeraBold", str(directory / "VeraBd.ttf")))

calls = []
failure = [case == "failure"]
def observe(font):
    calls.append(font.fontName)
    if font.fontName == "RRVeraBold" and failure[0]:
        failure[0] = False
        raise OSError("synthetic temporary registration error")
    return register(font)
exports.pdfmetrics.registerFont = observe
if case == "failure":
    try:
        exports._register_font()
    except OSError:
        pass
    else:
        raise AssertionError("first attempt should expose the registration error")
    assert "RRVera" in exports.pdfmetrics.getRegisteredFontNames()
    assert "RRVeraBold" not in exports.pdfmetrics.getRegisteredFontNames()

if case == "concurrent":
    barrier = threading.Barrier(8, timeout=5)
    def register_after_barrier():
        barrier.wait()
        exports._register_font()
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(register_after_barrier) for _ in range(8)]
        for future in futures:
            future.result(timeout=10)
else:
    exports._register_font()
exports._register_font()
assert {"RRVera", "RRVeraBold"} <= set(exports.pdfmetrics.getRegisteredFontNames())
assert calls.count("RRVera") == (0 if case in {"regular", "both"} else 1)
assert calls.count("RRVeraBold") == (0 if case in {"bold", "both"} else 2 if case == "failure" else 1)
content = exports.report_pdf({"id": "report_test", "version": 1, "event_cutoff": 1}, title="Café example", sources=[])
assert content.startswith(b"%PDF-")
text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(content)).pages)
assert "Café example" in text and "Recommendation" in text
'''


@pytest.mark.parametrize("scenario", ["fresh", "regular", "bold", "both", "failure", "concurrent"])
def test_registration_recovers_and_pdf_remains_readable(scenario):
    source = Path(exports.__file__).resolve().parents[2]
    pythonpath = os.pathsep.join(filter(None, (str(source), os.environ.get("PYTHONPATH", ""))))
    completed = subprocess.run([sys.executable, "-c", CHILD, scenario, str(source)],
                               env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": pythonpath},
                               capture_output=True, text=True, timeout=15, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
