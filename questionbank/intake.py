"""Read-only source extraction and conservative question-text parsing.

Extraction never rewrites an input.  Images and scanned pages deliberately go
through a human visual transcription step, rather than being reported as
successfully OCR'd.  Parsing keeps uncertain material visible in ``unparsed``.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import unquote


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
TYPE_NAMES = {
    "单选": "单选", "单选题": "单选", "单项选择": "单选", "单项选择题": "单选",
    "single": "单选", "single choice": "单选",
    "多选": "多选", "多选题": "多选", "多项选择": "多选", "多项选择题": "多选",
    "multiple": "多选", "multiple choice": "多选",
    "不定项": "不定项", "不定项题": "不定项", "不定项选择": "不定项", "不定项选择题": "不定项",
    "任选": "不定项", "任选题": "不定项", "任选选择题": "不定项",
    "判断": "判断", "判断题": "判断", "true/false": "判断", "true false": "判断",
    "填空": "填空", "填空题": "填空", "fill": "填空", "fill in the blank": "填空",
    "简答": "简答", "简答题": "简答", "问答": "简答", "问答题": "简答", "short": "简答", "short answer": "简答",
}
_NUMBER = r"(?:\d{1,5}|[一二三四五六七八九十百千]+)"
_HEADER = re.compile(
    rf"^(?:(?:question|q)\s*)?(?:[（(]\s*(?P<paren>{_NUMBER})\s*[）)]|"
    rf"(?P<number>{_NUMBER})\s*(?:[、．:：)\]]|\.(?!\d)))\s*(?P<body>.*)$", re.I
)
_TYPE_MARKER = re.compile(r"^(?:【(?P<cjk>[^】\n]{1,40})】|\[(?P<bracket>[^\]\n]{1,40})\])\s*")
_BARE_TYPE = re.compile(r"^(单[项]?选择?题?|单选题?|多[项]?选择?题?|多选题?|不定项(?:选择)?题?|任选(?:选择)?题?|判断题?|填空题?|简答题?|问答题?)\s*[:：]\s*")
_OPTION = re.compile(r"^\s*(?:[-*+]\s+)?(?:[（(]\s*([A-Z])\s*[）)]|([A-Z])\s*[.．、):：])\s*(.*)$", re.I)
PERSONAL_ANSWER_LABELS = frozenset({
    "我的答案", "我的作答", "个人作答", "个人答案", "用户答案", "用户作答", "学生答案", "学生作答",
    "你的答案", "你的作答", "考生答案", "your answer", "my answer", "user answer", "student answer",
    "personal answer", "selected answer",
})
_PERSONAL_LABEL_PATTERN = "|".join(
    re.escape(label).replace(r"\ ", r"\s+") for label in sorted(PERSONAL_ANSWER_LABELS, key=len, reverse=True)
)
_FIELD = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:\*\*)?(?:【|\[)?"
    r"(?P<label>正确答案|参考答案|标准答案|答案|"
    + _PERSONAL_LABEL_PATTERN + r"|"
    r"原解析|答案解析|解析|解释|正确答案及解析|案例|case|correct\s+answer|answer|explanation)"
    r"(?:】|\])?(?:\*\*)?\s*(?:[:：]\s*|(?=$))(?P<body>.*)$", re.I
)
_BRACKET_PERSONAL_FIELD = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:\*\*)?(?:【(?:" + _PERSONAL_LABEL_PATTERN + r")】|\[(?:"
    + _PERSONAL_LABEL_PATTERN + r")\])(?:\*\*)?\s*[:：]?\s*.*$", re.I
)
_IMAGE_MD = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<src>[^\s)]+)(?:\s+[^)]*)?\)")
_IMAGE_HTML = re.compile(r"<img\b[^>]*\bsrc=[\"'](?P<src>[^\"']+)[\"'][^>]*>", re.I)
_PAGE_MARKER = re.compile(r"^<!--\s*PDF page \d+\s*-->$")
_UNCONFIRMED_ANSWER = re.compile(
    r"^(?:待核实|未提供[，,、\s]*待核实)(?:\s*[（(](?:原题)?参考答案[：:].*[）)])?\s*[。.]?$", re.S
)


def _read_text(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8-sig"), "UTF-8 (BOM accepted)"
    except UnicodeDecodeError:
        try:
            return raw.decode("gb18030"), "GB18030 fallback"
        except UnicodeDecodeError as exc:
            raise ValueError(f"Cannot decode {path.name} as UTF-8 or GB18030") from exc


def _safe_work(path: Path, work: Path) -> Path:
    resolved = work.resolve()
    if resolved == path.resolve():
        raise ValueError("Extraction work directory must not be the input file")
    if resolved.exists() and not resolved.is_dir():
        raise ValueError("Extraction work must be a directory, not an existing file")
    return resolved


def extract_source(path: Path, work: Path, pandoc: str = "pandoc") -> dict[str, Any]:
    """Extract text with source identity, visual-review flags, and explicit notes.

    JSON is *not* parsed here: malformed JSON is returned verbatim so the caller
    can report a JSON error without changing the source.  DOCX media are placed
    in a source-specific child of ``work``.  Unsupported legacy DOC fails clearly.
    """
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Input file does not exist: {path}")
    work = _safe_work(path, Path(work))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    suffix = path.suffix.lower()
    source = {"id": "src-" + digest[:16], "path": str(path), "sha256": digest, "format": suffix.lstrip(".")}
    result: dict[str, Any] = {"source": source, "text": "", "needsVisualReview": False, "notes": []}

    if suffix in {".json", ".txt", ".md", ".markdown"}:
        result["text"], encoding = _read_text(path)
        result["notes"].append(f"Decoded with {encoding}.")
    elif suffix == ".docx":
        media = work / ("intake-" + source["id"]) / "media"
        if path == media or media in path.parents:
            raise ValueError("Derived extraction directory must not contain the input file")
        media.parent.mkdir(parents=True, exist_ok=True)
        try:
            process = subprocess.run(
                [pandoc, str(path), "--from=docx", "--to=markdown", "--wrap=none", "--extract-media=" + str(media)],
                capture_output=True, encoding="utf-8", errors="replace", check=False,
            )
        except OSError as exc:
            raise RuntimeError(f"Cannot run Pandoc for DOCX extraction: {exc}") from exc
        if process.returncode:
            raise RuntimeError(f"Pandoc DOCX conversion failed: {process.stderr.strip()[:1200]}")
        cleaned = strip_personal_answer_text(process.stdout)
        retained_images = _image_references(cleaned)
        discarded_images = _image_references(process.stdout) - retained_images
        removed_media = 0
        for reference in discarded_images:
            if re.match(r"^(?:https?://|data:|blob:)", reference, re.I):
                continue
            candidate = Path(unquote(reference.strip("<>")))
            if not candidate.is_absolute():
                candidate = media.parent / candidate
            candidate = candidate.resolve()
            if candidate.is_relative_to(media.resolve()) and candidate.is_file():
                candidate.unlink()
                removed_media += 1
        result["text"] = cleaned
        result["mediaDir"] = str(media)
        if cleaned != process.stdout:
            result["notes"].append(f"Recognized personal-answer text discarded; {removed_media} exclusively personal derived image file(s) removed.")
        if process.stderr.strip():
            result["notes"].append("Pandoc: " + process.stderr.strip()[:1200])
        if retained_images:
            result["needsVisualReview"] = True
            result["notes"].append("Embedded images extracted; their contents require visual review before transcription.")
        result["notes"].append("Pandoc Markdown extraction preserves available mathematical notation.")
    elif suffix == ".doc":
        raise ValueError("Legacy DOC is not a supported Pandoc input. Convert a copy to DOCX first; the source was not modified.")
    elif suffix == ".pdf":
        result["needsVisualPages"] = []
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("PDF extraction requires pypdf in the selected Python runtime") from exc
        try:
            reader = PdfReader(str(path))
            if reader.is_encrypted and not reader.decrypt(""):
                raise ValueError("Encrypted PDF requires an unlocked copy")
            page_parts = []
            for index, page in enumerate(reader.pages, 1):
                page_text = page.extract_text() or ""
                page_parts.append(f"<!-- PDF page {index} -->\n\n{page_text}")
                if not page_text.strip():
                    result["needsVisualReview"] = True
                    result["needsVisualPages"].append(index)
                    result["notes"].append(f"Page {index}: no extractable text; inspect the original page visually.")
                try:
                    has_images = len(page.images) > 0
                except Exception:
                    has_images = True
                    result["notes"].append(f"Page {index}: image inventory could not be read; visual review required.")
                if has_images:
                    result["needsVisualReview"] = True
                    if index not in result["needsVisualPages"]:
                        result["needsVisualPages"].append(index)
                    result["notes"].append(f"Page {index}: embedded images may contain question content; inspect the original page visually.")
            if not page_parts:
                result["needsVisualReview"] = True
                result["notes"].append("PDF has no pages; no question content was extracted.")
            result["text"] = "\n\n".join(page_parts)
            result["notes"].append("PDF text order and formulas require comparison with the original layout.")
        except (ValueError, RuntimeError):
            raise
        except Exception as exc:
            raise RuntimeError(f"PDF text extraction failed for {path.name}: {exc}") from exc
    elif suffix in IMAGE_SUFFIXES:
        result["needsVisualReview"] = True
        result["notes"].append(f"Image requires visual transcription of the original file: {path}")
    else:
        raise ValueError(f"Unsupported source format: {suffix or '(no extension)'}")

    # This catches unexpected mutation by a converter as well as mistakes in this module.
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise RuntimeError("Input hash changed during extraction; stop and investigate")
    return result


def _strip_heading(line: str) -> str:
    line = re.sub(r"^\s*#{1,6}\s+", "", line).strip()
    # Pandoc escapes paragraph prefixes such as `1. ` to avoid Markdown lists.
    # Unescape only the numbering/option prefix, never mathematical backslashes.
    return re.sub(r"^((?:(?:Question|Q)\s*)?\d+|[A-Z])\\([.)])", r"\1\2", line, flags=re.I)


def _remove_type(body: str) -> tuple[str, str | None]:
    match = _TYPE_MARKER.match(body)
    if match:
        return body[match.end():].strip(), (match.group("cjk") or match.group("bracket")).strip()
    bare = _BARE_TYPE.match(body)
    if bare:
        return body[bare.end():].strip(), bare.group(1)
    return body, None


def _question_header(line: str) -> tuple[str | None, str, str | None] | None:
    line = _strip_heading(line)
    match = _HEADER.match(line)
    if match:
        body, raw_type = _remove_type(match.group("body"))
        return match.group("paren") or match.group("number"), body, raw_type
    body, raw_type = _remove_type(line)
    if raw_type is not None:
        # A type marker can appear before a serial number or stand alone.
        numbered = _HEADER.match(body)
        if numbered:
            return numbered.group("paren") or numbered.group("number"), numbered.group("body"), raw_type
        return None, body, raw_type
    return None


def _personal_label(label: str) -> bool:
    return re.sub(r"\s+", " ", label.strip().lower()) in PERSONAL_ANSWER_LABELS


def _image_references(text: str) -> set[str]:
    return {match.group("src") for pattern in (_IMAGE_MD, _IMAGE_HTML) for match in pattern.finditer(text)}


def strip_personal_answer_text(text: str) -> str:
    """Remove only recognized personal-answer fields, retaining source line numbers.

    Removed lines become blank lines; the input itself is never written.  A
    correct-answer/explanation marker or a recognized new question ends the
    personal field.  Decimal lines and clearly continuous numbered answer steps
    stay inside that field.  Ambiguous new numbering is retained for review.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    rows = text.splitlines(keepends=True)
    output: list[str] = []
    personal = False
    question_number: str | None = None
    step_number: int | None = None
    fence: str | None = None

    def next_is_option(index: int) -> bool:
        for following in rows[index + 1:]:
            candidate = _strip_heading(following.rstrip("\r\n"))
            if not candidate or _PAGE_MARKER.match(candidate):
                continue
            return bool(_OPTION.match(candidate))
        return False

    for index, row in enumerate(rows):
        body = row.rstrip("\r\n")
        ending = row[len(body):]
        line = _strip_heading(body)
        marker = _FIELD.match(line)
        personal_marker = bool(
            marker and _personal_label(marker.group("label"))
        ) or bool(_BRACKET_PERSONAL_FIELD.match(line))
        fence_match = re.match(r"^\s*(`{3,}|~{3,})", body)
        if not personal and fence_match:
            if fence is None:
                fence = fence_match.group(1)[0]
            elif fence == fence_match.group(1)[0]:
                fence = None
            output.append(row)
            continue
        if fence is not None and not personal:
            output.append(row)
            continue
        if personal_marker:
            personal = True
            step_number = None
            output.append(ending)
            continue
        if personal and _PAGE_MARKER.match(line):
            # Page markers are provenance, not answer content or a field boundary.
            output.append(row)
            continue
        if personal and marker:
            personal = False
            step_number = None
            output.append(row)
            continue

        header = _question_header(body)
        if personal and header is not None:
            serial, title, raw_type = header
            clear_question = bool(
                raw_type is not None or title.lower() in TYPE_NAMES
                or re.match(r"^(?:question|q)\s*\d", line, re.I)
                or title.rstrip().endswith(("?", "？")) or next_is_option(index)
            )
            continuous_step = False
            if not clear_question and serial and serial.isdigit():
                value = int(serial)
                previous_question = int(question_number) if question_number and question_number.isdigit() else None
                continuous_step = (
                    value == 1 or step_number is not None and value == step_number + 1
                    or previous_question is not None and value <= previous_question
                )
                if continuous_step:
                    step_number = value
            if clear_question or not continuous_step:
                # Unknown bare numbering can be a new question; keep it rather
                # than silently deleting text whose role is not established.
                personal = False
                step_number = None
                question_number = serial
                output.append(row)
                continue
        if personal:
            output.append(ending)
            continue
        if header is not None:
            question_number = header[0]
        output.append(row)
    return "".join(output)


