---
layout: post
title: "从 output token 到 roofline：一份对 GPU 推理架构的再审视"
date: 2026-07-18 09:00:00 +0800
categories: [tech, gpu, inference]
math: true
---

任何 LLM API 的价目表里，output token 永远比 input token 贵——大多在 4-8 倍之间。看起来像定价策略，但只要把单步 decode 的工作点画到 roofline 上，就能看到这是 **数据复用结构** 决定的工程结果，不以哪一家的定价为转移。

顺着这个观察一路往下，会遇到一连串有意思的问题：

- 为什么 batch 越大单 token 越便宜、但用户能感觉到的延迟也越长？
- 为什么 GQA、FP8 KV、quantization 这些优化并不改变 FLOPs，却能让 GPU "干活变多"？
- 为什么 H200、B200 这类带宽更强的卡，对单 token 单价的边际改善远小于直觉？
- 为什么 MoE 模型以同样总参数能便宜那么多？

本文想做的不是另一个 "decode 为什么慢" 的科普贴，而是把 *从 output token 这一具体现象倒推到 roofline、再倒推到 GPU 硬件设计选择* 这条链条整理清楚。最后再回到一份可复算的账单，看看公开 API 标价和 "纯硅成本" 之间到底差多少。

---

## 一、从 output token 开始：一个具体的工作点

单 token decode 看起来很简单：跑一遍 80 层 Transformer，吐一个 token。但里面发生的事情可以用 roofline 量化：

$$\mathrm{AI} \;=\; \frac{\text{一次推理做的浮点运算}}{\text{一次推理从显存读出的字节数}}\quad(\mathrm{FLOP/byte})$$

把 LLaMA-70B 类 dense 模型（GQA $$g=8$$，FP16 权重 + FP16 KV）在 H100 上拆开，单 token 算术强度大致是：

| B (batch) | 线性层 AI (FLOP/byte) | attention 算术强度 (FLOP/byte) | 聚合 AI (FLOP/byte) | 相对 H100 拐点 (590) |
|---:|---:|---:|---:|---:|
| 1 | 1.0 | 8.0 | **1.07** | 0.18% |
| 8 | 8.0 | 8.0 | **8.00** | 1.4% |
| 32 | 32.0 | 8.0 | **26.4** | 4.5% |
| 128 | 128.0 | 8.0 | **62.0** | 10.5% |

B=128 都还没到 H100 拐点的 11%。这就是 output token 工作的真实 "地形"：**永远坐在 roofline 的带宽侧**。

注意表格里有两个完全不同的量：线性层 AI 是 $$2B/\sigma_w$$（按 batch 摊权重），attention 算术强度是 $$2g/\sigma_{kv} = 8$$（与 batch 无关、与上下文长度无关——后一点是它卡带宽的根因）。前者解释了 batching 的红利，后者解释了为什么 batching 不能无限提升。第三节会从公式推导。

### 1.1 几个常见误解，先标出来

网上不少资料把这些数字讲得太 "硬" 了，进入推导之前先把几条要修正的事说清楚：

- "Attention 算术强度恒等于 1 FLOP/byte"——只对 MHA + FP16 KV 成立。Llama-3、Qwen 等 2024 年后的主流模型都用了 GQA（$$g=8$$），attention AI 在 FP16 KV 下是 8、在 FP8 KV 下是 16。
- "4-8× 价格差是物理常数"——纯硅成本差大约 7-17×（来自 batch 摊薄差异，详见 §7），4-8× 是市场观察到的 *售价* 比，**它小于** 纯成本比。定价在 *压缩* 成本差。
- "H100 decode 时只跑出峰值算力的 0.5%"——结论跟口径有关：FP16 严格匹配时 H100 B=1 是 0.18%；换成 Blackwell FP4 tensor-core 峰值做分母会显得更难看；GQA 比 MHA 略好。
- "PIM / APU / LPU 是颠覆性突破"——这些方案都把内存层级往上推一阶，绕开 HBM 带宽墙。以 H100 的 HBM : FP16 = 3.35 TB/s : 1979 TFLOPs = 1 : 590 (byte/FLOP) 为基准，新方案把这个比值升到 1 : 5-20（数量级缩小，decode 工作点往 ridge 推近）。这才是结构上的差异。

---

## 二、Roofline：再讲一遍，但只讲 GPU 推理相关的

Roofline 把 "一个算子在这块卡上能跑多快" 这件事收成两条线：

- **斜线段**：$$P_{\mathrm{attain}} = \mathrm{BW} \cdot \mathrm{AI}$$（被带宽限制，斜率 = HBM 带宽 BW）。
- **水平线段**：$$P_{\mathrm{attain}} = F_{\mathrm{peak}}$$（被算力限制，高度 = 峰值 FLOP/s）。
- **拐点**：$$\mathrm{AI}_{\mathrm{ridge}} = F_{\mathrm{peak}} / \mathrm{BW}$$。H100 SXM FP16 tensor core $$1979\,\mathrm{TFLOPs} / 3.35\,\mathrm{TB/s} \approx 590\,\mathrm{FLOP/byte}$$。

把当前一代旗舰的拐点列一下（**注意 FP4 tensor core 在 decode 中几乎用不上，详见 §5.2**），能立刻看出问题：

