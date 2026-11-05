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
BG, NAVY, INK, MUTED = 'F7F9FC', '122B48', '20354B', '64748B'
BLUE, TEAL, PALE, WHITE = '245FDB', '087F8C', 'EAF1FF', 'FFFFFF'
LINE, AMBER, PALE_AMBER = 'D8E2EE', '9B5A12', 'FFF4DE'
CAQ = 'https://www.panyu.gov.cn/attachment/7/7835/7835136/10324082.pdf'
ASQ = 'https://asq.org/quality-resources/fmea'

prs = Presentation()
prs.slide_width, prs.slide_height = Inches(16), Inches(9)
prs.core_properties.title = '用 AI Agent 缩短 Lock/RCU 问题平均定位时长｜QCC 四页'
prs.core_properties.subject = 'Lock/RCU 软件与平台排查；vmcore 分析；QCC 要因确认与对策'
prs.core_properties.author = 'QCC 项目组'
prs.core_properties.keywords = 'QCC, Lock, RCU, AI, Agent, 要因确认, 5W1H'
prs.core_properties.comments = '面向内核团队及 ESL/FPGA 软硬协同。流程疑因待案例确认；未填虚构效果数据。'
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
    rect(0, 0, 1600, 11, NAVY)
    text(num, 60, 40, 100, 32, BLUE, True)
    text(title, 170, 38, 1100, 34, NAVY, True)
    text(subtitle, 60, 104, 1480, 22, MUTED)
    rect(1280, 39, 260, 44, PALE, radius=True)
    text(badge, 1292, 47, 236, 18, BLUE, True, 'center')

def finish(notes, footer):
    line(60, 842, 1540, 842)
    text(footer, 60, 855, 1380, 15, MUTED)
    text(f'{len(prs.slides)} / 4', 1460, 855, 80, 15, MUTED, align='right')
    slide.notes_slide.notes_text_frame.text = notes
    out = OUT / f'slide-{len(prs.slides)}.png'
    canvas.resize((W,H), Image.Resampling.LANCZOS).save(out)
    preview_images.append(canvas.resize((W,H), Image.Resampling.LANCZOS))

def table(x, y, widths, headers, rows, row_h, sizes=None, header_size=20):
    total = sum(widths)
    header_h = 52
    rect(x,y,total,header_h,NAVY)
    xx=x
    for head,ww in zip(headers,widths):
        text(head,xx+16,y+14,ww-30,header_size,WHITE,True)
        xx+=ww
    yy=y+header_h
    for ri,row in enumerate(rows):
        rect(x,yy,total,row_h,WHITE if ri%2==0 else 'EFF4FA')
        xx=x
        for ci,(value,ww) in enumerate(zip(row,widths)):
            sz=sizes[ci] if sizes else 21
            used=text(value,xx+16,yy+15,ww-30,sz,BLUE if ci==0 else INK,ci==0,lh=sz*1.42)
            if used > row_h-22:
                checks.append({'kind':'table_cell_height', 'row':ri, 'col':ci, 'used':used, 'available':row_h-22})
            xx+=ww
        yy+=row_h
        line(x,yy,x+total,yy,LINE,1)
    return yy


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
line(260,470,285,470,BLUE,3)
line(285,290,285,650,BLUE,3)
for cy in (290,470,650):
    line(285,cy,320,cy,BLUE,3)
    line(550,cy,580,cy,BLUE,2)
    line(580,cy-45,580,cy+45,BLUE,2)
    line(580,cy-45,610,cy-45,BLUE,2)
    line(580,cy+45,610,cy+45,BLUE,2)
rect(60,411,200,118,NAVY,radius=True)
text('Lock / RCU',76,431,168,26,WHITE,True,'center')
text('定位周期长',76,477,168,26,WHITE,True,'center')
groups=[(237,'等待关系难还原','内核机制'),(417,'软硬件原因难分','ESL / FPGA'),(597,'补采与复现反复','日常排查')]
for yy,head,sub in groups:
    rect(320,yy,230,106,PALE,radius=True)
    text(head,332,yy+19,206,25,BLUE,True,'center')
    text(sub,332,yy+64,206,18,MUTED,align='center')
