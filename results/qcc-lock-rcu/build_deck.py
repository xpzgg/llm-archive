from pathlib import Path
import csv
import json
import re
import os

from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE, MSO_CONNECTOR
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.oxml.xmlchemy import OxmlElement

OUT = Path(__file__).resolve().parent
W, H = 1600, 900
SCALE = 2
FONT_NAME = os.environ.get('QCC_FONT_NAME', 'Noto Sans CJK SC')
FONT_PATH = os.environ.get('QCC_FONT_REGULAR', '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')
BOLD_PATH = os.environ.get('QCC_FONT_BOLD', '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc')
FONT_INDEX = int(os.environ.get('QCC_FONT_INDEX', '2'))
BG, DARK, INK, MUTED = 'FFFFFF', '231815', '231815', '595757'
RED, DEEP_RED, PALE, WHITE = 'C7000B', '7F0001', 'FFF2F3', 'FFFFFF'
LINE = 'DCDDDD'
CAQ = 'https://www.panyu.gov.cn/attachment/7/7835/7835136/10324082.pdf'
ASQ = 'https://asq.org/quality-resources/fmea'

prs = Presentation()
prs.slide_width, prs.slide_height = Inches(16), Inches(9)
prs.core_properties.title = '用 AI Agent 缩短 Lock/RCU 问题平均定位时长｜QCC 四页'
prs.core_properties.subject = 'Lock/RCU 软件与平台排查；vmcore 分析；QCC 要因确认与对策'
prs.core_properties.author = 'QCC 项目组'
prs.core_properties.keywords = 'QCC, Lock, RCU, AI, Agent, RPN, SOD, 要因确认, 华为红'
prs.core_properties.comments = '使用华为红C7000B。30份材料、评分与确认结果均为模拟示例，非真实采样；对策目标未实测。'
checks, all_texts, preview_images = [], [], []
slide = None
canvas = None
draw = None

def rgb(s):
    return RGBColor.from_string(s)

def font(size, bold=False, scale=1):
    return ImageFont.truetype(BOLD_PATH if bold else FONT_PATH, round(size * scale), index=FONT_INDEX)

def bounds(x, y, w, h, kind):
    if min(x, y) < 0 or x + w > W + .1 or y + h > H + .1:
        checks.append({'kind': kind, 'bounds': [x, y, w, h]})

def rect(x, y, w, h, fill=WHITE, border=None, radius=False):
    bounds(x, y, w, h, 'rect')
    shp = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE,
                                Inches(x/100), Inches(y/100), Inches(w/100), Inches(h/100))
    if radius:
        shp.adjustments[0] = .08
    shp.fill.solid()
    shp.fill.fore_color.rgb = rgb(fill)
    if border:
        shp.line.color.rgb = rgb(border)
        shp.line.width = Pt(.7)
    else:
        shp.line.fill.background()
    xy = (x*SCALE, y*SCALE, (x+w)*SCALE, (y+h)*SCALE)
    if radius:
        draw.rounded_rectangle(xy, radius=10*SCALE, fill='#'+fill, outline='#'+border if border else None, width=SCALE)
    else:
        draw.rectangle(xy, fill='#'+fill, outline='#'+border if border else None, width=SCALE)
    return shp

def line(x1, y1, x2, y2, color=LINE, width=2):
    shp = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1/100), Inches(y1/100), Inches(x2/100), Inches(y2/100))
    shp.line.color.rgb = rgb(color)
    shp.line.width = Pt(width*.72)
    draw.line((x1*SCALE, y1*SCALE, x2*SCALE, y2*SCALE), fill='#'+color, width=round(width*SCALE))

def wrap_text(content, size, w, bold=False):
    f = font(size, bold)
    result = []
    for para in content.split('\n'):
        if not para:
            result.append('')
            continue
        tokens = re.findall(r'[A-Za-z0-9_./≤≥%~→+\-]+|.', para)
        curr = ''
        for token in tokens:
            if curr and f.getlength(curr + token) > w:
                result.append(curr.rstrip())
                curr = token.lstrip()
            else:
                curr += token
        result.append(curr)
    return result