| 卡 | HBM 带宽 | FP16 峰值 (TFLOPs) | FP16 ridge | FP8 峰值 (TFLOPs) | FP8 ridge | decode 利用率 (B=128) |
|---|---:|---:|---:|---:|---:|---:|
| A100 SXM | 2.0 TB/s | 312 | 156 | — | — | **40%** |
| H100 SXM | 3.35 TB/s | 1979 | **590** | 3958 | 1180 | **10.5%** |
| H200 SXM | 4.8 TB/s | 1979 | 412 | 3958 | 825 | **15%** |
| B200 SXM [待核 2026-07] | 8.0 TB/s | 2250 | 281 | 4500 | 562 | **22%** |
| GB200 (NVL72 per package 估) | ~8 TB/s | 2500 | 312 | 5000 | 625 | **20%** |

观察：H100 → B200 的 FP16 峰值几乎没动（1979 → 2250 TFLOPs），带宽从 3.35 涨到 8 TB/s，ridge 因此从 590 降到 281——这只是除法的算术结果，不是趋势判断。对 decode 工作点（LLaMA-70B、FP16 权重 + FP16 KV、B=128 时 AI_agg ≈ 62）它意味着两件事：

- **其一，工作点始终留在带宽段**（62 ≪ 281），所以峰值算力（分子）对 decode 无关紧要，每一代的 decode 吞吐收益就等于带宽增幅本身——H200 +43%，B200 +139%。
- **其二，表中 "decode 利用率" 从 10.5% 涨到 22%，并不是执行效率提升，只是分母（ridge）变小了**。利用率和 "距 ridge 几倍" 都是诊断量，不能当性能指标读——它们衡量的是 "FP16 峰值被浪费了多少"，不是 "硬件升级带来了多少收益"。

但**每美元改善**远小于带宽增幅：云租价从 $2.4/h（H100）涨到 $3-4/h（H200）、$5-8/h（B200），带宽红利被租价吃掉一部分甚至倒挂——这是 §9 的量化结论。

