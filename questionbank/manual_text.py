"""Convert reviewed Markdown into copyable text without guessing its content."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

from .core import strip_personal_answers
from .exporters import _LEARNING_URL, _markdown, _web_reference


_PRIVATE_PATH = re.compile(
    r'(?i)(?:["\x27](?:[a-z]:[\\/]|\\\\)[^"\x27\r\n]+["\x27]|'
    r'file://[^\s<>\[\]()]+|(?<![a-z0-9])(?:[a-z]:[\\/]|\\\\)[^\s<>\[\]()]+)'
)
_MATH_NS = '{http://www.w3.org/1998/Math/MathML}'
_SUB = dict(zip('0123456789+-=()aehijklmnoprstuvx', '₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ'))
_SUPER = dict(zip('0123456789+-=()ni', '⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ'))
_SUB['−'], _SUPER['−'] = '₋', '⁻'


class _Unsupported(ValueError):
    pass


def _warn(warnings, message):
    if message not in warnings:
        warnings.append(message)


def _privacy_fallback(text, warnings):
    """Expose HTML labels and filter them even when conversion never succeeds."""
    pieces = re.split(r'(```.*?```|~~~.*?~~~|`[^`\n]+`)', text, flags=re.S)
    had_html = False
    removed_personal = False

    def cell_text(match):
        nonlocal removed_personal
        visible = _markdown(match[1])
        cleaned = strip_personal_answers({'title': visible})['title']
        removed_personal |= cleaned != visible
        # Filter cells separately: one learner cell must not consume valid,
        # unlabeled steps or values in the next cell during a format fallback.
        return '\n' + cleaned.strip() + '\n'

    for index in range(0, len(pieces), 2):
        piece = pieces[index]
        had_html |= bool(re.search(r'</?[A-Za-z][^>]*>', piece))
        pieces[index] = re.sub(r'<(?:td|th)\b[^>]*>(.*?)</(?:td|th)\s*>', cell_text,
                               piece, flags=re.I | re.S)
    visible = _markdown(''.join(pieces))
    cleaned = strip_personal_answers({'title': visible})['title']
    if removed_personal or cleaned != visible:
        _warn(warnings, '已丢弃明确标注的个人作答，保留原题及正确答案内容。')
    if had_html:
        _warn(warnings, '回退内容已清理 HTML 并过滤个人作答；表格格式需对照本地原稿核对。')
    return cleaned


def _damaged_delimiters(text):
    # Code examples are literal data. An unmatched explicit TeX delimiter must
    # not be silently consumed as a Markdown backslash escape.
    visible = re.sub(r'```.*?```|~~~.*?~~~|`[^`\n]+`', '', text, flags=re.S)
    stack = []
    for match in re.finditer(r'\\([()\[\]])', visible):
        token = match[1]
        if token in {'(', '['}:
            stack.append(token)
        elif not stack or stack.pop() != {')': '(', ']': '['}[token]:
            return True
    if stack:
        return True
    # A lone currency sign is ordinary prose. A clearly mathematical fragment
    # with a missing dollar delimiter retains its original syntax and warns.
    dollars = list(re.finditer(r'(?<!\\)\$+', visible))
    if len(dollars) % 2 and re.search(r'(?<!\\)\$[^$\n]*(?:\\[A-Za-z]+|\^|_[A-Za-z0-9{])', visible):
        return True
    return False


def _pandoc(pandoc, source, source_format, target_format, *extra):
    result = subprocess.run([str(pandoc), '--from', source_format, '--to', target_format, *extra],
                            input=source, capture_output=True, text=True, encoding='utf-8', timeout=90)
    if result.returncode:
        raise RuntimeError('Pandoc conversion failed')
    return result.stdout, result.stderr.strip()


def _tag(node):
    return node.tag.rsplit('}', 1)[-1]


def _atom(node):
    kind = _tag(node)
    if kind in {'mi', 'mn', 'msub', 'msup', 'msubsup', 'msqrt', 'mroot', 'mfenced'}:
        return True
    if kind in {'mrow', 'mstyle'} and len(node) == 1:
        return _atom(node[0])
    if kind == 'mrow' and len(node) >= 2:
        opening = node[0].text or ''
        closing = {'(': ')', '[': ']'}.get(opening)
        if closing is None or (node[-1].text or '') != closing:
            return False
        depth = 0
        for index, child in enumerate(node):
            token = child.text or ''
            depth += (token == opening) - (token == closing)
            if depth == 0 and index < len(node) - 1:
                return False
        return depth == 0
    return False


def _operand(node):
    text = _math(node)
    return text if _atom(node) else '(' + text + ')'


def _script(text, upper=False):
    mapping = _SUPER if upper else _SUB
    compact = text.strip()
    if compact and all(char in mapping for char in compact):
        return ''.join(mapping[char] for char in compact)
    # Plain-text mathematical notation, with grouping retained. These are not
    # TeX commands and do not change a compound exponent or subscript's scope.
    return ('^' if upper else '_') + '(' + text + ')'


def _operand_node(node):
    """Recognize a neighboring operand without confusing Pandoc's mi slash."""
    kind = _tag(node)
    if kind in {'mi', 'mn'}:
        return bool(node.text) and re.fullmatch(r'[\wΩ]+', node.text) is not None
    return kind in {'mfrac', 'msub', 'msup', 'msubsup', 'msqrt', 'mroot', 'mfenced', 'mrow', 'mstyle'}


