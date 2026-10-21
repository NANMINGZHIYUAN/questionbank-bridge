"""Unresolved semantic doubts stay visible and cannot become answer cards."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

from questionbank.cli import write_json
from questionbank.core import file_sha256, merge_banks, normalize, UNSAFE_CARD_CODES
from questionbank.exporters import export_bundle


class ContentReviewTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / '.test-tmp'
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='content-review-', dir=base)
        self.folder = Path(self.temp.name)
        self.source = self.folder / 'source.json'
        self.question = '电阻的单位是什么？'
        self.issue = {
            'code': 'requires_content_review',
            'message': '核实问题：请确认课程原题中的单位是否指电阻，原答案暂保留。',
        }
        self.raw = {'items': [
            {'type': '单选', 'title': self.question,
             'options': [{'label': 'A', 'text': '欧姆'}, {'label': 'B', 'text': '伏特'}],
             'correctAnswer': ['A'], 'issues': [self.issue]},
            {'type': '单选', 'title': '电压的单位是什么？',
             'options': [{'label': 'A', 'text': '伏特'}, {'label': 'B', 'text': '瓦特'}],
             'correctAnswer': ['A']},
        ]}
        write_json(self.source, self.raw)
        self.source_hash = file_sha256(self.source)
        self.source_record = {
            'path': str(self.source), 'sha256': self.source_hash, 'format': 'goodreview',
        }
        self.bank = normalize(self.raw, '内容核实演示', self.source_record)
        questioned, safe = [item['id'] for item in self.bank['questions']]
        self.bank['topics'] = [{
            'id': 't-units', 'title': '电学单位', 'summary': '电阻与电压使用不同单位。',
            'explanation': '题库中记录了电阻和电压的单位；第一题含人工核实问题。',
            'questionIds': [questioned, safe],
            'cards': [
                {'id': 'c-review', 'prompt': '待核实题所说电阻的单位是什么？',
                 'answer': '欧姆', 'questionIds': [questioned]},
                {'id': 'c-safe', 'prompt': '电压的单位是什么？',
                 'answer': '伏特', 'questionIds': [safe]},
            ],
        }]
        for question in self.bank['questions']:
            question['topicIds'] = ['t-units']

    def tearDown(self):
        self.temp.cleanup()

    def test_content_review_marker_survives_repeat_merge_without_new_marker(self):
        self.assertIn('requires_content_review', UNSAFE_CARD_CODES)
        original = deepcopy(self.bank)
        fresh_raw = deepcopy(self.raw)
        fresh_raw['items'][0].pop('issues')
        incoming = normalize(fresh_raw, self.bank['course'], self.source_record)
        merged = merge_banks(self.bank, incoming)
        repeated = merge_banks(merged, incoming)
        for bank in (merged, repeated):
            question = bank['questions'][0]
            self.assertIn(self.issue, question['issues'])
            self.assertEqual(question['correctAnswer'], ['欧姆'])
            self.assertEqual(len(bank['questions']), 2)
        self.assertEqual(self.bank, original)
        self.assertEqual(file_sha256(self.source), self.source_hash)

    def test_content_review_skips_related_cards_but_preserves_answer_and_question(self):
        try:
            import genanki  # noqa: F401
            import docx  # noqa: F401
        except ImportError as error:
            self.skipTest(f'项目导出依赖未就绪: {error}')
        pandoc = shutil.which('pandoc')
        if not pandoc:
            self.skipTest('Pandoc 未安装')
        original = deepcopy(self.bank)
        report = export_bundle(self.bank, self.folder / 'out', str(pandoc))
        self.assertEqual(report['originalCards'], 1)
        self.assertEqual(report['topicCards'], 1)
        self.assertEqual(report['skippedOriginalCards'], 1)
        self.assertEqual(report['skippedTopicCards'], 1)
        skipped = {item['kind']: item for item in report['skippedCards']}
        self.assertIn('requires_content_review', skipped['original']['reason'])
        self.assertEqual(skipped['topic']['id'], 'c-review')
        saved = json.loads(Path(report['files']['normalizedJson']).read_text(encoding='utf-8'))
        question = saved['questions'][0]
        self.assertEqual(question['correctAnswer'], ['欧姆'])
        self.assertEqual(question['rawCorrectAnswer'], ['A'])
        self.assertIn(self.issue, question['issues'])
        markdown = Path(report['files']['questionMarkdown']).read_text(encoding='utf-8')
        self.assertIn(self.question, markdown)
        self.assertIn('欧姆', markdown)
        self.assertIn(self.issue['message'], markdown)
        with zipfile.ZipFile(report['files']['questionDocx']) as document:
            xml = ET.fromstring(document.read('word/document.xml'))
            text = ''.join(xml.itertext())
        self.assertNotIn(self.question, text)
        self.assertNotIn(self.issue['message'], text)
        pending = report['wordExport']['pending']
        self.assertEqual([item['questionId'] for item in pending], [question['id']])
        self.assertIn('requires_content_review', pending[0]['reasons'])
        self.assertTrue(pending[0]['requiresUserApprovalBeforeSupplement'])
        with zipfile.ZipFile(report['files']['anki']) as package:
            database = self.folder / 'collection.anki2'
            database.write_bytes(package.read('collection.anki2'))
        with closing(sqlite3.connect(database)) as connection:
            fields = connection.execute('SELECT flds FROM notes').fetchall()
        self.assertEqual(len(fields), 2)
        notes = '\n'.join(row[0] for row in fields)
        self.assertNotIn(self.question, notes)
        self.assertNotIn('待核实题所说', notes)
        self.assertIn('电压的单位是什么', notes)
        self.assertEqual(self.bank, original)
        self.assertEqual(file_sha256(self.source), self.source_hash)


if __name__ == '__main__':
    unittest.main()
