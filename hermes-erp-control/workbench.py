#!/usr/bin/env python3
"""Prabhat file jobs over MCP. No ERP credentials, shell execution or network API."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from xml.sax.saxutils import escape

ROOT = Path(os.environ.get("PWF_FILE_ROOT", "/home/hermeslab/files")).resolve()
STATE = Path(os.environ.get("PWF_STATE_ROOT", "/home/hermeslab/.local/state/prabhat-workbench")).resolve()
MAX_BYTES = 64 * 1024 * 1024
MAX_ATTEMPTS = 3
LEASE_SECONDS = 30
OPERATIONS = {"spreadsheet_create", "document_create", "pdf_split", "pdf_merge",
              "video_edit", "video_merge", "images_to_video", "video_thumbnail", "extract_audio"}
MEDIA_OPERATIONS = {op for op in OPERATIONS if op.startswith("video_")} | {"images_to_video", "extract_audio"}


@contextmanager
def db():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    con = sqlite3.connect(STATE / "jobs.sqlite3", timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, request_key TEXT UNIQUE, request_hash TEXT NOT NULL,
        operation TEXT NOT NULL, arguments TEXT NOT NULL, state TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
        updated REAL NOT NULL, retry_at REAL NOT NULL DEFAULT 0,
        lease_until REAL NOT NULL DEFAULT 0, cancelled INTEGER NOT NULL DEFAULT 0,
        result TEXT, error TEXT)""")
    os.chmod(STATE / "jobs.sqlite3", 0o600)
    try:
        with con:
            yield con
    finally:
        con.close()


def checked_file(path):
    p = Path(path)
    p = (ROOT / p if not p.is_absolute() else p).resolve()
    if not p.is_relative_to(ROOT) or p == ROOT:
        raise ValueError("File must be inside the configured file workspace")
    if not p.is_file() or p.stat().st_size > MAX_BYTES:
        raise ValueError("Input file is missing or exceeds 64 MB")
    return p


def output_path(job_id, index, suffix):
    folder = ROOT / "out"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not folder.resolve().is_relative_to(ROOT):
        raise ValueError("Output directory escapes the workspace")
    return folder / ("workbench-" + job_id + "-" + str(index) + "." + suffix)


