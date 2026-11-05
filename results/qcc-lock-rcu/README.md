# 用 AI Agent 缩短 Lock/RCU 问题平均定位时长

QCC 补充材料，共四页：2.1 根因分析、2.2 主疑因筛选、2.3 根因验证、2.4 拟定对策。目标是通过 AI/Agent 辅助条件整理、复现与采证准备、日志关联和源码检索，将常规问题的平均定位时长压缩至 1–2 个工作日。

当前为待案例确认的方案：疑因尚未通过真实问题单确认，时限与验收值是拟定目标。源码仅用于解释技术机制，实际根因需要依据问题单记录判断。

- [下载可编辑 PPT](Lock_RCU_QCC_AI_Agent_4slides.pptx)
- [查看四页 PDF 预览](Lock_RCU_QCC_AI_Agent_preview.pdf)
- [要因确认记录表](要因确认记录表.csv)

![四页总览](overview.png)

## 四页内容

### 2.1 根因分析

横向树图把定位耗时拆到四项末端疑因，并对应 Agent 的具体能力。

![2.1 根因分析](slide-1.png)

### 2.2 主疑因筛选

使用调查表提取事实，按可避免耗时安排确认顺序；全部末端原因仍逐条验证。

![2.2 主疑因筛选](slide-2.png)

### 2.3 根因验证

从已有问题单、脚本和日志回溯“缺口 → 追加工作 → 定位延迟”，留下证据链接与起止时间。

![2.3 根因验证](slide-3.png)

### 2.4 拟定对策

用 5W1H 明确每项能力的目标、措施、负责人角色、时间和使用环境。

![2.4 拟定对策](slide-4.png)

## 修改与重新生成

直接编辑 PPT，或修改 [build_deck.py](build_deck.py) 中的文字与布局后重新生成。脚本生成的 PDF/PNG 是版式预览，PPT 中的文字、框线和树图可编辑；详细方法说明与源码依据保存在 PPT 备注中。

```sh
cd results/qcc-lock-rcu
python -m venv /tmp/qcc-deck-venv
/tmp/qcc-deck-venv/bin/pip install -r requirements.txt
/tmp/qcc-deck-venv/bin/python build_deck.py
```

验证环境为 Python 3.13，使用 Noto Sans CJK 中文字体。默认读取 Linux 的 `/usr/share/fonts/opentype/noto/` 字体文件；其他系统可通过 `QCC_FONT_REGULAR`、`QCC_FONT_BOLD`、`QCC_FONT_INDEX` 和 `QCC_FONT_NAME` 指定字体。普通 TTF/OTF 文件的字体索引通常设为 `0`。

生成脚本会覆盖本目录内同名产物；已手动修改的 PPT 和已填写的 CSV 请先另存。

## 方法依据

要因确认使用调查分析，对策采用 5W1H，依据 [T/CAQ 10201—2024《质量管理小组活动准则》](https://www.panyu.gov.cn/attachment/7/7835/7835136/10324082.pdf)第 4.1.6–4.1.8 节。RPN 属于 [FMEA 风险排序方法](https://asq.org/quality-resources/fmea)，本课题以定位耗时事实判断原因的影响程度。
