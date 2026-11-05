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
prs.core_properties.subject = '2.1 原因分析；2.2 主疑因筛选；2.3 根因验证；2.4 拟定对策'
prs.core_properties.author = 'QCC 项目组'
prs.core_properties.keywords = 'QCC, Lock, RCU, AI, Agent, 要因确认, 5W1H'
prs.core_properties.comments = '四页补充材料。疑因与目标均标明状态，未填入虚构案例数据。'
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

def table(x, y, widths, headers, rows, row_h, sizes=None):
    total = sum(widths)
    header_h = 52
    rect(x,y,total,header_h,NAVY)
    xx=x
    for head,ww in zip(headers,widths):
        text(head,xx+16,y+14,ww-30,20,WHITE,True)
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

# 2.1: horizontal, editable native tree.
new_slide('2.1', '根因分析', '围绕“复现耗时”继续追问，拆到 AI / Agent 可改善、可验证的末端原因', '横向树图 · 疑因假设')
text('定位问题',60,161,205,18,MUTED,True)
text('耗时环节',330,161,235,18,MUTED,True)
text('末端疑因（待案例确认）',625,161,525,18,MUTED,True)
text('Agent 对应能力',1215,161,325,18,MUTED,True)
line(265,427,300,427,BLUE,3)
line(300,280,300,593,BLUE,3)
for yy in (280,475,593): line(300,yy,330,yy,BLUE,3)
line(565,280,595,280,BLUE,2)
line(595,245,595,360,BLUE,2)
line(595,245,625,245,BLUE,2)
line(595,360,625,360,BLUE,2)
for yy in (475,593): line(565,yy,625,yy,BLUE,2)
for yy in (245,360,475,593): line(1150,yy,1215,yy,TEAL,2)
rect(60,357,205,140,NAVY,radius=True)
text('Lock / RCU',76,382,173,25,WHITE,True,'center')
text('定位时间长',76,428,173,26,WHITE,True,'center')
rect(330,230,235,100,PALE,radius=True)
text('复现准备与等待',344,248,207,25,BLUE,True,'center')
text('现状初步指向',344,290,207,18,MUTED,align='center')
rect(330,430,235,90,PALE,radius=True)
text('复现后追加采证',344,457,207,24,BLUE,True,'center')
rect(330,548,235,90,PALE,radius=True)
text('分析方向收敛慢',344,575,207,24,BLUE,True,'center')
leaves=[
    (200,'R1  触发条件未结构化','环境、负载、并发参数与操作顺序反复补齐','整理触发条件\n一次性补充问题'),
    (315,'R2  复现与采证准备重复','脚本、观察点与预期结果逐单重新梳理','辅助生成脚本\n明确每轮观察目标'),
    (430,'R3  首轮材料缺关键上下文','完整告警、相关 CPU / 任务栈需后续补采','整理现场证据\n识别缺口与关联线索'),
    (548,'R4  源码与案例关联依赖手工','从等待栈到责任路径、相似案例需多轮检索','检索代码与案例\n给出有依据的候选'),
]
for yy,head,body,agent in leaves:
    rect(625,yy,525,90,WHITE,LINE,True)
    rect(625,yy,5,90,BLUE)
    text(head,644,yy+12,488,24,INK,True)
    text(body,644,yy+52,488,18,MUTED)
    rect(1215,yy,325,90,'E6F4F2',radius=True)
    text(agent,1231,yy+14,293,21,TEAL,True,lh=30)
rect(60,690,1480,129,'EDF2F8',radius=True)
text('源码解释的技术难点',80,706,380,19,NAVY,True)
text('并发时序敏感\n多 CPU、任务与中断上下文相互作用',80,744,460,19,INK,lh=29)
text('等待位置仍需向外追踪\n锁慢路径需要继续关联持有侧与调用者',580,744,455,19,INK,lh=29)
text('RCU 告警可能对应多种成因\n需结合多次堆栈、宽限期与调度线索',1080,744,435,19,INK,lh=29)
finish(
    '说明：本页是待验证的流程疑因树，不是已经证明的组织根因。用户提供的症结仅为“初步看是问题复现”。AI 是对策手段，“缺少 AI”本身不作为根因。\n'
    '源码只说明技术机制，不把当前仓库配置当作故障现场证据。\n'
    '代码依据（工作区 HEAD c2b0dd453961）：\n'
    '1. Documentation/locking/lockdep-design.rst:282-304，讨论多 CPU、多任务、中断上下文及罕见时序组合。\n'
    '2. kernel/locking/qspinlock.c:328-421，queued_spin_lock_slowpath 的竞争与等待路径；include/asm-generic/qspinlock_types.h:14-43 定义 locked、pending、tail，不能将 tail CPU 直接等同持锁者。\n'
    '3. kernel/locking/mutex.c:570 起，__mutex_lock_common 包含尝试加锁、自旋与等待路径。等待栈本身不能直接确定故障责任。\n'
    '4. kernel/rcu/tree_stall.h:528、590、629、656，区分报告 stall 的 CPU、停滞 CPU/任务，以及 GP 线程饥饿等线索。\n'
    '5. Documentation/RCU/stallwarn.rst:16-129、262-278、346-359，解释多种告警原因、连续栈比较及 detected by 的含义。\n'
    f'QCC 方法依据：T/CAQ 10201—2024 第 4.1.6、4.1.7 节，{CAQ}',
    '机制依据：locking / RCU 源码与配套文档；流程疑因须以问题单事实确认。详细来源见备注。')