def text(content, x, y, w, size=24, color=INK, bold=False, align='left', lh=None):
    lh = lh or size*1.42
    rows = wrap_text(content, size, w, bold)
    for i, row in enumerate(rows):
        yy = y + i*lh
        bounds(x, yy, w, lh, 'text')
        if font(size, bold).getlength(row) > w + 1:
            checks.append({'kind':'text_width', 'text':row, 'width':w})
        box = slide.shapes.add_textbox(Inches(x/100), Inches(yy/100), Inches(w/100), Inches(lh/100))
        tf = box.text_frame
        tf.clear()
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        tf.word_wrap = False
        tf.vertical_anchor = MSO_ANCHOR.TOP
        p = tf.paragraphs[0]
        p.alignment = {'left':PP_ALIGN.LEFT, 'center':PP_ALIGN.CENTER, 'right':PP_ALIGN.RIGHT}[align]
        p.space_before = p.space_after = Pt(0)
        p.line_spacing = 1.0
        run = p.add_run()
        run.text = row
        run.font.name, run.font.size = FONT_NAME, Pt(size*.72)
        run.font.bold = bold
        run.font.color.rgb = rgb(color)
        ea = OxmlElement('a:ea')
        ea.set('typeface', FONT_NAME)
        run._r.get_or_add_rPr().append(ea)
        actual = font(size, bold, SCALE).getlength(row)
        xx = x*SCALE
        if align == 'center':
            xx += (w*SCALE - actual)/2
        if align == 'right':
            xx += w*SCALE - actual
        draw.text((xx, yy*SCALE), row, font=font(size, bold, SCALE), fill='#'+color, anchor='lt')
    all_texts[-1]['text'].append(content)
    return len(rows)*lh

def new_slide(num, title, subtitle, badge):
    global slide, canvas, draw
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = rgb(BG)
    canvas = Image.new('RGB', (W*SCALE,H*SCALE), '#'+BG)
    draw = ImageDraw.Draw(canvas)
    all_texts.append({'section':num, 'title':title, 'text':[]})
    rect(0, 0, 1600, 5, RED)
    text(num, 60, 40, 100, 32, RED, True)
    text(title, 170, 38, 1100, 34, RED, True)
    text(subtitle, 60, 104, 1480, 22, MUTED)
    rect(1280, 39, 260, 44, PALE, radius=True)
    text(badge, 1292, 47, 236, 18, RED, True, 'center')

def finish(notes, footer):
    line(60, 842, 1540, 842)
    text(footer, 60, 855, 1380, 15, MUTED)
    text(f'{len(prs.slides)} / 4', 1460, 855, 80, 15, MUTED, align='right')
    slide.notes_slide.notes_text_frame.text = notes
    out = OUT / f'slide-{len(prs.slides)}.png'
    canvas.resize((W,H), Image.Resampling.LANCZOS).save(out)
    preview_images.append(canvas.resize((W,H), Image.Resampling.LANCZOS))

RCU = 'https://www.kernel.org/doc/html/latest/RCU/stallwarn.html'
CRASH = 'https://crash-utility.github.io/help_pages/bt.html'
DRGN = 'https://drgn.readthedocs.io/en/latest/helpers.html#locking'
ARM_TIME = 'https://docs.kernel.org/arch/arm64/booting.html'
ARM_MODEL = 'https://documentation-service.arm.com/static/648c77ea153eb247a5450e70'
ARM_IC = 'https://documentation-service.arm.com/static/683d2ddc771bba091ac6f76f?token='