def _neighbors(node, index):
    for step in (-1, 1):
        neighbor = index + step
        while 0 <= neighbor < len(node):
            child = node[neighbor]
            if _tag(child) != 'mspace' and not (child.text is not None and not child.text.strip()):
                yield child
                break
            neighbor += step


def _math(node):
    kind = _tag(node)
    if node.get('mathvariant') not in {None, 'normal', 'italic'}:
        raise _Unsupported('mathvariant=' + node.get('mathvariant'))
    if kind in {'mi', 'mn', 'mo', 'mtext'}:
        if len(node):
            raise _Unsupported('nested math token')
        text = node.text or ''
        return {'\u2061': ' ', '\u2062': '·', '\u2063': ', ', '\u2064': '+'}.get(text, text)
    if kind == 'mspace':
        return ' '
    if kind == 'semantics':
        expressions = [child for child in node if _tag(child) not in {'annotation', 'annotation-xml'}]
        if len(expressions) != 1:
            raise _Unsupported('semantics structure')
        return _math(expressions[0])
    if kind in {'math', 'mrow', 'mstyle'}:
        parts = []
        for index, child in enumerate(node):
            text = _math(child)
            # Fractions around an explicit division must remain separate
            # operands, e.g. (a/b)/(c/d), rather than a/b/c/d.
            neighbors = _neighbors(node, index)
            if _tag(child) == 'mfrac' and any((other.text or '') in {'/', '÷', '·', '⋅', '×', '*'} or _operand_node(other)
                                             for other in neighbors):
                text = '(' + text + ')'
            if parts and parts[-1] and text and re.search(r'[\wΩ²³⁰-⁹₀-₉)]$', parts[-1]) and re.match(r'[\wΩ(]', text):
                previous = node[index - 1]
                # A fraction followed by another operand is multiplication,
                # not an extension of its denominator: (a/b)·c.
                fraction_product = ((_tag(previous) == 'mfrac' or _tag(child) == 'mfrac')
                                    and _operand_node(previous) and _operand_node(child))
                parts.append('·' if fraction_product else ' ')
            parts.append(text)
        return ''.join(parts)
    if kind == 'mfrac' and len(node) == 2:
        return _operand(node[0]) + '/' + _operand(node[1])
    if kind == 'msub' and len(node) == 2:
        return _operand(node[0]) + _script(_math(node[1]))
    if kind == 'msup' and len(node) == 2:
        return _operand(node[0]) + _script(_math(node[1]), upper=True)
    if kind == 'msubsup' and len(node) == 3:
        return _operand(node[0]) + _script(_math(node[1])) + _script(_math(node[2]), upper=True)
    if kind == 'msqrt':
        content = ''.join(_math(child) for child in node)
        return 'sqrt(' + content + ')'
    if kind == 'mroot' and len(node) == 2:
        return 'root(' + _math(node[1]) + ', ' + _math(node[0]) + ')'
    if kind == 'mfenced':
        separators = node.get('separators', ',')
        if len(separators) != 1:
            raise _Unsupported('mfenced separators')
        return node.get('open', '(') + (separators + ' ').join(_math(child) for child in node) + node.get('close', ')')
    raise _Unsupported('MathML ' + kind)