# 2.2: measurable screening, with no fictional scores.
new_slide('2.2','主疑因筛选','按对定位耗时的实际贡献安排确认顺序，四项末端原因均逐条验证','调查表 + 耗时排列')
rect(60,159,1480,89,NAVY,radius=True)
text('先核实复现链路 R1 / R2',82,176,540,27,WHITE,True)
text('依据：现状初判“复现耗时”\n顺序可随案例数据调整，不提前宣布要因',790,174,720,21,'D9E7FB',lh=30)
rows=[
    ['R1','触发条件未结构化','对照首次提交与后续补充：\n是否补问了影响触发的关键条件？','记录补问轮次、\n条件澄清带来的额外等待'],
    ['R2','复现与采证准备重复','对照首版与有效脚本 / 采证方案：\n改了什么？为何需要重跑？','记录准备工时、\n因方案缺口增加的重跑等待'],
    ['R3','首轮材料缺关键上下文','定位最终使用的关键日志 / 栈：\n首轮是否已有？何时补齐？','记录补采次数、\n证据后补造成的额外等待'],
    ['R4','源码与案例关联依赖手工','回查关键线索首次出现的时间：\n何时关联到受证据支持的路径？','记录检索与研判工时；\n注明是否依赖新的现场信息'],
]
table(60,278,[95,330,620,435],['编号','末端疑因','从问题单提取的事实','影响程度记录'],rows,94,[22,22,21,21])
rect(60,730,723,92,'E6F4F2',radius=True)
text('排序依据',78,742,660,20,TEAL,True)
text('汇总各疑因引入的可避免耗时，再绘制耗时排列图。',78,777,679,20,INK)
rect(805,730,735,92,PALE_AMBER,radius=True)
text('统计口径',823,742,680,20,AMBER,True)
text('机器运行、资源排队单列；重叠时间只归属一项。',823,777,690,20,INK)
finish(
    '为什么不用 RPN：RPN 常见于 FMEA，用严重度、发生度、探测度进行风险优先级排序。本课题关注已发生的定位周期，应通过事实和数据确认末端原因对耗时症结的影响。RPN 评分或投票不能代替要因确认。\n'
    '筛选仅表示确认优先顺序，所有末端原因仍逐条确认。本页不填虚构的 RPN 分数、耗时占比或排列图柱高。\n'
    '首轮建议：选最近 5 个已结单的常规案例，覆盖 Lock 和 RCU，保留样本的选取范围；用于检查数据和流程，不用于声称统计显著性。后续效果评价沿用稳定的案例范围。\n'
    '记录定位时间线中的事件和起止时间。不同原因的同一时间段只计一次；无法合理区分的区间保留未归因，不强行分配。分别记录工程师工时和案件经过时间，两者不能相加。\n'
    'R1/R2 暂列优先是用户现状初判的工作假设；最终排序按实际可避免耗时调整。问题单分布 Lock/RCU 230、内存 150、异构 157、网络 262 仅提供业务规模，不证明根因或耗时排序。\n'
    f'方法来源：T/CAQ 10201—2024 4.1.7，{CAQ}\nASQ FMEA：{ASQ}',
    '方法：T/CAQ 10201—2024，4.1.7；RPN 用于 FMEA 风险排序，本页使用定位耗时事实。')

