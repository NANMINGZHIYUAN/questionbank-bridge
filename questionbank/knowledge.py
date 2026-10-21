"""Publish authored course knowledge while keeping source banks private.

This module arranges existing, verified text; it does not infer knowledge from
questions. Only this workflow's marked blocks are replaced. Original source
files are checked before and after publishing and are never copied or written.
"""
from __future__ import annotations

import copy
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from .core import UNSAFE_CARD_CODES, file_sha256, strip_personal_answers, validate_bank


class KnowledgePublishError(ValueError):
    """A source, ownership, privacy or concurrent-write check prevented publish."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _component(value, label):
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or value in {".", ".."} or re.search(r'[<>:"/\\|?*\x00-\x1f]', value)
            or value.endswith((".", " ")) or len(value) > 150
            or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", value, re.I)):
        raise KnowledgePublishError(f"Unsafe {label}: {value!r}")
    return value


def _under(root, path):
    if path.is_symlink():
        raise KnowledgePublishError("Refusing symlink destination")
    root, path = root.resolve(), path.resolve()
    if not path.is_relative_to(root):
        raise KnowledgePublishError("Destination escaped its permitted root")
    return path


def _snapshot(path):
    if path.is_symlink():
        raise KnowledgePublishError(f"Refusing symlink destination: {path}")
    if not path.exists():
        return None
    if not path.is_file():
        raise KnowledgePublishError(f"Destination is not a file: {path}")
    info = path.stat()
    return (path.read_bytes(), info.st_mtime_ns)


def _block(key, text):
    return f"<!-- questionbank:{key}:start -->\n{text.rstrip()}\n<!-- questionbank:{key}:end -->"


def _replace_block(old, key, text, *, owned=False):
    start, end = f"<!-- questionbank:{key}:start -->", f"<!-- questionbank:{key}:end -->"
    if old.count(start) != old.count(end) or old.count(start) > 1:
        raise KnowledgePublishError("Malformed or duplicate managed block")
    block = _block(key, text)
    if start in old:
        a, b = old.index(start), old.index(end) + len(end)
        if b <= a:
            raise KnowledgePublishError("Malformed managed block order")
        return old[:a] + block + old[b:]
    if owned and old:
        raise KnowledgePublishError("Existing course page is not owned by this workflow")
    return old + ("\n\n" if old and not old.endswith("\n\n") else "") + block + "\n"


def _yaml_title(title, kind, date_string, source):
    source_yaml = json.dumps(source, ensure_ascii=False)
    return (f"---\ntype: {kind}\ntags: [课程知识, 题库衍生]\ndate: {date_string}\n"
            f"source: {source_yaml}\nstatus: active\n---\n\n# {title}\n\n")


def _public_text(text, sources):
    """Redact local/learning references, leaving public HTTPS citations readable."""
    for source in sources:
        for field in ("path", "url"):
            value = source.get(field)
            if isinstance(value, str) and value:
                text = text.replace(value, f"private-source:QB-{source['id']}")
    def url(match):
        value = match.group(0)
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if parsed.username or parsed.password or host.endswith(("chaoxing.com", "chaoxing.cn", "xuexitong.com")):
            return "私密学习来源"
        return value
    text = re.sub(r"https?://[^\s<>\])]+", url, text)
    text = re.sub(r"file://[^\s\])]+|(?<![\w])(?:[A-Za-z]:[\\/])[^\n\])]+", "本地私密资料", text)
    if "<!-- questionbank:" in text or re.search(r"(?m)^#\s", text):
        raise KnowledgePublishError("Authored content contains a managed marker or another H1")
    return text


def _source_inventory(bank):
    result = []
    for source in bank["sources"]:
        _component(source["id"], "source id")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", source["id"]):
            raise KnowledgePublishError("Unsafe source ID")
        if not source.get("path") or not source.get("sha256"):
            raise KnowledgePublishError("A source requires its original path and SHA-256")
        path = Path(source["path"])
        if not path.is_absolute() or not path.is_file():
            raise KnowledgePublishError("Original source is missing or not an absolute file path")
        info = path.stat()
        actual = file_sha256(path)
        if actual.lower() != str(source["sha256"]).lower():
            raise KnowledgePublishError("Original source SHA-256 no longer matches the bank")
        if source.get("bytes") is not None and source["bytes"] != info.st_size:
            raise KnowledgePublishError("Original source byte count no longer matches the bank")
        result.append(dict(copy.deepcopy(source), path=str(path.resolve()), bytes=info.st_size,
                           mtimeNs=info.st_mtime_ns, sha256=actual))
    return result


def _check_sources(inventory):
    for source in inventory:
        path = Path(source["path"])
        if (not path.is_file() or path.stat().st_size != source["bytes"]
                or path.stat().st_mtime_ns != source["mtimeNs"]
                or file_sha256(path) != source["sha256"]):
            raise KnowledgePublishError("Original source changed during publication")


def _atomic_replace(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".qb-publish-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _commit(plans, baseline, inventory):
    originals = {Path(source["path"]).resolve() for source in inventory}
    if originals.intersection(path.resolve() for path in plans):
        raise KnowledgePublishError("A publication destination overlaps an original source")
    changed = [path for path, content in plans.items()
               if baseline[path] is None or baseline[path][0] != content]
    _check_sources(inventory)
    for path in plans:
        if _snapshot(path) != baseline[path]:
            raise KnowledgePublishError("An existing page changed concurrently; no publish performed")
    written = []
    written_snapshots = {}
    try:
        for path in changed:
            if _snapshot(path) != baseline[path]:
                raise KnowledgePublishError("An existing page changed concurrently")
            _atomic_replace(path, plans[path])
            written.append(path)
            written_snapshots[path] = _snapshot(path)
        _check_sources(inventory)
        for path in plans:
            expected = written_snapshots.get(path, baseline[path])
            if _snapshot(path) != expected:
                raise KnowledgePublishError("An existing page changed concurrently")
    except BaseException:
        for path in reversed(written):
            # Preserve another writer's edits even during rollback.
            if not path.exists() or path.read_bytes() != plans[path]:
                continue
            original = baseline[path]
            if original is None:
                path.unlink()
            else:
                _atomic_replace(path, original[0])
                os.utime(path, ns=(path.stat().st_atime_ns, original[1]))
        raise
    return changed


def publish_knowledge(bank, vault_root, date_string=None, private_root=None):
    """Publish authored topics; return paths and conservation evidence.

    ``vault_root`` is the local public vault (e.g. 本地知识库). The private root
    defaults to its sibling ``个人/题库`` and must be beneath that sibling
    ``个人`` directory, outside the public vault. Caller remains responsible for
    Git staging/commits and for approving semantic knowledge content.
    """
    validate_bank(bank)
    bank = strip_personal_answers(bank)
    for question in bank["questions"]:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", question["id"]):
            raise KnowledgePublishError("Unsafe question ID")
    course = _component(bank["course"], "course")
    date_string = date_string or date.today().isoformat()
    try:
        if date.fromisoformat(date_string).isoformat() != date_string:
            raise ValueError()
    except (ValueError, TypeError):
        raise KnowledgePublishError("date_string must be YYYY-MM-DD") from None
    vault = Path(vault_root).resolve()
    personal = vault.parent / "个人"
    if not personal.resolve().is_relative_to(vault.parent):
        raise KnowledgePublishError("Private storage symlink escaped the vault parent")
    private = _under(personal, Path(private_root) if private_root else personal / "题库")
    if private.is_relative_to(vault):
        raise KnowledgePublishError("Private question bank cannot be inside the public vault")
    private = _under(private, private / course)
    if not bank["topics"]:
        return {"status": "no_topics", "course": course, "written": [], "sourceConservation": [],
                "warnings": ["No authored topics; no knowledge pages or registry entries published."]}
    key = "course-" + hashlib.sha256(course.encode()).hexdigest()[:16]
    course_dir = _under(vault, vault / "03-资源库" / "课程" / course)
    state_path = _under(private, private / "publication-state.json")
    state_snapshot = _snapshot(state_path)
    state = json.loads(state_snapshot[0].decode("utf-8")) if state_snapshot else {}
    if state and (state.get("course") != course or state.get("vaultRoot") != str(vault)):
        raise KnowledgePublishError("Existing publication state belongs to another course or vault")
    question_index_path = private / "题目索引.json"
    index_snapshot = _snapshot(question_index_path)
    if state and not index_snapshot:
        raise KnowledgePublishError("Publication state exists but private question index is missing")
    previous_index = json.loads(index_snapshot[0].decode("utf-8")) if index_snapshot else {}
    if state:
        for field, older in (("questions", previous_index.get("questions", [])),
                             ("sources", previous_index.get("sources", [])),
                             ("topics", state.get("topics", []))):
            by_id = {entry["id"]: copy.deepcopy(entry) for entry in older}
            for entry in bank[field]:
                by_id[entry["id"]] = entry
            bank[field] = list(by_id.values())
        if 'knowledgeStages' not in bank and state.get('knowledgeStages'):
            bank['knowledgeStages'] = copy.deepcopy(state['knowledgeStages'])
    validate_bank(bank)
    diagnostics = validate_bank(bank)
    if any(entry["code"] in {"missing_source_reference", "unknown_topic_reference", "unknown_question_reference"}
           and not entry.get("cardId") for entry in diagnostics):
        raise KnowledgePublishError("Knowledge publication contains unresolved references")
    unsafe_ids = {entry["questionId"] for entry in diagnostics
                  if entry.get("questionId") and entry["code"] in UNSAFE_CARD_CODES}
    skipped_cards = []
    skipped_card_details = []
    question_ids = {question["id"] for question in bank["questions"]}
    inventory = _source_inventory(bank)
    if not inventory:
        raise KnowledgePublishError("Authored knowledge requires at least one conserved source")
    fingerprint = hashlib.sha256(_json(bank).encode()).hexdigest()
    topic_files = copy.deepcopy(state.get("topicFiles", {}))
    topic_dates = copy.deepcopy(state.get("topicDates", {}))
    first_date = state.get("firstPublishedDate", date_string)
    known_files = set(topic_files.values())
    for name in known_files:
        _component(name, "stored filename")
    # Preserve numbering and untouched prior topics in a partial update.
    next_number = max([int(name.split("-", 1)[0]) for name in known_files] or [0]) + 1
    for topic in bank["topics"]:
        _component(topic["title"], "topic title")
        if topic["id"] not in topic_files:
            topic_files[topic["id"]] = f"{next_number:02d}-{topic['title']}.md"
            next_number += 1
        topic_dates.setdefault(topic["id"], date_string)
    plans, baseline = {}, {}
    def read(path):
        if path not in baseline:
            baseline[path] = _snapshot(path)
        return baseline[path][0].decode("utf-8") if baseline[path] else ""
    def put(path, text):
        read(path)
        plans[path] = text.encode("utf-8")
    ignore = vault.parent / ".gitignore"
    ignore_text = read(ignore)
    # A literal root rule protects even freshly created private directories.
    ignore_lines = [line.strip() for line in ignore_text.splitlines()]
    if (not any(line in {"个人", "个人/", "/个人", "/个人/"} for line in ignore_lines)
            or any(line.startswith("!") and "个人" in line for line in ignore_lines)):
        put(ignore, ignore_text + ("\n" if ignore_text and not ignore_text.endswith("\n") else "") + "/个人/\n")
    # Record the state baseline immediately, before any page planning.
    baseline[state_path] = state_snapshot
    baseline[question_index_path] = index_snapshot
    page_names = sorted(topic_files.values(), key=lambda name: int(name.split("-", 1)[0]))
    map_name = f"00-{course}课程知识地图"
    source_name = f"QB-{course}-来源清单"
    source_link = f"03-资源库/来源清单/{source_name}"
    course_link = f"03-资源库/课程/{course}"
    page_links = [f"[[{course_link}/{Path(name).stem}]]" for name in page_names]
    for topic in bank["topics"]:
        filename = topic_files[topic["id"]]
        stem = Path(filename).stem
        path = _under(course_dir, course_dir / filename)
        old = read(path)
        header = _yaml_title(stem, "concept", topic_dates[topic["id"]], f"[[{source_link}]]") + f"> 返回：[[{map_name}]]\n\n"
        if old:
            if not old.startswith(header):
                raise KnowledgePublishError("Existing topic header/navigation changed; manual merge required")
        else:
            old = header
        body = _public_text(topic["explanation"], inventory).strip()
        if not body:
            raise KnowledgePublishError("An authored topic needs explanation; no fabricated knowledge page")
        cards = []
        for card in topic["cards"]:
            references = card["questionIds"] or topic["questionIds"]
            reason = ("empty_card" if not card["prompt"].strip() or not card["answer"].strip()
                      else "unknown_question_reference" if not references or any(qid not in question_ids for qid in references)
                      else "unsafe_question_reference" if unsafe_ids.intersection(references) else None)
            if reason:
                skipped_cards.append(card["id"])
                skipped_card_details.append({"id": card["id"], "reason": reason, "questionIds": references})
                continue
            cards.append(f"- **{_public_text(card['prompt'], inventory)}** {_public_text(card['answer'], inventory)}")
        if cards:
            body += "\n\n## 本课复习\n\n" + "\n".join(cards)
        qids = list(dict.fromkeys(topic["questionIds"] + [q["id"] for q in bank["questions"] if topic["id"] in q.get("topicIds", [])]))
        if qids:
            body += "\n\n## 关联原题\n\n" + "\n".join(f"- `{qid}`（完整题目与答案保存在本地私密题目索引）" for qid in qids)
        body += f"\n\n## 来源\n\n[[{source_link}]]\n"
        put(path, _replace_block(old, key + ":topic-" + hashlib.sha256(topic["id"].encode()).hexdigest()[:16], body))
        if baseline[path] and f"<!-- questionbank:{key}:topic-" not in baseline[path][0].decode():
            raise KnowledgePublishError("Existing topic page is not owned by this workflow")
    map_path = _under(course_dir, course_dir / (map_name + ".md"))
    old = read(map_path)
    header = _yaml_title(map_name, "overview", first_date, f"[[{source_link}]]")
    if old and not old.startswith(header):
        raise KnowledgePublishError("Existing knowledge map requires manual merge")
    map_body = "## 学习顺序\n\n" + "\n".join(f"{i + 1}. {link}" for i, link in enumerate(page_links))
    stages = bank.get("knowledgeStages", [])
    if stages:
        map_body += "\n\n## 阶段目标与过关条件\n"
        known_topics = {topic["id"] for topic in bank["topics"]}
        for stage in stages:
            references = stage.get("topicIds", [])
            if not references or any(tid not in known_topics for tid in references):
                raise KnowledgePublishError("A learning stage references missing topics")
            title = _public_text(str(stage.get("title", "")), inventory).strip()
            goal = _public_text(str(stage.get("goal", "")), inventory).strip()
            checkpoint = _public_text(str(stage.get("checkpoint", "")), inventory).strip()
            if not title or not goal or not checkpoint:
                raise KnowledgePublishError("A learning stage requires title, goal and checkpoint")
            links = "、".join(f"[[{course_link}/{Path(topic_files[tid]).stem}]]" for tid in references)
            map_body += f"\n### {title}\n\n学习范围：{links}\n\n阶段目标：{goal}\n\n过关标准：{checkpoint}\n"
    else:
        map_body += "\n\n## 阶段目标与过关条件\n\n按编号学习每个主题；能够解释该主题的基本概念，并完成对应页的复习检查后进入下一主题。\n"
    put(map_path, _replace_block(old or header, key, map_body, owned=bool(old)))
    previous_sources = state.get("sources", {})
    source_rows = []
    source_status = {}
    for source in inventory:
        prior_hash = previous_sources.get(source["id"])
        status = "unchanged" if prior_hash == source["sha256"] else "updated" if prior_hash else "new"
        source_status[source["id"]] = status
        related_ids = {q["id"] for q in bank["questions"] if any(ref["sourceId"] == source["id"] for ref in q.get("sources", []))}
        related_pages = [f"[[{course_link}/{Path(topic_files[t['id']]).stem}]]" for t in bank["topics"]
                         if related_ids.intersection(t["questionIds"] + [q["id"] for q in bank["questions"] if t["id"] in q["topicIds"]])]
        fmt = str(source.get("format", "unknown"))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", fmt):
            raise KnowledgePublishError("Unsafe source format")
        source_rows.append(f"| `private-source:QB-{source['id']}` | {fmt} | {source['bytes']} | `{source['sha256']}` | represented | {status} | {'；'.join(related_pages)} |")
    # Identical publishes must not turn first-run source statuses into changes.
    same_bank = state.get("bankFingerprint") == fingerprint
    source_path = _under(vault, vault / "03-资源库" / "来源清单" / (source_name + ".md"))
    source_old = read(source_path)
    source_body = "| 私密来源ID | 格式 | 字节 | SHA-256 | 来源状态 | 本次变更 | 关联课程页 |\n|---|---|---:|---|---|---|---|\n" + "\n".join(source_rows)
    if not same_bank:
        source_header = _yaml_title(source_name, "source-summary", first_date,
                                    [f"private-source:QB-{sid}" for sid in sorted(source_status)])
        if source_old:
            old_header = _yaml_title(source_name, "source-summary", first_date,
                                    [f"private-source:QB-{sid}" for sid in sorted(previous_sources)])
            if not source_old.startswith(old_header):
                raise KnowledgePublishError("Existing source-list header changed; manual merge required")
            source_old = source_header + source_old[len(old_header):]
        put(source_path, _replace_block(source_old or source_header, key, source_body, owned=bool(source_old)))
    elif not source_old:
        raise KnowledgePublishError("Publication state exists but source list is missing")
    shared = [(vault / "index.md", "# 索引\n", "\n".join([f"- [[{course_link}/{map_name}]]"] + [f"- {link}" for link in page_links] + [f"- [[{source_link}]]"])),
              (vault / "03-资源库" / "来源登记.md", "# 来源登记\n", f"- {course}：[[{source_link}]]；精确来源与题库仅在本地私密目录维护。")]
    for path, initial, body in shared:
        old = read(path)
        put(path, _replace_block(old or initial, key, body))
    log_path = vault / "log.md"
    log_old = read(log_path)
    log_key = key + ":batch-" + fingerprint[:16]
    if f"<!-- questionbank:{log_key}:start -->" not in log_old:
        put(log_path, _replace_block(log_old or "# 变更日志\n", log_key,
            f"## [{date_string}] ingest | {course}\n\n- [[{course_link}/{map_name}]]；本批{'增量更新' if state else '编译'} {len(bank['topics'])} 个已核对主题。"))
    put(question_index_path, _json({"course": course, "questions": bank["questions"], "sources": inventory}))
    source_map_path = private / "来源映射.json"
    put(source_map_path, _json({"course": course, "sources": inventory,
                               "questionLocators": {q["id"]: q["sources"] for q in bank["questions"]}}))
    new_state = {"course": course, "vaultRoot": str(vault), "topicFiles": topic_files,
                 "bankFingerprint": fingerprint, "sources": {s["id"]: s["sha256"] for s in inventory},
                 "topics": bank["topics"], "firstPublishedDate": first_date, "topicDates": topic_dates}
    if stages:
        new_state['knowledgeStages'] = stages
    put(state_path, _json(new_state))
    public_paths = [path for path in plans if path.is_relative_to(vault)]
    for path in public_paths:
        content = plans[path].decode("utf-8")
        for source in inventory:
            if any(str(source.get(field, "")) and str(source[field]) in content for field in ("path", "url")):
                raise KnowledgePublishError("A private source reference remains in a public page")
    report = {"status": "published", "course": course, "bankFingerprint": fingerprint,
              "map": str(map_path), "courseDirectory": str(course_dir), "topicFiles": topic_files,
              "sourceList": str(source_path), "privateDirectory": str(private),
              "questionIndex": str(question_index_path), "sourceMapping": str(source_map_path),
              "sourceConservation": [{"id": s["id"], "path": s["path"], "sha256": s["sha256"],
                                       "bytes": s["bytes"], "mtimeNs": s["mtimeNs"], "verified": True} for s in inventory],
              "sourceStatus": source_status, "publicFiles": [str(p) for p in public_paths]}
    report["skippedUnsafeCards"] = skipped_cards
    report["skippedUnsafeCardDetails"] = skipped_card_details
    report_path = private / "report.json"
    # Keep the persisted report stable on an identical publication.
    if not same_bank or not report_path.exists():
        put(report_path, _json(report))
    changed = _commit(plans, baseline, inventory)
    report["written"] = [str(path) for path in changed]
    report["report"] = str(report_path)
    return report