# 2.1: software, platform and day-to-day debugging causes in one horizontal tree.
new_slide('2.1', '根因分析', 'Lock / RCU 告警只给出入口：还要查清谁在等、什么没前进，以及平台当时是否正常', '横向树图 · 待确认疑因')
text('定位时间长',60,163,210,19,MUTED,True)
text('领域与工作难点',320,163,260,19,MUTED,True)
text('导致反复排查的具体疑因',610,163,930,19,MUTED,True)
line(260,470,285,470,RED,3)
line(285,290,285,650,RED,3)
for cy in (290,470,650):
    line(285,cy,320,cy,RED,3)
    line(550,cy,580,cy,RED,2)
    line(580,cy-45,580,cy+45,RED,2)
    line(580,cy-45,610,cy-45,RED,2)
    line(580,cy+45,610,cy+45,RED,2)
rect(60,411,200,118,RED,radius=True)
text('Lock / RCU',76,431,168,26,WHITE,True,'center')
text('定位周期长',76,477,168,26,WHITE,True,'center')
groups=[(237,'等待关系难还原','内核机制'),(417,'软硬件原因难分','ESL / FPGA'),(597,'补采与复现反复','日常排查')]
for yy,head,sub in groups:
    rect(320,yy,230,106,PALE,radius=True)
    text(head,332,yy+19,206,25,RED,True,'center')
    text(sub,332,yy+64,206,18,MUTED,align='center')
causes=[
    ('F1','等锁链分散在多 CPU，靠人工逐个还原','多核同卡慢路径；qspinlock 需结合锁地址、其他 CPU 栈与 MCS 节点追查'),
    ('F2','RCU 告警与状态分散，靠人工反复比对','同一类 stall 可能来自缺 QS、GP 线程饥饿或 timer 唤醒失败'),
    ('F3','频率、计时口径缺项，慢与停滞难区分','FPGA 主频/访存慢；ESL 计时模式各异，要结合进度、计时器和 IRQ 判断'),
    ('F4','软硬证据缺少共同事件，来回补证','互联响应、IRQ 投递可能异常；串口、硬件 trace、仿真事件要对到同一现场'),
    ('F5','dump、符号与版本信息未成套交接','vmcore、同版符号、全 CPU 栈与固件/镜像未成套，补采后才能继续'),
    ('F6','复现前未定好假设与观察点，跑完还要重来','低性能平台启动和触发慢；新增 printk / trace 又可能改变负载或时序'),
]
for yy,(cid,head,body) in zip((205,295,385,475,565,655),causes):
    rect(610,yy,930,80,WHITE,LINE,True)
    rect(610,yy,5,80,DEEP_RED if cid in ('F3','F4') else RED)
    text(cid+'  '+head,630,yy+10,888,24,INK,True)
    text(body,630,yy+49,888,19,MUTED)
rect(60,768,1480,60,'FFF2F3',radius=True)
text('改进重点：先利用已有现场减少盲查和补采，把昂贵的下一轮复现用于验证明确的假设。',80,787,1440,23,DEEP_RED,True)
finish(
    '本页分析的是定位周期长的原因。F1—F6 是结合团队 ESL/FPGA 与软芯协同背景提出的工作疑因，仍须用问题单记录确认；不能把某类硬件可能性写成已经查实的团队根因。\n'
    'F1：当前参考源码 include/asm-generic/qspinlock_types.h 的原始 qspinlock 字段为 locked/pending/tail；tail 表示队尾等锁者，不能当持锁 CPU。若故障内核保留了调试 owner 信息，应先使用该信息。MCS 队列也可能在过渡状态，或包含复用节点，分析需要对照对应版本和全 CPU 栈。\n'
    'F2：区分停滞 CPU/任务与打印告警的 CPU。源码 kernel/rcu/tree_stall.h 的 qsmask、GP 线程与 timer 诊断路径可作为工具设计依据。\n'
    'F3：低频是平台条件，并不自动等同假告警。分别记录 CPU、内存、互联频率与系统计数器频率；核实 CNTFRQ/时间换算。ESL 还要说明仿真时间如何推进以及模型时序精度。Arm Fast Models 的 PV 模型只是一个具体例子，不代表所有 ESL 平台。\n'
    'F4：待查硬件分支包括互联事务未返回、内存系统响应与 IRQ 投递异常。Linux 日志只提供软件观察面，硬件结论需要平台记录支持；对齐事件标记和时钟映射，不能直接把不同来源的时间戳排序。\n'
    'F5/F6：是否存在符号不匹配、关键对象不可读、日志缺段或无效重跑，以历史案例为准。当前工作区配置不作为现场事实。\n'
    f'技术来源：Linux RCU stall 文档 {RCU}；Arm64 booting {ARM_TIME}；Arm Fast Models 11.22 用户指南 p.25 {ARM_MODEL}。\n'
    f'互联实例仅证明此类机制存在：Arm CI-700 erratum 3031700，{ARM_IC}，不主张本平台受其影响。\n'
    f'QCC 原因分析与要因确认：T/CAQ 10201—2024 4.1.6—4.1.7，{CAQ}',
    '源码与公开资料用于说明机制；六项工作疑因的发生情况、影响时长及主次，须由案例记录确认。')