causes=[
    ('F1','跨 CPU 查等锁链，靠人工逐个还原','多核同卡慢路径；qspinlock 需结合锁地址、其他 CPU 栈与 MCS 节点追查'),
    ('F2','RCU 卡在哪一步，需要反复比对','同一类 stall 可能来自缺 QS、GP 线程饥饿或 timer 唤醒失败'),
    ('F3','低频、计时异常与真停滞混在一起','FPGA 主频/访存慢；ESL 计时模式各异，要结合进度、计时器和 IRQ 判断'),
    ('F4','互联或中断异常，软硬证据对不上','互联响应、IRQ 投递可能异常；串口、硬件 trace、仿真事件要对到同一现场'),
    ('F5','拿到现场后，材料仍不够用','vmcore、同版符号、全 CPU 栈与固件/镜像未成套，补采后才能继续'),
    ('F6','复现一轮很久，却没拿到要验证的证据','低性能平台启动和触发慢；新增 printk / trace 又可能改变负载或时序'),
]
for yy,(cid,head,body) in zip((205,295,385,475,565,655),causes):
    rect(610,yy,930,80,WHITE,LINE,True)
    rect(610,yy,5,80,TEAL if cid in ('F3','F4') else BLUE)
    text(cid+'  '+head,630,yy+10,888,24,INK,True)
    text(body,630,yy+49,888,19,MUTED)
rect(60,768,1480,60,'E6F4F2',radius=True)
text('改进重点：先利用已有现场减少盲查和补采，把昂贵的下一轮复现用于验证明确的假设。',80,787,1440,23,TEAL,True)
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

# 2.2: distinguish measured cause priority from tool implementation order.
new_slide('2.2','主疑因筛选','逐项核查补材料、人工分析和重复复现花了多久，再按可避免耗时排先后','调查表 · 按耗时排序')
rect(60,155,1480,64,NAVY,radius=True)
text('先把时间拆开：取证与交接等待  /  人工分析工时  /  平台运行与复现等待',80,175,1440,24,WHITE,True)
rows=[
    ['F1 跨 CPU 查锁','查了多少 CPU / 任务、跑了几轮命令？\n哪些时间花在重复翻栈、查锁对象？','vmcore + 同版符号 → crash / drgn 批量查栈和锁；\n可先封装，保留命令、地址和原始输出。'],
    ['F2 判断 RCU 分支','多少次告警被反复比对？\n是否把报告 CPU 当成停滞 CPU 追查？','多次 stall + vmcore / RCU trace → 按 GP、CPU\n比对 QS、GP 线程和 timer 线索。'],
    ['F3 核对平台时序','换频率、调超时、重新跑了几轮？\n何时才查清当时的频率与计时口径？','CPU/内存/互联频率 + 计时模式 + IRQ/进度记录\n→ 区分执行慢、时钟/中断异常与证据不足。'],
    ['F4 对齐软硬证据','等平台日志、补寄存器和来回沟通多久？\n双方记录能否定位同一次异常？','串口 + 互联/仿真 trace + 寄存器快照\n→ 按共同事件对齐，列出需芯片侧确认的问题。'],
    ['F5 补齐现场材料','是否重新索取符号、镜像或 dump？\n关键对象不可读是否造成再次取证？','vmcore + vmlinux/模块符号 + 镜像清单\n→ 校验版本、检查关键对象可读性，列缺项。'],
    ['F6 重复复现','每轮想排除什么、实际新增了什么证据？\n哪些运行因观察点不够而重做？','旧脚本 + 当前假设 + 平台限制\n→ 选观察点、写预期现象与停止条件。'],
]
table(60,234,[258,570,652],['疑因','从原问题单核查什么','Agent 所需输入与可做的工作'],rows,80,[21,20,20])
rect(60,780,1480,48,'E6F4F2',radius=True)
text('工具可从离线 vmcore 分析和材料检查起步；要因仍按实际耗时排序，全部六项逐条核查。',80,794,1440,21,TEAL,True)
finish(
    '筛选采用调查表与耗时分层，数据齐后再画排列图。此版不填写虚构的耗时占比、RPN 分数或排名。F1/F2/F5 的工具可先做，是因为可用旧材料离线试用，并不代表它们已经被证明是主要原因。\n'
    '总体案件时长从受理到根因证据经工程师确认。工程师主动操作时间与案件经过时间分别记录，不能直接相加；重叠的等待区间只归属一项，无法区分的保留未归因。平台不可压缩的运行、排队和跨团队等待仍包含在总时长中，另列构成用于辨别改进空间。\n'
    'crash 的 bt -a 可取各 CPU 活跃任务栈，foreach bt 可批量取任务栈，runq 用于观察调度队列。drgn 提供 mutex_owner 等对象查询；RCU 与 qspinlock 的细查应按内核版本封装，不能假定存在一个通用命令能证明所有锁根因。\n'
    'Agent 的收益是减少重复操作、整理关系和提出下一步取证，而不是绕过 dump 缺失或硬件观测限制。无 dump 时使用现有全 CPU 栈/日志，并明确可完成的检查范围。\n'
    f'工具依据：{CRASH}；https://crash-utility.github.io/help_pages/foreach.html；https://crash-utility.github.io/help_pages/runq.html；{DRGN}。\n'
    f'QCC 方法：{CAQ}，4.1.7。RPN 属于 FMEA 风险排序（{ASQ}），不能代替此处的耗时事实确认。',
    '筛选依据：案例事实与耗时。工具实现顺序不等同于要因排名；具体字段与版本要求见备注及输入清单。')

