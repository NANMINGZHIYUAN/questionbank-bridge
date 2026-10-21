"""Generate reproducible synthetic input and enriched bank; never a real course."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from questionbank.cli import write_json
from questionbank.core import file_sha256, merge_banks, normalize, validate_bank


def main() -> None:
    from PIL import Image, ImageDraw
    folder = ROOT / '工作区' / '演示'
    inputs = folder / 'inputs'
    inputs.mkdir(parents=True, exist_ok=True)
    image = Image.new('RGB', (420, 160), 'white')
    draw = ImageDraw.Draw(image)
    for index in range(5):
        color = '#3267bb' if index < 2 else '#42a56b'
        draw.rectangle((30 + 70 * index, 40, 75 + 70 * index, 110), fill=color)
    draw.text((110, 130), '2 + 3 = 5', fill='black')
    image.save(inputs / 'five-blocks.png')
    options = lambda values: [{'label': chr(65+i), 'text': value} for i, value in enumerate(values)]
    items = [
        {'id':1,'type':'单选','title':'【虚构演示】2 + 3 等于多少？','options':options(['4','5','6']),'correctAnswer':['B'],'selectedAnswer':['A'],'images':[]},
        {'id':2,'type':'多选','title':'【虚构演示】下列哪些整数是偶数？','options':options(['2','3','4','5']),'correctAnswer':['A','C'],'images':[]},
        {'id':3,'type':'判断','title':'【虚构演示】0 是偶数。','options':[],'correctAnswer':['正确'],'images':[]},
        {'id':4,'type':'填空','title':'【虚构演示】1 加 0.5 等于 ____。','options':[],'correctAnswer':['1.5'],'images':[]},
        {'id':5,'type':'简答','title':'【虚构演示】原样写出英文短语 two words。','options':[],'correctAnswer':['two words'],'images':[]},
        {'id':6,'type':'简答','title':'【虚构演示】说明偶数的定义及判断方法。','options':[],'correctAnswer':['偶数是能够被 2 整除的整数。判断整数是否为偶数，可以检查其除以 2 的余数是否为 0；0、正偶数和负偶数都符合这个定义。'],'images':[]},
        {'id':7,'type':'填空','title':'【虚构演示】依据公式 $2+3=5$，结果是 ____。','options':[],'correctAnswer':['5'],'images':[]},
        {'id':8,'type':'单选','title':'【虚构演示】图中有几个色块？','options':options(['4','5','6']),'correctAnswer':['B'],'images':[{'path':str(inputs / 'five-blocks.png'),'required':True}]},
        {'id':9,'type':'未知','title':'【虚构异常】未知题型需要核实。','options':[],'correctAnswer':['演示值'],'images':[]},
        {'id':10,'type':'单选','title':'【虚构异常】这道题没有原答案。','options':options(['甲','乙']),'correctAnswer':[],'selectedAnswer':['A'],'images':[]},
        {'id':11,'type':'单选','title':'【虚构异常】不同来源答案冲突。','options':options(['甲','乙']),'correctAnswer':['A'],'images':[]},
        {'id':12,'type':'填空','title':'【虚构演示】3 加 3 等于 ____。','options':[],'correctAnswer':['6'],'images':[]},
    ]
    raw = {'source':'https://example.invalid/synthetic-demo','ts':0,'items':items}
    source = inputs / 'synthetic-good-review.json'
    write_json(source, raw)
    metadata = lambda p: {'id':'src-'+file_sha256(p)[:16],'path':str(p),'sha256':file_sha256(p),'format':'json'}
    bank = normalize(raw, '演示课程 虚构数据', metadata(source))
    conflicting = deepcopy(raw)
    conflicting['items'] = [deepcopy(items[10])]
    conflicting['items'][0]['correctAnswer'] = ['B']
    variant = inputs / 'synthetic-conflict.json'
    write_json(variant, conflicting)
    bank = merge_banks(bank, normalize(conflicting, bank['course'], metadata(variant)))
    lookup = {q['title']:q for q in bank['questions']}
    normal = [q for q in bank['questions'] if q['title'].startswith('【虚构演示】')]
    topic_id = 'topic-demo-basic'
    for q in normal:
        q['topicIds'] = [topic_id]
        if '偶数' in q['title']:
            q['explanation'] = '偶数可写成 $2k$，其中 $k$ 为整数。0 满足 $0=2\\times 0$，因此也是偶数。'
        elif '+' in q['title'] or '加' in q['title']:
            q['explanation'] = '按照普通加法计算；本题用于验证数学、数值和文字格式的保存。'
        else:
            q['explanation'] = '这是虚构格式演示，保留原答案完整内容，不代表实际课程材料。'
    even = lookup['【虚构演示】0 是偶数。']['id']
    addition = lookup['【虚构演示】2 + 3 等于多少？']['id']
    bank['topics'] = [{
        'id':topic_id,'title':'基础算术与格式演示','summary':'了解加法和偶数，并检查题库内容能否完整保存。',
        'explanation':'本页全部为虚构演示。\n\n普通加法如 $2+3=5$。偶数可以写成 $2k$，其中 $k$ 为整数；零也满足这个条件。\n\n小数答案应完整保存为 1.5，英文答案 two words 中的空格也不能截掉。',
        'questionIds':[q['id'] for q in normal],
        'cards':[
            {'id':'card-demo-even','prompt':'0 为什么是偶数？','answer':'因为 $0=2\\times 0$，即零可以被 2 整除。','questionIds':[even]},
            {'id':'card-demo-addition','prompt':'计算 $2+3$。','answer':'$2+3=5$。','questionIds':[addition]},
        ]}]
    issues = validate_bank(bank)
    write_json(folder / 'bank.json', bank)
    write_json(folder / 'demo-report.json', {'synthetic':True,'questions':len(bank['questions']), 'issues':issues})
    print('虚构演示 bank.json 已生成：' + str(folder / 'bank.json'))


if __name__ == '__main__':
    main()