# Reviewer-facing tables are calculated from one explicitly synthetic data source.
DATA = json.loads((OUT/'review-data.example.json').read_text(encoding='utf-8'))
items = DATA['causes']
sample_n = sum(len(c['delays_h']) for c in items)
total_delay = sum(sum(c['delays_h']) for c in items)
case_rows = []

def level(value, boundaries):
    for score, upper in zip((2,4,6,8), boundaries):
        if value <= upper:
            return score
    return 10

for c in items:
    c['n'] = len(c['delays_h'])
    assert c['n'] == len(c['platforms']) == len(c['symptoms'])
    c['hours'] = sum(c['delays_h'])
    c['mean'] = c['hours']/c['n']
    c['share'] = c['hours']/total_delay
    assert c['S'] == level(c['mean'], (2,4,8,16))
    assert c['O'] == level(c['n']/sample_n, (.05,.1,.2,.3))
    assert c['D'] in (2,4,6,8,10)
    c['rpn'] = c['S']*c['O']*c['D']
    c['screened'] = c['rpn'] >= DATA['rpn_threshold']
    # All six scenarios assume records support the delay mechanism. This is
    # illustrative attribution, not verification that any real record exists.
    assert c['example_evidence'].startswith('示例设定：')
    c['major'] = c['share'] >= DATA['confirmation_share_threshold']
    first = len(case_rows)+1
    for delay, platform, symptom in zip(c['delays_h'],c['platforms'],c['symptoms']):
        case_rows.append(['模拟示例，非实测',f'SIM-{len(case_rows)+1:02d}',symptom,platform,
                          c['id'],c['name'],delay,c['example_evidence'],'无真实材料；待补'])
    c['case_range'] = f'SIM-{first:02d}–{len(case_rows):02d}'
major_items = [c for c in items if c['major']]
major_hours = sum(c['hours'] for c in major_items)
major_n = sum(c['n'] for c in major_items)
ranked = sorted(items,key=lambda c:c['rpn'],reverse=True)
assert sample_n == 30 and total_delay == 352
assert {c['id'] for c in items if c['screened']} == {c['id'] for c in major_items}

