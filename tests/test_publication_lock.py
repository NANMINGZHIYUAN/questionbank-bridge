"""Cross-process publication isolation with fictional local artifacts."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest

from questionbank.publication import LOCK, MANIFEST, publish_export


_PUBLISH_PROCESS = r"""
import json, sys
from pathlib import Path
from questionbank import publication
stage, out = Path(sys.argv[1]), Path(sys.argv[2])
report = json.loads(sys.argv[3])
if sys.argv[4] == 'hold':
    original = publication._publish_export_locked
    def paused_publish(*args):
        # publish_export has acquired the real OS lock before reaching here.
        print('LOCKED', flush=True)
        if sys.stdin.readline().strip() != 'resume':
            raise RuntimeError('Test publisher was not resumed')
        return original(*args)
    publication._publish_export_locked = paused_publish
result = publication.publish_export(stage, out, report)
print(json.dumps(result, ensure_ascii=False), flush=True)
"""


class PublicationLockTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(__file__).resolve().parents[1]
        base = self.workspace / '.test-tmp'
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='publication-process-', dir=base)
        self.root = Path(self.temp.name)
        self.out = self.root / '最终输出 含空格'
        self.children = []
        seed, report = self.stage('旧成果', {'题库.docx': b'seed-word'})
        publish_export(seed, self.out, report)

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)
        self.temp.cleanup()

    def stage(self, name, files):
        folder = self.root / name
        folder.mkdir()
        for filename, content in files.items():
            (folder / filename).write_bytes(content)
        parts = [str(folder / filename) for filename in files if filename.endswith('.docx')]
        return folder, {'files': {'questionDocxParts': parts}, 'warnings': []}

    def snapshot(self, folder=None):
        folder = folder or self.out
        return {path.relative_to(folder).as_posix(): path.read_bytes()
                for path in folder.rglob('*') if path.is_file()
                and (folder != self.out or path.name != LOCK)}

    def command(self, stage, report, mode='publish'):
        return [sys.executable, '-c', _PUBLISH_PROCESS, str(stage), str(self.out),
                json.dumps(report, ensure_ascii=False), mode]

    def subprocess_options(self):
        environment = os.environ.copy()
        environment['PYTHONIOENCODING'] = 'utf-8'
        environment['PYTHONUTF8'] = '1'
        options = {'cwd': str(self.workspace), 'env': environment,
                   'text': True, 'encoding': 'utf-8'}
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NO_WINDOW
        return options

    def hold_publisher(self, stage, report):
        child = subprocess.Popen(self.command(stage, report, 'hold'), stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 **self.subprocess_options())
        self.children.append(child)
        ready = queue.Queue()
        threading.Thread(target=lambda: ready.put(child.stdout.readline()), daemon=True).start()
        try:
            self.assertEqual(ready.get(timeout=15).strip(), 'LOCKED')
        except queue.Empty:
            self.fail('Actual publisher did not acquire its OS lock within 15 seconds')
        return child

    def run_publisher(self, stage, report):
        return subprocess.run(self.command(stage, report), capture_output=True, timeout=15,
                              **self.subprocess_options())

    def assert_manifest(self, expected_parts):
        manifest = json.loads((self.out / MANIFEST).read_text(encoding='utf-8'))
        paths = {item['path'] for item in manifest['files']}
        self.assertTrue(set(expected_parts).issubset(paths))
        self.assertNotIn(LOCK, paths, 'Runtime lock must never become an owned deliverable')
        for item in manifest['files']:
            self.assertEqual(item['sha256'], hashlib.sha256((self.out / item['path']).read_bytes()).hexdigest())
        report = json.loads((self.out / 'export-report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['files']['questionDocxParts'],
                         [str(self.out / part) for part in expected_parts])

    def test_competing_process_is_rejected_then_can_publish_after_lock_release(self):
        first_stage, first_report = self.stage('发布者 A', {'题库.docx': b'writer-A'})
        second_stage, second_report = self.stage('发布者 B', {
            '题库.docx': b'writer-B', '题库-02.docx': b'writer-B-part-two'})
        # The inner CLI exporter can leave its runtime lock in a stage. The
        # outer publication must exclude it rather than copy its bytes.
        (second_stage / LOCK).write_bytes(b'fictional-stage-lock-do-not-publish')
        child = self.hold_publisher(first_stage, first_report)
        previous, staged = self.snapshot(), self.snapshot(second_stage)
        competing = self.run_publisher(second_stage, second_report)
        self.assertNotEqual(competing.returncode, 0)
        self.assertIn('同一输出目录正在发布', competing.stderr)
        self.assertEqual(self.snapshot(), previous)
        self.assertEqual(self.snapshot(second_stage), staged)
        self.assertFalse((self.out / '题库-02.docx').exists())

        child.stdin.write('resume\n')
        child.stdin.flush()
        _, first_error = child.communicate(timeout=15)
        self.assertEqual(child.returncode, 0, first_error)
        self.assertEqual((self.out / '题库.docx').read_bytes(), b'writer-A')
        self.assert_manifest(['题库.docx'])

        retry = self.run_publisher(second_stage, second_report)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual((self.out / '题库.docx').read_bytes(), b'writer-B')
        self.assertEqual((self.out / '题库-02.docx').read_bytes(), b'writer-B-part-two')
        self.assert_manifest(['题库.docx', '题库-02.docx'])
        self.assertEqual((self.out / LOCK).read_bytes(), b'0')
        self.assertTrue((second_stage / LOCK).is_file())

    def test_process_exit_releases_os_lock_without_deleting_runtime_lock_file(self):
        first_stage, first_report = self.stage('中断发布者', {'题库.docx': b'not-published'})
        next_stage, next_report = self.stage('恢复发布者', {'题库.docx': b'after-process-exit'})
        previous = self.snapshot()
        child = self.hold_publisher(first_stage, first_report)
        child.kill()
        child.communicate(timeout=15)
        self.assertEqual(self.snapshot(), previous)
        self.assertTrue((self.out / LOCK).is_file())
        resumed = self.run_publisher(next_stage, next_report)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual((self.out / '题库.docx').read_bytes(), b'after-process-exit')
        self.assert_manifest(['题库.docx'])


if __name__ == '__main__':
    unittest.main()
