# AI Development Skills Roadmap



```
AI 开发能力学习路线
│
├── 一、Prompt Engineering（基础表达能力）
│   ├── 清晰直接表达需求
│   ├── Few-shot 示例引导
│   ├── 输出格式控制
│   ├── XML 结构化输入
│   ├── 角色设定
│   ├── Chain of thought 推理链
│   ├── Prompt chaining 任务拆分
│   └── Extended Thinking（深度推理模式）
│
├── 二、Context Engineering（理解 AI 的认知边界）
│   ├── context window 的本质与限制
│   ├── 什么信息该放进 context、什么不该
│   ├── 信噪比：高质量 token vs 噪声
│   ├── Agent 记忆：短期（context）vs 长期（外部存储）
│   └── 结构化文档作为 agent 的单一可信来源
│
├── 三、Claude Code 核心机制
│   ├── Agentic Loop（先理解它怎么跑）
│   ├── Plan Mode（先规划再执行）
│   ├── CLAUDE.md 项目配置（最快上手，立竿见影）
│   ├── Settings 层级管理（配置体系）
│   ├── 权限模型与安全边界
│   ├── 沙箱隔离（文件系统 / 网络）
│   └── Context 管理与 Compaction（长会话记忆管理）
│
├── 四、Claude Code 扩展能力（效率提升）
│   ├── Skills（可复用工作流）
│   ├── MCP（接入外部系统）
│   ├── Hooks（生命周期自动化）
│   └── Best Practices & Common Workflows
│
├── 五、多 Agent 编排（复杂任务）
│   ├── Subagents（隔离上下文子任务）
│   ├── Agent Teams（多 agent 协作）
│   ├── Plugins（团队级分发打包）
│   └── Agent SDK（用代码驱动 agent loop，脱离人工交互）
│
├── 六、评估与可观测性（生产质量保障）
│   ├── Evals：如何判断 prompt/agent 是否真的有效
│   ├── Observability：日志、trace、行为监控
│   └── 迭代改进闭环
│
└── 七、Harness Engineering（顶层方法论）
    ├── 工程师角色转变：从写代码到设计环境
    ├── Context engineering 在项目层面的落地
    ├── 架构约束机械化（linter、结构测试）
    ├── Entropy management（文档漂移治理）
    └── 反馈循环设计
```



## 一、Prompt Engineering（基础表达能力）

[Prompting best practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices#general-principles)



## 二、Context Engineering

[Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)





[Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)



## 三、Claude Code 核心机制

**1. How Claude Code Works（先读，建立整体认知）** https://code.claude.com/docs/en/how-claude-code-works

Agentic Loop 的完整解释，理解 Claude Code 的运行模型。

------

**2. Store Instructions and Memories - CLAUDE.md（最快上手）** https://code.claude.com/docs/en/memory

CLAUDE.md 的写法、作用域、最佳实践。

------

**3. Settings（配置层级）** https://code.claude.com/docs/en/settings

Managed → User → Project → Local 四层层级，权限配置语法。

------

**4. Permissions（权限模型）** https://code.claude.com/docs/en/permissions

allow / deny / ask 三级模型，工具级权限规则。

------

**5. Sandboxing（沙箱隔离）** https://code.claude.com/docs/en/sandboxing

文件系统 + 网络隔离，与权限层的关系。

------

**6. Explore the Context Window（Context 管理与 Compaction）** https://code.claude.com/docs/en/context-window

Context 的构成、compaction 触发机制、如何手动控制。



## 四、Claude Code 扩展能力

**1. Extend Claude Code（先读，建立全局认知）** https://code.claude.com/docs/en/features-overview

解释 Skills、MCP、Hooks、Subagents、Plugins 各自解决什么问题，以及怎么选择——这是第四章的地图，先读它再读具体文档。 [Claude](https://code.claude.com/docs/en/how-claude-code-works)

------

**2. Skills** https://code.claude.com/docs/en/skills

可复用工作流，slash 命令触发或 Claude 自动调用。

------

**3. MCP（Model Context Protocol）** https://code.claude.com/docs/en/mcp

接入外部系统，连接 GitHub、数据库、内部工具等。

------

**4. Hooks Guide（先读 Guide，再查 Reference）** https://code.claude.com/docs/en/hooks-guide https://code.claude.com/docs/en/hooks

Hooks 提供确定性控制，确保某些动作一定发生，而不是依赖 LLM 自主决定是否执行。 [Claude API Docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool)Guide 讲用法，Reference 查事件 schema，配合使用。



## 五、多 Agent 编排

**1. Subagents** https://code.claude.com/docs/en/sub-agents

隔离上下文的子任务，第五章入口。先读这篇建立基础概念。

------

**2. Agent Teams** https://code.claude.com/docs/en/agent-teams

多 agent 协作，目前是实验性功能。和 Subagents 的核心区别：Subagents 在单个 session 内运行并汇报结果，Agent Teams 是独立的 Claude Code session，可以互相通信。 [GitHub](https://github.com/ai-boost/awesome-harness-engineering)

------

**3. Plugins（Create + Discover）** https://code.claude.com/docs/en/plugins https://code.claude.com/docs/en/discover-plugins

两页配合读：前者讲怎么创建，后者讲怎么安装使用。

------

**4. Agent SDK** https://code.claude.com/docs/en/agent-sdk/overview

SDK 概览入口，左侧导航有 Quickstart、Agent Loop、Subagents in SDK 等子页，按顺序读完。这是从"用工具"到"写程序驱动工具"的跨越，放最后读。



## 六、评估与可观测性（生产质量保障）

**1. Evals：如何判断 prompt/agent 是否有效** https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents

Anthropic Engineering Blog，讲 agent eval 的方法论。日常开发最直接有用。

------

**2. Observability：trace 和日志定位 agent 行为问题** https://code.claude.com/docs/en/monitoring-usage

官方 Monitoring 文档，覆盖 OpenTelemetry 配置、metrics、events、trace span 层级。用 Agent SDK 跑自动化任务时，出问题靠这个定位。

**配合读：** https://code.claude.com/docs/en/agent-sdk/observability

Agent SDK 专项的 observability 配置，比上面那篇更聚焦。







## 七、Harness Engineering（顶层方法论）

**1. OpenAI：Harness Engineering（概念起源，先读）** https://openai.com/index/harness-engineering/

这篇文章是 Harness Engineering 这个概念的起点——三人团队用 agent 交付了超过百万行代码，工程师的工作从写代码变成了设计环境、指定意图、构建反馈循环。读完这篇才能理解第七章在讲什么。



------

**2. Anthropic：Effective Harnesses for Long-Running Agents（落地实践）** https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents

Anthropic 内部经验，讲跨多个 context window 的 agent 如何通过结构化文档（progress file、feature list）保持状态连续性。和第七章的 entropy management、context engineering 落地直接对应。