def review_table(x,y,widths,headers,rows,row_h,sizes,header_h=50,pad=10,
                 alignments=None,emphasis=None,header_size=19):
    total=sum(widths)
    rect(x,y,total,header_h,'F1F1F1')
    line(x,y,x+total,y,RED,3)
    xx=x
    for ci,(head,ww) in enumerate(zip(headers,widths)):
        hh=len(wrap_text(head,header_size,ww-2*pad,True))*header_size*1.25
        text(head,xx+pad,y+(header_h-hh)/2,ww-2*pad,header_size,INK,True,
             'center' if alignments and alignments[ci]=='center' else 'left',lh=header_size*1.25)
        if hh>header_h-8:
            checks.append({'kind':'table_header_height','text':head})
        xx+=ww
    yy=y+header_h
    for ri,row in enumerate(rows):
        assert len(row)==len(widths)
        marked=bool(emphasis and emphasis[ri])
        rect(x,yy,total,row_h,'FFF7F7' if marked else (WHITE if ri%2==0 else 'FAFAFA'))
        xx=x
        for ci,(value,ww,sz) in enumerate(zip(row,widths,sizes)):
            bold = ci==0 or (marked and ci==len(row)-1)
            color = RED if bold else INK
            used=text(str(value),xx+pad,yy+pad,ww-2*pad,sz,color,bold,
                      alignments[ci] if alignments else 'left',lh=sz*1.25)
            if used>row_h-2*pad+.1:
                checks.append({'kind':'review_table_cell_height','row':ri,'col':ci,'text':value,
                               'used':used,'available':row_h-2*pad})
            if ci:
                line(xx,yy,xx,yy+row_h,LINE,.7)
            xx+=ww
        yy+=row_h
        line(x,yy,x+total,yy,LINE,1)
    return yy

new_slide('2.2','主疑因筛选','借用 FMEA 的 S / O / D 评分，按对定位时长的影响筛选主疑因','RPN筛选 · 示例数据')
rect(60,155,1480,45,PALE,radius=True)
text('示例数据，待实际记录替换  |  模拟30份结单材料  |  RPN = S × O × D  |  本例筛选线：RPN ≥ 300',78,168,1444,21,RED,True)
for xx,title,body in [
    (60,'严重度 S｜单例平均延迟','2 / 4 / 6 / 8 / 10 分，依次对应：\n≤2h / ≤4h / ≤8h / ≤16h / >16h'),
    (560,'发生度 O｜主要延迟原因占比','2 / 4 / 6 / 8 / 10 分，依次对应：\n≤5% / ≤10% / ≤20% / ≤30% / >30%'),
    (1060,'检出度 D｜分数越高，初诊越难识别','2 清单发现；4 输入核验；6 单份材料\n8 跨CPU/多记录；10 跨团队/再次取证'),
]:
    rect(xx,212,480,115,WHITE,LINE)
    text(title,xx+16,226,448,20,INK,True)
    text(body,xx+16,264,448,17.5,MUTED,lh=24)
rpn_rows=[]
for rank,c in enumerate(ranked,1):
    rpn_rows.append([str(rank),c['id']+' '+c['name'],str(c['S']),str(c['O']),str(c['D']),str(c['rpn']),
                    f"{c['n']}/{sample_n}例（{c['n']/sample_n:.1%}）；平均延迟{c['mean']:g}h\n{c['d_basis']}",
                    '列入主疑因' if c['screened'] else '保留核查'])
review_table(60,340,[64,310,66,66,66,100,570,238],
             ['排序','末端原因','S','O','D','RPN','赋分依据（模拟数据 / 经验）','筛选结果'],
             rpn_rows,66,[19,19.5,23,23,23,23,18,21],header_h=48,pad=8,
             alignments=['center','left','center','center','center','center','left','center'],
             emphasis=[c['screened'] for c in ranked])
