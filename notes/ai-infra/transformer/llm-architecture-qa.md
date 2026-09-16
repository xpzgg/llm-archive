# 从 Transformer 到现代 LLM 架构 复习问答笔记

> 来源：2026-09 读 Illustrated Transformer、DeepSeek-V3.2 报告、准备 GLM-5.2 DSA_CP 设计期间的提问记录。
> 收录标准：当时理解有偏差、经讨论纠正的关键概念。按主题归类，每条记"误区 → 正解"。
> 相关：[阅读计划](../vllm/reading-plan.md)、[vLLM 学习路线](../vllm/vllm-learning-roadmap.md)。

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

**与 max_seq_len 的关系**：vanilla PE 表一行一个位置，表长 = max_seq_len，超出的 token
没有编码可用——"上限被物理写死"。现代 RoPE 无表，上限的性质完全不同，
见"十三、上下文长度上限"。

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

## 八、MLA（Multi-head Latent Attention）

**误区：MLA 是在 MHA 里给 K、V 各加一个压缩矩阵。**
正解：K 和 V 是**联合压缩**——hidden state 压成一个共享 latent 向量 `c_t`（512 维）+ 一个很小的
decoupled RoPE key（位置信息压不进 latent，单独存）；cache 里每 token 每层就这两样。
使用时每个 head 有自己的上投影矩阵，从 `c_t` 各自重建出不同的 K/V——这是和 MQA
（全头共享同一份 K/V）的本质区别，也是质量不掉的原因。

**误区：推理时真的把 K/V 升维展开来算。**
正解：工程上用**权重吸收**——上投影矩阵预先合并进 query 投影和 output 投影，decode 直接拿
query 对 cache 里的 latent 算注意力，全程不展开完整 K/V。这就是论文附录说的 "MQA mode"。
对 CP 的意义：切分时面对的 KV 条目就是这个 latent（512+64 维，全头共享），不是 per-head K/V。

**误区：MQA 共享 KV，说明各 head 有同样的 W_K/W_V，那还要 multi head 干什么。**
正解：共享的只有 W_K/W_V（库存只有一份），每个 head 保留独立的 W_Q（各自提不同的问题）。
多样性在 query 侧：同一份 K/V 上，不同 query 算出不同注意力分布。质疑方向是对的——
单份 KV 编码确实损失表达力，实测 MQA 质量略降。谱系：MHA（K/V 每头一份）→ GQA（分组共享）
→ MQA（全局一份）→ MLA（不共享，压缩重建，cache 接近 MQA、质量接近 MHA）。

**误区：MQA 没有 multi head，只有一层 head。**
正解：Q 侧仍是多头（如 128 个），只有 K/V head = 1。名字即含义：Multi-**Query**。
KV cache 大小正比于 K/V head 数，Q 用完即弃不进 cache。

## 九、DSA（DeepSeek Sparse Attention）

**主干**：lightning indexer（小头数、FP8、ReLU）给每个 query 对全部历史 token 打分
（O(L²) 但常数极小）→ top-k 选择（k=2048）→ 主 MLA attention 只算选中条目（O(L²)→O(Lk)）。
indexer 训练：KL 散度蒸馏主 attention 分布，梯度 detach，与主模型分开优化。

**误区：选出的不重要 token 用 mask 掩盖掉。**
正解：是 **select/gather 不是 mask**。mask 是"全算一遍再置零"，计算和读取一点没省；
DSA 是只 gather 2048 条出来算，其余根本不读。省的是计算量和读取带宽。
（论文里的 "masked MHA mode" 只是短序列 prefill 时模拟 DSA 行为的工程技巧。）

**误区：DSA 节省了 KV cache。**
正解：**存储量不变**——任何历史 token 都可能被未来 query 选中，全量 latent KV 必须保留。
省的是**每步读取**：100K 上下文 MLA 后总 cache ~7GB（不变），decode 每步读取 7GB → ~140MB。
对 DSA_CP 的直接约束：全量 cache 必须保留且任意条目可能被选中 → "按 token 切 KV" 会导致
跨卡 gather → 这就是选"KV 全量复制、只切 query"方案的算法层理由。

**误区：DSA 是 FFN 层的技术。**
正解：DSA 在 **attention 层**（序列轴稀疏）；FFN 层的稀疏是 MoE。V3.2 相对 V3.1-Terminus
唯一架构改动就是 attention 换 DSA，FFN 未动。

**indexer 也有自己的 cache**：indexer 的 `k^I` 每 token 每层一份（128 维、FP8），打分要扫全序列，
所以 CP 下每个 rank 必须能看到全序列的 `k^I`——"KV 全量复制"的另一条理由。
GLM-5.2 的 **IndexShare**：每 4 个 DSA 层共享一个 indexer（producer 算 top-k，后 3 层复用），
1M 上下文下 per-token FLOPs 降 2.9×；CP 设计里 producer/consumer 层的索引边界必须对齐同一切分。

**MoE router 与 DSA indexer 的异同**：骨架相同（打分 → top-k），差异在：
MoE 候选集是**固定** 256 个专家（router 线性层打分，O(256) 小计算）；
DSA 候选集是**动态增长**的 L 个历史 token（indexer 点积打分，O(L) 且要和 cache 交互）。
CP 设计围着 DSA 转、不用管 MoE，原因在此。

## 十、KV cache 成本账（以 100K / 1M 上下文为例）

