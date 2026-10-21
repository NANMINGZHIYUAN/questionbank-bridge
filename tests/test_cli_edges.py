"""Regression checks for intake review flags and source-integrity export gates."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from questionbank.cli import main, read_bank, write_json
from questionbank.core import file_sha256


class CliEdgeTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / '.test-tmp'
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.folder = Path(self.temp.name)
        self.inputs = self.folder / 'inputs'
        self.inputs.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def protected_output(self, name):
        out = self.folder / name
        write_json(out / 'bank.json', {'existing': '保留已有题库'})
        write_json(out / 'intake-report.json', {'existing': '保留已有报告'})
        return out, {path.name: path.read_bytes() for path in out.iterdir()}

    def assert_output_preserved(self, out, previous):
        for name, content in previous.items():
            self.assertEqual((out / name).read_bytes(), content)

    def ingest_source(self):
        source = self.inputs / 'source.json'
        write_json(source, {'items': [{
            'type': '单选', 'title': '1 加 1 等于多少？',
            'options': [{'label': 'A', 'text': '2'}, {'label': 'B', 'text': '3'}],
            'correctAnswer': ['A'],
        }]})
        out = self.folder / 'ingested'
        code, _, error = self.run_cli(
            'ingest', str(source), '--course', '边界演示', '--out', str(out))
        self.assertEqual(code, 0, error)
        bank = out / 'bank.json'
        self.assertEqual(read_bank(bank)['sources'][0]['sha256'], file_sha256(source))
        return source, bank

    def test_missing_or_ambiguous_question_array_preserves_existing_output(self):
        for name, payload in (
            ('missing', {'unexpected': []}),
            ('ambiguous', {'items': [], 'questions': []}),
        ):
            with self.subTest(payload=name):
                source = self.inputs / (name + '.json')
                write_json(source, payload)
                source_hash = file_sha256(source)
                out, previous = self.protected_output(name + '-out')
                code, _, error = self.run_cli(
                    'ingest', str(source), '--course', '边界演示', '--out', str(out))
                self.assertEqual(code, 1)
                self.assertIn('必须且只能提供一个 items 或 questions 数组', error)
                self.assert_output_preserved(out, previous)
                self.assertEqual(file_sha256(source), source_hash)

    def test_missing_correct_answer_requires_review_without_using_personal_answer(self):
        source = self.inputs / 'no-answer.json'
        write_json(source, {'items': [{
            'type': '单选', 'title': '1 加 1 等于多少？',
            'options': [{'label': 'A', 'text': '2'}, {'label': 'B', 'text': '3'}],
            'selectedAnswer': ['A'],
        }]})
        source_hash = file_sha256(source)
        out = self.folder / 'no-answer-out'
        code, stdout, error = self.run_cli(
            'ingest', str(source), '--course', '边界演示', '--out', str(out))
        self.assertEqual(code, 0, error)
        self.assertTrue(json.loads(stdout)['requiresReview'])
        report = json.loads((out / 'intake-report.json').read_text(encoding='utf-8'))
        self.assertTrue(report['requiresReview'])
        self.assertIn('missing_correct_answer', {issue['code'] for issue in report['issues']})
        question = read_bank(out / 'bank.json')['questions'][0]
        self.assertEqual(question['correctAnswer'], [])
        self.assertNotIn('selectedAnswer', question)
        self.assertNotIn('rawSelectedAnswer', question)
        self.assertEqual(file_sha256(source), source_hash)

    def test_empty_question_array_requires_review_and_retains_source(self):
        source = self.inputs / 'empty.json'
        write_json(source, {'items': []})
        source_hash = file_sha256(source)
        out = self.folder / 'empty-out'
        code, stdout, error = self.run_cli(
            'ingest', str(source), '--course', '边界演示', '--out', str(out))
        self.assertEqual(code, 0, error)
        self.assertTrue(json.loads(stdout)['requiresReview'])
        report = json.loads((out / 'intake-report.json').read_text(encoding='utf-8'))
        self.assertTrue(report['requiresReview'])
        self.assertEqual(report['questions'], 0)
        bank = read_bank(out / 'bank.json')
        self.assertEqual(bank['questions'], [])
        self.assertEqual(len(bank['sources']), 1)
        self.assertEqual(bank['sources'][0]['sha256'], source_hash)
        self.assertEqual(file_sha256(source), source_hash)

    def test_missing_registered_source_prevents_export_and_sample(self):
        source, bank = self.ingest_source()
        bank_hash = file_sha256(bank)
        source.unlink()
        for command in ('export', 'sample'):
            with self.subTest(command=command):
                out, previous = self.protected_output(command + '-missing-out')
                with patch('questionbank.exporters.export_bundle') as exporter:
                    code, _, error = self.run_cli(command, str(bank), '--out', str(out))
                self.assertEqual(code, 1)
                self.assertIn('登记的原件已不存在', error)
                exporter.assert_not_called()
                self.assert_output_preserved(out, previous)
                self.assertFalse((out / 'export-report.json').exists())
        self.assertEqual(file_sha256(bank), bank_hash)

    def test_changed_registered_source_prevents_export_and_sample(self):
        source, bank = self.ingest_source()
        bank_hash = file_sha256(bank)
        source.write_text('{"items": []}\n', encoding='utf-8')
        changed_hash = file_sha256(source)
        for command in ('export', 'sample'):
            with self.subTest(command=command):
                out, previous = self.protected_output(command + '-changed-out')
                with patch('questionbank.exporters.export_bundle') as exporter:
                    code, _, error = self.run_cli(command, str(bank), '--out', str(out))
                self.assertEqual(code, 1)
                self.assertIn('原件内容与摄入时哈希不同', error)
                exporter.assert_not_called()
                self.assert_output_preserved(out, previous)
                self.assertFalse((out / 'export-report.json').exists())
        self.assertEqual(file_sha256(source), changed_hash)
        self.assertEqual(file_sha256(bank), bank_hash)

    def test_duplicate_transcript_mapping_is_rejected_before_read_or_output(self):
        source = self.inputs / 'source.json'
        write_json(source, {'items': []})
        first, second = self.inputs / 'first.md', self.inputs / 'second.md'
        first.write_text('1. 【填空】1 加 1？\n答案：2\n', encoding='utf-8')
        second.write_text('1. 【填空】1 加 1？\n答案：3\n', encoding='utf-8')
        before = {path: file_sha256(path) for path in (source, first, second)}
        out, previous = self.protected_output('transcript-out')
        with patch('questionbank.cli.extract_source') as extractor:
            code, _, error = self.run_cli(
                'ingest', str(source), '--course', '边界演示', '--out', str(out),
                '--transcript', str(source), str(first),
                '--transcript', str(source.parent / '.' / source.name), str(second))
        self.assertEqual(code, 1)
        self.assertIn('同一原件只能绑定一份文字稿', error)
        extractor.assert_not_called()
        self.assert_output_preserved(out, previous)
        self.assertEqual({path: file_sha256(path) for path in before}, before)


if __name__ == '__main__':
    unittest.main()