text('筛选结果：F4、F1、F6、F2 列为主疑因；六项末端原因仍逐项确认，低 RPN 不直接判为非要因。',76,799,1450,22,RED,True)
finish(
    DATA['data_status']+'。本页不是实际完成的团队评审记录。\n'
    '方法采用FMEA的S/O/D思路，RPN为三者乘积。用于本课题的定位流程延迟风险排序，不宣称开展了完整的产品安全FMEA。采用2/4/6/8/10五档赋分；量表和300分筛选线均为本项目示例约定，不是QCC或FMEA通用阈值。\n'
    +'\n'.join(DATA['scales'].values())+'\n'
    'S按示例中的单例平均延迟映射，O按主要延迟原因在30例中的占比映射；D是根据初诊观察条件给出的经验评分。此处的检出指现有排查手段识别该原因的难度，不是故障告警能否触发，也不是Agent实施后的检出率。边界由低到高取首次满足的档位。\n'
    '样本设定为Lock 15例、RCU 15例；平台为硅片7例、FPGA 15例、ESL 8例。每例只归一个主要定位延迟原因，因此案例数可加；实际案件可能多因共存，不得强行套用这一示例分布。RPN仅决定先查什么，不替代下一页的原因与耗时确认。\n'
    '排序：'+ '；'.join(f"{c['id']}={c['S']}×{c['O']}×{c['D']}={c['rpn']}" for c in ranked)+'。\n'
    f'方法来源：ASQ FMEA {ASQ}；NASA-HDBK-8739.19-2第4章给出RPN乘积及评分方向，https://standards.nasa.gov/sites/default/files/standards/NASA/Baseline/0/nasa-nhbk-873919-2.pdf 。'
    f'QCC要因确认依据：T/CAQ 10201—2024 4.1.7，{CAQ}。',
    'RPN仅用于筛选；五档量表、阈值、分布与评分为示例设定。D越高表示现有手段越难识别。')

new_slide('2.3','根因验证','以材料复查和耗时归因展示确认过程：既确认发生过什么，也确认它对定位延迟的影响','九列确认表 · 示例数据')
rect(60,155,1480,67,PALE,radius=True)
text(f'示例数据，待实际记录替换  |  模拟30份材料，归因延迟合计 {total_delay}h；每例只归一个主要延迟原因',78,166,1444,22,RED,True)
text('示例判据：有对应的补采 / 返工记录，且该原因累计延迟占比 ≥10%。确认日期为示例日期。',78,199,1444,18.5,INK)
short_names={
    'F1':'等锁链靠人工\n跨CPU还原',
    'F2':'RCU状态与告警\n人工比对',
    'F3':'频率与计时口径\n记录缺项',
    'F4':'软硬证据缺少\n共同事件对应',
    'F5':'dump与符号\n未成套交接',
    'F6':'复现前未明确\n假设和观察点',
}
confirm_rows=[]
for c in items:
    confirm_rows.append([c['id'],short_names[c['id']],c['confirmation_content'],c['confirmation_method'],
                         c['confirmation_standard'],f"{c['n']}/{sample_n}例；{c['hours']}h\n占延迟 {c['share']:.1%}\n{c['case_range']}",
                         c['owner'],c['example_date'][5:].replace('-','/')+'\n（示例）',
                         '要因' if c['major'] else '非主要\n原因'])
confirmation_headers=['序号','末端原因','确认内容','确认方法','确认标准','确认结果','责任人','完成日期','根因确认']
review_table(60,237,[54,212,232,178,204,252,112,110,126],confirmation_headers,confirm_rows,82,
             [19,19,19,18.5,18.5,18.5,18.5,18,19.5],header_h=54,pad=6,
             alignments=['center','left','left','left','left','left','center','center','center'],
             emphasis=[c['major'] for c in items],header_size=18.5)
