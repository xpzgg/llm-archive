# vLLM CustomOp 复习笔记

> 已学过一遍，仅供快速回忆。细节忘了再回去翻文档：
> `https://docs.vllm.ai/en/latest/design/custom_op/`

## 一句话

CustomOp 是 vLLM 的**硬件抽象层**：把"各硬件上最优实现不同"的算子包成统一接口，平台差异收进 `forward_xxx` 内部；`register_oot` 是留给第三方硬件厂商的替换入口。

## 核心判断框架：分界的标准是什么

**host 侧代码是否一致**，决定一个算子需不需要 CustomOp：

- **一致**（matmul、elementwise）：同一行 Python 任何硬件都能跑，按设备选 kernel 由 **PyTorch dispatcher**（DispatchKey）完成。硬件厂商注册 PyTorch 后端即可，vLLM 零改动 → 不需要 CustomOp。
- **不一致**（vLLM 特有算子、融合策略不同、调用约定不同）：差异暴露在调 kernel **之前**的 Python 逻辑里，dispatcher 管不到 → vLLM 自己抽象一层，就是 CustomOp。

## CustomOp 对 vLLM 自己的意义

硬件分支收进算子类内部，模型代码只写统一调用。收益：

- 一份模型定义跑所有平台；
- 新硬件接入/性能优化只动一个类，所有模型受益；
- `forward_native` 是纯 PyTorch 兜底 + 正确性对拍基准。

## register_oot 的本质

**monkey patch 的正规化。** 早期插件运行时直接改 vLLM 内部方法，脆弱点：依赖内部命名/签名、执行时机敏感、多插件互相覆盖无提示、出问题难定位。register_oot 把这事变成契约：上游承诺实例化走 registry，插件承诺只继承指定类 + 实现 `forward_oot`。

## 为什么只能替换 CustomOp

不是限制，是定义使然：

1. **值得换的 op 都已经是 CustomOp**——"需要 OOT 替换的集合"与"CustomOp 的集合"是同一份清单；
2. 不是 CustomOp 的部分由 PyTorch dispatcher 消化，不需要这层；
3. 实例化时偷换类的机制，只对 vLLM 自己的类有效；
4. 白名单式收敛，兼容性可控。

→ 想换的 op 不是 CustomOp？给上游提 PR 把它包成 CustomOp，而不是绕过。

## 融合与模型结构：差异藏在哪层

模型 = 三层，融合只动最里面一层：

| 层 | 位置 | 跨平台 |
|---|---|---|
| 拓扑（有哪些模块） | `__init__` | 不变，权重 key 相同 |
| 数据流（模块怎么串） | `forward` | 不变 |
| 算子内部实现 | `forward_xxx` 函数体 | **不同，但封装在接口内** |

关键设计：接口签名按**融合后的最通用形态**定义（如 `forward(x, residual=None)`），所以"GPU 两个 kernel、NPU 一个融合 kernel"只是函数体差异，拓扑和权重不受影响。

失效条件：连输入输出语义都对不齐时，拆成独立 CustomOp（清单里一堆 norm/quant 变体就是这么来的）。

## 记忆锚点

```
标准算子  → PyTorch dispatcher（逐 tensor 动态分发）
vLLM 算子 → CustomOp.forward_xxx（按平台分发，启动时定）
厂商优化  → register_oot（实例化时偷换类）
```

一句话收束：**CustomOp 是划好的可替换边界，register_oot 是边界上的官方入口。**