# 2.3: retrospective confirmation, with a small separate dump-tool trial.
new_slide('2.3','根因验证','直接使用留存的 dump、日志、脚本和交接记录，查明哪些环节确实造成了延迟','旧案例核查 · 待实测')
rect(60,155,1480,64,PALE,radius=True)
text('首轮建议：锁等待、RCU stall、ESL / FPGA 问题各取 1 个已结单案例；未覆盖的疑因保留待确认。',80,176,1440,22,BLUE,True)
rows=[
    ['F1 跨 CPU 查锁','回看旧 dump 的命令记录；对照结单依据，\n标出找等锁者、锁地址和相关 CPU 的步骤。','确认重复操作确实拖慢分析；\n记录人工工时及错误 / 漏查项。'],
    ['F2 判断 RCU 分支','按同一 GP、CPU 比较连续告警或 trace；\n回查何时才分清缺 QS、GP 饥饿或 timer 问题。','记录分支判断的返工次数；\n补齐哪条证据后，分析方向才改变。'],
    ['F3 核对平台时序','用已有运行记录核对镜像、负载、频率、\n计时模式、IRQ 与进度；比较当时的判断。','确认平台差异是否导致误判 / 返工；\n单凭调大超时后不报警，不能确认。'],
    ['F4 对齐软硬证据','按共同事件对齐串口与现有平台记录；\n请芯片侧核实异常事务 / IRQ 是否成立。','记录补证与交接等待；\n无对应硬件证据，保留未确认。'],
    ['F5 补齐现场材料','实际加载旧 dump，核对符号和关键对象；\n回查补齐前后，哪一步检查才得以继续。','留存加载结果与补材料记录；\n确认是否造成额外等待或重新取证。'],
    ['F6 重复复现','比较每轮脚本、观察点和得到的日志；\n找出没有回答当前疑问、因而重跑的轮次。','记录无效轮次及运行时间；\n找出后来增加的有效观察点。'],
]
table(60,234,[280,650,550],['疑因','用现有材料怎么核实','确认依据与记录'],rows,80,[21,20,20])
rect(60,778,1480,52,'E6F4F2',radius=True)
text('另做 vmcore 小试：隐藏结单答案，人工与 Agent 按同一清单检查；核对结果，并计入加载、分析、复核与补查工时。',79,795,1440,19,TEAL,True)
finish(
    '要因确认采用调查分析，以“该问题确实出现—造成了哪些追加工作—耗时多大”为判定链。排序依据来自实际影响时间；本页不宣称已验证六项疑因，也不要求为填表重新触发罕见 hang。\n'
    '先找三个现有已结单案例用于检查可行性，并逐项标记 F1—F6 是否有证据。锁等待和 RCU 案例优先选有旧 vmcore 的；平台案例使用已有日志/仿真记录。缺少某类资料就写未确认，不能用另一类症状替代。三个样本也不足以证明总体效率提升。\n'
    'F3 对照时，保留镜像、负载、频率和时钟来源；不同版本或多个变量同时改变只能提供线索。若已有同负载不同频率的运行资料，可以比较进度和相关告警；不强制组织一轮新的频率扫描。\n'
    'F4 硬件判断由相应平台/芯片责任人核实事务、IRQ、寄存器或仿真记录。没有硬件证据不能仅因软件卡在锁或 MMIO 路径就判互联故障。\n'
    'vmcore 对策试用与根因确认分开记录：固定输出清单为各 CPU 栈、相关任务/锁地址与等待关系、RCU GP/CPU 状态、未能完成的检查。隐藏该案结单结论及修复补丁，并从案例检索库排除对应答案。由不了解结论的工程师分别处理不同样本，或交叉分配样本，避免同人二次分析的熟悉效应。\n'
    '比较端到端人工投入，包含载入、命令执行、Agent 调用、人工复核和补查；同时记录墙钟耗时、错误项和遗漏项。由原处理人对照结单证据核验。只记录实测数值，不事先写“快几倍”或把纯模型输出时间当分析耗时。\n'
    '一个 dump 是有限时刻的内存/寄存器现场，通常不能恢复之前所有事件或未记录的互联事务；这是一项基于 dump 数据性质的分析边界，所需时间序列或硬件证据须另补。\n'
    f'方法：T/CAQ 10201—2024 4.1.7、4.1.9、4.1.10，{CAQ}；dump 数据性质参考 https://docs.kernel.org/admin-guide/kdump/kdump.html。',
    '根因确认看证据及耗时贡献；Agent 是否更快另做小试，以核对后的实测结果为准。')