def _find_math(value):
    if isinstance(value, dict):
        if value.get('t') == 'Math':
            yield value
        else:
            for item in value.values():
                yield from _find_math(item)
    elif isinstance(value, list):
        for item in value:
            yield from _find_math(item)


def _math_texts(document, pandoc, warnings):
    nodes = list(_find_math(document))
    if not nodes:
        return {}
    selected = {'pandoc-api-version': document['pandoc-api-version'], 'meta': {},
                'blocks': [{'t': 'Para', 'c': [node]} for node in nodes]}
    results = {}
    try:
        html, stderr = _pandoc(pandoc, json.dumps(selected, ensure_ascii=False), 'json', 'html', '--mathml')
        root = ET.fromstring('<root>' + html + '</root>')
        if stderr:
            _warn(warnings, '部分公式转换需要核对；不能转换的公式保留原 TeX。')
        for expression in root.iter(_MATH_NS + 'math'):
            annotation = expression.find('.//' + _MATH_NS + 'annotation')
            if annotation is None or annotation.get('encoding') != 'application/x-tex':
                continue
            tex = annotation.text or ''
            try:
                results[tex] = _math(expression).strip()
            except _Unsupported as error:
                _warn(warnings, '公式结构暂不能可靠线性化，保留原 TeX：' + str(error))
    except (OSError, RuntimeError, subprocess.SubprocessError, ET.ParseError, KeyError):
        _warn(warnings, '公式转换未完成；原 TeX 完整保留，需核对纯文本表达。')
    return results