# 2.3: executable retrospective checks, no new live hang required.
new_slide('2.3','根因验证','用已有问题单、脚本和日志回溯“缺口 → 追加工作 → 定位延迟”','要因确认 · 待回填')
rect(60,158,1480,66,PALE,radius=True)
text('首轮建议：最近 5 个已结单常规案例，覆盖 Lock / RCU；由原处理人每单回顾 20–30 分钟。',80,179,1440,22,BLUE,True)
rows=[
    ['R1\n触发条件未结构化','翻首次工单与补充回复；圈出后补的关键条件，\n核实该条件是否阻断了复现准备或有效尝试。','留存：前后记录链接、补问轮次、起止时间。\n确认点：条件缺失确实造成追加工作和等待。'],
    ['R2\n复现与采证准备重复','比较首版与有效脚本 / 采证方案；标出新增的\n触发参数、观察点，以及由这些缺口造成的重跑。','留存：脚本差异、执行记录、额外轮次和耗时。\n确认点：准备缺口造成重复执行或无效采证。'],
    ['R3\n首轮材料缺关键上下文','找到最终定位依赖的关键日志 / 栈；回查它\n是否在首轮缺失，以及补采后推进了哪一步。','留存：关键证据、首轮材料清单、补采时间。\n确认点：后补证据是当时继续分析的必要条件。'],
    ['R4\n源码与案例关联依赖手工','把首次可用材料与结单依据并排核对；查明\n关键线索是否早已具备、何时才关联到代码路径。','留存：日志位置、源码路径、首次关联时间。\n确认点：延迟发生在检索 / 关联，而非新增取证。'],
]
table(60,252,[275,625,580],['疑因','怎么核实：直接使用留存材料','留下什么证据 / 如何判断'],rows,114,[22,21,20])
rect(60,784,1480,43,'E6F4F2',radius=True)
text('要因判定：证据链成立且对症结耗时贡献居前；证据不足则保留“未确认”，结论回填后再定正式对策。',79,795,1442,19,TEAL,True)
finish(
    '本页是一张可立即执行的要因确认表，尚无真实案例数据，不能写成“已验证通过”。\n'
    '实施方式：原处理人打开问题单及当时保存的日志、脚本和补丁记录，在记录表中填证据链接、起止时间和结论。单个案例建议 20–30 分钟，5 个案例先完成一轮可行性核查。样本少时仅做初步确认。\n'
    '不要求重新复现罕见 hang，不安排大规模环境复建，不以关闭/开启调试开关进行无依据的实验。\n'
    'R1：只记录影响复现准备或有效尝试的关键遗漏，普通沟通不自动算作根因。\n'
    'R2：仅归因于可查明的脚本或观测方案缺口，低概率自然触发的等待单独记录。\n'
    'R3：需说明补齐哪个证据才使分析得以推进；不能将所有日志缺失都认定为要因。\n'
    'R4：结单材料用于回查线索何时存在；如果必须依赖后来才出现的运行时信息，不把那段等待归为检索效率问题。\n'
    '判定采用调查分析法：缺口真实存在，导致追加工作，有记录支持定位延迟；然后按影响程度确定主要原因。并不把 AI 回放速度当作根因存在的证明。\n'
    f'QCC 依据：T/CAQ 10201—2024 4.1.7，允许现场测量、试验或调查分析，{CAQ}',
    '确认方法：调查分析。20–30 分钟与 5 个案例是首轮执行建议；所有结果以留存记录为准。')

