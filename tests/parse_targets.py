"""What the page parser's child process runs in tests. A separate module, because the child
process imports the function by its name. Markers in a page trigger the failure cases."""

import os
import time

from screener.html_text import ExtractedPage, extract_page

HANG = b"<!-- parse forever -->"
RAISE = b"<!-- parser raises -->"
CRASH = b"<!-- parser crashes -->"
REPORT_PID = b"<!-- report pid -->"


def extract_for_tests(body: bytes, **kwargs) -> ExtractedPage:
    if HANG in body:
        time.sleep(3600)  # a parse that never ends: only killing the process stops it
    if RAISE in body:
        raise ValueError("broken page")
    if CRASH in body:
        os._exit(3)
    if REPORT_PID in body:
        return ExtractedPage(title=str(os.getpid()), text="")
    return extract_page(body, **kwargs)
