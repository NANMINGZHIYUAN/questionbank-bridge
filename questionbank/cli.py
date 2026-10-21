"""Small local helpers; semantic explanation stays with the assistant."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from .core import file_sha256, merge_banks, normalize, select_sample, strip_personal_answers, validate_bank
from .intake import extract_source, parse_question_text, strip_personal_answer_text


def _knowledge_date(value):
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError()
    except ValueError:
        raise argparse.ArgumentTypeError('知识发布日期必须为 YYYY-MM-DD。') from None
    return value


class _KnowledgePublicationFailure(RuntimeError):
    """Exports are complete, but their separate knowledge publication failed."""
    def __init__(self, message: str, result: dict):
        super().__init__(message)
        self.result = result


def _publish_course_knowledge(args, bank, out, result, source_hashes):
    """Attach phase evidence without changing manifest-managed exports.

    The export directory and knowledge vault have independent transactions.
    A failed second phase leaves the valid file exports available for review.
    """
    if args.command == 'sample':
        return result
    if args.skip_knowledge_publication:
        result['knowledgePublication'] = {'status': 'skipped', 'reason': 'explicit_skip',
                                          'published': False}
        return result
    if not args.knowledge_vault:
        result['knowledgePublication'] = {'status': 'skipped', 'reason': 'no_vault_specified',
                                          'published': False}
        return result
    if not bank['topics']:
        result['knowledgePublication'] = {'status': 'no_topics', 'published': False,
                                          'reason': '尚无已编写知识点，未读写知识库。'}
        return result
    vault = Path(args.knowledge_vault).resolve()
    try:
        after = {filename: file_sha256(filename) for filename in source_hashes}
        if after != source_hashes:
            raise RuntimeError('文件导出后来源变化，停止知识发布，请重新核对来源。')
        from .knowledge import publish_knowledge
        publication = publish_knowledge(bank, vault, date_string=args.knowledge_date)
    except (ValueError, OSError, RuntimeError, ImportError) as error:
        phase = {'status': 'failed', 'published': False, 'vaultRoot': str(vault),
                 'error': str(error), 'fileExportCompleted': True,
                 'fileExportReport': str(out / 'export-report.json')}
        result['knowledgePublication'] = phase
        # A distinct run report deliberately lives outside the export manifest.
        # mkstemp creates an owned path and never overwrites a user's sibling.
        inspection = {'schemaVersion': 1, 'operation': 'knowledge-publication-failure',
                      'knowledgePublication': deepcopy(phase), 'fileExports': deepcopy(result)}
        try:
            descriptor, filename = tempfile.mkstemp(prefix=out.name + '-knowledge-failure-',
                                                   suffix='.json', dir=out.parent)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(inspection, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
            phase['inspectionReport'] = str(Path(filename).resolve())
        except OSError as report_error:
            phase['inspectionReportError'] = str(report_error)
        raise _KnowledgePublicationFailure(
            '文件导出已完成；知识库发布未完成：' + str(error), result) from error
    result['knowledgePublication'] = publication
    return result


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_suffix(path.suffix + '.tmp')
    staged.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    staged.replace(path)


def read_bank(path: str | Path) -> dict:
    value = strip_personal_answers(json.loads(Path(path).read_text(encoding='utf-8-sig')))
    validate_bank(value)
    for question in value['questions']:
        if question.get('rawText'):
            question['rawText'] = strip_personal_answer_text(question['rawText'])
    return value


def guard_output(out: Path, sources: list[Path]) -> None:
    resolved = out.resolve()
    if resolved.exists() and not resolved.is_dir():
        raise ValueError('输出位置必须是目录，不能覆盖来源文件。')
    for source in sources:
        if source.resolve().is_relative_to(resolved):
            raise ValueError('原始输入不能位于输出目录内；请使用单独的输出目录。')


def ingest(args: argparse.Namespace) -> dict:
    paths = [Path(x).resolve() for x in args.inputs]
    transcripts = {}
    for source, text_file in args.transcript or []:
        resolved_source = Path(source).resolve()
        if resolved_source in transcripts:
            raise ValueError('同一原件只能绑定一份文字稿；重复 --transcript 不允许静默覆盖。')
        transcripts[resolved_source] = Path(text_file).resolve()
    if set(transcripts) - set(paths):
        raise ValueError('--transcript 的原始文件必须也出现在输入列表中。')
    out = Path(args.out)
    existing = read_bank(args.existing) if args.existing else None
    if existing and existing['course'] != args.course:
        raise ValueError('已有题库的课程名与本次课程不同，不能混合。')
    protected = paths + list(transcripts.values()) + ([Path(args.existing)] if args.existing else [])
    guard_output(out, protected)
    bank = deepcopy(existing) if existing else {
        'schemaVersion': 1, 'course': args.course, 'sources': [], 'questions': [], 'topics': []}
    reports = []
    before = {str(p): file_sha256(p) for p in protected}
    for path in paths:
        folder = out / 'work' / ('src-' + file_sha256(path)[:16])
        extracted = extract_source(path, folder, args.pandoc)
        text = extracted['text']
        visual = extracted.get('needsVisualReview', False)
        notes = list(extracted.get('notes', []))
        transcription_source = None
        if path in transcripts:
            transcription = extract_source(transcripts[path], folder / 'transcript', args.pandoc)
            text = transcription['text']
            visual = transcription.get('needsVisualReview', False)
            notes.append('使用经人工视觉识别和核对的文字稿；原始文件及文字稿哈希均保留。')
            extracted['source']['transcript'] = transcription['source']
            transcription_source = transcription['source']
        parser_report = {'warnings': [], 'unparsed': []}
        if path.suffix.lower() == '.json' and path not in transcripts:
            raw = json.loads(text)
        else:
            parser_report = parse_question_text(text)
            raw = {'items': parser_report['items']}
            if visual:
                notes.append('需要人工核对原始图片或 PDF 页面后提供文字稿，尚未完整识别。')
                visual_pages = set(extracted.get('needsVisualPages', []))
                for item in raw['items']:
                    pages = set(item.get('sourcePages', [item.get('sourcePage')]))
                    if pages.intersection(visual_pages) or item.get('images') or not visual_pages:
                        item.setdefault('issues', []).append({
                            'code': 'requires_visual_review',
                            'message': '此题所在页面或图片尚未完成视觉核对，暂不生成确定答案卡。'})
        if isinstance(raw, list):
            raw = {'items': raw}
        if not isinstance(raw, dict):
            raise ValueError('题库 JSON 必须为对象或题目数组。')
        if ('items' in raw) == ('questions' in raw):
            raise ValueError('题库必须且只能提供一个 items 或 questions 数组。')
        if not isinstance(raw.get('items', raw.get('questions', [])), list):
            raise ValueError('items 或 questions 必须是数组。')
        if transcription_source:
            if 'items' in raw:
                raw['questions'] = raw.pop('items')
            raw.setdefault('sources', []).append(transcription_source)
            for item in raw.get('items', raw.get('questions', [])):
                item.setdefault('sources', []).append({
                    'sourceId': transcription_source['id'],
                    'locator': '视觉文字稿;line=' + str(item.get('sourceLine', ''))})
        # The original is already retained and hash-registered. Avoid caching an
        # unfiltered text copy that could keep discarded personal answers.
        if raw.get('items') or raw.get('questions'):
            incoming = normalize(raw, args.course, extracted['source'])
            bank = merge_banks(bank, incoming)
        elif extracted['source']['id'] not in {s['id'] for s in bank['sources']}:
            bank['sources'].append(extracted['source'])
            if transcription_source:
                bank['sources'].append(transcription_source)
        reports.append({'source': extracted['source'], 'needsVisualReview': visual,
                        'notes': notes, 'parserWarnings': parser_report.get('warnings', []),
                        'unparsed': parser_report.get('unparsed', []),
                        'recognizedQuestions': len(raw.get('items', raw.get('questions', [])))})
    after = {str(p): file_sha256(p) for p in protected}
    if before != after:
        raise RuntimeError('来源文件在处理期间变化，停止交付；请重新读取来源。')
    issues = validate_bank(bank)
    report = {'course': args.course, 'sources': reports, 'questions': len(bank['questions']),
              'issues': issues, 'sourceHashesUnchanged': True,
              'requiresReview': not bank['questions'] or bool(issues) or
                                any(r['needsVisualReview'] or r['unparsed'] or r['parserWarnings']
                                    for r in reports)}
    write_json(out / 'bank.json', bank)
    write_json(out / 'intake-report.json', report)
    return {'bank': str((out / 'bank.json').resolve()), 'report': str((out / 'intake-report.json').resolve()),
            'questions': len(bank['questions']), 'requiresReview': report['requiresReview']}


def build(args: argparse.Namespace) -> dict:
    from .exporters import export_bundle
    path = Path(args.bank).resolve()
    input_hash = file_sha256(path)
    bank = deepcopy(read_bank(path))
    if file_sha256(path) != input_hash:
        raise RuntimeError('读取期间题库变化，停止交付，上一版成果保持不变。')
    for question in bank['questions']:
        for media in question.get('images', []):
            if media.get('path') and not Path(media['path']).is_absolute():
                candidate = path.parent / media['path']
                if candidate.is_file():
                    media['path'] = str(candidate.resolve())
    out = Path(args.out)
    protected = [path] + [Path(s['path']) for s in bank.get('sources', []) if s.get('path')]
    guard_output(out, protected)
    source_hashes = {str(path): input_hash}
    for source in bank['sources']:
        original = Path(source['path'])
        if not original.is_file():
            raise ValueError('登记的原件已不存在，无法复验来源：' + str(original))
        source_hash = file_sha256(original)
        if source_hash != source['sha256']:
            raise ValueError('原件内容与摄入时哈希不同，请重新摄入：' + str(original))
        source_hashes[str(original)] = source_hash
    if args.command == 'sample':
        bank = select_sample(bank, args.limit)
    if not bank['questions']:
        raise ValueError('没有已识别题目，请先完成识别或提供视觉文字稿。')
    # Source checks finish before replacing any final deliverable. The inner
    # exporter also protects direct callers through the same publication helper.
    from .publication import publish_export
    out = out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cli-export-stage-', dir=out.parent) as folder:
        staged = Path(folder)
        result = export_bundle(bank, staged, args.pandoc, args.word_qa_mode)
        after = {str(p): file_sha256(p) for p in protected}
        if source_hashes != after:
            raise RuntimeError('导出期间来源变化，停止交付，上一版成果保持不变。')
        result['sourceHashesUnchanged'] = True
        result['actualAnkiImportVerified'] = False
        result['actualFojiaoImportVerified'] = False
        result['sample'] = args.command == 'sample'
        result = publish_export(staged, out, result)
    return _publish_course_knowledge(args, bank, out, result, source_hashes)


def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(description='题库提取、规范化与本地导出助手')
    commands = main.add_subparsers(dest='command', required=True)
    entry = commands.add_parser('ingest', help='只读提取并规范化，不生成知识解释')
    entry.add_argument('inputs', nargs='+')
    entry.add_argument('--course', required=True)
    entry.add_argument('--out', required=True)
    entry.add_argument('--existing', help='增量合并已有 bank.json')
    entry.add_argument('--transcript', nargs=2, action='append', metavar=('SOURCE', 'TEXT'),
                       help='原图/扫描 PDF 与已核对文字稿的一一对应')
    entry.add_argument('--pandoc', default='pandoc')
    for command in ('sample', 'export'):
        entry = commands.add_parser(command, help='生成小样' if command == 'sample' else '生成整批产物')
        entry.add_argument('bank')
        entry.add_argument('--out', required=True)
        entry.add_argument('--pandoc', default='pandoc')
        entry.add_argument('--word-qa-mode', choices=('include', 'manual'), default='manual',
                           help='manual 默认将问答分流至 AI导题 JSON 文本并保留手动稿；include 仅供显式 Word 兼容复测')
        if command == 'sample':
            entry.add_argument('--limit', type=int, default=10, choices=range(5, 11), metavar='5..10')
        else:
            entry.add_argument('--knowledge-vault',
                               help='显式指定要发布已编写知识点的本地知识库；省略时仅导出文件')
            entry.add_argument('--knowledge-date', type=_knowledge_date,
                               help='知识发布日期 YYYY-MM-DD；用于明确客户所在时区的日期')
            entry.add_argument('--skip-knowledge-publication', action='store_true',
                               help='明确仅导出文件（虚构测试或格式核对），不发布正式知识库')
    entry = commands.add_parser('validate', help='检查统一数据，内容疑点不等于全部通过')
    entry.add_argument('bank')
    return main


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == 'ingest':
            result = ingest(args)
        elif args.command in ('sample', 'export'):
            result = build(args)
        else:
            issues = validate_bank(read_bank(args.bank))
            result = {'issues': issues, 'contentReady': not issues}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except _KnowledgePublicationFailure as error:
        print(json.dumps(error.result, ensure_ascii=False, indent=2))
        print(str(error), file=sys.stderr)
        return 1
    except (ValueError, OSError, RuntimeError, ImportError, subprocess.SubprocessError) as error:
        print(f'处理失败：{error}', file=sys.stderr)
        return 1