# 2.4: four scoped tools, each tied to causes and concrete inputs.
new_slide('2.4','拟定对策','先把工程师每天重复做的检查交给工具，再让 Agent 根据结果追问和查代码','5W1H · 拟定对策')
rect(60,155,1480,64,NAVY,radius=True)
text('先做两件事：材料能不能用  /  用旧 vmcore 把 CPU、锁与 RCU 状态一次查齐',80,175,1440,24,WHITE,True)
rows=[
    ['F5 现场包检查','必查项逐项有结果\n缺项一次列清','vmcore + 同版 vmlinux / 模块符号\n+ 镜像清单 → 核对版本和关键对象\n可读性，输出缺项及影响。','内核分析\n工具开发','第 1 周','离线分析机'],
    ['F1 / F2\nvmcore 批量分析','旧案例关键项无错漏\n初查 + 复核工时\n逐例对比','按内核版本封装 crash / drgn：全 CPU\n栈、mutex owner、qspinlock 队列、\nRCU 状态；保留命令、地址和输出。','内核分析\n工具开发','第 2 周','离线分析机'],
    ['F3 / F4\n平台联查','列清可疑 CPU / 地址\n及待核实的事务 / IRQ\n每项注明证据出处','频率 / 计时模式 + IRQ / 进度记录\n+ 串口 / 硬件 trace → 对齐同一事件，\n生成给芯片侧的具体核查项。','内核分析\n芯片 / 平台','第 3 周','原平台记录\n离线分析机'],
    ['F6 定向复现','每轮验证 1 个假设\n有预期现象\n与停止条件','现有脚本 + 当前假设 → 查代码选\n最小观察点，限定采集范围和时长；\n测试人员审查后在原平台执行。','内核分析\n测试 / 平台','第 3–4 周','ESL / FPGA\n或原故障平台'],
]
table(60,249,[238,235,470,180,132,225],['对策 What','目标 Why','措施 How','负责人 Who','时间 When','地点 Where'],rows,109,[20,20,19,20,20,20],header_size=18)
rect(60,755,1480,73,'E6F4F2',radius=True)
text('验收目标：常规问题平均定位 1–2 个工作日',80,767,1440,25,TEAL,True)
text('受理到根因证据确认；分析工时、平台运行与协同等待分别记录，ESL / FPGA 按同一范围分层比较。',80,805,1440,18,INK)
finish(
    '5W1H 表为拟定安排：负责人先填角色，启动时落实姓名；第 1 周起点是项目启动日。通过要因确认的项目进入正式对策，未确认项先保留调查。1—2 工作日是用户既定目标，不是已经达到的结果。\n'
    '现场包检查需核对实际 dump 与符号/模块构建，不以当前工作区配置推断现场。dump 经过过滤并不等于不可用，重点判断本次诊断必需对象是否可读取。\n'
    'vmcore 是最适合先试的工具方向之一：基础命令结果交给确定性的解析器，Agent 用这些结果选择下一条查询、关联源码和解释证据。原始 dump 保留在分析机，模型接收有地址和上下文的查询结果，不把整个内存镜像塞入上下文。\n'
    '锁分析：mutex 可读 owner；qspinlock 的 tail/MCS 是等锁队列，不能直接给出持锁者。若故障内核有可靠调试 owner 信息则优先用它；没有时结合调用路径和全 CPU 栈缩小范围。读取失败、节点复用或过渡状态必须保留，不能补造确定关系。\n'
    'RCU 分析可借鉴本仓库 scripts/parse_rcu_gp.py 的按 GP/CPU 整理方式，但需先验证对应内核 trace 格式与 CPU hotplug 等场景。缺少某事件只说明记录未包含该事件，不直接证明从未发生。\n'
    '平台联查由已有适配脚本读取经平台确认的寄存器/trace，Agent 负责对照、解释与提出问题。频率记录要区分 CPU/内存/互联与系统计数器，仿真/guest/host 时间需要明确映射。当前仅准备工具输入和任务要求，尚未实现或测量这些 Agent 工具。\n'
    '验收保留原有常规问题范围；等待硬件分析、平台运行和取证的时间不得为了达标而删掉。分别统计有 dump 与无 dump、ESL、FPGA、硅片环境，使用相同起止点比较平均定位时长，并列样本量、未结单数与错误/返工情况。\n'
    f'工具来源：{CRASH}；{DRGN}。参考已有 results/qspinlock_mcs_walk.md 和 scripts/parse_rcu_gp.py 时按内核版本复核。\n'
    f'QCC 对策要求：T/CAQ 10201—2024 4.1.8，{CAQ}',
    '角色、排期与指标为拟定值；工具输入清单随材料归档。实际提效以旧案例小试和后续问题单统计为准。')

pptx_path = OUT / 'Lock_RCU_QCC_AI_Agent_4slides.pptx'
prs.save(pptx_path)
preview_images[0].save(OUT/'Lock_RCU_QCC_AI_Agent_preview.pdf', save_all=True, append_images=preview_images[1:], resolution=100)
overview = Image.new('RGB',(1600,900),'#E3EAF2')
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