class _Renderer:
    def __init__(self, math_texts, warnings, pandoc=None):
        self.math_texts = math_texts
        self.warnings = warnings
        self.pandoc = pandoc

    def inline(self, nodes):
        values = []
        for node in nodes:
            kind, content = node.get('t'), node.get('c')
            if kind == 'Str':
                values.append(content)
            elif kind == 'Space':
                values.append(' ')
            elif kind in {'SoftBreak', 'LineBreak'}:
                values.append('\n')
            elif kind in {'Emph', 'Strong', 'SmallCaps', 'Underline'}:
                values.append(self.inline(content))
            elif kind == 'Strikeout':
                values.append('（已划去：' + self.inline(content) + '）')
            elif kind in {'Superscript', 'Subscript'}:
                values.append(_script(self.inline(content), kind == 'Superscript'))
            elif kind == 'Quoted':
                quote = '“' if content[0].get('t') == 'DoubleQuote' else '‘'
                values.append(quote + self.inline(content[1]) + ('”' if quote == '“' else '’'))
            elif kind == 'Code':
                values.append(content[1])
            elif kind == 'Math':
                tex = content[1]
                if tex in self.math_texts:
                    values.append(self.math_texts[tex])
                else:
                    _warn(self.warnings, '复杂或未知公式保留原 TeX，尚未完成纯文本公式核验。')
                    delimiter = '$$' if content[0].get('t') == 'DisplayMath' else '$'
                    values.append(delimiter + tex + delimiter)
            elif kind == 'Link':
                label = self.inline(content[1])
                destination = _web_reference(content[2][0])
                values.append(label + ('（' + destination + '）' if destination and destination != label else ''))
            elif kind == 'Image':
                _warn(self.warnings, '图片作为附件保留，纯文本不自动嵌入图片。')
                values.append('（图片附件：' + (self.inline(content[1]) or '请核对原图') + '）')
            elif kind == 'Span':
                values.append(self.inline(content[1]))
            elif kind == 'Note':
                values.append('（注：' + self.blocks(content) + '）')
            elif kind == 'Cite':
                values.append(self.inline(content[1]))
            elif kind == 'RawInline':
                _warn(self.warnings, '未支持的行内格式保留原内容：' + content[0])
                values.append(content[1])
            else:
                raise _Unsupported('inline ' + str(kind))
        return ''.join(values)

    def blocks(self, nodes):
        values = []
        for node in nodes:
            kind, content = node.get('t'), node.get('c')
            if kind in {'Para', 'Plain'}:
                values.append(self.inline(content))
            elif kind == 'Header':
                values.append(self.inline(content[2]))
            elif kind == 'LineBlock':
                values.append('\n'.join(self.inline(line) for line in content))
            elif kind == 'CodeBlock':
                values.append(content[1])
            elif kind in {'BlockQuote', 'Div'}:
                values.append(self.blocks(content if kind == 'BlockQuote' else content[1]))
            elif kind in {'OrderedList', 'BulletList'}:
                items = content[1] if kind == 'OrderedList' else content
                start = content[0][0] if kind == 'OrderedList' else 1
                lines = []
                for index, item in enumerate(items, start):
                    marker = str(index) + '. ' if kind == 'OrderedList' else '• '
                    body = self.blocks(item).split('\n')
                    lines.append(marker + body[0])
                    lines.extend('   ' + line if line else '' for line in body[1:])
                values.append('\n'.join(lines))
            elif kind == 'DefinitionList':
                values.append('\n'.join(self.inline(term) + '：' + '\n'.join(self.blocks(item) for item in definitions)
                                        for term, definitions in content))
            elif kind == 'Table':
                values.append(self.table(content))
            elif kind == 'Figure':
                values.append(self.caption(content[1]) + '\n' + self.blocks(content[2]))
            elif kind == 'RawBlock':
                if content[0] == 'html' and re.match(r'\s*<table\b', content[1], re.I):
                    try:
                        result, stderr = _pandoc(self.pandoc, content[1], 'html', 'json')
                        document = json.loads(result)
                        if stderr or not re.search(r'</table\s*>', content[1], re.I):
                            _warn(self.warnings, 'HTML 表格格式存在问题，转换后的表格内容需要核对。')
                        texts = _math_texts(document, self.pandoc, self.warnings)
                        values.append(_Renderer(texts, self.warnings, self.pandoc).blocks(document['blocks']))
                    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError):
                        _warn(self.warnings, 'HTML 表格未能可靠转换，保留已脱敏原内容。')
                        values.append(_privacy_fallback(content[1], self.warnings))
                else:
                    _warn(self.warnings, '未支持的块格式保留原内容：' + content[0])
                    values.append(content[1])
            elif kind in {'HorizontalRule', 'Null'}:
                continue
            else:
                raise _Unsupported('block ' + str(kind))
        return '\n\n'.join(value.strip('\n') for value in values if value.strip())

    def caption(self, content):
        return self.blocks(content[1]) or (self.inline(content[0]) if content[0] else '')

    def row(self, content):
        cells = content[1]
        if any(cell[2:4] != [1, 1] for cell in cells):
            raise _Unsupported('table with merged cells')
        results = []
        for cell in cells:
            text = self.blocks(cell[4]).strip()
            cleaned = strip_personal_answers({'title': text})['title'].strip()
            if cleaned != text:
                _warn(self.warnings, '已丢弃明确标注的个人作答，保留原题及正确答案内容。')
            results.append(cleaned)
        return results

    def table(self, content):
        if len(content) != 6:
            raise _Unsupported('table structure')
        _, caption, columns, head, bodies, foot = content
        header_rows = [self.row(row) for row in head[1]]
        count = len(columns)
        if any(len(row) != count for row in header_rows):
            raise _Unsupported('table header size')
        headings = [' / '.join(row[index] for row in header_rows if row[index]) or f'第{index + 1}列'
                    for index in range(count)]
        rows = [self.row(row) for body in bodies for row in body[2] + body[3]]
        rows += [self.row(row) for row in foot[1]]
        if any(len(row) != count for row in rows):
            raise _Unsupported('table row size')
        lines = [self.caption(caption)] if self.caption(caption) else []
        lines += ['；'.join(label + '：' + cell.replace('\n', '\n  ') for label, cell in zip(headings, row))
                  for row in rows]
        if not rows:
            lines.append('；'.join(headings))
        return '\n'.join(lines)


def _escaped_image_literals(text):
    """Protect escaped image examples from generic Markdown link filtering."""
    parts, cursor = [], 0
    for match in re.finditer(r'(?<!\\)\\!\[[^\]\n]*\]', text):
        if match.start() < cursor:
            continue
        end = match.end()
        if end < len(text) and text[end] in '([':
            opening, closing = ('(', ')') if text[end] == '(' else ('[', ']')
            depth, index = 1, end + 1
            while index < len(text) and depth and text[index] != '\n':
                if text[index] == '\\' and index + 1 < len(text):
                    index += 2
                    continue
                depth += (text[index] == opening) - (text[index] == closing)
                index += 1
            if not depth:
                end = index
        literal = text[match.start() + 1:end]
        fence = '`' * (max((len(item) for item in re.findall(r'`+', literal)), default=0) + 1)
        parts += [text[cursor:match.start()], fence + ' ' + literal + ' ' + fence]
        cursor = end
    return ''.join(parts) + text[cursor:]


