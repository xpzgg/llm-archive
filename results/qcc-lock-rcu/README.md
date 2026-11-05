# 用 AI Agent 缩短 Lock/RCU 问题平均定位时长

面向内核团队的四页 QCC 材料，纳入 ESL/FPGA 平台的低频、计时、互联、中断和软芯协同排查。目标是减少翻栈、补材料、软硬证据对照和无效复现花费的时间，将常规问题的平均定位时长压缩至 1–2 个工作日。

六项疑因待实际案例确认；工具安排与验收要求为拟定方案，尚未声称 Agent 已实现提效。

- [下载可编辑 PPT](Lock_RCU_QCC_AI_Agent_4slides.pptx)
- [查看四页 PDF](Lock_RCU_QCC_AI_Agent_preview.pdf)
- [要因确认记录表](要因确认记录表.csv)
- [工具输入与研究依据](输入与依据.md) · [可填写的输入模板](agent-inputs.example.json)

![四页总览](overview.png)

## 2.1 根因分析

横向树图覆盖六项疑因：跨 CPU 查等锁链、判断 RCU 停滞分支、核对平台时序、对齐软硬证据、补齐现场材料和减少无效复现。

![2.1 根因分析](slide-1.png)

## 2.2 主疑因筛选

逐项核查实际耗时，并列出 Agent 需要的输入与可做的工作。工具实现可先从旧 vmcore 与材料检查起步，要因排名仍以案例事实为准。

![2.2 主疑因筛选](slide-2.png)

## 2.3 根因验证

使用已有 dump、日志、脚本和交接记录确认延迟来源；另用固定清单回放旧 vmcore，比较分析加复核的实际工时。

![2.3 根因验证](slide-3.png)

## 2.4 拟定对策

5W1H 对应现场包检查、vmcore 批量分析、平台联查和定向复现四项工作，写明工具输入、结果及责任角色。

![2.4 拟定对策](slide-4.png)

## 修改与重新生成

直接编辑 PPT，或修改 [build_deck.py](build_deck.py) 中的文字与布局。PPT 中的文字、框线和树图可编辑；PDF/PNG 是脚本生成的版式预览。详细排查边界、验证方法和来源在 PPT 备注及[研究依据](输入与依据.md)中。

```sh
cd results/qcc-lock-rcu
python -m venv /tmp/qcc-deck-venv
/tmp/qcc-deck-venv/bin/pip install -r requirements.txt
/tmp/qcc-deck-venv/bin/python build_deck.py
```

验证环境为 Python 3.13，使用 Noto Sans CJK。默认读取 Linux 的 `/usr/share/fonts/opentype/noto/` 字体文件；其他系统可用 `QCC_FONT_REGULAR`、`QCC_FONT_BOLD`、`QCC_FONT_INDEX`、`QCC_FONT_NAME` 指定字体，普通 TTF/OTF 的索引通常为 `0`。

脚本会覆盖 PPT、PDF、图片和生成内容 JSON；手工修改的 PPT 请先另存。已有要因确认 CSV 会保留，输入模板也不会被生成脚本覆盖。