text(f'示例结论：F1 / F2 / F4 / F6 为主要原因，覆盖 {major_n}/30 例、{major_hours}/{total_delay}h（{major_hours/total_delay:.1%}）；F3 / F5 纳入基础检查。',76,800,1450,21,RED,True)
finish(
    DATA['data_status']+'。表中案例、耗时、记录内容与完成日期均为模拟设定；没有真实采样或复核发生。责任人仅为职责角色，未落实姓名或签字。CSV提供30行模拟分布用于检查表内数字，不是伪造问题单、日志或凭证。\n'
    '按用户指定的九列展示回顾式调查结果，方法为调查分析和资料对照，不要求重新复现罕见hang或组织大规模实验。确认对象是“造成定位延迟的流程原因”，不是宣称某种硬件故障已被证实。\n'
    +DATA['sample_basis']+'。'+DATA['time_unit']+'。一个工作日按8个工作时段小时解释。跨团队等待和复现运行在工作时段内仍计入；不把工程师工时相加充当案件经过时间。352h仅为示例中可归因的追加延迟，不能当成30例的总定位时长，也不能推出全部延迟都能消除。\n'
    '根因确认标准为：支持原因造成追加工作的一对一材料关联，加累计延迟贡献至少10%。本例假设六项都有对应材料，但F3、F5贡献各约5.1%，因此判为非主要原因；这不表示它们在其他批次或单案中不重要。主疑因经此确认转为对策对象。\n'
    '示例逐项结果：'+ '；'.join(f"{c['id']} {c['n']}例/{c['hours']}h/{c['share']:.1%}，{c['case_range']}" for c in items)+'。\n'
    'F1核对旧dump的查询记录和结单关系；F2核对连续stall及分支调整；F3核对频率/计数器/IRQ/进度与返工；F4核对软硬交接及事件对应，不能仅凭软件栈判互联坏；F5核对材料补齐前后的可检查项；F6核对每轮假设、输出和补观察点记录。确认日期10/21—10/23仅是示例时间线。\n'
    f'QCC方法出处：T/CAQ 10201—2024 4.1.7，{CAQ}。公开技术来源支持排查机制，不证明本表采样真实发生。',
    '本表为完整填表示例，数据及日期待真实材料替换；h为工作时段内经过时间，不等于人工工时或总定位时长。')

new_slide('2.4','拟定对策','将确认的主要原因转为可交付工具：查锁、分清RCU停滞、对齐软硬证据、减少无效复现','六列对策表 · 拟定目标')
rect(60,155,1480,62,PALE,radius=True)
text('基于前页示例结论拟定：用现有 dump / 日志 / 平台记录起步，Agent 调用确定性工具完成批量检查',78,175,1444,23,RED,True)
counter_headers=['根因','对策','目标','具体措施','责任人','完成时间']
counter_rows=DATA['countermeasures']
review_table(60,237,[245,205,220,510,170,130],counter_headers,counter_rows,116,
             [20,21,19.5,19,19,20],header_h=52,pad=12,header_size=21,
             alignments=['left','left','left','left','center','center'])
rect(60,768,1480,64,PALE,radius=True)
text('项目目标：常规问题平均定位 1–2 个工作日',78,777,1420,24,RED,True,lh=29)
text('同批旧案例核对初查、复核及补查的合计投入；问题单总时长仍保留平台运行、补采及协同等待。',78,809,1420,17,INK,lh=21)
finish(
    '本页为拟定对策，承接前页模拟确认结果。所有量化目标和完成时间均为示例计划，不是已完成实施或已达到的效果；第1周指实际项目启动周，责任角色须落实为真实人员。\n'
    '按用户指定的六列呈现根因、对策、目标、具体措施、责任人和完成时间。保留QCC对策应对应要因、目标可度量、措施具体的原则，不为形式增加地点或成本空列。F3频率/计时核查并入平台材料检查，F5材料配套检查并入vmcore工具入口；未列独立重点不表示可以忽略。\n'
    'F1：分析机保留原始dump，crash/drgn等工具取结构化结果，Agent基于这些结果关联源码。qspinlock tail是队尾等锁者而非owner；没有可靠owner信息时，应保留持锁者未知及缺证据项，不能硬推确定关系。mutex owner也应依现场内核版本核对。\n'
    'F2：分支初判及复核2h指必要材料可用后，完成固定检查清单与人工复核的投入时间；包含补查，不以模型生成速度代替。材料等待另计且不从总定位时长删除。GP/CPU连续日志、qsmask、GP线程、timer/IRQ线索可结合分析；单份dump不能恢复未记录的整个历史。\n'
    'F4：首份联查材料0.5工作日从受理计时，缺项必须列出。完整率定义为第一次交接已具备双方约定必需项的案件数/交接案件总数；不把列出缺项算作材料完整。以共同事件和时钟映射关联软件与硬件记录，由平台侧核实事务/IRQ结论。若硬件资料未捕获，Agent只能指出缺口。\n'
    'F6：无效重跑是因未覆盖当轮假设而不得不追加的复现轮次。排期和收益目标为拟定值，参考同类旧案例的轮次基线；配置正确、按预定观察点得到有效阴性结果的运行不算无效。测试人员审查采集范围与扰动，再在原平台运行。\n'
    '试用可以直接回看同批旧案：从分析输入和案例检索中隐藏已结单答案，交叉分配样本避免同人重复熟悉效应；固定关键检查项，核对错误/漏项和人工复核负担。这里未填写任何已测得的Agent加速倍数。平均定位时长沿用同一受理到根因证据确认口径，并对硅片、ESL、FPGA分层；不能将89.8%的延迟贡献当成可兑现的提效比例。\n'
    f'方法来源：T/CAQ 10201—2024 4.1.8，{CAQ}。工具依据：{CRASH}；{DRGN}；{RCU}。',
    '对策依据为前页示例；目标和排期为拟定值，尚无实施与效果数据。责任人列使用角色，启动时落实姓名。')

