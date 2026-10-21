"""CLI publication gates with fake file exports and isolated knowledge vaults."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from questionbank.cli import main, parser, write_json
from questionbank.core import file_sha256, normalize


class KnowledgeCliTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / '.test-tmp'
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='knowledge-cli-', dir=base)
        self.root = Path(self.temp.name)
        self.source = self.root / '原件.json'
        self.source.write_text('{"source":"original"}', encoding='utf-8')
        self.bank_path = self.root / '工作数据' / 'bank.json'
        self.out = self.root / '文件导出'
        self.vault = self.root / '本地知识库'
        self.bank = normalize({'items': [{'title': '2+3等于多少？', 'type': '单选',
            'options': [{'label': 'A', 'text': '5'}, {'label': 'B', 'text': '6'}],
            'correctAnswer': ['A']}]}, '虚构测试课程', {'path': str(self.source.resolve()),
                'format': 'json', 'sha256': file_sha256(self.source)})
        qid = self.bank['questions'][0]['id']
        self.bank['questions'][0]['topicIds'] = ['t-arithmetic']
        self.bank['topics'] = [{'id': 't-arithmetic', 'title': '基础算术', 'summary': '基础运算',
            'explanation': '按已提供的算术规则计算。', 'questionIds': [qid],
            'cards': [{'id': 'c-arithmetic', 'prompt': '怎样计算？', 'answer': '逐项相加。',
                       'questionIds': [qid]}]}]
        write_json(self.bank_path, self.bank)
        self.export_patch = patch('questionbank.exporters.export_bundle', side_effect=self.fake_export)
        self.exporter = self.export_patch.start()

    def tearDown(self):
        self.export_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def fake_export(bank, out, pandoc, word_qa_mode):
        question_file = Path(out) / '题库.md'
        question_file.write_text('# 虚构题库\n', encoding='utf-8')
        return {'course': bank['course'], 'files': {'questionMarkdown': str(question_file)}, 'warnings': []}

    def run_cli(self, *args):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = main([str(value) for value in args])
        text = output.getvalue()
        return code, json.loads(text) if text else None, error.getvalue()

    def export(self, *extra):
        return self.run_cli('export', self.bank_path, '--out', self.out, *extra)

    def assert_manifest_valid(self):
        manifest = json.loads((self.out / '.export-manifest.json').read_text(encoding='utf-8'))
        for entry in manifest['files']:
            path = self.out / entry['path']
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), entry['sha256'])
        report = json.loads((self.out / 'export-report.json').read_text(encoding='utf-8'))
        self.assertNotIn('knowledgePublication', report)
        return report

    def test_sample_never_publishes_knowledge_or_opens_default_vault(self):
        with patch('questionbank.knowledge.publish_knowledge') as publish:
            code, result, error = self.run_cli('sample', self.bank_path, '--out', self.out)
        self.assertEqual(code, 0, error)
        publish.assert_not_called()
        self.assertTrue(result['sample'])
        self.assertNotIn('knowledgePublication', result)
        self.assertFalse(self.vault.exists())
        self.assert_manifest_valid()

    def test_export_without_vault_skips_knowledge_publication(self):
        with patch('questionbank.knowledge.publish_knowledge') as publish:
            code, result, error = self.export()
        self.assertEqual(code, 0, error)
        self.assertEqual(result['knowledgePublication']['status'], 'skipped')
        self.assertEqual(result['knowledgePublication']['reason'], 'no_vault_specified')
        publish.assert_not_called()
        self.assertFalse(self.vault.exists())
        self.assert_manifest_valid()

    def test_explicit_vault_publishes_real_isolated_knowledge_and_preserves_sources(self):
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (self.source, self.bank_path)}
        code, result, error = self.export('--knowledge-vault', self.vault, '--knowledge-date', '2026-10-10')
        self.assertEqual(code, 0, error)
        publication = result['knowledgePublication']
        self.assertEqual(publication['status'], 'published')
        self.assertEqual(Path(publication['map']).parent.parent.parent.parent, self.vault)
        self.assertTrue(Path(publication['report']).is_file())
        self.assertTrue(publication['sourceConservation'][0]['verified'])
        self.assertIn('date: 2026-10-10', Path(publication['map']).read_text(encoding='utf-8'))
        for path, old in before.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), old)
        self.assert_manifest_valid()

    def test_explicit_skip_does_not_call_publisher_or_create_vault(self):
        with patch('questionbank.knowledge.publish_knowledge') as publish:
            code, result, error = self.export('--knowledge-vault', self.vault, '--skip-knowledge-publication')
        self.assertEqual(code, 0, error)
        publish.assert_not_called()
        self.assertEqual(result['knowledgePublication']['status'], 'skipped')
        self.assertFalse(result['knowledgePublication']['published'])
        self.assertFalse(self.vault.exists())
        self.assert_manifest_valid()

    def test_empty_topics_does_not_call_publisher_or_touch_any_vault(self):
        empty = deepcopy(self.bank)
        empty['topics'] = []
        empty['questions'][0]['topicIds'] = []
        write_json(self.bank_path, empty)
        with patch('questionbank.knowledge.publish_knowledge') as publish:
            code, result, error = self.export('--knowledge-vault', self.vault)
        self.assertEqual(code, 0, error)
        publish.assert_not_called()
        self.assertEqual(result['knowledgePublication']['status'], 'no_topics')
        self.assertFalse(self.vault.exists())
        self.assertFalse((self.root / '个人').exists())
        self.assert_manifest_valid()

    def test_knowledge_failure_is_nonzero_preserves_complete_exports_and_check_report(self):
        with patch('questionbank.knowledge.publish_knowledge', side_effect=ValueError('知识页已有手写冲突')):
            code, result, error = self.export('--knowledge-vault', self.vault)
        self.assertEqual(code, 1)
        self.assertIn('文件导出已完成', error)
        self.assertIn('知识库发布未完成', error)
        phase = result['knowledgePublication']
        self.assertEqual(phase['status'], 'failed')
        self.assertTrue(phase['fileExportCompleted'])
        self.assertFalse(phase['published'])
        inspection = Path(phase['inspectionReport'])
        self.assertTrue(inspection.is_file())
        self.assertFalse(inspection.is_relative_to(self.out))
        saved = json.loads(inspection.read_text(encoding='utf-8'))
        self.assertEqual(saved['knowledgePublication']['error'], '知识页已有手写冲突')
        self.assertTrue(Path(result['files']['questionMarkdown']).is_file())
        self.assert_manifest_valid()

    def test_source_mismatch_prevents_both_phases(self):
        self.source.write_text('modified original', encoding='utf-8')
        with patch('questionbank.knowledge.publish_knowledge') as publish:
            code, result, error = self.export('--knowledge-vault', self.vault)
        self.assertEqual(code, 1)
        self.assertIsNone(result)
        self.assertIn('原件内容与摄入时哈希不同', error)
        self.exporter.assert_not_called()
        publish.assert_not_called()
        self.assertFalse(self.out.exists())
        self.assertFalse(self.vault.exists())

    def test_source_change_between_phases_keeps_exports_and_stops_knowledge(self):
        from questionbank.publication import publish_export
        def export_then_change(*args):
            result = publish_export(*args)
            self.source.write_text('external modification after export', encoding='utf-8')
            return result
        with patch('questionbank.publication.publish_export', side_effect=export_then_change), \
                patch('questionbank.knowledge.publish_knowledge') as publish:
            code, result, error = self.export('--knowledge-vault', self.vault)
        self.assertEqual(code, 1)
        publish.assert_not_called()
        self.assertIn('文件导出后来源变化', error)
        self.assertEqual(result['knowledgePublication']['status'], 'failed')
        self.assertTrue(result['knowledgePublication']['fileExportCompleted'])
        self.assertFalse(self.vault.exists())
        self.assert_manifest_valid()

    def test_explicit_date_reaches_publisher_and_invalid_date_is_rejected(self):
        with patch('questionbank.knowledge.publish_knowledge', return_value={'status': 'published'}) as publish:
            code, _, error = self.export('--knowledge-vault', self.vault, '--knowledge-date', '2026-10-10')
        self.assertEqual(code, 0, error)
        self.assertEqual(publish.call_args.kwargs, {'date_string': '2026-10-10'})
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser().parse_args(['export', str(self.bank_path), '--out', str(self.out),
                                 '--knowledge-date', '2026-02-30'])

    def test_failure_report_write_error_still_reports_partial_completion(self):
        with patch('questionbank.knowledge.publish_knowledge', side_effect=OSError('vault unavailable')), \
                patch('questionbank.cli.tempfile.mkstemp', side_effect=OSError('report unavailable')):
            code, result, error = self.export('--knowledge-vault', self.vault)
        self.assertEqual(code, 1)
        self.assertIn('文件导出已完成', error)
        self.assertEqual(result['knowledgePublication']['inspectionReportError'], 'report unavailable')
        self.assertTrue(result['knowledgePublication']['fileExportCompleted'])
        self.assert_manifest_valid()


if __name__ == '__main__':
    unittest.main()