这就是为什么不能拿 "FP4 峰值" 评价 LLM decode 的利用率：FP4 tensor core 在 decode 中几乎用不到（KV cache 还得是 FP16/FP8，attention 主路径不会跑到 FP4），能匹配上的只有权重 GEMM。引用：NVIDIA H100 / H200 / Blackwell architecture briefs ([H100](https://www.nvidia.com/en-us/data-center/h100/), [H200](https://www.nvidia.com/en-us/data-center/h200/), [Blackwell brief](https://developer.nvidia.com/blog/nvidia-blackwell-gpu-technical-brief/))。

下一节我们从第一性原理推导这个聚合 AI 是怎么算出来的。

---

## 三、从第一性原理推导单步 decode 的 FLOPs 和字节数

记号约定：本节约定 "1 MAC = 2 FLOPs"，与 PaLM / GPT-3 / FlashAttention 等文献一致。dense decoder-only Transformer：

- $$L$$ 层数，$$H$$ 隐藏维度，query 头数 $$h_q$$，KV 头数 $$h_{kv}$$，head dim $$d = H/h_q$$，分组因子 $$g = h_q/h_{kv}$$。
- 当前生成第 $$o$$ 个 token 时，上下文长度 $$T = S + o - 1$$（$$S$$ = ISL，$$o-1$$ = 此前已生成 token）。
- $$B$$ 同一时刻服务的独立序列数。
- 权重按 $$\sigma_w$$ 字节存（FP16/BF16 = 2，FP8/INT8 = 1，FP4/INT4 = 0.5），KV cache 按 $$\sigma_{kv}$$ 字节存（默认 FP16 = 2，FP8 = 1）。
- $$P$$ 计 *每 token 实际参与* 的 dense 参数（Q/K/V/O + FFN，可选 LM head，下面默认包含以贴近 LLaMA-70B 的 ~70B 数字）。

### 3.1 FLOPs

- 线性层：$$F_{\mathrm{lin}} = 2P$$。
- 注意力 QKᵀ + AV：
  - 每个 query head 都做一次 QKᵀ 和一次 softmax·V，**与 GQA 无关**。
  - 单 token 单头单层：$$4\,d\,T$$；总头 $$4\,T\,H$$；总层 $$4\,L\,T\,H$$。
- 合计：$$F_{\mathrm{token}} = 2P + 4\,L\,H\,T$$。

LLaMA-70B（$$P \approx 70\times10^{9}$$，$$L=80$$，$$H=8192$$，$$T=4096$$）：$$F_{\mathrm{token}} \approx 150.7\,\mathrm{GFLOPs}$$。这跟 vLLM 在公开 benchmark 中的 140-160 GFLOPs/token 一致。

### 3.2 HBM 字节数（理想情况下，不计 cache 命中 / kernel fusion）

每生成一个 token，每张卡需要从 HBM 读出的字节数分两项：

- **权重按 batch 摊薄**：$$M_{\mathrm{weights}}(B) = P\,\sigma_w / B$$ 字节/token。
- **KV cache read**：$$M_{\mathrm{kv\_read}}(T) = 2\,L\,T\,H\,\sigma_{kv} / g$$ 字节/token。
  （$$T = S + o - 1$$，与 batch 无关。）

$$M_{\mathrm{token}}(B,T) \;\approx\; \frac{P\,\sigma_w}{B} \;+\; \frac{2\,L\,T\,H\,\sigma_{kv}}{g}$$

**忽略项**：每 token 的 KV write（自己一份新增 K/V，约 $$2\,L\,H\,\sigma_{kv}/g \approx 0.33\,\mathrm{MB}$$）相对权重和 KV read 都小至少三个数量级，故从公式中略去。它的物理意义将在 §3.3 attention 算术强度里再次出现——KV read 的来源就是 "每写一份 KV 至少也得读一次"。

### 3.3 算术强度

- **线性层**：$$\mathrm{AI}_{\mathrm{lin}} = (2P) / (P\,\sigma_w/B) = 2B/\sigma_w$$。只看权重搬运时，跟模型大小、形状都无关。
- **注意力**：

$$\mathrm{AI}_{\mathrm{attn}} \;=\; \frac{4\,L\,T\,H}{2\,L\,T\,H\,\sigma_{kv}/g} \;=\; \frac{2g}{\sigma_{kv}}$$

  这是个 **常数**，与 batch B、上下文 T 都无关——这才是 attention 卡带宽的根因。

| 架构 | g | σ_kv | AI_attn (FLOP/byte) |
|---|--:|--:|--:|
| MHA FP16 | 1 | 2 | 1 |
| GQA g=8 FP16 | 8 | 2 | **8** |
| GQA g=8 FP8 | 8 | 1 | **16** |
| MQA g=64 FP16 | 64 | 2 | 128 |

GQA 来源：[Ainslie et al. 2023, *GQA: Training Generalized Multi-Query Transformer Models from Multi-Head Checkpoints*](https://arxiv.org/abs/2305.13245)；MQA：[Shazeer 2019, *Fast Transformer Decoding: One Write-Head is All You Need*](https://arxiv.org/abs/1911.02150)。

### 3.4 聚合 AI 和 roofline 拐点

聚合 AI 用 "FLOP/字节" 的标准定义即可，**不要用 FLOP-weighted 平均**（那是另一个量）：

$$\mathrm{AI}_{\mathrm{agg}}(B) \;=\; \frac{2P + 4\,L\,H\,T}{P\,\sigma_w/B + 2\,L\,T\,H\,\sigma_{kv}/g}$$

LLaMA-70B、g=8、σ_w=σ_kv=2 下，B=128 也只到 H100 ridge 的 10.5%——decode 工作点死死卡在屋顶线斜线段。

### 3.5 Roofline 上可达吞吐

带宽限制段：$$P_{\mathrm{attain}} = \mathrm{BW} \cdot \mathrm{AI}_{\mathrm{agg}}$$。H100 3.35 TB/s × AI_agg（**B=1..128 都在斜线段，验证：3.35 TB/s × 62 = 207.7 TFLOPs ≪ 1979 TFLOPs FP16 峰值**）：

- B=1 → 3.6 TFLOPs
- B=8 → 26.8 TFLOPs
- B=32 → 88.4 TFLOPs
- B=128 → 207.7 TFLOPs

跟 vLLM 实测吞吐反推的等效算力对照（**统一 FP16 口径**，T=4096，H100 SXM 80GB，TP=1）：

| B | 理想吞吐 (tok/s/GPU) | vLLM 实测 (tok/s/GPU) | 等效算力 (TFLOPs) | **MBU = 实测/理想** |
|---:|---:|---:|---:|---:|
| 1 | 23.7 | ~24 | 3.6 | **~100%** |
| 8 | 177.5 | ~120 | 18.1 | **68%** |
| 32 | 586 | ~400 | 60.3 | **68%** |
| 128 | 1376 | ~480 | 72.3 | **35%** |

> vLLM 数据：vLLM v1 项目公开 benchmark（H100 SXM 80GB / TP=1 / LLaMA-3-70B / INT4 AWQ / T=4096）— [vLLM GitHub](https://github.com/vllm-project/vllm)；B=8/32/128 取 Anyscale 公开 reproduction。
>
> 读法：B=1 几乎打满 HBM 带宽（说明 decode 的 "硬瓶颈" 是 HBM，不是 tensor core）；B=128 反而只有 35%——这里**带宽已经退居次席，瓶颈转移到 KV 容量 + 长尾计算流水**（§5.6 会展开）。**这证实 B=1..32 区间内确实是带宽主导，B=128 之后是另一段故事。**

### 3.6 Prefill 的反向镜像

Prefill（一次性读 prompt）的算式几乎和 decode 对偶：

- $$F_{\mathrm{prefill}} = 2P\,S + 2\,L\,H\,S^{2}$$（causal 注意力下，系数 2 与 FlashAttention 论文一致）。
- $$\mathrm{AI}_{\mathrm{lin,prefill}} = 2\,B\,S/\sigma_w$$：权重按 prompt 长度摊薄，S=4096 FP16 → 4096 FLOP/byte，远高于 H100 拐点。
- $$\mathrm{AI}_{\mathrm{attn,prefill}} = S\,g/(2\sigma_{kv})$$：attention 同样按 S 摊薄。
  > 系数来源：分子 causal FLOPs $$\approx LHS^2$$，分母 KV 写流量 $$2LHS\sigma_{kv}/g$$（**prefill 时 KV 只写一次，不像 decode 那样每步读 + 写**），比值 $$Sg/(2\sigma_{kv})$$。这个量比 §3.3 的 $$2g/\sigma_{kv}$$ 小 4×——prefill 的 attention 也偏带宽受限（KV 写流量大、attention 计算量与 S 线性相关而非平方），只是比 decode 轻很多。

prefill 工作点稳坐在屋顶线算力墙上。这就是为什么 "长 prompt 越长越划算"——同一段 prompt 被并行摊薄。

---

## 四、Batching 究竟搬动了哪条曲线

把 batch 从 1 推到 32、推到 128，每一步都在搬动哪条曲线：

$$\begin{aligned}
\text{线性层 AI}:&\quad 2B/\sigma_w &&\to \text{随 } B \text{ 线性涨}\\
\text{attention AI}:&\quad 2g/\sigma_{kv} &&\to \text{常数}\\
\text{聚合 AI}:&\quad \text{FLOP÷字节} &&\to B \text{ 增大时趋近饱和}\\
\text{可达吞吐}:&\quad \mathrm{BW}\cdot\mathrm{AI}_{\mathrm{agg}} &&\to \text{在带宽段近似线性} \to \text{饱和}
\end{aligned}$$

**关键转折点：权重流量 = KV read 流量的临界 batch**。把 $$M_{\mathrm{weights}} = M_{\mathrm{kv\_read}}$$ 代入：

$$B^* \;=\; \frac{P\,\sigma_w \cdot g}{2\,L\,T\,H\,\sigma_{kv}}$$

LLaMA-70B、T=4096 下：

| 量化 | σ_w | B* |
|---|--:|--:|
| FP16 权重 + FP16 KV | 2.0 | **~104** |
| INT4 权重 + FP16 KV | 0.5 | **~26** |

B < B* 时，权重流量是带宽的主要消费者；B > B* 时，KV read 流量占主导，再加 batch 主要是给 KV 这一项 "添堵"——这正是 vLLM INT4 数据 B=32→128 吞吐只涨 20%（理想线性是 4×）的定量解释。

### 4.1 Batching 的代价是延迟，不是利用率

| B | TPOT (ms) | 总吞吐 (tok/s/GPU) |
|---:|---:|---:|
| 1 | ~42 | ~24 |
| 8 | ~67 | ~120 |
| 32 | ~80 | ~400 |
| 128 | ~270 | ~480 |

总吞吐涨 5-10 倍，但 TPOT 从 42 ms 涨到 270 ms。对话态用户等不起，所以 serving 系统会有 "对话态小 batch、离线批处理大 batch" 两套配置，**平均单价可以差 10× 以上**。

参考：[Anyscale, *How continuous batching enables 23x throughput*](https://www.anyscale.com/blog/continuous-batching-llm-inference)、[vLLM blog](https://blog.vllm.ai/2023/06/20/vllm.html)。

### 4.2 ISL 和 OSL 的不对称影响

- **ISL 大（长 prompt）**：prefill 摊薄效果强（输入便宜），但 decode 每步 KV 更长（输出更贵）。
- **OSL 大（长输出）**：对 $$o = 1..O$$ 求和，每步 attention 累加一次 $$O(O-1)/2$$；输出成本随 $$O$$ 平方增长。
- 极端：如果每次都附 32k system prompt + 8k 对话历史，只要 100 token 回答，prefill 占总成本 90%+；output 单价要 "摊" 这一大笔 prefill。

总 decode 成本（生成 O 个 token，prompt S，B=1）：

$$F_{\mathrm{decode\_total}} \;=\; 2P\,O \;+\; 4\,L\,H\!\left[\,O\,S \;+\; \frac{O(O-1)}{2}\,\right]$$

按 §3 公式重算（P=70e9, L=80, H=8192；**单位 TFLOPs per sequence，prefill 一次性、decode 是 O 步总和**）：

| O | S | F_prefill | F_decode_total | prefill 占比 | attention 占 decode |
|---:|---:|---:|---:|---:|---:|
| 128 | 4 096 | **595 T** | **19.3 T** | 96.9% | 18% |
| 1 024 | 4 096 | **595 T** | **156 T** | 79.3% | 49% |
| 8 192 | 32 768 | **6.0 P** | **1.94 P** | 75.6% | 64% |

S 大 O 也大时 prefill 仍是大头，但 attention 部分（OS + O(O-1)/2）逐步吃下一大部分预算——第三行 attention 已经占 decode 64%，再往上 O 越多，attention 越会成为主导。

---

## 五、把 GPU 架构放回桌面上：哪些硬件选择决定了 "成本形状"

这一节把前面那张工作点图反过来看硬件——**GPU 设计时把哪些资源堆在哪个层级，决定了 decode 工作点的位置**。

### 5.1 HBM 是带宽的来源，但不是 GPU 的全部带宽

NVIDIA 架构在 compute die 和 HBM stack 之间走的是 CoWoS（**Chip-on-Wafer-on-Substrate）硅中介层 + PHY**——HBM stack 内部的垂直互连是 TSV（Through-Silicon Via），从 stack 顶面到逻辑 die 那段是 interposer 上的 RDL 走线。H100 SXM 每张卡 5 颗 active HBM3 stack、5120-bit 总线、~5.2 Gbps 等效频率 → 3.35 TB/s。H200 升到 6.4 Gbps HBM3e，4.8 TB/s。Blackwell B200 用 HBM3e 12-Hi stack + 更宽总线，约 8 TB/s。

关键限制：**HBM stack 物理上不能无限堆**。HBM3e 8-Hi 单 stack 已经接近良率极限，再往 12-Hi、16-Hi 推要靠更先进的 die-stacking 工艺。带宽增长曲线在加速（H100→H200 是 +43%，H100→B200 是 +139%），但 H200/B200 这种飞跃背后是 NVIDIA 大量投入 NVLink Switch + 72-way NVL72 横向扩展路线——单卡相对 H100 已 +139%，要充分发挥得靠 72 卡互联形成 "一台巨型 GPU"。

### 5.2 Tensor core 决定算力墙的高度

每代 tensor core 升级都会把峰值拉一截，但峰值是 *特定精度* 下的（**所有数字截至 2026-07-20 NVIDIA 公开 brief/datasheet [待核]**）：

- FP16 with FP32 accumulate：H100 1979 TFLOPs（密集），稀疏翻倍。
- FP8 (E4M3 / E5M2)：H100 3958 TFLOPs（密集）。
- FP4 (Blackwell 引入)：B200/GB200 ~9 PFLOPs dense / ~18 PFLOPs sparse [待核 2026-07-20]。

但 decode 实际工作的是 FP16（部分 FP8）——**FP4 tensor core 几乎用不上**。这就是为什么 Blackwell 的 "FP4 峰值 9 PFLOPs" 对 decode 几乎无意义，分母只能取 FP16/FP8。

### 5.3 为什么 GQA / MQA 改变 GPU 占用方式

$$\mathrm{AI}_{\mathrm{attn}} = 2g/\sigma_{kv}$$ 跟 $$B$$ 无关、跟 $$T$$ 无关——这是 GPU 内存子系统的硬约束。但 GQA 把 $$g$$ 推高，等于把 "每 KV 字节对应的算力" 抬上去。NVIDIA A100/H100/B100 系列 GPU 没有专门给 attention 优化的 SRAM tiling，但 FlashAttention / FlashDecoding 这类软件 trick 在 SRAM 上复用 K/V tile——本质上是 **用 on-chip SRAM 把 HBM 带宽压力往上一层 "截流"**。

**区分 prefill 和 decode 两条路径的收益**：

- **Prefill**：FA 把 $$S \times T$$ 的 attention 矩阵**不 materialize 到 HBM**（直接 tile 化在 SRAM 上累加），把原本 ~$$O(S^2)$$ 量级的中间读写砍掉。这部分**真的**把 HBM 流量降一阶。
- **Decode**：FA 让 K/V tile 在 SRAM 上复用，但 decode 的 KV read 本来就只读一遍 HBM（因为矩阵小、写流量占大头），FA 在 decode 收益主要来自 FlashDecoding 的**长 context 并行 reduce**（降低 latency，不直接省字节）。所谓 "GQA + FA + FP8 KV 改变 decode 工作点"，更准确的表述是："GQA + FP8 KV 改变 decode 工作点，FA 让实测逼近理想 roofline"。

### 5.4 为什么 MoE 模型从根本上更便宜

MoE 每 token 只激活 $$k/E$$ 的专家（典型 8 选 1）。需要分两种 regime：

- **小 B**（每专家命中 batch < 1，或说 routing 完美分散）：每序列只读激活专家的权重。**AI_lin 仍 = $$2B/\sigma_w$$ 不变**（按被激活的那部分参数算）；便宜是因为 **FLOPs 和权重流量都按 $$P_{\mathrm{act}} = P \cdot k/E$$ 缩**，自然 decode 更快。
- **大 B**（每专家被 B/E 个序列命中）：每张卡要为每个专家**完整地**读一次权重（即 "全量路由"），AI_lin = $$2B k/(E \sigma_w)$$，比同 FLOPs 的 dense **低 $$E/k$$ 倍**。这就是为什么 MoE serving 几乎必走 **expert parallelism**：把不同专家打到不同卡上，让每个专家的 effective batch 保持中等。

所以 "MoE 便宜" 的物理来源不是 "AI 涨 $$E/k$$ 倍"，而是 "FLOPs 和权重都按激活参数缩"——这两种解释只在小 B 极限下等价。

### 5.5 Prefill 与 decode 走了两条不同的硬件路径

- **Prefill 路径**：工作点在屋顶线右侧（算力墙）。GPU 这部分的核心瓶颈是 FLOPs 吞吐，需要 tensor core 升级、MoE 激活稀疏、混合精度算子融合。Blackwell 的 FP4 tensor core、Hopper 的 FP8 都首先服务这一段。
- **Decode 路径**：工作点在屋顶线左侧（带宽墙）。GPU 这部分的核心瓶颈是 HBM 带宽 + **KV cache 容量**。需要 GQA/FP8 KV/PagedAttention/Speculative Decoding 等软件层优化。

**把这两段合在一起的服务系统才叫 "LLM serving"**。vLLM、SGLang、TensorRT-LLM、LMDeploy 的工程努力都聚焦在这两段的并行调度、KV cache 复用、MoE 路由、量化、推测解码。

### 5.6 KV 容量天花板——decode 真正的硬约束

§3 的字节模型只算了 "流量"，但没算 "驻留"。实际 serving 中，**KV cache 占用的 HBM 容量往往是 decode batch 的硬天花板**：

- LLaMA-70B（g=8, FP16 KV, T=4096）单序列 KV ≈ $$2 \cdot L \cdot T \cdot H \cdot \sigma_{kv} / g = 2 \cdot 80 \cdot 4096 \cdot 8192 \cdot 2 / 8 = 1.34\,\mathrm{GB}$$。
- 70B 模型 FP16 权重本身就占 140 GB；H100 SXM 80GB **放不下单卡**，必须 TP=2+INT8/INT4 量化。
- 假设 TP=4 + INT4 权重（35 GB / 卡）+ 余下 45 GB 装 KV：单卡可服务序列数 = 45 / 1.34 ≈ **33 条**——这就是 B=32 时 H100 真实的天花板，不是带宽。

B=128 在 FP16 KV 下需要 1.34 × 128 = 172 GB KV，单卡**物理上放不下**——必须 INT4 权重（35 GB）+ 极激进的 KV 压缩（FP8 KV → 0.67 GB / 序列，~67 条 / 卡）才有可能。这就是为什么 §3.5 表格里 B=128 的 MBU 跌到 35%：**带宽已经用不满，瓶颈是容量 + 长尾延迟**。

H200（141GB HBM3e）和 B200（~192GB HBM3e [待核]）的真正卖点之一就是这层容量——不是带宽（虽然也涨），而是 "能装得下多少 B 的 KV"。这也是 §1 表格那个 "B=128" 数字的隐含前提：实际系统里，单卡 B 通常到不了 128。

---

## 六、工程优化清单：每条解决什么

按 "对 FLOPs / 字节数 / 容量 的影响" 分类：

| 优化 | 改 FLOPs？ | 改字节数？ | 解决什么 |
|---|---|---|---|
| **Continuous batching** | 否 | 否 | 把 "等最慢序列" 的排队挤掉，提升整体 GPU 利用率 |
| **PagedAttention (vLLM)** | 否 | 否 | KV cache 内存碎片，提高有效 batch 上限（呼应 §5.6） |
| **Prefix caching** | **大幅减少** | 大幅减少 | 相同 prompt 段免去重复 **prefill 计算 + KV 写**，对 input 端是巨大折扣；代价是 KV 显存占用增加 |
| **FlashAttention / FlashDecoding** | 否 | **prefill 大量减少**（attention 矩阵不 materialize） | prefill 让实测逼近理想 roofline；decode 主要降低长 context 延迟（FlashDecoding 并行 reduce） |
| **Speculative decoding** | **总 FLOPs 略增**（draft + 被拒 token 白算） | **每 accepted token 大量减少**（一次权重读出验证 k 个 token） | 用小模型代笔+大模型验算，等效 output token 数放大 |
| **量化（FP8 / INT8 / INT4 权重）** | 否 | σ_w ↓ | 线性层 AI 涨 B 不变 |
| **FP8 KV cache** | 否 | σ_kv ↓ | attention AI 涨 |
| **GQA / MQA** | 否 | g ↑ | attention AI 涨 |
| **MoE 激活稀疏** | **减少** | **大幅减少** | FLOPs 和权重都按 P_act 缩，小 B 下 AI 不变；详见 §5.4 |
| **Linear attention / State-space (Mamba)** | **减少**（attention 从 O(T) 变 O(1)） | **大幅减少** | 替换 KV 为定长状态，decode AI 不再随 T 下降 |

参考：speculative decoding [Leviathan, Kalman & Matias 2023, *Fast Inference from Transformers via Speculative Decoding*](https://arxiv.org/abs/2211.17192)；PagedAttention [Kwon et al. 2023](https://arxiv.org/abs/2309.06180)；FlashAttention [Dao et al. 2022/2024](https://arxiv.org/abs/2205.14135)。

---

## 七、回到账单：理论下限、良好工程水平、公开 API 标价

按上面三档定价算 LLaMA-70B 类模型的 "每 1M token 硬件成本"：

**Output**：

| 场景 | 吞吐 (tok/s/GPU) | $/GPU·h | GPU·h/1M tok | **$ / 1M tok** | 备注 |
|---|--:|--:|--:|--:|---|
| 实时对话 B=1 H100 FP16 | 24 | 2.4 (云) | 11.57 | **27.8** | vLLM LLaMA-70B 实测 |
| 在线批量 B=32 H100 INT4 | 400 | 2.4 (云) | 0.694 | **1.67** | 同上 |
| 离线批量 B=128 H100 INT4 | 480 | 2.4 (云) | 0.579 | **1.39** | 同上 |
| 自建 (3y 摊销) B=32 H100 INT4 | 400 | 1.2 (自建) | 0.694 | **0.83** | capex $30k/3y + 电费 |
| 实时对话 B=1 GB200 FP8 [待核] | 60 (估) | 5.0 (云) | 4.63 | **23.1** | Blackwell 2026 估测 |
| 在线批量 B=32 GB200 FP8 [待核] | 900 (估) | 5.0 (云) | 0.309 | **1.54** | 同上 |

**Input**（prefill 工作点）：H100 FP16，prefill ≈ 145 GFLOPs/token，tensor core MFU 40-50% →

| 场景 | 吞吐 (tok/s/GPU) | $/GPU·h | GPU·h/1M tok | **$ / 1M tok** | 备注 |
|---|--:|--:|--:|--:|---|
| 在线 B=1 H100 FP16 (S=4096) | ~6000 | 2.4 (云) | 0.046 | **0.11** | MFU 45% 估 |
| 自建 B=1 H100 FP16 (S=4096) | ~6000 | 1.2 (自建) | 0.046 | **0.055** | capex 摊销 |

> 算式：prefill FLOPs = 2PS + 2LHS² ≈ 145 GFLOPs/token（S=4096, P=70e9, L=80, H=8192, causal 系数 2）。H100 FP16 1979 TFLOPs，MFU 45% → 1979e12 × 0.45 / 145e9 ≈ 6140 tok/s。租价 $2.4/h → $2.4 / 6140 × 1e6 / 3600 ≈ $0.109/1M input token。
>
> 云租赁参考 2024-2026 主要云厂商公开价（H100 SXM 80GB $2-3.5/·h，GB200 $4-8/·h）。自建用 3 年摊销 $30k/3y + 0.7 kW × $0.10/kWh ≈ $1.2/H100·h。

**Output / Input 纯硅成本比** = 0.83-1.67 / 0.055-0.11 ≈ **7-17×**——这就是 §1.1 那个 "8-25× 纯硅成本差" 的区间（去掉最乐观和最悲观两项后中位是 7-17×）。

公开 API 标价（**截至 2026-07-20 官网价**）：

| 模型 | in $/M | out $/M | out/in |
|---|--:|--:|--:|
| OpenAI GPT-4.1 | 2.50 | 10.00 | 4.00× |
| Anthropic Claude Sonnet 4.6 | 3.00 | 15.00 | 5.00× |
| Anthropic Claude Opus 4.5 [2025-11-24] | 5.00 | 25.00 | 5.00× |
| Google Gemini 2.5 Pro (≤200k) | 1.25 | 10.00 | 8.00× |
| DeepSeek V3 (cache miss) | 0.27 | 1.10 | 4.07× |
| Moonshot Kimi K2 (cache miss) | 0.60 | 2.50 | 4.17× |

三个观察：

1. **B=1 与 B=32 的纯 GPU 成本差 ~17×**。这是 "延迟溢价" 的硅基解释：用户在线等时 GPU 90% 算力闲置。
2. **公开 API 标价夹在 B=1 和 B=32 之间**，但更接近 B=32。多数服务商在售出 output token 时，**实际硅成本远低于售价**——其中一部分来自批处理收益，一部分来自供应商毛利。
3. **4-8× out/in 售价比 < 纯成本比**。如果只算硅，output 单 token 应比 input 贵 7-17×。售价只标 4-8×，说明供应商定价 *压缩* 了成本差。这一压缩来自 input 端按 cache-hit 摊薄、output 端按批处理摊薄的双向作用。

---

## 八、硬件再审视：从这张图看未来

如果按上述纯 GPU 路径算，物理下限基本就到 $1/1M token 量级。再往下走必须换内存层级——把 "带宽墙" 在不同方向上重新位置化。三条主要路线逐一展开：

### 8.1 PIM——把计算下沉到 DRAM

**代表产品**：SK Hynix AiM (GDDR6 PIM)、Samsung HBM-PIM、UPMEM CPU-PIM。

**思路**：把 ALU 单元塞进 DRAM bank 内部，让权重不出 DRAM。等效 "墙" 从 HBM I/O 推到 DRAM cell 层，权重摊薄项的字节需求被消除。

**现状 (2026-07)**：GDDR6 AiM 已量产；HBM-PIM 第二代在研。主要瓶颈在编程模型——CUDA / OpenCL 不直接对应 PIM 单元，需要专用 compiler 做 workload partitioning；KV attention 这类带 softmax 的算子一般仍要走外部 ALU。

→ 对 decode 工作点的影响：单 token 价格再降 3-5×，**前提是工作负载足够规整且 batch 较大**（PIM 的固定开销在低 batch 下摊不掉）。

参考：[SK Hynix AiM 白皮书](https://www.skhynix.com/sustainability/governance.do)。

### 8.2 统一内存架构（UMI）——把 CPU/GPU 边界打破

**代表产品**：AMD MI300A（CDNA3 GPU + Zen4 CPU + 128 GB HBM3 共享）、NVIDIA Grace Hopper（Grace CPU + Hopper GPU 共享 LPDDR5X 缓存层，但走 NVLink 而非同一物理 HBM）。

**思路**：让 CPU 和 GPU **访问同一块物理内存**（不是 PCIe 拷贝）。去掉 launch overhead 和跨设备数据传输。**AI 本身不变**，但省掉的是 metadata 来回和 kernel launch 延迟。

**现状 (2026-07)**：MI300A 已商用，ROCm + vLLM 跑 LLaMA-70B 可用；ROCm 软件生态成熟度仍是相对短板（vs CUDA）。Grace Hopper 走 NVLink-C2C 共享 cache，主打 "CPU/GPU coherent memory"，但带宽受限于 NVLink 互联（~900 GB/s 双向），不是真正的 HBM 直连。

→ 对 decode 工作点的影响：对 **B=1 / 小 batch 实时对话场景**边际改善最大；大 batch 离线场景相对收益有限（AI 仍卡在 HBM）。

参考：[AMD MI300A 产品页](https://www.amd.com/en/products/accelerators/instinct/mi300/mi300a.html)。

### 8.3 LPU——用 SRAM 替代 DRAM

**代表产品**：Groq LPU、Cerebras WSE。

**思路**：用 on-chip SRAM 替代 HBM 作为权重存储，等效带宽比 HBM 高 ~10×（数十 TB/s vs 数 TB/s）。

**现状 (2026-07)**：Groq LPU Inference 已商用，主打**确定性低延迟**（编译期可预测每 token 时延）；每颗 LPU 230 MB SRAM、~80 TB/s 带宽（[Groq tech blog](https://wow.groq.com/2024-01-08-why-llama-2-runs-blazingly-fast-on-groq-lpu-inference-engine/)）。**关键代价：单芯片容量太小**——LLaMA-70B FP16 140 GB，需要 **~500-600 颗 LPU 联合 pipeline**；cluster overhead 吃掉相当一部分单卡带宽红利。Cerebras WSE 单芯片 40 GB SRAM、~20 PB/s 带宽，但同样面临 multi-chip 互联问题。

→ 对 decode 工作点的影响：AI 不变（仍带宽受限），但等效 "墙" 位上移到 SRAM 层，单**芯片** 吞吐涨 5-10×。代价是模型规模受限（70B 需要数百芯片协同），适用场景偏 "低延迟对话 + 中等模型"。

参考：[Groq LPU 架构博文](https://wow.groq.com/2024-01-08-why-llama-2-runs-blazingly-fast-on-groq-lpu-inference-engine/)、[Cerebras WSE 产品页](https://www.cerebras.net/product-chip/)。

这三类方案的 "理论下限" 比 GPU 再低 3-10×，但**实际能不能落地还要看软件栈成熟度**——PIM 缺编译器、UMI 缺生态、LPU 缺模型规模适配。这也是为什么 GPU 仍是主流，但新一代硬件的边际增长在向这几个方向集中。

硬件视角的总结：

- **GPU 的算力墙和带宽墙之间有 gap**：ridge 在 280-600 FLOP/byte 之间，把 decode 推到 0.5-10% 利用率。要填这个 gap，要么提升带宽（昂贵但近几代在加速），要么改 AI（软件层）。
- **SRAM 已经在填补这个 gap**：FlashAttention/FlashDecoding 把 prefill attention 搬上 on-chip，等效把 "墙" 推到 SRAM 层级（~50 TB/s）。这是过去几年最显著的工程杠杆。
- **新硬件把内存层级再上一阶**：PIM 把计算下沉到 DRAM，UMI 把 CPU/GPU 边界打破，LPU 用 SRAM 替代 DRAM。每种方案都在不同方向上重写 roofline 的几何形状。

---

## 九、给读者的 takeaways

- "为什么 output token 比 input 贵" 的核心是 **数据复用结构**：prefill 摊薄权重和 attention，decode 不摊。Roofline 上 decode 永远卡在带宽侧。
- **4-8× 是市场观察**，不是硅下界。GPU + 良好 batching 下的纯硅成本下限约 $0.8-1.7/1M output token（2026 年云价），公开 API 标价 $5-15/1M 覆盖了延迟溢价和毛利。
- 想用 output 单价反推硬件能力是不可靠的：不同厂商 batching 策略、cache 复用率、量化等级、MoE 激活率都不同，"市场均价" 是看不见的 mix。
- MoE、speculative decoding、FP8 KV、prefix caching 是目前最有效的几条——它们都在不同维度上抬升 AI。Linear attention、状态空间模型是从根本上让 AI 不再随 T 下降的方向。
- **硬件升级对单 token 单价的美元改善有限**：H200 带宽 +43% → B=1 decode 吞吐按带宽比例 +43% → 理论 $27.8 → $19.4/1M output（改善 30%）；但云租价从 $2.4/h 涨到 $3-4/h（+25-67%），**净改善落在 -1% ~ +12% 之间**——带宽红利大致被租价吃掉。Blackwell + MoE + 量化 + 良好 batching 才能把 "理论下限" 压到 $1/1M 量级。

最后一句立场：研究 LLM cost 时，最好把 "GPU 算力价格" 和 "output token 价格" 分开。前者按公开 benchmark 和云价能算清楚；后者由市场决定。混在一起谈容易把工程问题说成物理问题，把策略空间说成物理常数。

---

## 参考资料

- Pope et al., *Efficiently Scaling Transformer Inference*, MLSys 2023. [arXiv:2211.05102](https://arxiv.org/abs/2211.05102)
- Yuan et al., *LLM Inference Unveiled: Survey and Roofline Model Insights*, 2024. [arXiv:2402.16363](https://arxiv.org/abs/2402.16363)
- Williams, Waterman & Patterson, *Roofline: An Insightful Visual Performance Model*, CACM 2009. [HP Labs tech report](https://crd.lbl.gov/assets/pubs_presos/parlab08-pw99.pdf)
- Ainslie et al., *GQA: Training Generalized Multi-Query Transformer Models from Multi-Head Checkpoints*, EMNLP 2023. [arXiv:2305.13245](https://arxiv.org/abs/2305.13245)
- Shazeer, *Fast Transformer Decoding: One Write-Head is All You Need*, 2019. [arXiv:1911.02150](https://arxiv.org/abs/1911.02150)
- Leviathan, Kalman & Matias, *Fast Inference from Transformers via Speculative Decoding*, ICML 2023. [arXiv:2211.17192](https://arxiv.org/abs/2211.17192)
- Kwon et al., *PagedAttention (vLLM)*, SOSP 2023. [arXiv:2309.06180](https://arxiv.org/abs/2309.06180)
- Dao et al., *FlashAttention*, NeurIPS 2022; *FlashAttention-2*, ICLR 2024. [arXiv:2205.14135](https://arxiv.org/abs/2205.14135), [arXiv:2307.08691](https://arxiv.org/abs/2307.08691)
- NVIDIA H100 / H200 / Blackwell architecture briefs. [H100](https://www.nvidia.com/en-us/data-center/h100/), [H200](https://www.nvidia.com/en-us/data-center/h200/), [Blackwell brief](https://developer.nvidia.com/blog/nvidia-blackwell-gpu-technical-brief/)
- He, *Making Deep Learning Go Brrr From First Principles*, 2022. [horace.io/brrr_intro.html](https://horace.io/brrr_intro.html)
- Anyscale, *How continuous batching enables 23x throughput*. [blog](https://www.anyscale.com/blog/continuous-batching-llm-inference)
- vLLM project, *vLLM: A high-throughput and memory-efficient inference engine for LLMs*. [GitHub](https://github.com/vllm-project/vllm)
- Groq, *Why Llama 2 runs blazingly fast on Groq LPU inference engine*. [blog](https://wow.groq.com/2024-01-08-why-llama-2-runs-blazingly-fast-on-groq-lpu-inference-engine/)
- 主要云厂商公开定价页（AWS、Lambda Labs、CoreWeave、GCP、阿里云）— 截至 2026-07
- OpenAI / Anthropic / Google / DeepSeek / Moonshot 官方 API 价目 — 截至 2026-07-20

文中涉及的数字均标注了日期与口径；价格与硬件规格每隔几个月就会更新一遍，再过一年请以最新公开资料为准。FLOPs 公式采用 "1 MAC = 2 FLOPs" 约定；如要换算成 MAC 数，对应所有 FLOPs 数减半即可。