def artifact(path):
    path = checked_file(str(path))
    if path.stat().st_size == 0:
        raise ValueError("Output file is empty")
    return {"path": str(path), "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "send_with": "MEDIA:" + str(path)}


def reader(path):
    from pypdf import PdfReader
    p = checked_file(path)
    if p.suffix.lower() != ".pdf":
        raise ValueError("Expected a PDF file")
    r = PdfReader(p)
    if r.is_encrypted:
        raise ValueError("Encrypted PDFs require staff to supply an unlocked copy")
    if not 0 < len(r.pages) <= 300:
        raise ValueError("PDF must have between 1 and 300 pages")
    return r


def spreadsheet_create(job_id, columns, rows, sheet_name="Candidates", title=""):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    if not columns or len(columns) > 100 or len(rows) > 10000 or len(rows) * len(columns) > 200000:
        raise ValueError("Expected 1–100 columns, at most 10000 rows and 200000 cells")
    if any(not isinstance(c, str) or not c.strip() or len(c) > 200 for c in columns):
        raise ValueError("Column names must be nonempty text up to 200 characters")
    wb = Workbook()
    ws = wb.active
    ws.title = re.sub(r"[\\/*?:\[\]]", "_", str(sheet_name))[:31] or "Data"
    wb.properties.title = str(title)[:200]
    for row_number, row in enumerate([columns] + rows, 1):
        if not isinstance(row, list) or len(row) != len(columns):
            raise ValueError("Each row must have exactly one value per column")
        for col, value in enumerate(row, 1):
            if value is not None and not isinstance(value, (str, int, float, bool)):
                raise ValueError("Cells must contain text, numbers, booleans or null")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("Numbers must be finite")
            if isinstance(value, str) and len(value) > 10000:
                raise ValueError("Cell text exceeds 10000 characters")
            cell = ws.cell(row_number, col, value)
            # Never interpret user-provided text as an Excel formula.
            if isinstance(value, str):
                cell.data_type = "s"
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    for cell in ws[1]:
        cell.fill = PatternFill("solid", fgColor="163A63")
        cell.font = Font(color="FFFFFF", bold=True)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = min(45, max(14, max(len(str(c.value or "")) for c in col[:101]) + 2))
    p = output_path(job_id, 0, "xlsx")
    wb.save(p)
    return {"artifacts": [artifact(p)], "rows": len(rows), "columns": len(columns)}


def document_create(job_id, title, sections, subtitle="", format="pdf"):
    if format not in {"pdf", "docx"} or not isinstance(title, str) or not title.strip():
        raise ValueError("Supply a title and format pdf or docx")
    if not isinstance(sections, list) or len(sections) > 50:
        raise ValueError("Expected at most 50 sections")
    for section in sections:
        if set(section) - {"heading", "lines"} or not isinstance(section.get("lines", []), list):
            raise ValueError("Each section accepts heading and a list of text lines")
        if any(not isinstance(line, str) for line in section.get("lines", [])):
            raise ValueError("Document lines must be text")
    p = output_path(job_id, 0, format)
    if format == "docx":
        from docx import Document
        document = Document()
        document.add_heading(title, 0)
        if subtitle:
            document.add_paragraph(subtitle)
        for section in sections:
            if section.get("heading"):
                document.add_heading(str(section["heading"]), 1)
            for line in section.get("lines", []):
                document.add_paragraph(line)
        document.save(p)
    else:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        text = title + subtitle + json.dumps(sections, ensure_ascii=False)
        if any(0x0900 <= ord(ch) <= 0x0DFF for ch in text):
            raise ValueError("Use DOCX for Indic text; this PDF renderer does not promise Indic shaping")
        font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
        font = "Helvetica"
        if font_path.exists():
            pdfmetrics.registerFont(TTFont("PWFUnicode", str(font_path)))
            font = "PWFUnicode"
        styles = getSampleStyleSheet()
        for style in styles.byName.values():
            style.fontName = font
        styles["Title"].textColor = colors.HexColor("#163A63")
        styles["Heading2"].textColor = colors.HexColor("#163A63")
        story = [Paragraph(escape(title), styles["Title"])]
        if subtitle:
            story.append(Paragraph(escape(subtitle), styles["Normal"]))
        for section in sections:
            story.append(Spacer(1, 9))
            if section.get("heading"):
                story.append(Paragraph(escape(str(section["heading"])), styles["Heading2"]))
            for line in section.get("lines", []):
                story.extend([Paragraph(escape(line).replace("\n", "<br/>"), styles["Normal"]), Spacer(1, 4)])
        SimpleDocTemplate(str(p), pagesize=A4, rightMargin=35, leftMargin=35,
                          topMargin=30, bottomMargin=30).build(story)
    return {"artifacts": [artifact(p)], "note": "Contains only the supplied text; facts were not independently verified"}


def pdf_split(job_id, path, groups):
    from pypdf import PdfWriter
    r = reader(path)
    if not isinstance(groups, list) or not 1 <= len(groups) <= 30:
        raise ValueError("Supply 1–30 groups, each with a name and 1-based pages")
    results = []
    for index, group in enumerate(groups):
        if not isinstance(group, dict) or set(group) - {"name", "pages"}:
            raise ValueError("Group accepts only name and pages")
        pages = group.get("pages")
        if not isinstance(pages, list) or not pages or len(pages) > 300:
            raise ValueError("Supply a nonempty list of page numbers")
        if any(type(n) is not int or n < 1 or n > len(r.pages) for n in pages):
            raise ValueError("Page numbers must be valid 1-based integers")
        w = PdfWriter()
        for page in pages:
            w.add_page(r.pages[page - 1])
        p = output_path(job_id, index, "pdf")
        with p.open("wb") as f:
            w.write(f)
        results.append({**artifact(p), "name": str(group.get("name", "Document"))[:120], "pages": pages})
    return {"artifacts": results, "source_pages": len(r.pages)}


def pdf_merge(job_id, paths):
    from pypdf import PdfWriter
    if not isinstance(paths, list) or not 1 <= len(paths) <= 30:
        raise ValueError("Supply 1–30 PDF paths")
    w = PdfWriter()
    for path in paths:
        r = reader(path)
        if len(w.pages) + len(r.pages) > 300:
            raise ValueError("Merged PDF exceeds 300 pages")
        for page in r.pages:
            w.add_page(page)
    p = output_path(job_id, 0, "pdf")
    with p.open("wb") as f:
        w.write(f)
    return {"artifacts": [artifact(p)], "pages": len(w.pages)}


def execute(operation, arguments, job_id):
    if operation not in OPERATIONS or not isinstance(arguments, dict):
        raise ValueError("Unsupported operation or arguments")
    if operation in MEDIA_OPERATIONS:
        # Reuse existing fixed media functions, including their path checks.
        spec = importlib.util.spec_from_file_location("pwf_existing_media", "/home/hermeslab/media_mcp.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = getattr(module, operation)(**arguments)
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error", "Media processing failed"))[:400])
        if result.get("output"):
            result["artifacts"] = [artifact(result["output"])]
        return result
    return globals()[operation](job_id, **arguments)


def submit(operation, arguments, idempotency_key=None):
    if operation not in OPERATIONS or not isinstance(arguments, dict):
        raise ValueError("Unsupported operation or arguments")
    raw = json.dumps(arguments, sort_keys=True, ensure_ascii=False, allow_nan=False)
    if len(raw.encode()) > 512000:
        raise ValueError("Job arguments exceed 512 KB")
    if idempotency_key is not None and (not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 200):
        raise ValueError("Idempotency key must contain 8–200 characters")
    digest = hashlib.sha256((operation + raw).encode()).hexdigest()
    now, job_id = time.time(), uuid.uuid4().hex
    with db() as con:
        if idempotency_key:
            old = con.execute("SELECT id, request_hash FROM jobs WHERE request_key=?", (idempotency_key,)).fetchone()
            if old:
                if old["request_hash"] != digest:
                    raise ValueError("Idempotency key already belongs to different arguments")
                return status(old["id"])
        try:
            con.execute("INSERT INTO jobs(id,request_key,request_hash,operation,arguments,state,created,updated) VALUES (?,?,?,?,?,'queued',?,?)",
                        (job_id, idempotency_key, digest, operation, raw, now, now))
        except sqlite3.IntegrityError:
            old = con.execute("SELECT id, request_hash FROM jobs WHERE request_key=?", (idempotency_key,)).fetchone()
            if not old or old["request_hash"] != digest:
                raise ValueError("Idempotency conflict")
            return status(old["id"])
    return status(job_id)


def checked_id(job_id):
    if not isinstance(job_id, str) or not re.fullmatch("[0-9a-f]{32}", job_id):
        raise ValueError("Invalid job id")
    return job_id


def status(job_id):
    with db() as con:
        row = con.execute("SELECT * FROM jobs WHERE id=?", (checked_id(job_id),)).fetchone()
    if not row:
        raise ValueError("Job not found")
    return {"job_id": row["id"], "operation": row["operation"], "state": row["state"],
            "attempts": row["attempts"], "created_at": row["created"], "updated_at": row["updated"],
            "result": json.loads(row["result"]) if row["result"] else None, "error": row["error"],
            "next": "Poll workbench_status with this job_id" if row["state"] in {"queued", "running"} else None}


def cancel(job_id):
    with db() as con:
        con.execute("UPDATE jobs SET cancelled=1,state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END,updated=? WHERE id=? AND state IN ('queued','running')",
                    (time.time(), checked_id(job_id)))
    return status(job_id)


def claim():
    now = time.time()
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute("UPDATE jobs SET state=CASE WHEN attempts>=? THEN 'failed' ELSE 'queued' END,error='Worker interrupted; recovered after lease expiry',updated=? WHERE state='running' AND lease_until<?",
                    (MAX_ATTEMPTS, now, now))
        con.execute("UPDATE jobs SET state='cancelled',updated=? WHERE cancelled=1 AND state='queued'", (now,))
        row = con.execute("SELECT id FROM jobs WHERE state='queued' AND retry_at<=? AND cancelled=0 AND attempts<? ORDER BY created LIMIT 1", (now, MAX_ATTEMPTS)).fetchone()
        if not row:
            return None
        con.execute("UPDATE jobs SET state='running',attempts=attempts+1,updated=?,lease_until=?,error=NULL WHERE id=?", (now, now + LEASE_SECONDS, row["id"]))
        return row["id"]


def finish(job_id, result=None, error=None, retry=False):
    now = time.time()
    with db() as con:
        row = con.execute("SELECT cancelled,attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
        state = "cancelled" if row["cancelled"] else "queued" if retry and row["attempts"] < MAX_ATTEMPTS else "failed" if error else "done"
        con.execute("UPDATE jobs SET state=?,result=?,error=?,updated=?,lease_until=0,retry_at=? WHERE id=? AND state='running'",
                    (state, json.dumps(result) if result is not None else None, error, now, now + 5 * row["attempts"], job_id))


def execute_job(job_id):
    with db() as con:
        row = con.execute("SELECT * FROM jobs WHERE id=? AND state='running'", (checked_id(job_id),)).fetchone()
    if row is None:
        return
    try:
        result = execute(row["operation"], json.loads(row["arguments"]), job_id)
        finish(job_id, result=result)
    except (ValueError, TypeError, KeyError, ImportError) as exc:
        finish(job_id, error=type(exc).__name__ + ": " + str(exc)[:400])
    except Exception as exc:
        finish(job_id, error=type(exc).__name__ + ": " + str(exc)[:400], retry=True)


def run_one():
    job_id = claim()
    if job_id is None:
        return False
    child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--execute-job", job_id],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    start = time.monotonic()
    while child.poll() is None:
        heartbeat()
        with db() as con:
            row = con.execute("SELECT cancelled FROM jobs WHERE id=?", (job_id,)).fetchone()
            con.execute("UPDATE jobs SET lease_until=?,updated=? WHERE id=? AND state='running'", (time.time() + LEASE_SECONDS, time.time(), job_id))
        if row["cancelled"] or time.monotonic() - start > 900:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            finish(job_id, error="Cancelled" if row["cancelled"] else "Job exceeded 15 minutes")
            break
        time.sleep(0.5)
    if status(job_id)["state"] == "running":
        finish(job_id, error="Worker process exited without a result", retry=True)
    return True


def heartbeat():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = STATE / "heartbeat.json"
    temporary = STATE / "heartbeat.tmp"
    temporary.write_text(json.dumps({"at": time.time(), "pid": os.getpid()}))
    temporary.replace(path)


def capabilities():
    beat = STATE / "heartbeat.json"
    age = max(0, time.time() - beat.stat().st_mtime) if beat.exists() else None
    return {"operations": sorted(OPERATIONS), "max_input_bytes": MAX_BYTES,
            "worker_online": age is not None and age < 15, "worker_heartbeat_age_seconds": age,
            "libraries": {m: bool(importlib.util.find_spec(m)) for m in ("pypdf", "openpyxl", "reportlab", "docx")},
            "scope": "Local file/media processing only. ERP actions use PrabhatJobs MCP under the requesting user's permissions.",
            "delivery": "Return the verified MEDIA path or upload through the authenticated ERP tool. No public sharing is created."}


def schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


TOOLS = [
    {"name": "workbench_capabilities", "description": "List supported file jobs and installed libraries; read-only.", "inputSchema": schema({})},
    {"name": "workbench_submit", "description": "Queue durable file work: spreadsheet_create(columns,rows,sheet_name,title); document_create(title,subtitle,sections:[{heading,lines}],format:pdf|docx); pdf_split(path,groups:[{name,pages:[1,2]}]); pdf_merge(paths); or existing video_edit/video_merge/images_to_video/video_thumbnail/extract_audio arguments. No arbitrary commands. Preserve supplied facts. Returns a job id, not completion. Poll status and deliver verified artifacts. This never sends messages or changes ERP data.",
     "inputSchema": schema({"operation": {"type": "string", "enum": sorted(OPERATIONS)}, "arguments": {"type": "object"}, "idempotency_key": {"type": "string", "minLength": 8, "maxLength": 200}}, ("operation", "arguments"))},
    {"name": "workbench_status", "description": "Read one job's queued/running/done/failed/cancelled state. Only done plus verified artifacts means completion. Keep the opaque job id private.", "inputSchema": schema({"job_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"}}, ("job_id",))},
    {"name": "workbench_cancel", "description": "Cancel one queued/running local file job using its opaque id. Does not delete files or ERP records.", "inputSchema": schema({"job_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"}}, ("job_id",))},
    {"name": "workbench_verify_file", "description": "Verify a workspace file exists, is nonempty and return its SHA256 and MEDIA delivery path; read-only.", "inputSchema": schema({"path": {"type": "string"}}, ("path",))},
]
FUNCTIONS = {"workbench_capabilities": capabilities, "workbench_submit": submit,
             "workbench_status": status, "workbench_cancel": cancel, "workbench_verify_file": artifact}


def handle(message):
    method = message.get("method")
    if method == "initialize":
        return {"protocolVersion": message.get("params", {}).get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "prabhat-workbench", "version": "1.0.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        try:
            params = message["params"]
            result = FUNCTIONS[params["name"]](**(params.get("arguments") or {}))
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
        except Exception as exc:
            return {"isError": True, "content": [{"type": "text", "text": type(exc).__name__ + ": " + str(exc)[:500]}]}
    raise KeyError(method)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--execute-job")
    args = parser.parse_args()
    if args.execute_job:
        execute_job(args.execute_job)
    elif args.worker:
        while True:
            heartbeat()
            if not run_one():
                time.sleep(1)
    else:
        for line in sys.stdin:
            try:
                message = json.loads(line)
                if "id" not in message:
                    continue
                try:
                    response = {"jsonrpc": "2.0", "id": message["id"], "result": handle(message)}
                except KeyError:
                    response = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "Unknown method"}}
            except (ValueError, TypeError):
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
