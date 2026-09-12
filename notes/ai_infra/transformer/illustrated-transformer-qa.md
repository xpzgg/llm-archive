# Illustrated Transformer 复习问答笔记

> 来源：2026-09 读 Illustrated Transformer 期间的提问记录。
> 收录标准：当时理解有偏差、经讨论纠正的关键概念。按主题归类，每条记"误区 → 正解"。
> 相关：[阅读计划](../vllm/reading-plan.md)。

## 一、attention 与 FFN 的本质分工

**误区：self-attention 层的输出是 K/V。**
正解：输出是每个 token 的新 hidden 向量（对历史 V 的加权和再经 o_proj），送往 FFN/下一层。
K、V 是中间产物；KV cache 缓存的是中间产物，不是层输出。

**FFN 做什么**：逐 token 独立的 MLP（升维→非线性→降维）。attention 管 token 间"交流"，
FFN 管单 token"思考"。模型约 2/3 参数在 FFN。现代变体：SwiGLU 门控（两路投影相乘）、
MoE（FFN 换成 N 个专家 + 路由器）。

**"逐 token"结构贯穿始终**：embedding 之后每层的输入输出都是"一排 token 向量"，
token 数不变，变的只是内容。这是推理框架以 token 为单位组织 metadata 的原因。

**注意力可以并行**：attention 的依赖是"数据依赖"（输入需要其他 token 的 K/V），
不是"顺序依赖"——K/V 备齐后各 token 的 attention 照样同时算。RNN 才有顺序依赖。
这决定了 CP 切分的难度分布：FFN 段零通信，通信全在 attention 的 KV 可得性上。

## 二、token 与词表

**误区：token ≈ 单词。**
正解：token 是 tokenizer（BPE 等）切出的子词单位，可能是词根碎片、汉字、标点。
不用单词的原因：词表必须固定大小（embedding 表维度）；开放词表问题（OOV、变体、
多语言）；子词组合可覆盖未见词。

**lm_head 与 logits**：decoder 输出经 Linear 投影成词表大小的 logits 向量，softmax 成
概率分布后选词。词表现代规模 10 万~25 万。embedding 与 lm_head 常共享权重（weight tying）。
训练时每个位置都出 logits（每位置都在预测下一个词）；推理时**每请求每步只算末尾位置**
——它是唯一看完整个上下文（causal mask）、且预测目标（下一个词）尚未知的位置。

**logits 的开销治理**：推理只算每请求末尾一行；训练用 fused linear cross-entropy
避免完整 logits 落盘；lm_head 权重 TP 下按词表维切（每卡一段词表，logits all-gather 拼全）。

## 三、位置编码与序列长度

**误区：Illustrated Transformer 里 "list 的 size" 指向量维度。**
正解：list 的 size 是向量个数（序列有多少个 token）；每个向量 512 维是 d_model，固定。
最后一句话的意思是训练要预设最大序列长度（超参数），大致按训练集最长句取值。

**为什么需要位置编码**：attention 是集合操作，对顺序不敏感（"狗咬人"和"人咬狗"一袋
向量输出相同），顺序信息必须外部注入。

**位置编码是什么（vanilla）**：给每个位置编号 p 配一个 d_model 维向量（sin/cos 多频率
公式，零参数、纯函数、可预计算成表），加到 embedding 上。低维高频区分近邻、高维低频
区分远位，每行是独一无二的"位置指纹"；PE(p+k) 是 PE(p) 的线性变换，便于学相对位置。

**误区：为什么不直接用 0~(N-1) 序号编码。**
正解：标量序号没有可用的几何结构——原始值会淹没 embedding（±1 vs 500），归一化后
相邻位置又无法区分。sin/cos 相当于对序号做傅里叶展开。

**与 max_seq_len 的关系**：PE 表一行一个位置，表长 = max_seq_len，超出的 token 没有
编码可用——这是"上限被物理写死"的原因。现代 RoPE 改为按位置对 q/k 现场旋转，硬上限
消失，约束退到 KV cache 预算与训练长度外推。

## 四、batch 与训练

**误区：一句话就是一个 batch，超长切多个 batch。**
正解：一个 batch 装很多句（batch_size 个独立样本），凑一起是为了喂饱 GPU。
超长句的处理是截断（或预训练中机械定长切段），不是切 batch。

**batch 内句子如何区分**：靠张量结构。`[B, L, d]` 中 attention/FFN 逐行独立，
数学上没有跨行项（不是靠掩码挡的），掩码只管行内的 pad 和因果。batch 维是"免费维"，
行号模型不感知（打乱行序结果不变）。

**位置编码在 batch 内**：每句位置从 0 重新编号，共用同一张 PE 表。pad 位置也加了 PE
但被掩码屏蔽，无害。

**batch 内句子的关系**：计算上完全独立，但 loss 取整批平均、联合更新一次参数——
"各自独立计算、联合更新"。batch_size 因此也影响训练动态（大 batch 梯度稳，要配大学习率）。

**误区：batch 都要 pad 到 max_seq_len。**
正解：pad 到**本 batch 最长那句**即可（dynamic padding），batch 间长度随意，
上限才是 max_seq_len。工程上再配长度分组（bucketing）减少 padding 浪费。

