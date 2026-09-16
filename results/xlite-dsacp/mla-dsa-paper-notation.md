# MLA + DSA：论文符号表与 attention 流程

## 1. 符号表

以下沿用 DeepSeek-V3 §2.1.1 的 MLA 符号和 DeepSeek-V3.2 §2.1 的 DSA 符号。它们用于统一讨论对象，不代表 GLM-5.2 的全部模型配置。公式采用列向量约定。公式中的上下标是标记，不是乘方。

### 1.1 索引、维度与运算

| 符号 | 含义 |
|---|---|
| $t,s$ | 当前 query 位置、被查询的历史位置（允许 $s=t$） |
| $i$ | 主 attention head 的编号 |
| $j$ | Lightning Indexer head 的编号 |
| $d,n_h,d_h$ | hidden size、主 attention head 数、论文中的每头内容维度 |
| $d_h^R$ | 每头 RoPE 维度 |
| $d_c,d_c'$ | KV 压缩维度、Q 压缩维度；二者没有必须相等的要求 |
| $H^I,d^I$ | Indexer head 数、每头维度 |
| $[\cdot;\cdot]$ | 向量拼接 |
| $C,R,I$ | 内容分支、RoPE 分支、Indexer 分支 |
| $D,U$ | 下投影、上投影 |

### 1.2 MLA 激活和权重

| 论文符号 | 含义 |
|---|---|
| $\mathbf h_t$ | 当前层 attention 输入 |
| $\mathbf c_t^Q$ | Q 的压缩 latent，维度 $d_c'$ |
| $\mathbf c_s^{KV}$ | K/V 共享的压缩 latent，维度 $d_c$ |
| $W^{DQ},W^{UQ}$ | Q 下投影、内容 Q 上投影 |
| $W^{DKV}$ | 联合 KV 下投影 |
| $W^{UK},W^{UV}$ | 内容 K 上投影、V 上投影 |
| $W^{QR},W^{KR}$ | 生成 RoPE Q/K 分支的投影 |
| $\mathbf q_{t,i}^C,\mathbf q_{t,i}^R$ | 第 i 个 head 的内容 Q、RoPE Q |
| $\mathbf k_{s,i}^C,\mathbf k_s^R$ | 第 i 个 head 的内容 K、所有 heads 共享的 RoPE K |
| $\mathbf q_{t,i},\mathbf k_{s,i}$ | 拼接内容与 RoPE 部分后的完整 Q/K |
| $\mathbf v_{s,i}^C$ | 第 i 个 head 展开后的 V |
| $\mathbf o_{t,i}$ | 第 i 个 head 的 attention 输出（V 上投影之后） |
| $W^O,\mathbf u_t$ | 输出投影、attention 子层输出 |

### 1.3 DSA 符号

| 论文符号 | 含义 |
|---|---|
| $\mathbf q_{t,j}^I$ | 当前 token 在第 j 个 indexer head 的 query |
| $\mathbf k_s^I$ | 历史 token 的 indexer key，各 indexer heads 共享 |
| $w_{t,j}^I$ | 随当前输入生成的 head 加权系数；不是固定模型参数矩阵 |
| $I_{t,s}$ | 当前 token 对历史位置 s 的 index score |
| $\mathcal S_t$ | 当前 query 选中的 top-k 历史位置集合 |

### 1.4 分头矩阵的辅助约定

本文用 $W_i^{UK}$、$W_i^{UV}$ 分别表示论文整体矩阵 $W^{UK}$、$W^{UV}$ 对应第 i 个 head 的行块。权重吸收使用完整表达式表示。

## 2. Q 压缩表示与吸收后的内容 query

论文的 Q 压缩表示是：