**误区（震撼点）：100K 上下文一个 token 要读 400GB KV cache。**
正解：400GB 是 **MHA 假设下**的总 cache 量（2×128头×128维×61层×fp16 ≈ 4MB/token × 100K）。
decode 每生成一个 token 要全读一遍——H800 带宽 3.35TB/s → 上限 ~8 token/s。
注意力计算本身只要 <1ms，120ms 全在搬数据：**decode attention 是伪装成计算任务的数据搬运任务**。

**三件套各打一段**：

| 问题 | 武器 | 效果（GLM-5.2，1M 上下文） |
|---|---|---|
| 存不下 | MLA 压条目大小 | TB 级 → ~90GB（78 层 × 576 维 × 2B ≈ 90KB/token），放得下 |
| 每步读太多 | DSA 压读取条数 | 90GB → ~180MB（top-2048），读得少 |
| 单卡放不下 90GB | CP/TP 摊多卡 | 摊得开 |
| indexer 每层扫全序列还是贵 | IndexShare | 4 层摊一次 |

## 十一、Pre-Norm 与 RMSNorm

**疑问：为什么 DeepSeek 是先 RMSNorm 再 attention/FFN，经典 Transformer 是后 LayerNorm。**
正解：原版是 Post-LN（6 层没问题）；层数上到几十层后 Post-LN 训不动——残差通路每层
被 LN 调制，靠近输入层梯度逐层衰减，必须精细 warmup 且易 loss spike。
**Pre-Norm**（`x = x + F(norm(x))`）让残差流从首层到末层是纯恒等映射，梯度无损直达。
代价：残差流数值随深度累积、表征约束弱，浅网下 Post-LN 精度略好——但敌不过"深了训不动"。
**RMSNorm**：砍掉 LN 的均值居中（保留重缩放主效应），计算更省、kernel 好融合，实测无损。
2023 年后所有主流大模型的共识配置。

## 十二、训练流水线（预训练 / 后训练 / 基座）

**基座（base model）**：预训练完成、后训练之前的 checkpoint。V3 与 R1 同基座：
架构和预训练权重完全相同，区别只在后训练路线（V3 = SFT+对齐；R1 = 重 RL 激发推理）。
R1-Zero 证明：只给奖励信号（答案对不对）不给教法，长链推理可以自己涌现。

**疑问：后训练改不改参数？**
正解：改，全参数梯度更新，机制上和预训练无区别。区别在数据（无标注海量文本 → 带偏好
的小数据）、目标（知识容量 → 行为塑造）、量级（后训练算力占比 <5%）。
环节：SFT（指令跟随）→ RL（GRPO，推理强化 + 对齐）→ 蒸馏。

**误区：后训练不会让模型学到新知识。**
修正：不是"不能"而是"注入效率低、代价大"——知识写入量 ∝ 数据量×算力，后训练数据小
3-4 个数量级；且小数据猛训有灾难性遗忘。RL 基本不注入（只在已有行为分布里调权）；
SFT 可注入小剂量领域知识；大剂量注入要回到 continued pre-training（灰色地带，
如 V3 续训扩 128K、V3.2 装 DSA）。
定稿表述：**预训练 = 无偏好的知识注入；后训练 = 带偏好的行为塑造**
（每份数据都在表达"我更希望你输出 A 而不是 B"；RL 是偏好的极端形态——只留奖励信号）。

## 十三、上下文长度上限

**误区：上下文上限 = 位置编码表的 size（max_seq_len 参数）。**
正解：分两种编码。老式可学习位置 embedding（GPT-2）：确实是 `[max_seq_len, hidden]` 的表，
超了没向量可查，硬上限。**RoPE：没有表**——旋转角按位置编号现场算，任意大都算得出；
`max_position_embeddings` 只是"训练担保范围"，超了不是报错而是质量渐烂（没训过的旋转角）。
YaRN 类扩展：对 RoPE 频率做缩放把更多位置挤进已训范围 + 继续训练（DS 扩 128K、GLM 上 1M 均此路线）。

**生效上限**：`min(max_position_embeddings, 框架的 max_model_len, 商业/API 限制)`。
物理约束是 KV cache 显存——1M 上下文 ≈ 5MB 文本 ≈ 90GB 显存（MLA 加持下），
"长上下文即服务"的真实成本结构。

## 十四、推理与精度测试

**贪心解码的可复现性**：数学上确定（每步 argmax 无随机源）；工程上不保证 bit 一致——
浮点归约顺序随 batch 组成变化，末位抖动可能让分数接近的候选翻面。batch invariance
工作（vllm-ascend 有 batch_invariant.py）就是治这个。

**golden（基准答案）**：可信实现对固定输入的输出，存作标准，改动后逐 token 比对。
必须贪心解码才有意义；golden 本身会随环境过期。比对分两档：严格档（逐 token 一致，
适用于重构类改动）和容差档（benchmark 分数对比，适用于数值路径变化的改动）。
DSA_CP 验证策略：先严格档（dsacp 开/关逐 token 对拍），再 benchmark 分数验收。

**decode 停止条件**：生成 EOS；命中用户配置的 stop token/字符串；撞长度上限
（max_tokens 或 max_model_len）；客户端取消。性能测试常用 ignore_eos 固定输出长度。

## 十五、与 DSA_CP 的连接点

- CP 切的是**实际请求的 token 序列**（packed 布局），与 max_seq_len 上限无关；
- 长 context 拉高 TTFT 的原因：prefill 要处理全部 prompt token，attention 计算量 ∝ N²；
- DSA_CP 的主目标不是降 TTFT，而是稀疏模型 TP 下 attention 计算太碎、通信占比过高；
- FlashComm/SP 切的正是 LayerNorm/残差段（逐 token 独立、零通信）；
- 做 CP 切片改的是 metadata 边界数组，不是张量形状。
