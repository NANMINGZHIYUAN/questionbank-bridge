"""Publish staged local exports with a manifest and rollback on failure."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import os
import stat
from contextlib import contextmanager


MANIFEST = '.export-manifest.json'
LOCK = '.export.lock'


@contextmanager
def _publication_lock(out):
    """OS lock released on process exit; same-directory publishers cannot overlap."""
    out.mkdir(parents=True, exist_ok=True)
    lock_path = out / LOCK
    if lock_path.is_symlink() or not lock_path.resolve().is_relative_to(out.resolve()):
        raise ValueError('发布锁路径不能是链接或位于输出目录之外。')
    if lock_path.exists():
        existing = lock_path.stat()
        if not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
            raise ValueError('发布锁必须是独立的普通文件。')
    with lock_path.open('a+b') as stream:
        opened = os.fstat(stream.fileno())
        if opened.st_nlink != 1 or lock_path.is_symlink() or lock_path.stat().st_ino != opened.st_ino:
            raise ValueError('发布锁在打开期间发生变化，未写入。')
        if stream.seek(0, 2) == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError('同一输出目录正在发布，请等当前导出完成后重试。') from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rebase_paths(value, old, new):
    """Rebase generated absolute paths, without touching source paths or text."""
    if isinstance(value, dict):
        return {key: rebase_paths(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [rebase_paths(item, old, new) for item in value]
    if isinstance(value, str):
        # Windows paths can be emitted by Python or recorded with forward slashes.
        for separator in ('\\', '/'):
            prefix = str(old).rstrip('\\/') + separator
            if value.startswith(prefix):
                return str(new / Path(value[len(prefix):]))
    return value


def _inside(out, relative):
    path = Path(relative)
    if path.is_absolute() or path.drive or '..' in path.parts or ':' in str(path) or str(path) == LOCK:
        raise ValueError('输出清单包含非法相对路径。')
    target = out / path
    if not target.resolve().is_relative_to(out.resolve()) or target.is_symlink():
        raise ValueError('输出清单路径离开输出目录或指向链接。')
    return target


def _previous(out):
    manifest = out / MANIFEST
    if manifest.is_file():
        value = json.loads(manifest.read_text(encoding='utf-8'))
        if value.get('schemaVersion') != 1 or not isinstance(value.get('files'), list):
            raise ValueError('旧导出清单格式无效，未更新现有成果。')
        result = {}
        for entry in value['files']:
            _inside(out, entry['path'])
            result[entry['path']] = entry.get('sha256')
        result[MANIFEST] = None
        return result
    # Upgrade the existing workflow using its previous explicit artifact list.
    legacy = out / 'export-report.json'
    if not legacy.is_file():
        return {}
    value = json.loads(legacy.read_text(encoding='utf-8'))
    result = {'export-report.json': None}
    def collect(node):
        if isinstance(node, dict):
            for item in node.values():
                collect(item)
        elif isinstance(node, list):
            for item in node:
                collect(item)
        elif isinstance(node, str):
            path = Path(node)
            if path.is_absolute() and path.resolve().is_relative_to(out.resolve()):
                relative = path.resolve().relative_to(out.resolve()).as_posix()
                _inside(out, relative)
                result[relative] = None
    collect(value.get('files', {}))
    normalized_value = value.get('files', {}).get('normalizedJson', '')
    normalized = out / '统一题库.json'
    if isinstance(normalized_value, str) and normalized_value:
        registered = Path(normalized_value)
        if registered.is_absolute() and registered.resolve().is_relative_to(out.resolve()):
            normalized = _inside(out, registered.resolve().relative_to(out.resolve()).as_posix())
    if normalized.is_file():
        bank = json.loads(normalized.read_text(encoding='utf-8'))
        for question in bank.get('questions', []):
            for image in question.get('images', []):
                path = image.get('path', '')
                if path.startswith('assets/'):
                    _inside(out, path)
                    result[path] = None
    return result


def _publish_export_locked(stage, out, report):
    """Update only owned artifacts; an interrupted replacement rolls back."""
    stage, out = Path(stage).resolve(), Path(out).resolve()
    if stage == out or stage.is_relative_to(out) or out.is_relative_to(stage):
        raise ValueError('导出暂存目录与最终目录必须相互独立。')
    previous = _previous(out) if out.exists() else {}
    final_report = rebase_paths(report, stage, out)
    staged_report = stage / 'export-report.json'
    staged_report.write_text(json.dumps(final_report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    candidates = {path.relative_to(stage).as_posix(): path for path in stage.rglob('*') if path.is_file() and path.name not in {MANIFEST, LOCK}}
    for relative in candidates:
        target = _inside(out, relative)
        if target.exists() and relative not in previous and _hash(target) != _hash(candidates[relative]):
            raise ValueError('输出位置已有未登记的不同内容文件，未覆盖：' + relative)
        if target.is_file() and previous.get(relative) and _hash(target) != previous[relative]:
            raise ValueError('已有生成物被外部修改，未覆盖：' + relative)
    stale = []
    for relative, digest in previous.items():
        target = _inside(out, relative)
        if relative not in candidates and relative != MANIFEST and target.is_file():
            if digest and _hash(target) != digest:
                final_report.setdefault('warnings', []).append('旧生成物已被外部修改，保留：' + relative)
            else:
                stale.append(relative)
    staged_report.write_text(json.dumps(final_report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    manifest = {'schemaVersion': 1, 'files': [{'path': relative, 'sha256': _hash(path)} for relative, path in sorted(candidates.items())]}
    staged_manifest = stage / MANIFEST
    staged_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    candidates[MANIFEST] = staged_manifest
    out.parent.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    changed = []
    with tempfile.TemporaryDirectory(prefix='export-rollback-', dir=out.parent) as folder:
        backup = Path(folder).resolve()
        if not backup.is_relative_to(out.parent.resolve()):
            raise ValueError('回滚暂存路径不在预期父目录内。')
        targets = list(candidates) + stale
        for relative in targets:
            target = _inside(out, relative)
            if target.is_file():
                saved = backup / relative
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(target, saved)
        try:
            for relative, source in candidates.items():
                target = _inside(out, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                # The manifest is committed last. Existing files were already
                # copied to rollback storage before any replacement occurred.
                source.replace(target)
                changed.append(relative)
            for relative in stale:
                _inside(out, relative).unlink()
                changed.append(relative)
        except BaseException:
            for relative in reversed(changed):
                target = _inside(out, relative)
                saved = backup / relative
                if saved.is_file():
                    shutil.copyfile(saved, target)
                elif target.is_file():
                    target.unlink()
            raise
    return final_report


def publish_export(stage, out, report):
    stage, out = Path(stage).resolve(), Path(out).resolve()
    if stage == out or stage.is_relative_to(out) or out.is_relative_to(stage):
        raise ValueError('导出暂存目录与最终目录必须相互独立。')
    with _publication_lock(out):
        return _publish_export_locked(stage, out, report)
