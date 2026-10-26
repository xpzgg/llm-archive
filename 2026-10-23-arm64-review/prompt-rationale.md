# 提示词调整依据

来源：OpenAI, Rethinking skills and prompts for GPT-6 Astra，2026-09-11，读取于 2026-10-23。
https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra

应用：缩减强制步骤和无条件文档加载，保留任务目标、证据门槛、权限及完成条件；调查与验证按实际风险决定。相较此前清单，避免逐函数机械输出、固定调用深度和重复测试要求。

Sashiko 提供审查维度与核验思想，未运行 Sashiko 本身：
https://github.com/sashiko-dev/sashiko#multi-stage-review-pipeline
https://github.com/masoncl/review-prompts/blob/main/kernel/false-positive-guide.md

本次使用 Astra 执行特性审查，主代理汇总并核验候选结论。