# 2.4: all six 5W1H fields, four capabilities in one agent workflow.
new_slide('2.4','拟定对策','一个定位 Agent 串联四项能力，由领域工程师复核证据与下一步动作','5W1H · 拟定方案')
rect(60,155,1480,64,NAVY,radius=True)
text('改进链路：条件整理  →  复现与采证准备  →  现场证据关联  →  源码 / 案例检索',80,175,1440,24,WHITE,True)
rows=[
    ['R1 条件整理\n结构化输入','必填项全部填写\n或明确标缺','提取环境、负载、顺序与并发参数；\n生成一轮补充问题，工程师确认。','领域工程师\nAgent 开发','第 1 周','工单入口\n分析工作区'],
    ['R2 复现与采证\n辅助生成脚本','首版脚本准备\n≤ 0.5 工作日','复用模板生成参数化脚本；写明观察点、\n预期结果与退出条件，审查后测试机试跑。','领域工程师\nAgent 开发','第 2 周','测试机\n脚本库'],
    ['R3 证据关联\n整理日志与栈','关键判断 100%\n引用原始证据','归并多 CPU、多次告警；关联稳定栈、\n锁等待与 GP 线索，列出缺证及补采项。','领域工程师\nAgent 开发','第 2–3 周','日志 / 转储\n分析工作区'],
    ['R4 代码与案例\n生成候选原因','≤ 1 小时输出\n首轮候选与动作','按版本检索调用路径与历史案例；\n每条候选附日志 / 代码依据及反证。','领域工程师\n案例库维护','第 3 周','版本化源码\n案例库'],
]
table(60,249,[238,227,465,190,140,220],['对策 What','目标 Why','措施 How','负责人 Who','时间 When','地点 Where'],rows,105,[21,21,20,20,20,20])
rect(60,742,1480,82,'E6F4F2',radius=True)
text('第 4 周试用验收：常规问题平均定位时长目标 1–2 个工作日',80,754,1440,25,TEAL,True)
text('统一口径：受理 → 根因证据经工程师确认，包含复现与协同等待；同时记录正确性、返工次数与人工复核工时。',80,793,1440,18,INK)
finish(
    '本页是拟定方案。四项能力均对应待确认的末端原因；依据 2.3 的结论确定纳入正式执行的对策，不将未确认疑因直接写成已证实根因。\n'
    '按 T/CAQ 10201—2024 4.1.8 使用 5W1H：What 对策、Why 目标、Who 负责人、Where 地点、When 时间、How 措施。5W2H 可额外加入 How much 费用，但不是本页必须项。\n'
    '负责人为拟定角色，实际实施时替换成姓名。第 1 周从项目启动算起。所有时限、完整率和 1–2 日均是拟定目标，不是已经实现的效果。\n'
    'Agent 做辅助分析和产物生成：给出可追溯依据、反证与下一步；工程师确认脚本及证据后再执行。锁等待路径、RCU 告警类型、检测 CPU 与停滞 CPU 都需要按实际材料区分。\n'
    '对策效果检查的最小做法：保留 2.3 的案例材料副本，隐藏结论与最终修复；先用人工流程或历史可靠记录建立可比基线，再计时 Agent 生成 + 人工审查 + 修正的完整工时。用于检索的案例库剔除当前验收案例的答案。若比较两种流程，安排不同案例或处理人交叉使用，避免同一人知道答案后的熟悉效应。\n'
    '脚本类产物先检查语法、参数和短时正常运行，无需为验收而强制触发真实 hang。只有实际现场的新问题试用，才能评估受理至根因确认的端到端工作日目标。离线回放分钟数不能换算成端到端定位日数。\n'
    '常规问题口径在改善前后保持一致，保留长耗时与未结单个案；实际指标建议同时报告均值、样本量、目标内完成比例及未结单数量。当前未提供现状平均时长，因此不填写缩短百分比。\n'
    f'方法来源：T/CAQ 10201—2024 4.1.8—4.1.10，{CAQ}',
    '时限、角色与验收值为拟定目标；完成要因确认后执行。W1 从项目启动日起算。')

pptx_path = OUT / 'Lock_RCU_QCC_AI_Agent_4slides.pptx'
prs.save(pptx_path)
preview_images[0].save(OUT/'Lock_RCU_QCC_AI_Agent_preview.pdf', save_all=True, append_images=preview_images[1:], resolution=100)
overview = Image.new('RGB',(1600,900),'#E3EAF2')
for i,img in enumerate(preview_images):
    overview.paste(img.resize((792,445),Image.Resampling.LANCZOS),((i%2)*808,(i//2)*455))
overview.save(OUT/'overview.png')
(OUT/'slide-content.json').write_text(json.dumps(all_texts,ensure_ascii=False,indent=2),encoding='utf-8')
(OUT/'layout-check.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
with (OUT/'要因确认记录表.csv').open('w',encoding='utf-8-sig',newline='') as f:
    writer=csv.writer(f, lineterminator='\n')
    writer.writerow(['案例编号（待填）','Lock或RCU','疑因编号','缺口事实及记录链接','追加工作及记录链接','影响区间开始','影响区间结束','可归因经过时间（小时）','人工工时（小时，单列）','与其他项有无重叠','确认/非要因/证据不足','原处理人复核'])
    for case in range(1,6):
        for cause in range(1,5):
            writer.writerow([f'样本{case}（待填）','',f'R{cause}','','','','','','','','待确认',''])
check_prs=Presentation(pptx_path)
assert len(check_prs.slides)==4
assert all(len(s.notes_slide.notes_text_frame.text)>400 for s in check_prs.slides)
assert not checks, checks
print(json.dumps({'pptx':str(pptx_path),'slides':len(check_prs.slides),'layout_issues':checks,'native_shapes':sum(len(s.shapes) for s in check_prs.slides)},ensure_ascii=False))