def _answer_values(value: str, labels: set[str], qtype: str) -> list[str]:
    value = value.strip()
    if not value:
        return []
    # Only split unequivocal choice labels.  Never shorten decimal or prose answers.
    if qtype in {"单选", "多选", "不定项"} and labels:
        compact = re.sub(r"[\s,，、;；/]+", "", value).upper()
        if compact and all(ch in labels for ch in compact) and len(set(compact)) == len(compact):
            return list(compact)
    return [value]


def _field_name(label: str) -> str:
    label = label.lower()
    if _personal_label(label):
        return "ignored_personal"
    if label in {"原解析", "答案解析", "解析", "解释", "explanation", "正确答案及解析"}:
        return "explanation"
    if label in {"案例", "case"}:
        return "case"
    return "answer"


def parse_question_text(text: str) -> dict[str, Any]:
    """Parse common numbered question text without fabricating missing content.

    Unnumbered, unmarked prose is retained in ``unparsed``.  A type is accepted
    only when supplied explicitly; unknown labels are preserved as ``rawType``.
    """
    lines = strip_personal_answer_text(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    result: dict[str, Any] = {"items": [], "warnings": [], "unparsed": []}
    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    unmatched: list[str] = []
    unmatched_start = 1
    section_type: str | None = None
    current_page: int | None = None
    field_state = "title"
    for number, line in enumerate(lines, 1):
        standalone_type = _strip_heading(line).lower()
        if standalone_type in TYPE_NAMES and (current is None or re.match(r"^\s*#{1,6}\s", line)):
            section_type = standalone_type
            field_state = "title"
            continue
        if _PAGE_MARKER.match(line.strip()):
            current_page = int(re.search(r"\d+", line).group())
            if current is not None and current_page not in current["pages"]:
                # Until a new question begins, a following page can continue this
                # question even when it contains only an unextracted image.
                current["pages"].append(current_page)
            continue
        if re.fullmatch(r"\s*[-*_]{3,}\s*", line):
            continue
        # Answer field lines are not question headers even if their contents are numbered.
        field_marker = _FIELD.match(_strip_heading(line))
        header = None if field_marker else _question_header(line)
        if field_marker:
            field_state = _field_name(field_marker.group("label"))
        if header is not None:
            serial, title, raw_type = header
            boundary_issues = []
            if (not title and raw_type is not None) or (raw_type is None and title.lower() in TYPE_NAMES):
                # Explicit section headings provide a type for following questions.
                section_type = raw_type if raw_type is not None else title
                continue
            if current is not None and raw_type is None and field_state in {"answer", "explanation"}:
                previous = current["serial"]
                current_type = TYPE_NAMES.get((current["rawType"] or "").lower(), "未知")
                following = next((candidate for row in lines[number:]
                                  if (candidate := _strip_heading(row)) and not _PAGE_MARKER.match(candidate)), "")
                explicit_next_question = bool(
                    re.match(r"^(?:question|q)\s*\d", _strip_heading(line), re.I)
                    or title.rstrip().endswith(("?", "？")) or _OPTION.match(following)
                )
                repeated_number = (serial and previous and serial.isdigit() and previous.isdigit()
                                   and int(serial) <= int(previous))
                if (repeated_number or current_type == "简答") and not explicit_next_question:
                    # Bare numbered prose in a short answer may be steps even
                    # when the question number is 1. Retain it for review rather
                    # than truncating the answer into invented questions.
                    current["lines"].append((number, line))
                    current["lastTextPage"] = current_page
                    issue = {"code": "numbered_field_continuation", "message": "Numbered line inside an answer/explanation was retained there; verify the question boundary."}
                    result["warnings"].append(dict(issue, line=number))
                    current["issues"].append(issue)
                    continue
                issue = {"code": "ambiguous_question_boundary", "message": "A bare numbered line follows an answer/explanation; verify that it starts a new question rather than a numbered answer step."}
                result["warnings"].append(dict(issue, line=number))
                current["issues"].append(issue)
                boundary_issues.append(issue)
            if current is not None:
                if (current_page is not None and current_page != current["page"]
                        and current["lastTextPage"] != current_page
                        and current["pages"] and current["pages"][-1] == current_page):
                    # A new question is the first text on this page.  Keep prior
                    # empty scan pages associated, but not this unrelated page.
                    current["pages"].pop()
                blocks.append(current)
            elif any(piece.strip() for piece in unmatched):
                result["unparsed"].append({"line": unmatched_start, "text": "\n".join(unmatched).strip(), "reason": "Text before the first recognized question"})
            unmatched = []
            current = {"serial": serial, "title": title, "rawType": raw_type or section_type, "lines": [], "line": number, "page": current_page, "pages": [current_page] if current_page else [], "lastTextPage": current_page, "header": line, "issues": boundary_issues}
            field_state = "title"
        elif current is not None:
            current["lines"].append((number, line))
            if line.strip():
                current["lastTextPage"] = current_page
        else:
            if not unmatched:
                unmatched_start = number
            unmatched.append(line)
    if current is not None:
        blocks.append(current)
    elif any(piece.strip() for piece in unmatched):
        result["unparsed"].append({"line": unmatched_start, "text": "\n".join(unmatched).strip(), "reason": "No recognized question number or type marker"})

    for block in blocks:
        raw_type = block["rawType"]
        qtype = TYPE_NAMES.get((raw_type or "").lower(), "未知")
        item: dict[str, Any] = {
            "id": "", "type": qtype, "rawType": raw_type or "", "title": block["title"], "options": [],
            "correctAnswer": [], "originalExplanation": "", "images": [],
            "sourceLine": block["line"], "sourceQuestionNumber": block["serial"],
            "sourcePage": block["page"], "sourcePages": block["pages"], "rawText": "\n".join([block["header"], *(line for _, line in block["lines"])]),
            "issues": list(block["issues"]),
        }
        parts: dict[str, list[str]] = {"title": [block["title"]], "answer": [], "explanation": [], "case": []}
        field = "title"
        for line_number, line in block["lines"]:
            stripped = _strip_heading(line)
            marker = _FIELD.match(stripped)
            if marker:
                field = _field_name(marker.group("label"))
                if field == "ignored_personal":
                    continue
                parts[field].append(marker.group("body"))
                continue
            option = _OPTION.match(stripped) if field in {"title", "option"} else None
            if option:
                field = "option"
                label = (option.group(1) or option.group(2)).upper()
                if any(existing["label"] == label for existing in item["options"]):
                    issue = {"code": "duplicate_option_label", "message": f"Question at line {block['line']} repeats option {label}; check the source."}
                    result["warnings"].append(dict(issue, line=line_number))
                    item["issues"].append(issue)
                item["options"].append({"label": label, "text": option.group(3)})
            elif field == "option":
                item["options"][-1]["text"] += "\n" + line
            elif field != "ignored_personal":
                parts[field].append(line)
        item["title"] = "\n".join(parts["title"]).strip()
        if not item["title"]:
            result["unparsed"].append({"line": block["line"], "text": "\n".join(line for _, line in block["lines"]), "reason": "Recognized question marker has an empty question stem"})
            continue
        for option in item["options"]:
            option["text"] = option["text"].strip()
        original_answer = "\n".join(parts["answer"]).strip()
        item["originalAnswer"] = original_answer
        item["correctAnswer"] = _answer_values(original_answer, {option["label"] for option in item["options"]}, qtype)
        if _UNCONFIRMED_ANSWER.fullmatch(original_answer):
            item["correctAnswer"] = []
            issue = {"code": "requires_content_review", "message": "来源保留待核实状态；所列参考答案不能作为已确认答案。"}
            item["issues"].append(issue)
            result["warnings"].append(dict(issue, line=block["line"]))
        item["originalExplanation"] = "\n".join(parts["explanation"]).strip()
        if parts["case"]:
            item["case"] = "\n".join(parts["case"]).strip()
        image_fields = [("title", None, "\n".join(piece for piece in (item.get("case", ""), item["title"]) if piece))]
        image_fields += [("option", index, option["text"]) for index, option in enumerate(item["options"])]
        image_fields += [("answer", None, original_answer), ("explanation", None, item["originalExplanation"])]
        seen_images: set[tuple[str, str, int | None]] = set()
        for image_field, option_index, field_text in image_fields:
            for pattern in (_IMAGE_MD, _IMAGE_HTML):
                for image in pattern.finditer(field_text):
                    src = image.group("src")
                    identity = (src, image_field, option_index)
                    if identity not in seen_images:
                        seen_images.add(identity)
                        key = "url" if re.match(r"^(?:https?://|data:|blob:)", src, re.I) else "path"
                        entry = {key: src, "alt": image.groupdict().get("alt") or "", "required": True, "field": image_field}
                        if option_index is not None:
                            entry["optionIndex"] = option_index
                        item["images"].append(entry)
        identity = "\0".join([qtype, item["title"], *(option["label"] + ":" + option["text"] for option in item["options"])])
        item["id"] = "text-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        if qtype == "未知":
            result["warnings"].append({"line": block["line"], "code": "unknown_type", "message": f"Question type is unrecognized or absent: {raw_type or '(not supplied)'}. Kept for review."})
        if not item["correctAnswer"]:
            result["warnings"].append({"line": block["line"], "code": "missing_answer", "message": "Question has no original answer; do not create a definite-answer card."})
        result["items"].append(item)
    return result