$$
\mathbf c_t^Q=W^{DQ}\mathbf h_t\in\mathbb R^{d_c'}.
$$

而下面这个表达式表示内容 Q 经过 K 上投影转置后的结果：

$$
(W_i^{UK})^T\mathbf q_{t,i}^C\in\mathbb R^{d_c}.
$$

两者处于不同计算阶段：$\mathbf c_t^Q$ 是维度为 $d_c'$ 的 Q 压缩表示；$(W_i^{UK})^T\mathbf q_{t,i}^C$ 是维度为 $d_c$、可直接与 KV 压缩表示进行内容打分的 query。完整生成路径和等价推导见第 3、6 节。

## 3. MLA：准备当前 token 的 Q 与 KV

以下保持论文式 (1)–(9) 的数学抽象，不额外把实现中的 RMSNorm 等操作插进论文等式。

### a. Q 下投影

$$
\mathbf c_t^Q=W^{DQ}\mathbf h_t.
$$

### b. 分别生成内容 Q 与 RoPE Q

$$
[\mathbf q_{t,1}^C;\ldots;\mathbf q_{t,n_h}^C]
=W^{UQ}\mathbf c_t^Q,
$$

$$
[\mathbf q_{t,1}^R;\ldots;\mathbf q_{t,n_h}^R]
=\operatorname{RoPE}(W^{QR}\mathbf c_t^Q),
\qquad
\mathbf q_{t,i}=[\mathbf q_{t,i}^C;\mathbf q_{t,i}^R].
$$

内容 Q 与 RoPE Q 分别通过 $W^{UQ}$ 和 $W^{QR}$ 从 $\mathbf c_t^Q$ 生成。

### c. KV 下投影与独立的位置 K

对每个位置 s：

$$
\mathbf c_s^{KV}=W^{DKV}\mathbf h_s,
\qquad
\mathbf k_s^R=\operatorname{RoPE}(W^{KR}\mathbf h_s).
$$

主 MLA cache 保存历史位置的 $\mathbf c_s^{KV}$ 和 $\mathbf k_s^R$。

### d. 内容 K 与 V 的展开定义

$$
[\mathbf k_{s,1}^C;\ldots;\mathbf k_{s,n_h}^C]
=W^{UK}\mathbf c_s^{KV},
$$

$$
[\mathbf v_{s,1}^C;\ldots;\mathbf v_{s,n_h}^C]
=W^{UV}\mathbf c_s^{KV},
\qquad
\mathbf k_{s,i}=[\mathbf k_{s,i}^C;\mathbf k_s^R].
$$

这是数学定义，不要求推理时必须显式展开所有历史 K/V。第 6 节给出直接使用压缩 cache 的等价计算。

来源：[DeepSeek-V3 §2.1.1，式 (1)–(9)](https://arxiv.org/html/2412.19437v2#S2.SS1.SSS1)。

## 4. DSA：Lightning Indexer 筛选 top-k

### a. 生成独立的索引表示

Indexer 根据输入生成 $\mathbf q_{t,j}^I$、$\mathbf k_s^I$、$w_{t,j}^I$。它们属于独立的索引分支。本文使用 DSA 论文正文中对这些量的符号定义。

历史 $\mathbf k_s^I$ 可以缓存，称为 indexer key cache；它与主 MLA cache 是不同对象。

### b. 计算索引分数

$$
I_{t,s}=\sum_{j=1}^{H^I}w_{t,j}^I
\operatorname{ReLU}\left((\mathbf q_{t,j}^I)^T\mathbf k_s^I\right).
$$

### c. 选择历史位置

$\mathcal S_t$ 是合法历史范围内 index score 最大的至多 k 个位置的集合。这里输出的是位置，不是主 attention 的 softmax 权重。同一个 query 的主 attention heads 使用这个位置集合。

整批 $I_{t,s}$ 构成逻辑上的 indexer 打分矩阵；它不是模型权重，也不是 indexer cache，计算时不必完整物化。

来源：[DeepSeek-V3.2 §2.1，式 (1)–(2) 及稀疏训练中的集合定义](https://arxiv.org/html/2512.02556v1#S2.SS1)。

## 5. 主 attention：在所选位置上打分、softmax、聚合、输出

把 MLA 原式 (10) 的历史范围限制为 DSA 选出的 $\mathcal S_t$，得到以下组合推导式：

$$
\mathbf o_{t,i}=
\sum_{s\in\mathcal S_t}
\operatorname{Softmax}_{s\in\mathcal S_t}
\left(
\frac{\mathbf q_{t,i}^T\mathbf k_{s,i}}
{\sqrt{d_h+d_h^R}}
\right)\mathbf v_{s,i}^C.
$$

### a. 内容分数加位置分数

$$
\mathbf q_{t,i}^T\mathbf k_{s,i}
=(\mathbf q_{t,i}^C)^T\mathbf k_{s,i}^C
+(\mathbf q_{t,i}^R)^T\mathbf k_s^R.
$$

### b. 缩放与 softmax

每个主 attention head 独立对所选位置归一化。这里的分数不是 $I_{t,s}$；indexer 的分数负责选择位置，主 attention 重新计算聚合权重。

### c. 加权 V，得到每头输出

上面加权求和的结果就是论文中的 $\mathbf o_{t,i}$，表示第 i 个 head 在 V 空间中的输出。

### d. 输出投影

$$
\mathbf u_t=W^O[\mathbf o_{t,1};\ldots;\mathbf o_{t,n_h}].
$$

$\mathbf u_t$ 是 attention 输出；之后还需当前 Transformer block 的残差和 FFN/MoE 等操作，并非压缩向量直接进入下一层。

## 6. 等价推导：为什么可以直接使用 KV latent cache

这一节通过矩阵乘法的结合律和线性映射的分配律，推导直接使用 KV 压缩表示的计算形式。

### a. 将 K 上投影移到 Q 侧

$$
\begin{aligned}
(\mathbf q_{t,i}^C)^T\mathbf k_{s,i}^C
&=(\mathbf q_{t,i}^C)^TW_i^{UK}\mathbf c_s^{KV}\\
&=\left((W_i^{UK})^T\mathbf q_{t,i}^C\right)^T\mathbf c_s^{KV}.
\end{aligned}
$$

这只是点积的等价重排。左侧可以显式展开历史 K，右侧可以保持历史 KV 压缩，仅转换当前内容 Q。RoPE 部分仍单独参与打分。

### b. 将 V 上投影移到加权求和之后

直接代入 $\mathbf v_{s,i}^C=W_i^{UV}\mathbf c_s^{KV}$：

$$
\mathbf o_{t,i}=W_i^{UV}
\left[
\sum_{s\in\mathcal S_t}
\operatorname{Softmax}_{s\in\mathcal S_t}
\left(
\frac{
\left((W_i^{UK})^T\mathbf q_{t,i}^C\right)^T\mathbf c_s^{KV}
+(\mathbf q_{t,i}^R)^T\mathbf k_s^R
}{\sqrt{d_h+d_h^R}}
\right)\mathbf c_s^{KV}
\right].
$$

方括号内是压缩空间中的加权聚合结果，维度 $d_c$；乘 $W_i^{UV}$ 后得到论文定义的 $\mathbf o_{t,i}$，再经 $W^O$ 得到 attention 输出。在 latent 上计算与展开 K/V 后计算，是同一层的两种等价计算形式。

## 7. 计算对象速查

- Q 压缩表示：$\mathbf c_t^Q$。
- 吸收 K 上投影后的 query：$(W_i^{UK})^T\mathbf q_{t,i}^C$。
- 主 KV cache：$\mathbf c_s^{KV}$ 与 $\mathbf k_s^R$。
- Indexer cache：$\mathbf k_s^I$，不是 $I_{t,s}$ 或 $\mathcal S_t$。
- 主 attention logits、softmax 权重与 index score 分开称呼。
- $\mathbf o_{t,i}$ 是每头 V 空间输出；$\mathbf u_t$ 是经过 $W^O$ 的输出。

## 8. 阅读与范围

本文使用 `$...$` 和 `$$...$$`；需在支持数学公式的 Markdown 预览器中查看，纯文本编辑器会显示 LaTeX 源码。

本文对齐上述两篇论文的符号与数学机制，不声明 GLM-5.2 的具体 head 维度、top-k 数、跨层索引共享、归一化位置和门控结构与其完全相同。
