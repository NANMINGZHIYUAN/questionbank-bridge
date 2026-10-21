from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

import questionbank.manual_text as manual_text
from questionbank.manual_text import render_plain


class PlainTextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pandoc = shutil.which('pandoc')
        if not cls.pandoc:
            raise unittest.SkipTest('Pandoc 未安装')

    def render(self, source):
        return render_plain(source, self.pandoc)

    def test_ohms_law_is_copyable_exact_text(self):
        source = r'$R=\frac{U}{I}=\frac{12}{2}=6\,\Omega$'
        before = source
        plain, warnings = self.render(source)
        self.assertEqual(plain, 'R=U/I=12/2=6 Ω')
        self.assertEqual(warnings, [])
        self.assertEqual(source, before)
        self.assertNotIn('$', plain)
        self.assertNotIn('\\', plain)

    def test_compound_fraction_operands_and_nested_fraction_keep_scope(self):
        for source, expected in [
            (r'$\frac{a+b}{c-d}$', '(a+b)/(c−d)'),
            (r'$\frac{\frac{a}{b}}{\frac{c}{d}}$', '(a/b)/(c/d)'),
            (r'$\frac{a}{b}/\frac{c}{d}$', '(a/b)/(c/d)'),
            (r'$\frac{1}{a b}$', '1/(a b)'),
            (r'$\frac{1}{(a)+(b)}$', '1/((a)+(b))'),
            (r'$\frac{1}{(a-b)(c+d)}$', '1/((a−b) (c+d))'),
        ]:
            with self.subTest(source=source):
                self.assertEqual(self.render(source), (expected, []))

    def test_adjacent_fraction_operands_cannot_be_read_as_denominator(self):
        for source, expected in [
            (r'$\frac{a}{b}c$', '(a/b)·c'),
            (r'$a\frac{b}{c}$', 'a·(b/c)'),
            (r'$\frac{a}{b}\frac{c}{d}$', '(a/b)·(c/d)'),
            (r'$\frac{a}{b}\cdot\frac{c}{d}$', '(a/b)⋅(c/d)'),
            (r'$\frac{a}{b}\,c$', '(a/b) c'),
            (r'$\frac{a}{b}\,\frac{c}{d}$', '(a/b) (c/d)'),
            (r'$\frac{a}{b}+\frac{c}{d}$', 'a/b+c/d'),
        ]:
            with self.subTest(source=source):
                self.assertEqual(self.render(source), (expected, []))

    def test_subscript_exponent_and_root_keep_values_and_scope(self):
        for source, expected in [
            (r'$I_1^2$', 'I₁²'),
            (r'$x^{n+1}$', 'xⁿ⁺¹'),
            (r'$x^{a b}$', 'x^(a b)'),
            (r'$\left(\frac{a}{b}\right)^2$', '(a/b)²'),
            (r'$\sqrt{a+b}$', 'sqrt(a+b)'),
            (r'$\sqrt[3]{x}$', 'root(3, x)'),
        ]:
            with self.subTest(source=source):
                self.assertEqual(self.render(source), (expected, []))

    def test_tex_delimiter_variants_convert_without_rendering_syntax(self):
        source = r'先用 \(U=IR\)。' + '\n\n' + r'\[R=\frac{U}{I}\]'
        plain, warnings = self.render(source)
        self.assertIn('U=I R', plain)
        self.assertIn('R=U/I', plain)
        self.assertNotIn('\\', plain)
        self.assertNotIn('$', plain)
        self.assertEqual(warnings, [])

    def test_complete_case_and_numbered_steps_remain(self):
        source = ('# 案例\n\n一台电器的电压为 12 V，电流为 2 A。\n\n'
                  '7. **切断电源。**\n'
                  '8. 测量电压和电流。\n'
                  '9. 用 $R=\\frac{U}{I}$ 计算。\n\n'
                  '   验证单位为 Ω，保留该步骤的补充说明。\n\n'
                  '10. 写下完整结论，未省略最后一步。')
        plain, warnings = self.render(source)
        for value in ('案例', '12 V', '2 A', '7. 切断电源。', '8. 测量电压和电流。',
                      '9. 用 R=U/I 计算。', '补充说明', '10. 写下完整结论'):
            self.assertIn(value, plain)
        self.assertNotIn('**', plain)
        self.assertNotIn('#', plain)
        self.assertEqual(warnings, [])

    def test_markdown_table_preserves_every_header_and_cell(self):
        source = ('| 量 | 数值 | 说明 |\n|---|---|---|\n'
                  '| U | 12 V | 电压 |\n| I | 2 A | 电流 |\n'
                  '| R | $\\frac{12}{2}=6\\,\\Omega$ | 计算结果 |')
        plain, warnings = self.render(source)
        self.assertEqual(plain, '量：U；数值：12 V；说明：电压\n'
                               '量：I；数值：2 A；说明：电流\n'
                               '量：R；数值：12/2=6 Ω；说明：计算结果')
        self.assertEqual(warnings, [])
        self.assertNotIn('|', plain)
        self.assertNotIn('---', plain)

    def test_link_labels_and_legal_public_urls_survive(self):
        source = '[欧姆定律](https://example.org/reference/ohm) 和 [参考][ref]。\n\n[ref]: https://example.org/paper'
        plain, warnings = self.render(source)
        self.assertIn('欧姆定律（https://example.org/reference/ohm）', plain)
        self.assertIn('参考（https://example.org/paper）', plain)
        self.assertEqual(warnings, [])
        self.assertNotIn('本地路径', plain)

    def test_html_table_keeps_cell_boundaries(self):
        source = ('<table><tr><th>量</th><th>值</th></tr>'
                  '<tr><td>U</td><td>12 V</td></tr>'
                  '<tr><td>I</td><td>2 A</td></tr></table>')
        plain, warnings = self.render(source)
        self.assertEqual(plain, '量：U；值：12 V\n量：I；值：2 A')
        self.assertEqual(warnings, [])

    def test_html_visible_personal_label_is_filtered_after_cleanup(self):
        source = '<p>正确答案：6 Ω。</p><p>我的答案：FICTIONAL_PRIVATE</p>'
        plain, warnings = self.render(source)
        self.assertEqual(plain, '正确答案：6 Ω。')
        self.assertTrue(any('个人作答' in value for value in warnings))

    def test_html_table_personal_cell_is_filtered_after_parse(self):
        source = ('<table><tr><th>题目</th><th>答案</th></tr>'
                  '<tr><td>电阻值</td><td><p>正确答案：6 Ω。</p>'
                  '<p>我的答案：FICTIONAL_PRIVATE</p></td></tr></table>')
        plain, warnings = self.render(source)
        self.assertNotIn('FICTIONAL_PRIVATE', plain)
        self.assertIn('正确答案：6 Ω', plain)
        self.assertIn('题目：电阻值', plain)
        self.assertTrue(any('个人作答' in value for value in warnings))

    def test_images_are_attachments_and_never_copy_local_location(self):
        source = r'参照电路图：![已有电路图](D:\虚构\媒体\电路.png)' + '\n\n答案仍为 6 Ω。'
        plain, warnings = self.render(source)
        self.assertIn('电路图', plain)
        self.assertIn('答案仍为 6 Ω', plain)
        self.assertNotIn('D:', plain)
        self.assertNotIn('电路.png', plain)
        self.assertNotIn('![', plain)
        self.assertTrue(any('图片' in value for value in warnings))

    def test_escaped_image_examples_preserve_complete_syntax_and_nested_filename(self):
        for literal in ('![图片](sample.png)', '![图片](sample(a).png)'):
            with self.subTest(literal=literal):
                plain, warnings = self.render('字面示例：\\' + literal + ' 后续文字保留。')
                self.assertIn(literal, plain)
                self.assertIn('后续文字保留', plain)
                self.assertNotIn('图片附件', plain)
                self.assertFalse(any('图片' in warning and '附件' in warning for warning in warnings))

    def test_escaped_reference_image_stays_literal_instead_of_becoming_attachment(self):
        source = r'字面示例：\![图片][ref]' + '\n\n[ref]: sample.png'
        plain, warnings = self.render(source)
        self.assertIn('![图片][ref]', plain)
        self.assertNotIn('图片附件', plain)
        self.assertFalse(any('图片' in warning and '附件' in warning for warning in warnings))

    def test_code_and_math_image_syntax_keep_filename_without_attachment_warning(self):
        sources = ['`![图片](sample.png)`', '```text\n![图片](sample.png)\n```',
                   r'$\text{![图片](sample.png)}$']
        for source in sources:
            with self.subTest(source=source):
                plain, warnings = self.render(source)
                self.assertIn('![图片](sample.png)', plain)
                self.assertNotIn('图片附件', plain)
                self.assertFalse(any('图片' in warning and '附件' in warning for warning in warnings))

    def test_real_image_still_warns_when_next_to_escaped_literal_example(self):
        source = r'字面示例 \![示例](sample.png)；实际题图 ![真实图](assets/real.png)。'
        plain, warnings = self.render(source)
        self.assertIn('![示例](sample.png)', plain)
        self.assertIn('真实图', plain)
        self.assertNotIn('assets/real.png', plain)
        self.assertTrue(any('图片' in warning and '附件' in warning for warning in warnings))

    def test_escaped_image_literal_protection_cannot_bypass_private_location_redaction(self):
        sources = [r'字面示例 \![图片](C:/private/PRIVATE_LITERAL_FILE.png)',
                   r'字面示例 \![图片](https://mooc1.chaoxing.com/exam?courseid=PRIVATE_LITERAL_URL)']
        for source in sources:
            with self.subTest(source=source):
                plain, warnings = self.render(source)
                for private in ('C:/private', 'PRIVATE_LITERAL_FILE', 'chaoxing.com',
                                'PRIVATE_LITERAL_URL', 'courseid='):
                    self.assertNotIn(private, plain)
                self.assertIn('字面示例', plain)
                self.assertIn('省略', plain)
                self.assertFalse(any('图片' in warning and '附件' in warning for warning in warnings))

    def test_private_locations_and_learning_urls_are_redacted(self):
        source = ('案例资料："D:\\虚构 含空格\\source.docx"。\n\n'
                  '图源：file:///D:/fictional/private.png\n\n'
                  '学习通：https://mooc1.chaoxing.com/exam?courseid=FICTIONAL_PRIVATE\n\n'
                  '[公开材料](https://example.org/ref)')
        plain, warnings = self.render(source)
        for value in ('source.docx', 'private.png', 'courseid', 'FICTIONAL_PRIVATE', 'chaoxing.com', 'D:'):
            self.assertNotIn(value, plain)
        self.assertIn('公开材料（https://example.org/ref）', plain)
        self.assertTrue(any('路径' in value for value in warnings))

    def test_personal_answer_block_removed_but_correct_steps_kept(self):
        source = ('题干：计算电阻。\n'
                  '我的答案：FICTIONAL_PRIVATE\n2. FICTIONAL_PRIVATE_STEP\n'
                  '正确答案：\n1. 读出 U=12 V。\n2. 读出 I=2 A。\n3. R=6 Ω。')
        plain, warnings = self.render(source)
        self.assertNotIn('FICTIONAL_PRIVATE', plain)
        self.assertIn('题干：计算电阻', plain)
        for value in ('正确答案', '1. 读出 U=12 V', '2. 读出 I=2 A', '3. R=6 Ω'):
            self.assertIn(value, plain)
        self.assertTrue(any('个人作答' in value for value in warnings))

    def test_literal_code_and_unlabelled_valid_answer_are_preserved(self):
        source = ('未标注个人答案的正确正文。\n\n1. 得到 $U=IR$。\n2. 得到 6 Ω。\n\n'
                  '```text\n我的答案：literal code example\n2. code step\n```\n\n'
                  '正文讨论我的答案这个字段。代码 `$U=IR$` 是字面数据。')
        plain, warnings = self.render(source)
        for value in ('未标注个人答案的正确正文', '1. 得到 U=I R', '2. 得到 6 Ω',
                      '我的答案：literal code example', '2. code step', '$U=IR$', '正文讨论我的答案'):
            self.assertIn(value, plain)
        self.assertEqual(warnings, [])

    def test_struck_text_retains_its_withdrawn_meaning(self):
        plain, warnings = self.render('答案由 ~~错误旧值~~ 改为 **6 Ω**。')
        self.assertEqual(plain, '答案由 （已划去：错误旧值） 改为 6 Ω。')
        self.assertEqual(warnings, [])

    def test_unsupported_matrix_keeps_original_tex_and_warns(self):
        source = r'$\begin{matrix}a & b\\c & d\end{matrix}$'
        plain, warnings = self.render(source)
        self.assertEqual(plain, source)
        self.assertTrue(any('原 TeX' in value for value in warnings))

    def test_unknown_and_damaged_math_preserve_complete_source(self):
        for source in (r'$\unknowncommand{x}$', r'$R=\frac{U}{I$', r'\(x^2', r'$x^2'):
            with self.subTest(source=source):
                plain, warnings = self.render(source)
                self.assertEqual(plain, source)
                self.assertTrue(warnings)

    def test_missing_converter_preserves_sanitized_full_content(self):
        source = '# 案例\n\n完整第一段。\n\n1. 完整步骤。\n2. 最后一步。\n\n$R=\\frac{U}{I}$'
        with patch('questionbank.manual_text._pandoc', side_effect=FileNotFoundError):
            plain, warnings = self.render(source)
        self.assertEqual(plain, source)
        self.assertTrue(any('未能可靠转换' in value for value in warnings))

    def test_converter_timeout_keeps_content_and_warning(self):
        with patch('questionbank.manual_text._pandoc', side_effect=subprocess.TimeoutExpired('pandoc', 90)):
            plain, warnings = self.render('完整答案：第一步和最后一步。')
        self.assertEqual(plain, '完整答案：第一步和最后一步。')
        self.assertTrue(warnings)

    def test_converter_failures_cannot_reveal_html_personal_answer(self):
        source = ('<table><tr><td>普通名称</td><td>我的答案：FICTIONAL_PRIVATE</td>'
                  '<td>正确答案：6 Ω。</td><td>必要步骤。</td></tr></table>')
        real_convert = manual_text._pandoc
        def fail_html_only(*args):
            if args[2] == 'html':
                raise RuntimeError('fictional HTML conversion failure')
            return real_convert(*args)
        for failure in (FileNotFoundError(), subprocess.TimeoutExpired('pandoc', 90), fail_html_only):
            with self.subTest(failure=type(failure).__name__), patch(
                    'questionbank.manual_text._pandoc', side_effect=failure):
                plain, warnings = self.render(source)
            self.assertNotIn('FICTIONAL_PRIVATE', plain)
            self.assertNotIn('我的答案', plain)
            self.assertIn('普通名称', plain)
            self.assertIn('正确答案：6 Ω', plain)
            self.assertIn('必要步骤', plain)
            self.assertTrue(any('个人作答' in value for value in warnings))
            self.assertTrue(any('HTML' in value for value in warnings))

    def test_merged_cell_fallback_cannot_reveal_html_personal_answer(self):
        source = ('完整题干。\n\n<table><tr><td colspan="2">我的答案：FICTIONAL_PRIVATE</td></tr>'
                  '<tr><td>正确答案：6 Ω。</td><td>必要步骤。</td></tr></table>\n\n完整结论。')
        plain, warnings = self.render(source)
        self.assertNotIn('FICTIONAL_PRIVATE', plain)
        self.assertNotIn('我的答案', plain)
        for value in ('完整题干', '正确答案：6 Ω', '必要步骤', '完整结论'):
            self.assertIn(value, plain)
        self.assertTrue(any('HTML 表格未能可靠转换' in value for value in warnings))
        self.assertTrue(any('个人作答' in value for value in warnings))

    def test_unknown_ast_node_cannot_silently_drop_content(self):
        document = {'pandoc-api-version': [1, 23, 1, 2], 'meta': {},
                    'blocks': [{'t': 'FutureBlock', 'c': 'unsupported'}]}
        with patch('questionbank.manual_text._pandoc', return_value=(json.dumps(document), '')):
            plain, warnings = self.render('原始完整答案，必须保留。')
        self.assertEqual(plain, '原始完整答案，必须保留。')
        self.assertTrue(warnings)

    def test_empty_text_never_calls_converter(self):
        with patch('questionbank.manual_text._pandoc') as convert:
            self.assertEqual(self.render(' \n\n'), ('', []))
        convert.assert_not_called()

    def test_wrong_input_type_is_rejected_without_conversion(self):
        with patch('questionbank.manual_text._pandoc') as convert, self.assertRaises(TypeError):
            self.render(None)
        convert.assert_not_called()


if __name__ == '__main__':
    unittest.main()