def export_csv(filename,headers,rows):
    with (OUT/filename).open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f,lineterminator='\n')
        writer.writerow(headers)
        writer.writerows(rows)

export_csv('采样明细.example.csv',
           ['数据性质','示例编号','症状','平台','主要延迟原因编号','主要延迟原因','归因延迟（工作时段小时）','情景设定（非真实记录）','原始材料'],case_rows)
export_csv('RPN评分.example.csv',
           ['数据性质','排序','末端原因','S','O','D','RPN','赋分依据','筛选结果'],
           [['模拟示例，非实测']+r for r in rpn_rows])
export_csv('根因确认.example.csv',confirmation_headers,
           [[c['id'],c['name'],c['confirmation_content'],c['confirmation_method'],c['confirmation_standard'],
             '模拟示例：'+r[5],c['owner'],c['example_date']+'（示例）','示例：'+r[8]]
            for c,r in zip(items,confirm_rows)])
export_csv('对策表.example.csv',counter_headers,
           [[r[0],r[1],'拟定目标：'+r[2],r[3],r[4],'示例排期：'+r[5]] for r in counter_rows])


pptx_path = OUT / 'Lock_RCU_QCC_AI_Agent_4slides.pptx'
prs.save(pptx_path)
preview_images[0].save(OUT/'Lock_RCU_QCC_AI_Agent_preview.pdf', save_all=True, append_images=preview_images[1:], resolution=100)
overview = Image.new('RGB',(1600,900),'#ECECEC')
for i,img in enumerate(preview_images):
    overview.paste(img.resize((792,445),Image.Resampling.LANCZOS),((i%2)*808,(i//2)*455))
overview.save(OUT/'overview.png')
(OUT/'slide-content.json').write_text(json.dumps(all_texts,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
(OUT/'layout-check.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

# Keep filled-in investigation records when regenerating the slides.
record_path = OUT/'要因确认记录表.csv'
if not record_path.exists():
    with record_path.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f, lineterminator='\n')
        writer.writerow(['案例编号（待填）','Lock/RCU','平台与镜像版本','疑因编号','疑因内容','缺口/重复工作及证据链接','追加工作及证据链接','影响区间开始','影响区间结束','可归因经过时间（小时）','人工工时（小时，单列）','重叠区间处理','确认/非要因/未确认/不适用','原处理人与平台侧复核'])
        for case in range(1,4):
            for cid,title,_ in causes:
                writer.writerow([f'样本{case}（待填）','','',cid,title,'','','','','','','','未确认',''])

check_prs=Presentation(pptx_path)
assert len(check_prs.slides)==4
assert all(len(s.notes_slide.notes_text_frame.text)>400 for s in check_prs.slides)
assert not checks, checks
print(json.dumps({'pptx':str(pptx_path),'slides':len(check_prs.slides),'layout_issues':checks,'native_shapes':sum(len(s.shapes) for s in check_prs.slides)},ensure_ascii=False))