def render_plain(text: str, pandoc: str | Path) -> tuple[str, list[str]]:
    """Return copyable text plus explicit warnings; never generate or trim answers.

    Unsupported formatting retains sanitized source content. Images remain
    attachments. A warning distinguishes preserved TeX from converted math.
    """
    if not isinstance(text, str):
        raise TypeError('text must be a string')
    warnings = []
    cleaned = strip_personal_answers({'title': text})['title']
    if cleaned != text:
        _warn(warnings, '已丢弃明确标注的个人作答，保留原题及正确答案内容。')
    cleaned, replaced = _PRIVATE_PATH.subn('[本地路径已省略]', cleaned)
    if replaced:
        _warn(warnings, '正文中的本地路径已脱敏；原始路径仅保留在本地原稿和附件映射。')
    cleaned = _LEARNING_URL.sub('[来源链接已省略]', cleaned)
    # Code and math examples are literal text. Escaped image syntax must keep
    # its filename, and must not be reported as an attachment dependency.
    pieces = re.split(r'(```.*?```|~~~.*?~~~|`[^`\n]+`|\$\$.*?\$\$|(?<!\$)\$[^$\n]+\$(?!\$)|\\\(.*?\\\)|\\\[.*?\\\])', cleaned, flags=re.S)
    for index in range(0, len(pieces), 2):
        pieces[index] = _escaped_image_literals(pieces[index])
    cleaned = ''.join(pieces)
    visible = re.sub(r'```.*?```|~~~.*?~~~|`+[^`\n]*`+|\$\$.*?\$\$|(?<!\$)\$[^$\n]+\$(?!\$)|\\\(.*?\\\)|\\\[.*?\\\]', '', cleaned, flags=re.S)
    if re.search(r'!\[[^\]]*\]\(|<img\b', visible, re.I):
        _warn(warnings, '图片作为附件保留，纯文本不自动嵌入图片。')
    # Preserve tables for Pandoc's HTML reader instead of flattening cells during
    # generic HTML cleanup. Code examples remain literal data.
    tables = {}
    pieces = re.split(r'(```.*?```|~~~.*?~~~|`[^`\n]+`)', cleaned, flags=re.S)
    def table_source(match):
        token = 'QBHTMLTABLE' + str(len(tables)) + 'PLACEHOLDER'
        tables[token] = match[0]
        return '\n\n' + token + '\n\n'
    for index in range(0, len(pieces), 2):
        pieces[index] = re.sub(r'<table\b.*?(?:</table\s*>|$)', table_source, pieces[index], flags=re.I | re.S)
    # Reuse citation/HTML sanitization and mathematical delimiter normalization.
    prepared = _markdown(''.join(pieces))
    visible_cleaned = strip_personal_answers({'title': prepared})['title']
    if visible_cleaned != prepared:
        _warn(warnings, '已丢弃明确标注的个人作答，保留原题及正确答案内容。')
    prepared = visible_cleaned
    for token, content in tables.items():
        prepared = prepared.replace(token, content)
    if not prepared.strip():
        return '', warnings
    if _damaged_delimiters(cleaned):
        _warn(warnings, '公式分隔符不完整，保留已脱敏原内容，需核对格式。')
        return _privacy_fallback(prepared, warnings).strip(), warnings
    try:
        result, stderr = _pandoc(pandoc, prepared,
                                'markdown+tex_math_dollars+tex_math_single_backslash-markdown_in_html_blocks', 'json')
        document = json.loads(result)
        if stderr:
            _warn(warnings, '原文存在格式提示，纯文本结果需按保留的本地原稿核对。')
        math_texts = _math_texts(document, pandoc, warnings)
        plain = _Renderer(math_texts, warnings, pandoc).blocks(document['blocks'])
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError) as error:
        _warn(warnings, '格式未能可靠转换，保留已脱敏原内容：' + type(error).__name__)
        plain = _privacy_fallback(prepared, warnings)
    return plain.strip(), warnings
