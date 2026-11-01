#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pdfplumber>=0.11", "pypdf>=5"]
# ///
"""Deterministic layer of the fact-checker skill.

The model reads and judges. This script checks what code can check: quotes are
verbatim, arithmetic is right, every number-bearing sentence was looked at.
A finding that fails these checks is downgraded to `unverified`.

    factcheck.py extract report.pdf --work .factcheck
    factcheck.py fetch https://example.org/stats --work .factcheck
    factcheck.py calc "(616-514)/514*100" --stated 21.8 --decimals 1
    factcheck.py check --work .factcheck
    factcheck.py report --work .factcheck
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import operator
import re
import subprocess
import sys
import tempfile
import unicodedata
import urllib.request
from decimal import ROUND_HALF_UP, Decimal
from html.parser import HTMLParser
from pathlib import Path

VERDICTS = {"confirmed", "wrong", "inconsistent", "misleading", "outdated", "unverified"}
FINDINGS = {"wrong", "inconsistent", "misleading", "outdated"}
SEVERITY = ["wrong", "inconsistent", "misleading", "outdated", "unverified"]
MAX_FETCH_BYTES = 30_000_000
FETCH_TIMEOUT = 30
# Rough words of source reading per checked claim, for the pre-run cost line only.
TOKENS_PER_WEB_CLAIM = 12_000
TOKENS_PER_INTERNAL_CLAIM = 1_500
HOMEPAGE = "https://github.com/BrickyStudios/ai-fact-checker-claude-skill"


# --- normalising -----------------------------------------------------------

def normalize(text: str) -> str:
    """Fold everything that differs between a PDF, a web page and a typed quote."""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[­​‌‍﻿]", "", text)  # soft hyphens, zero-width
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)  # word broken across a line
    text = re.sub(r"[‘’‚‛`´]", "'", text)
    text = re.sub(r"[“”„‟«»]", '"', text)
    text = re.sub(r"[‐-―−]", "-", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


NUMBER_RE = re.compile(r"(?<![\w.,])\d[\d.,  ']*\d|(?<![\w.,])\d")


def to_decimal(token: str) -> Decimal | None:
    """Read 1,234.5 and 1.234,5 and 1 234,5 as the same number."""
    token = re.sub(r"[\s  ']", "", token).rstrip(".,")
    if not token:
        return None
    last_dot, last_comma = token.rfind("."), token.rfind(",")
    if last_dot >= 0 and last_comma >= 0:
        decimal_mark = "." if last_dot > last_comma else ","
    elif last_dot >= 0 or last_comma >= 0:
        mark = "." if last_dot >= 0 else ","
        groups = token.split(mark)
        # 1,234 and 1.234.567 are grouped thousands; 1,5 and 12.34 are decimals.
        is_thousands = len(groups) > 2 or (len(groups[-1]) == 3 and groups[0] not in ("", "0"))
        decimal_mark = None if is_thousands else mark
    else:
        decimal_mark = None
    if decimal_mark:
        whole, frac = token.rsplit(decimal_mark, 1)
        cleaned = re.sub(r"[.,]", "", whole) + "." + frac
    else:
        cleaned = re.sub(r"[.,]", "", token)
    try:
        return Decimal(cleaned)
    except ArithmeticError:
        return None


def numbers_in(text: str) -> set[Decimal]:
    found = (to_decimal(t) for t in NUMBER_RE.findall(unicodedata.normalize("NFKC", text)))
    return {n.normalize() for n in found if n is not None}


# --- documents -------------------------------------------------------------

class _TextOnly(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "head"}
    BLOCK = {"p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "br", "section", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.depth += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if not self.depth:
            self.parts.append(data)


def html_to_text(raw: str) -> str:
    parser = _TextOnly()
    parser.feed(raw)
    return re.sub(r"\n\s*\n+", "\n\n", "".join(parser.parts)).strip()


def pdf_pages(path: Path) -> list[str]:
    """pdftotext if present (fast, keeps tables in reading order), else pdfplumber."""
    try:
        out = subprocess.run(
            ["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"],
            capture_output=True, check=True, timeout=300,
        ).stdout.decode("utf-8", "replace")
        return [p for p in out.split("\f")][: -1 if out.endswith("\f") else None]
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        return [page.extract_text() or "" for page in pdf.pages]


def read_pages(path: Path) -> list[str]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return pdf_pages(path)
    raw = path.read_text(encoding="utf-8", errors="replace")
    if suffix in {".html", ".htm"}:
        return [html_to_text(raw)]
    return [raw]


def sentences_of(pages: list[str]) -> list[dict]:
    """Split into sentences and flag the ones holding a number, the coverage baseline."""
    out = []
    for page_no, text in enumerate(pages, 1):
        # A table row or short line is a unit of its own; prose is split on full stops.
        for block in re.split(r"\n\s*\n|\n(?=\s*\S+\s{2,}\S)", text):
            flat = re.sub(r"\s+", " ", block).strip()
            for sent in re.split(r"(?<=[a-zäöüß\d\)%\"”])[.!?]\s+(?=[A-ZÄÖÜ\"“„])", flat):
                sent = sent.strip()
                if len(sent) >= 8 and re.search(r"[A-Za-zÄÖÜäöü]", sent):
                    out.append({
                        "id": f"S{len(out) + 1:04d}",
                        "page": page_no,
                        "text": sent,
                        "has_number": bool(re.search(r"\d", sent)),
                    })
    return out


def load_doc(work: Path) -> dict:
    path = work / "doc.json"
    if not path.exists():
        sys.exit(f"{path} not found. Run: factcheck.py extract <document> --work {work}")
    return json.loads(path.read_text(encoding="utf-8"))


def cmd_extract(args) -> None:
    source = Path(args.document)
    if not source.is_file():
        sys.exit(f"Not a file: {source}")
    pages = read_pages(source)
    if not any(p.strip() for p in pages):
        sys.exit(f"No text in {source}. It is probably a scan: run OCR first, e.g. `ocrmypdf in.pdf out.pdf`.")
    work = Path(args.work)
    (work / "sources").mkdir(parents=True, exist_ok=True)
    sentences = sentences_of(pages)
    words = sum(len(p.split()) for p in pages)
    numbered = sum(s["has_number"] for s in sentences)
    (work / "doc.json").write_text(json.dumps({
        "source": str(source.resolve()), "words": words, "pages": pages, "sentences": sentences,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    (work / "doc.txt").write_text(
        "\n".join(f"[{s['id']} p{s['page']}] {s['text']}" for s in sentences), encoding="utf-8")
    print(json.dumps({
        "pages": len(pages), "words": words, "sentences": len(sentences),
        "number_sentences": numbered,
        "read_this": str(work / "doc.txt"),
        "token_estimate": {
            "quick": numbered * TOKENS_PER_INTERNAL_CLAIM,
            "standard": numbered * TOKENS_PER_INTERNAL_CLAIM + max(numbered // 4, 1) * TOKENS_PER_WEB_CLAIM,
            "deep": numbered * TOKENS_PER_WEB_CLAIM,
        },
    }, indent=1))


# --- sources ---------------------------------------------------------------

def source_path(work: Path, url: str) -> Path:
    return work / "sources" / (hashlib.sha256(url.encode()).hexdigest()[:16] + ".txt")


def cmd_fetch(args) -> None:
    """Snapshot a source as raw text. Summarising fetch tools cannot prove a verbatim quote."""
    url, work = args.url, Path(args.work)
    if not re.match(r"https?://", url, re.I):
        sys.exit("Only http and https URLs can be fetched.")
    (work / "sources").mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; fact-checker-skill)",
        "Accept": "text/html,application/pdf,text/plain;q=0.9,*/*;q=0.5",
    })
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            body = response.read(MAX_FETCH_BYTES + 1)
            ctype = response.headers.get("Content-Type", "").lower()
    except OSError as exc:
        sys.exit(f"Fetch failed for {url}: {exc}. Find another copy of this source or save it with "
                 f"`factcheck.py fetch --from-file page.html {url}`.")
    if len(body) > MAX_FETCH_BYTES:
        sys.exit(f"{url} is over {MAX_FETCH_BYTES} bytes.")
    write_source(work, url, body, ctype)


def write_source(work: Path, url: str, body: bytes, ctype: str) -> None:
    if body[:5] == b"%PDF-" or "pdf" in ctype:
        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            tmp.write(body)
            tmp.flush()
            text = "\n\f\n".join(pdf_pages(Path(tmp.name)))
    else:
        text = body.decode("utf-8", "replace")
        if "html" in ctype or "<html" in text[:2000].lower():
            text = html_to_text(text)
    if not text.strip():
        sys.exit(f"No text in the snapshot of {url}. It may be a script-rendered page: save it from a browser "
                 "and use --from-file, or find another copy.")
    path = source_path(work, url)
    path.write_text(text, encoding="utf-8")
    print(json.dumps({"url": url, "snapshot": str(path), "chars": len(text),
                      "sha256": hashlib.sha256(text.encode()).hexdigest()[:16]}, indent=1))


def cmd_fetch_entry(args) -> None:
    if args.from_file:
        work = Path(args.work)
        (work / "sources").mkdir(parents=True, exist_ok=True)
        body = Path(args.from_file).read_bytes()
        write_source(work, args.url, body, "text/html" if args.from_file.endswith(("htm", "html")) else "")
    else:
        cmd_fetch(args)


# --- arithmetic ------------------------------------------------------------

_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def _eval(node: ast.AST) -> Decimal:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return Decimal(str(node.value))
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError("exponent too large")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError("only numbers and + - * / ** ( ) are allowed")


def evaluate(expr: str) -> Decimal:
    """Exact decimal arithmetic through a whitelist. Never eval(): the input is model output."""
    return _eval(ast.parse(expr, mode="eval"))


def calc_result(expr: str, stated: float | str, decimals: int) -> dict:
    value = evaluate(expr)
    quantum = Decimal(1).scaleb(-decimals)
    rounded = value.quantize(quantum, rounding=ROUND_HALF_UP)
    stated_d = Decimal(str(stated)).quantize(quantum, rounding=ROUND_HALF_UP)
    return {"value": str(rounded), "stated": str(stated_d), "matches": rounded == stated_d}


def cmd_calc(args) -> None:
    try:
        print(json.dumps(calc_result(args.expr, args.stated, args.decimals)))
    except (ValueError, SyntaxError, ArithmeticError) as exc:
        sys.exit(f"Cannot evaluate {args.expr!r}: {exc}")


def operands(expr: str) -> set[Decimal]:
    """Numbers in the expression, minus 100 and 1000 which are unit scaling."""
    tree = ast.parse(expr, mode="eval")
    found = {Decimal(str(n.value)).normalize() for n in ast.walk(tree)
             if isinstance(n, ast.Constant) and type(n.value) in (int, float)}
    return found - {Decimal(100).normalize(), Decimal(1000).normalize()}


# --- the gate --------------------------------------------------------------

def doc_page_text(doc: dict, page: int) -> str:
    pages = doc["pages"]
    return normalize(pages[page - 1]) if 1 <= page <= len(pages) else ""


def quote_problem(quote: str, haystack: str) -> str | None:
    if len(quote.strip()) < 8:
        return "quote is under 8 characters, too short to prove anything"
    return None if normalize(quote) in haystack else "quote is not verbatim in the text"


def table_tolerant_hit(quote: str, haystack: str) -> bool:
    """Table rows extract with columns in a different order: accept all tokens within one page."""
    return all(tok in haystack for tok in normalize(quote).split())


def check_claim(claim: dict, doc: dict, work: Path) -> list[str]:
    problems = []
    verdict = claim.get("verdict")
    if verdict not in VERDICTS:
        return [f"verdict must be one of {sorted(VERDICTS)}"]

    page_text = doc_page_text(doc, claim.get("page", 0))
    if not page_text:
        problems.append(f"page {claim.get('page')} does not exist in the document")
    elif problem := quote_problem(claim.get("quote", ""), page_text):
        problems.append(f"claim quote on page {claim.get('page')}: {problem}")

    if verdict == "unverified":
        return problems

    evidence_text = claim.get("quote", "")
    verified_evidence = 0
    for ev in claim.get("evidence", []):
        kind, quote = ev.get("kind"), ev.get("quote", "")
        if kind == "document":
            text = doc_page_text(doc, ev.get("page", 0))
            problem = quote_problem(quote, text) if text else f"page {ev.get('page')} does not exist"
            if problem and text and len(quote.strip()) >= 8 and table_tolerant_hit(quote, text):
                problem = None
        elif kind == "source":
            url = ev.get("url", "")
            snapshot = source_path(work, url)
            if not snapshot.exists():
                problem = f"no snapshot for {url}: run `factcheck.py fetch {url}`"
            else:
                problem = quote_problem(quote, normalize(snapshot.read_text(encoding="utf-8")))
        else:
            problem = 'evidence kind must be "document" or "source"'
        if problem:
            problems.append(f"evidence {quote[:50]!r}: {problem}")
        else:
            verified_evidence += 1
            evidence_text += " " + quote

    calc = claim.get("calc")
    calc_ok = False
    if calc:
        try:
            result = calc_result(calc["expr"], calc["stated"], calc.get("decimals", 0))
        except (KeyError, ValueError, SyntaxError, ArithmeticError) as exc:
            problems.append(f"calc does not evaluate: {exc}")
        else:
            calc["result"] = result
            calc_ok = True
            if result["matches"] and verdict in {"wrong", "inconsistent"}:
                problems.append(f"marked {verdict}, but {calc['expr']} = {result['value']} matches the stated value")
            if not result["matches"] and verdict == "confirmed":
                problems.append(f"marked confirmed, but {calc['expr']} = {result['value']}, stated {result['stated']}")
            missing = operands(calc["expr"]) - numbers_in(evidence_text)
            if missing:
                problems.append("calc inputs not found in the verified quotes: "
                                + ", ".join(sorted(format(m, "f") for m in missing)))

    if not verified_evidence and not calc_ok:
        problems.append(f"verdict {verdict} needs verified evidence or a calculation")
    if verdict in FINDINGS and not claim.get("correction", "").strip() and verdict != "outdated":
        problems.append("a finding needs a proposed correction")
    return problems


def coverage(claims: list[dict], skipped: list[dict], doc: dict) -> dict:
    """Which number-bearing sentences were neither claimed nor consciously skipped."""
    required = [s for s in doc["sentences"] if s["has_number"]]
    skipped_ids = {s.get("sentence") for s in skipped if s.get("reason", "").strip()}
    claimed_ids = {c.get("sentence") for c in claims}
    claim_quotes = [(c.get("page"), normalize(c.get("quote", ""))) for c in claims]
    untouched = []
    for sent in required:
        text = normalize(sent["text"])
        if sent["id"] in skipped_ids or sent["id"] in claimed_ids:
            continue
        if any(page == sent["page"] and q and (q in text or text in q) for page, q in claim_quotes):
            continue
        untouched.append(sent)
    total = len(required)
    return {
        "required": total,
        "covered": total - len(untouched),
        "percent": round(100 * (total - len(untouched)) / total, 1) if total else 100.0,
        "untouched": [{"id": s["id"], "page": s["page"], "text": s["text"][:120]} for s in untouched],
    }


def run_gate(work: Path) -> dict:
    doc = load_doc(work)
    path = work / "claims.json"
    if not path.exists():
        sys.exit(f"{path} not found. Write it first, see references/claims-schema.md.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"{path} is not valid JSON: {exc}")
    claims = data.get("claims", [])
    ids = [c.get("id") for c in claims]
    failed = {}
    for claim in claims:
        problems = check_claim(claim, doc, work)
        if ids.count(claim.get("id")) > 1:
            problems.append("duplicate id")
        if problems:
            failed[claim.get("id", "?")] = problems
    return {"claims": claims, "failed": failed, "coverage": coverage(claims, data.get("skipped", []), doc), "doc": doc}


def cmd_check(args) -> None:
    gate = run_gate(Path(args.work))
    print(json.dumps({
        "claims": len(gate["claims"]),
        "passed": len(gate["claims"]) - len(gate["failed"]),
        "failed": gate["failed"],
        "coverage_percent": gate["coverage"]["percent"],
        "untouched_sentences": gate["coverage"]["untouched"][:25],
        "untouched_total": len(gate["coverage"]["untouched"]),
    }, ensure_ascii=False, indent=1))
    sys.exit(1 if gate["failed"] or gate["coverage"]["untouched"] else 0)


# --- output ----------------------------------------------------------------

def apply_downgrades(gate: dict) -> list[dict]:
    final = []
    for claim in gate["claims"]:
        claim = dict(claim)
        problems = gate["failed"].get(claim.get("id"))
        if problems:
            claim["model_verdict"] = claim.get("verdict")
            claim["verdict"] = "unverified"
            claim["explanation"] = ("Failed the automatic check, so it is not reported as a finding: "
                                    + "; ".join(problems) + ". Model note: " + claim.get("explanation", ""))
        final.append(claim)
    rank = {v: i for i, v in enumerate(SEVERITY)}
    return sorted(final, key=lambda c: (rank.get(c["verdict"], 99), c.get("page", 0)))


def evidence_line(ev: dict) -> str:
    if ev.get("kind") == "document":
        return f'Same document, p. {ev.get("page")}: "{ev.get("quote")}"'
    label = ev.get("title") or ev.get("url")
    date = f' ({ev["date"]})' if ev.get("date") else ""
    return f'[{label}]({ev.get("url")}){date}: "{ev.get("quote")}"'


def render_markdown(final: list[dict], gate: dict) -> str:
    doc, cov = gate["doc"], gate["coverage"]
    counts = {v: sum(c["verdict"] == v for c in final) for v in [*SEVERITY, "confirmed"]}
    lines = [
        f"# Fact-check report: {Path(doc['source']).name}", "",
        f"**{len(final)} claims checked, {sum(counts[v] for v in FINDINGS)} findings.** "
        + " · ".join(f"{counts[v]} {v}" for v in [*SEVERITY, "confirmed"] if counts[v]),
        "",
        f"Coverage: {cov['covered']} of {cov['required']} number-bearing sentences ({cov['percent']}%). "
        "Every quote below was matched verbatim by script and every calculation recomputed.",
        "",
    ]
    for claim in final:
        if claim["verdict"] == "confirmed":
            continue
        lines += [f"## {claim.get('id')} · {claim['verdict'].upper()} · page {claim.get('page')}", "",
                  f"> {claim.get('quote', '')}", ""]
        if claim.get("explanation"):
            lines += [claim["explanation"], ""]
        if claim.get("correction"):
            lines += [f"**Proposed correction:** {claim['correction']}", ""]
        if claim.get("calc", {}).get("result"):
            result = claim["calc"]["result"]
            lines += [f"**Calculation:** `{claim['calc']['expr']}` = {result['value']} (stated: {result['stated']})", ""]
        lines += [f"- {evidence_line(ev)}" for ev in claim.get("evidence", [])]
        lines.append("")
    confirmed = [c for c in final if c["verdict"] == "confirmed"]
    if confirmed:
        lines += ["## Confirmed", ""]
        lines += [f"- p. {c.get('page')}: {c.get('quote', '')}" for c in confirmed]
        lines.append("")
    if cov["untouched"]:
        lines += ["## Not checked", ""]
        lines += [f"- p. {s['page']}: {s['text']}" for s in cov["untouched"]]
        lines.append("")
    lines += ["---", "", f"Checked with the free [AI fact checker]({HOMEPAGE}) for Claude.", ""]
    return "\n".join(lines)


def find_quads(page, quote: str) -> list[tuple[float, float, float, float]]:
    """Locate a quote on a PDF page; one (x0, top, x1, bottom) box per text line."""
    words = page.extract_words(use_text_flow=True, keep_blank_chars=False)
    edge = ".,;:!?()[]\"'"  # a quote may stop before the punctuation glued to the PDF word
    tokens = [normalize(w["text"]).strip(edge) for w in words]
    target = [t for t in (piece.strip(edge) for piece in normalize(quote).split()) if t]
    if not target:
        return []
    for start in range(len(tokens)):
        pos, used = start, []
        for piece in target:
            if pos < len(tokens) and tokens[pos] == piece:
                used.append(words[pos])
                pos += 1
            else:
                used = []
                break
        if used:
            lines: dict[int, list[dict]] = {}
            for word in used:
                lines.setdefault(round(word["top"] / 3), []).append(word)
            return [(min(w["x0"] for w in ws), min(w["top"] for w in ws),
                     max(w["x1"] for w in ws), max(w["bottom"] for w in ws)) for ws in lines.values()]
    return []


def annotate_pdf(source: Path, final: list[dict], out: Path) -> int:
    import pdfplumber
    from pypdf import PdfReader, PdfWriter
    from pypdf.annotations import Highlight, Text
    from pypdf.generic import ArrayObject, FloatObject, NameObject, TextStringObject

    reader = PdfReader(str(source))
    writer = PdfWriter(clone_from=reader)
    colors = {"wrong": "ff6b6b", "inconsistent": "ffb347", "misleading": "ffe066",
              "outdated": "c9b6ff", "unverified": "cccccc"}
    placed = 0
    with pdfplumber.open(source) as pdf:
        for claim in final:
            if claim["verdict"] == "confirmed" or not 1 <= claim.get("page", 0) <= len(pdf.pages):
                continue
            index = claim["page"] - 1
            page = pdf.pages[index]
            note = f"{claim['verdict'].upper()}: {claim.get('explanation', '')}"
            if claim.get("correction"):
                note += f"\n\nProposed correction: {claim['correction']}"
            for ev in claim.get("evidence", []):
                note += "\n\n" + re.sub(r"\[(.*?)\]\((.*?)\)", r"\1 \2", evidence_line(ev))
            boxes = find_quads(page, claim.get("quote", ""))
            height = float(page.height)
            if boxes:
                rect = (min(b[0] for b in boxes), height - max(b[3] for b in boxes),
                        max(b[2] for b in boxes), height - min(b[1] for b in boxes))
                quads = ArrayObject()
                for x0, top, x1, bottom in boxes:
                    for value in (x0, height - top, x1, height - top, x0, height - bottom, x1, height - bottom):
                        quads.append(FloatObject(value))
                annotation = Highlight(rect=rect, quad_points=quads, highlight_color=colors[claim["verdict"]])
            else:  # quote wraps a table the word search cannot follow: pin a note to the margin
                annotation = Text(rect=(10, height - 40 - 22 * placed % 600, 30, height - 20 - 22 * placed % 600),
                                  text=note)
            annotation[NameObject("/Contents")] = TextStringObject(note)
            annotation[NameObject("/T")] = TextStringObject(f"Fact check {claim.get('id')}")
            writer.add_annotation(page_number=index, annotation=annotation)
            placed += 1
    with out.open("wb") as handle:
        writer.write(handle)
    return placed


def cmd_report(args) -> None:
    work = Path(args.work)
    gate = run_gate(work)
    final = apply_downgrades(gate)
    report = work / "report.md"
    report.write_text(render_markdown(final, gate), encoding="utf-8")
    result = {"report": str(report), "downgraded": list(gate["failed"]),
              "coverage_percent": gate["coverage"]["percent"]}
    source = Path(gate["doc"]["source"])
    if source.suffix.lower() == ".pdf" and source.exists():
        out = work / (source.stem + ".annotated.pdf")
        result["annotated_pdf"] = str(out)
        result["annotations"] = annotate_pdf(source, final, out)
    print(json.dumps(result, ensure_ascii=False, indent=1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("extract", help="read a PDF, HTML, Markdown or text file into the work folder")
    p.add_argument("document")
    p.add_argument("--work", default=".factcheck")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("fetch", help="snapshot a source URL as raw text")
    p.add_argument("url")
    p.add_argument("--work", default=".factcheck")
    p.add_argument("--from-file", help="use a page you saved by hand when the site blocks scripts")
    p.set_defaults(func=cmd_fetch_entry)

    p = sub.add_parser("calc", help="recompute a figure exactly")
    p.add_argument("expr")
    p.add_argument("--stated", required=True)
    p.add_argument("--decimals", type=int, default=0)
    p.set_defaults(func=cmd_calc)

    p = sub.add_parser("check", help="run the gate on claims.json; exit 1 while anything fails")
    p.add_argument("--work", default=".factcheck")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("report", help="write report.md and the annotated PDF; failed claims become unverified")
    p.add_argument("--work", default=".factcheck")
    p.set_defaults(func=cmd_report)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