**pad token**：词表里的专用占位符，填充短句。必须配两个掩码：attention mask
（pad 位置分数压 -∞）和 loss mask（不算 pad 位置的损失）。

**训练 padding vs 推理 packed**：训练求吞吐，离线数据可分组，pad 整齐换 kernel 效率；
推理求延迟，请求在线随机到达、随时进出（continuous batching），且 varlen kernel
（FlashAttention 式）支持拍平一维 + 边界数组（query_start_loc/seq_lens），零填充。
推理侧"batch"抽象从张量形状升级为 metadata。

**术语**：mini-batch 是历史术语（相对全量 GD），现在说 batch 默认就是它；
epoch = 过一遍数据集；step = 一（mini-）batch 更新一次；micro-batch = 逻辑 batch
拆成能装进显存的小块做梯度累积。现代 LLM 训练按"每步 token 数"表达 batch 预算。

**batch_size 是超参数**，约束：显存（激活 ∝ B×L，硬）、梯度噪声与学习率耦合（软）、
硬件对齐（tensor core 粒度）。

## 五、多头注意力

**多头 vs 多卷积核（功能类比成立）**：动机相同（一个通道只能表达一种模式 → 并行多个
子空间/探测器 → 汇合）、实证相同（head/kernel 都会专职分化、大量冗余可剪枝）。
原论文动机：多子空间让不同关系（相邻/句法/指代）各自有表达空间。

**机制差异（要记的边界）**：卷积核是静态局部滤波器；attention head 学的是"固定子空间
投影 + 现场按内容决定注意谁"。训练完 QKV 矩阵同样固定——"动态"指的是施加在 V 上的
混合权重由输入现算（softmax(Q·Kᵀ)），参数学的是"生成变换的规则"。

**工程含义**：head 间计算完全独立，是 TP 的天然切分单位。DSA_CP 打破 head 切分
（改切 token、每卡全 head），因为稀疏注意力下 head 切分的通信代价超过收益。

## 六、MoE

**MoE = N 个专家 MLP + 路由器**：路由器给每个 token 打分选 top-k 专家，输出按权重
加权求和。核心卖点：**解耦参数量与计算量**（稀疏激活）——加专家只加显存不加每 token
计算。

**误区：MoE 是 DeepSeek 提出的。**
正解：1991 年 Jacobs/Jordan/Hinton 提出原始 MoE；2017 年 Google（Shazeer）提出稀疏
门控 MoE；Switch Transformer、Mixtral 带入主流。DeepSeek 的原创是 DeepSeekMoE
（细粒度专家 + 共享专家）和把 MoE 推到 frontier 水准的系统工程（MLA、低成本训练）。

**代际参数增长主要来自专家数**（DeepSeek V2→V3：层数 60→61 几乎不动，专家 160→256，
总参数 ×2.8 而激活参数只 ×1.8）。所以大模型的系统压力在显存与专家间数据搬运
（EP、EPLB），不在算力。OpenAI/Anthropic 架构不公开，"是否 MoE"无公开答案。

**代价**：路由通信（EP 下 token 要 all-to-all 快递到专家所在卡）、负载均衡、显存
存全量专家。

## 七、encoder-decoder（原版结构，LLM 可跳过）

**交叉注意力**：decoder 中间层，Q 来自 decoder 自己，K/V 来自 encoder 最终输出
（源句 hidden 向量经**本层自己的** W_K/W_V 投影——K/V 只是"向量×投影矩阵"的产物，
任何向量都能当原料，无循环依赖）。作用：生成每步回头看源句任意位置。
现代 LLM 全是 decoder-only，无此层。

**decoder-only 的 KV cache**：每层 self-attention 的 K、V，每个历史 token 一份，
每层各存一份（不是最后一层，也不是 encoder 的）。prefill 写全量，decode 每步追加一格。

## 八、推理与精度测试

**贪心解码的可复现性**：数学上确定（每步 argmax 无随机源）；工程上不保证 bit 一致——
浮点归约顺序随 batch 组成变化，末位抖动可能让分数接近的候选翻面。batch invariance
工作（vllm-ascend 有 batch_invariant.py）就是治这个。

**golden（基准答案）**：可信实现对固定输入的输出，存作标准，改动后逐 token 比对。
必须贪心解码才有意义；golden 本身会随环境过期。比对分两档：严格档（逐 token 一致，
适用于重构类改动）和容差档（benchmark 分数对比，适用于数值路径变化的改动）。
DSA_CP 验证策略：先严格档（dsacp 开/关逐 token 对拍），再 benchmark 分数验收。

**decode 停止条件**：生成 EOS；命中用户配置的 stop token/字符串；撞长度上限
（max_tokens 或 max_model_len）；客户端取消。性能测试常用 ignore_eos 固定输出长度。

## 九、与 DSA_CP 的连接点

- CP 切的是**实际请求的 token 序列**（packed 布局），与 max_seq_len 上限无关；
- 长 context 拉高 TTFT 的原因：prefill 要处理全部 prompt token，attention 计算量 ∝ N²；
- DSA_CP 的主目标不是降 TTFT，而是稀疏模型 TP 下 attention 计算太碎、通信占比过高；
- FlashComm/SP 切的正是 LayerNorm/残差段（逐 token 独立、零通信）；
- 做 CP 切片改的是 metadata 边界数组，不是张量形状。
