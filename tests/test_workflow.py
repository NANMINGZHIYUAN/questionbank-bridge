"""CLI integration: source preservation, honest partial intake and portable output."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest

from questionbank.cli import main, read_bank, write_json
from questionbank.core import file_sha256


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / '.test-tmp'
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.folder = Path(self.temp.name)
        self.input = self.folder / 'input'
        self.input.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_real_json_ingest_and_repeat_preserve_source_and_ids(self):
        source = self.input / 'bank.json'
        write_json(source, {'items':[{'id':1,'type':'单选','title':'2+3=?',
                                    'options':[{'label':'A','text':'5'},{'label':'B','text':'6'}],
                                    'correctAnswer':['A'],'selectedAnswer':['B']}]})
        before = file_sha256(source)
        out = self.folder / 'ingested'
        self.assertEqual(self.run_cli('ingest', str(source), '--course','演示','--out',str(out))[0],0)
        bank = read_bank(out / 'bank.json')
        self.assertEqual(bank['questions'][0]['correctAnswer'], ['5'])
        self.assertNotIn('selectedAnswer', bank['questions'][0])
        self.assertNotIn('rawSelectedAnswer', bank['questions'][0])
        self.assertEqual(before, file_sha256(source))
        repeated = self.folder / 'repeated'
        self.assertEqual(self.run_cli('ingest', str(source), '--course','演示','--out',str(repeated),
                                     '--existing',str(out / 'bank.json'))[0],0)
        new = read_bank(repeated / 'bank.json')
        self.assertEqual(len(new['questions']),1)
        self.assertEqual(new['questions'][0]['id'],bank['questions'][0]['id'])

    def test_invalid_json_does_not_replace_existing_bank(self):
        source = self.input / 'bad.json'
        source.write_text('{broken',encoding='utf-8')
        out = self.folder / 'out'
        write_json(out / 'bank.json', {'unchanged':True})
        before = file_sha256(out / 'bank.json')
        code, _, error = self.run_cli('ingest',str(source),'--course','演示','--out',str(out))
        self.assertEqual(code,1)
        self.assertIn('处理失败',error)
        self.assertEqual(before,file_sha256(out / 'bank.json'))
        self.assertEqual(source.read_text(encoding='utf-8'),'{broken')

    def test_scalar_json_error_and_input_output_guard(self):
        source = self.input / 'scalar.json'
        write_json(source,42)
        self.assertEqual(self.run_cli('ingest',str(source),'--course','演示','--out',str(self.folder / 'scalar-out'))[0],1)
        before = file_sha256(source)
        self.assertEqual(self.run_cli('ingest',str(source),'--course','演示','--out',str(self.input))[0],1)
        self.assertEqual(before,file_sha256(source))

    def test_visual_transcription_keeps_original_identity_and_text_hash(self):
        from PIL import Image
        source = self.input / 'scan.png'
        Image.new('RGB',(8,8),'white').save(source)
        transcription = self.input / 'checked.md'
        transcription.write_text('1. 【填空】1 加 0.5 等于什么？\n答案：1.5\n',encoding='utf-8')
        original_hash, text_hash = file_sha256(source), file_sha256(transcription)
        partial = self.folder / 'partial'
        self.assertEqual(self.run_cli('ingest',str(source),'--course','演示','--out',str(partial))[0],0)
        report = json.loads((partial / 'intake-report.json').read_text(encoding='utf-8'))
        self.assertTrue(report['requiresReview'])
        self.assertEqual(report['questions'],0)
        complete = self.folder / 'complete'
        self.assertEqual(self.run_cli('ingest',str(source),'--course','演示','--out',str(complete),
                                     '--transcript',str(source),str(transcription))[0],0)
        bank = read_bank(complete / 'bank.json')
        self.assertEqual(bank['questions'][0]['correctAnswer'],['1.5'])
        self.assertIn(original_hash,[s['sha256'] for s in bank['sources']])
        self.assertIn(text_hash, [s['sha256'] for s in bank['sources']])
        transcription_source = next(s for s in bank['sources'] if s['sha256'] == text_hash)
        self.assertIn(transcription_source['id'], [r['sourceId'] for r in bank['questions'][0]['sources']])
        self.assertEqual(original_hash,file_sha256(source))
        self.assertEqual(text_hash,file_sha256(transcription))

    def test_incompatible_course_does_not_merge(self):
        source = self.input / 'source.json'
        write_json(source, {'items':[{'type':'填空','title':'填空？','correctAnswer':['一']}]})
        existing = self.folder / 'old.json'
        write_json(existing, {'schemaVersion':1,'course':'甲','questions':[],'sources':[],'topics':[]})
        code, _, error = self.run_cli('ingest',str(source),'--course','乙','--out',str(self.folder / 'out'),
                                      '--existing',str(existing))
        self.assertEqual(code,1)
        self.assertIn('课程',error)


if __name__ == '__main__':
    unittest.main()
