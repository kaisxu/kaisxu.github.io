---
layout: post
title: "从 output token 到 roofline：一份对 GPU 推理架构的再审视"
date: 2026-07-18 09:00:00 +0800
categories: [tech, gpu, inference]
math: true
---

任何 LLM API 的价目表里，output token 永远比 input token 贵——大多在 4-8 倍之间（这里说的是**闭源 frontier API**：OpenAI / Anthropic / Google；开源模型如 Llama-70B 在 Together / Fireworks 上 out/in ≈ 1×）。看起来像定价策略，但只要把单步 decode 的工作点画到 roofline 上，就能看到这是 **数据复用结构** 决定的工程结果，不以哪一家的定价为转移。

顺着这个观察一路往下，会遇到一连串有意思的问题：

- 为什么 batch 越大单 token 越便宜、但用户能感觉到的延迟也越长？
- 为什么 GQA、FP8 KV、quantization 这些优化并不改变 FLOPs，却能让 GPU "干活变多"？
- 为什么 H200、B200 这类带宽更强的卡，对单 token 单价的边际改善远小于直觉？
- 为什么 MoE 模型以同样总参数能便宜那么多？

本文想做的不是另一个 "decode 为什么慢" 的科普贴，而是把 *从 output token 这一具体现象倒推到 roofline、再倒推到 GPU 硬件设计选择* 这条链条整理清楚。**这条链的终点是一个实用结论：decode 时代评价加速器，标称 TFLOPs 是无效指标——尤其 sparse 营销值（H100 / B200 datasheet 上的 1979 / 4500 是 2:4 结构化稀疏，decode 路径走不到）——该看的是 TB/s、HBM 容量，以及更长期的内存层级重构（§8）。** 最后再回到一份可复算的账单，看看公开 API 标价和 "纯硅成本" 之间到底差多少——结论会让你意外：两者其实没差那么多。

---

## 一、从 output token 开始：一个具体的工作点

单 token decode 看起来很简单：跑一遍 80 层 Transformer，吐一个 token。但里面发生的事情可以用 roofline 量化：

$$\mathrm{AI} \;=\; \frac{\text{一次推理做的浮点运算}}{\text{一次推理从显存读出的字节数}}\quad(\mathrm{FLOP/byte})$$

把 LLaMA-70B 类 dense 模型（GQA $$g=8$$，FP16 权重 + FP16 KV）在 H100 上拆开，单 token 算术强度大致是：

| B (batch) | 线性层 AI (FLOP/byte) | attention 算术强度 (FLOP/byte) | 聚合 AI (FLOP/byte) | 相对 H100 ridge dense (295) |
|---:|---:|---:|---:|---:|
| 1 | 1.0 | 8.0 | **1.07** | **0.36%** |
| 8 | 8.0 | 8.0 | **8.00** | **2.7%** |
| 32 | 32.0 | 8.0 | **26.4** | **8.9%** |
| 128 | 128.0 | 8.0 | **62.0** | **21%** † |

† B=128 这个数字本身是**理论上限**：70B FP16 + 1.34 GB/序列 KV 在 H100 SXM 80GB 单卡物理上放不下（详见 §5.5，TP=1 INT4 + 余 45 GB 装 KV 也只能服务 ~33 条），实际部署需 TP ≥ 2 + 量化 + 容量调整。本节先在 "假设能跑" 的口径下展示 AI 随 B 的趋势；§5.5 会展开 KV 容量的硬约束。

B=128 都还没到 H100 ridge (dense) 的 21%。这就是 output token 工作的真实 "地形"：**永远坐在 roofline 的带宽侧**。

![Decode 与 prefill 工作点在 roofline 上的位置（H100/H200/B200 三条 BW 斜线 + FP16 dense 算力墙）](/assets/img/decode-roofline.svg){: width="900" height="600"}

注意表格里有两个完全不同的量：线性层 AI 是 $$2B/\sigma_w$$（按 batch 摊权重），attention 算术强度是 $$2g/\sigma_{kv} = 8$$（与 batch 无关、与上下文长度无关——**与 batch 无关**才是它卡带宽的根因：让 attention 摆脱带宽只能改 $$g$$ 和 $$\sigma_{kv}$$，即 GQA / FP8 KV，详见 §3.3）。前者解释了 batching 的红利，后者解释了为什么 batching 不能无限提升。第三节会从公式推导。

> **图 1（§一）**：上文的 roofline 图把 H100/H200/B200 三条 BW 斜线、FP16 算力墙、四个 decode 工作点（B=1/8/32/128）和一个 prefill 工作点画在同一张图上——它就是全文的直觉锚点。

### 1.1 几个常见误解，先标出来

网上不少资料把这些数字讲得太 "硬" 了，进入推导之前先把四条要修正的事说清楚——每条在后文某节展开：

- "Attention 算术强度恒等于 1 FLOP/byte"——只对 MHA + FP16 KV 成立。Llama-3、Qwen 等 2024 年后的主流模型都用了 GQA（$$g=8$$），attention AI 在 FP16 KV 下是 8、在 FP8 KV 下是 16。**§3.3** 详推。
- "4-8× 价格差是物理常数"——这是**闭源 frontier API**（OpenAI / Anthropic / Google）的观察；batched serving 下纯硅成本差 ~6-8×，B=1 端到端 ~127×（详见 §7）。4-8× 价目与 batched 纯硅 6-8× **基本一致**，定价大致按 batched 成本结构定的（注意 Together / Fireworks 等跑开源 70B 的服务商 out/in ≈ 1×，是因为它们按 blended 成本 + input 大流量定价竞争，不在 4-8 规律内）。**§7**。
- "H100 decode 时只跑出峰值算力的 0.5%"——结论跟口径有关：FP16 dense 严格匹配时 H100 B=1 是 0.36%（即 AI_agg=1.07 ÷ ridge_dense=295）；如果把分子换成 sparse 1979 TFLOPs，"利用率"虚升至 0.18%，**这把 sparse 营销值洗成"$'ll 0.18% 都吃不到"的相反结论**——下一条会回到这个问题。换成 Blackwell FP4 tensor-core 峰值做分母会更难看；GQA 比 MHA 略好。**§3.5**。
- "PIM / UMA / LPU 是颠覆性突破"——这些方案都把内存层级往上推一阶，绕开 HBM 带宽墙。结构上的差异是**每 FLOP 可用字节数（byte/FLOP）**：H100 是 1 FLOP ≈ 0.0034 字节（FP16 dense = 989 TFLOPS : 3.35 TB/s = 1 : 295），Groq LPU 是 1 FLOP ≈ 0.43 字节（FP16 188 TFLOPS : 80 TB/s = 1 : 2.3）——**把 byte/FLOP 从 ~0.003 提到 ~0.4 量级**，让 ridge 在 AI 轴上**左移**（295 → 2.3 量级）。对 decode 工作点的实际效果：B=1 时 AI_agg=1.07 仍小于 2.3，还在带宽段；B≥8 时 AI_agg ≥ 8.0 才跨越 ridge 落到算力段——**对 batched serving 这些方案才真正显效**（详见 §8.3）。

---

## 二、Roofline：再讲一遍，但只讲 GPU 推理相关的

Roofline 把 "一个算子在这块卡上能跑多快" 这件事收成两条线：

- **斜线段**：$$P_{\mathrm{attain}} = \mathrm{BW} \cdot \mathrm{AI}$$（被带宽限制，斜率 = HBM 带宽 BW）。
- **水平线段**：$$P_{\mathrm{attain}} = F_{\mathrm{peak}}$$（被算力限制，高度 = 峰值 FLOP/s）。
- **拐点**：$$\mathrm{AI}_{\mathrm{ridge}} = F_{\mathrm{peak}} / \mathrm{BW}$$。H100 SXM FP16 tensor core **dense** $$989\,\mathrm{TFLOPs} / 3.35\,\mathrm{TB/s} \approx 295\,\mathrm{FLOP/byte}$$；sparse 营销值 1979 TFLOPs 给出 ridge = 590，但 decode 不会走 sparse 路径（详见 §5.2），本文 ridge 一律取 dense。

把当前一代旗舰的拐点列一下（**注意 FP4 tensor core 在 decode 中几乎用不上，详见 §5.2**）。表中所有数字都是 **FP16 dense**，sparse 营销值见脚注：

| 卡 | HBM 带宽 | FP16 dense (TFLOPs) | FP16 ridge (dense) | FP8 dense (TFLOPs) | FP8 ridge (dense) | decode 利用率 (B=128, FP16 dense) |
|---|---:|---:|---:|---:|---:|---:|
| A100 SXM | 2.0 TB/s | 312 | 156 | — | — | **40%** |
| H100 SXM | 3.35 TB/s | **989** | **295** | 1979 | 590 | **21%** |
| H200 SXM | 4.8 TB/s | **989** | 206 | 1979 | 412 | **30%** |
| B200 SXM | 8.0 TB/s | **2250** | 281 | 4500 | 562 | **22%** |
| GB200 (NVL72 per package 估) | ~8 TB/s | 2500 | 312 | 5000 | 625 | **20%** |

> 上表所有 FP16 / FP8 / HBM 规格截至 2026-07-20 NVIDIA 公开 brief/datasheet 口径。**Sparse 营销值**：H100/H200 FP16 sparse = 1979 / FP8 sparse = 3958（A100 不支持 2:4 sparse）；B200/GB200 5th-gen Tensor Core 仍支持 2:4 sparse，FP16 sparse = 4500 / 5000。**Decode 永远不会跑到 sparse**（详见 §5.2）。

观察：H100 → B200 的 **FP16 dense 峰值从 989 涨到 2250（+127%）**，带宽从 3.35 涨到 8 TB/s（+139%）——两条线涨幅接近，ridge（dense）从 295 降到 281，**几乎没变**。对 decode 工作点（LLaMA-70B、FP16 权重 + FP16 KV、B=128 时 AI_agg ≈ 62）这件事意味着两件事：

- **其一，工作点始终留在带宽段**（62 ≪ 295 / 281），所以标称 TFLOPs（dense 或 sparse）对 decode 都是无效指标——选哪条 ridge 都比工作点高 4-9×。**评 decode 性价比应该看 TB/s，不看 TFLOPs**。每一代的 decode 吞吐收益约等于带宽增幅本身——H200 +43%，B200 +139%。
- **其二，表中 "decode 利用率" 是诊断量，不能当性能指标读**——它衡量的是 "FP16 峰值被浪费了多少"，不是 "硬件升级带来了多少收益"。它**随口径变化**：取 sparse 1979 分母时 H100 是 10.5%，但 sparse 在 decode 路径上不去（§5.2）；dense 一致口径下 H100 = 21%、H200 = 30%（带宽涨算力峰没动 → 分母减小 + 分子不变 → 利用率"虚高"）、B200 = 22%。**利用率数值的提升不是执行效率提升，是分母选择变化**——这就是 §1 表格密度列的真正读法。

但**每美元改善**远小于带宽增幅：云租价从 $2.4/h（H100）涨到 $3-4/h（H200）、$5-8/h（B200），带宽红利被租价吃掉一部分甚至倒挂——这是 §7.2 的量化结论。

这就是为什么不能拿 "FP4 峰值" 评价 LLM decode 的利用率：FP4 tensor core 在 decode 中几乎用不到（KV cache 还得是 FP16/FP8，attention 主路径不会跑到 FP4），能匹配上的只有权重 GEMM。引用：NVIDIA H100 / H200 / Blackwell architecture briefs ([H100](https://www.nvidia.com/en-us/data-center/h100/), [H200](https://www.nvidia.com/en-us/data-center/h200/), [Blackwell brief](https://developer.nvidia.com/blog/nvidia-blackwell-gpu-technical-brief/))。

下一节我们从第一性原理推导这个聚合 AI 是怎么算出来的。

---

## 三、从第一性原理推导单步 decode 的 FLOPs 和字节数

### 基准配置（贯穿全文）

| 维度 | 默认值 | 备选 |
|---|---|---|
| 模型 | LLaMA-3-70B，$$P \approx 70\times10^9$$，$$L=80$$，$$H=8192$$ | — |
| 架构 | dense decoder-only Transformer，GQA $$g=8$$ | — |
| 上下文 | $$T = S + o - 1 = 4096$$（S=ISL, o-1=已生成 token） | — |
| 权重记号 ① FP16 | $$\sigma_w = 2$$，模型 140 GB | — |
| 权重记号 ② INT4 | $$\sigma_w = 0.5$$，模型 35 GB | — |
| KV cache | $$\sigma_{kv} = 2$$（默认 FP16），单序列 ≈ 1.34 GB | FP8 $$\sigma_{kv}=1$$ → 0.67 GB |
| 硬件 | H100 SXM 80GB（3.35 TB/s，FP16 dense 989 / sparse 1979 TFLOPs，FP8 dense 1979 / sparse 3958 TFLOPs）| H200 / B200 已在 §2 列出 |
| 算力约定 | 1 MAC = 2 FLOPs（PaLM / GPT-3 / FlashAttention 文献一致） | — |

下文每张表 / 每个公式都在表头或行内标注口径是 ① FP16 还是 ② INT4，避免来回切换。

记号：$$h_q$$ query 头数，$$h_{kv}$$ KV 头数，head dim $$d = H/h_q$$，分组因子 $$g = h_q/h_{kv}$$。$$B$$ 同一时刻服务的独立序列数。

### 3.1 FLOPs

- 线性层：$$F_{\mathrm{lin}} = 2P$$。
- 注意力 QKᵀ + AV：
  - 每个 query head 都做一次 QKᵀ 和一次 softmax·V，**与 GQA 无关**。
  - 单 token 单头单层：$$4\,d\,T$$；总头 $$4\,T\,H$$；总层 $$4\,L\,T\,H$$。
- 合计：$$F_{\mathrm{token}} = 2P + 4\,L\,H\,T$$。

LLaMA-70B（$$P \approx 70\times10^{9}$$，$$L=80$$，$$H=8192$$，$$T=4096$$）：$$F_{\mathrm{token}} \approx 150.7\,\mathrm{GFLOPs}$$。这跟 vLLM / 第三方在 LLaMA-70B decode 上的公开 benchmark（典型 140-160 GFLOPs/token 量级）一致——**此数字按公式直推**，不直接等于任一配置下的实测，因模型 / 量化 / TP / 上下文不同而不严格可比。

### 3.2 HBM 字节数（理想情况下，不计 cache 命中 / kernel fusion）

每生成一个 token，每张卡需要从 HBM 读出的字节数分两项：

- **权重按 batch 摊薄**：$$M_{\mathrm{weights}}(B) = P\,\sigma_w / B$$ 字节/token。
- **KV cache read**：$$M_{\mathrm{kv\_read}}(T) = 2\,L\,T\,H\,\sigma_{kv} / g$$ 字节/token。
  （$$T = S + o - 1$$，与 batch 无关。）

$$M_{\mathrm{token}}(B,T) \;\approx\; \frac{P\,\sigma_w}{B} \;+\; \frac{2\,L\,T\,H\,\sigma_{kv}}{g}$$

**忽略项**：每 token 的 KV write（自己一份新增 K/V，约 $$2\,L\,H\,\sigma_{kv}/g \approx 0.33\,\mathrm{MB}$$）相对权重和 KV read 都小至少三个数量级，故从公式中略去。它的物理意义将在 §3.3 attention 算术强度里再次出现——KV read 的来源是 "每份写入的 KV 会在后续每一步被读一次"，累计起来 read ≫ write。

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

LLaMA-70B、g=8、σ_kv=2 下：

| B | AI_agg ① FP16 (FLOP/byte) | AI_agg ② INT4 (FLOP/byte) |
|---:|---:|---:|
| 1 | **1.07** | 4.15 |
| 8 | 8.00 | 26.4 |
| 32 | 26.4 | 61.9 |
| 128 | 62.0 | 93.4 |

B=128 在 FP16 下也只到 H100 ridge (dense) 的 ~21%——decode 工作点死死卡在屋顶线斜线段。

### 3.5 Roofline 上可达吞吐（理想上限）

带宽限制段：$$P_{\mathrm{attain}} = \mathrm{BW} \cdot \mathrm{AI}_{\mathrm{agg}}$$。H100 3.35 TB/s × AI_agg（**B=1..128 都在斜线段，验证：3.35 TB/s × 62 = 207.7 TFLOPs ≪ 989 TFLOPs FP16 dense 峰值**——用 sparse 1979 也仍然 ≪ 1979，但口径要标 dense 才严谨，详见 §5.2）：

按两套量化口径给理想吞吐上限（T=4096、g=8、FP16 KV）：

| B | AI_agg ① FP16 | AI_agg ② INT4 | 理想吞吐 — FP16 权重 | 理想吞吐 — INT4 权重 |
|---:|---:|---:|---:|---:|
| 1 | 1.07 | 4.15 | **23.7** | **92** |
| 8 | 8.00 | 26.4 | 178 | 586 |
| 32 | 26.4 | 61.9 | 586 | 1376 |
| 128 | 62.0 | 93.4 | 1376 | **2078** |

> **方法论提醒**：把 MBU = 实测 / 理想 当成 "decode 利用率" 是常见但不严谨的做法——70B FP16 单卡物理放不下（§5.5），所有公开 benchmark 都在 INT4 + TP ≥ 2 配置下跑；用 FP16 理想值去除 INT4 实测值会得到无意义的 MBU（典型 20-30%，不是 "B=1 满、B=128 跌"）。**对比时必须先对齐量化与并行口径**。Roofline 能保证的硬结论是 **AI_agg ≪ ridge，全部留在带宽段**——这是物理层面的结论，与实测是否打满无关。每个 B 下实测能跑到理想的几成由 attention kernel、KV layout、PCIe/NVLink 互联、SM occupancy 等综合决定，应直接查 [vLLM GitHub benchmark](https://github.com/vllm-project/vllm)，**不要用他们的实测反推本文公式**——本文给出的是 "如果能跑出理想 AI 时的上限"。

### 3.6 Prefill 的反向镜像

Prefill（一次性读 prompt）的算式几乎和 decode 对偶：

- $$F_{\mathrm{prefill}} = 2P\,S + 2\,L\,H\,S^{2}$$（causal 注意力下，系数 2 与 FlashAttention 论文一致）。
- $$\mathrm{AI}_{\mathrm{lin,prefill}} = 2\,B\,S/\sigma_w$$：权重按 prompt 长度摊薄，S=4096 FP16 → 4096 FLOP/byte，远高于 H100 拐点。
- $$\mathrm{AI}_{\mathrm{attn,prefill}} = S\,g/\sigma_{kv}$$（分子 causal FLOPs $$2LHS^2$$，分母 KV 写流量 $$2LHS\sigma_{kv}/g$$，比值 $$Sg/\sigma_{kv}$$——prefill KV 只写一次）：g=8、σ_kv=2 下与 S 线性，S=4096 时 ≈16 384 FLOP/byte，**比 ridge 高两个数量级，prefill 连 attention 也是算力受限的**——和 decode 那种 "attention 卡带宽" 完全相反。

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

B < B* 时，权重流量是带宽的主要消费者；B > B* 时，KV read 流量占主导，再加 batch 主要是给 KV 这一项 "添堵"——这正是 vLLM 等系统在 INT4 + TP 配置下 B=32→128 吞吐仅涨 ~20-40%（理想线性是 4×）的定量解释。

### 4.1 Batching 的代价是延迟，不是利用率

| B | TPOT 量级 (ms) | 总吞吐量级 (tok/s/GPU) |
|---:|---:|---:|
| 1 | 40-100 | 10-30 |
| 8 | 60-100 | 100-200 |
| 32 | 80-150 | 300-500 |
| 128 | 200-500 | 400-700 |

> 上述数字是公开材料（vLLM / Anyscale / TGI / DeepSpeed-MII）的**典型量级**，因模型规模、量化、TP 配置不同而不严格可比——本文只关心 "TPOT vs 总吞吐" 的反向关系与 ~10× 单价差这一结论。

**总吞吐涨 ~20-40×，TPOT 涨 ~5×——这个不对称恰恰是 batching 划算的原因**：批处理把 GPU 的算力 / 带宽填得更满，但用户对单条请求的等待时间只涨 ~5×（B 涨 128×、TPOT 涨 ~5×，亚线性）。用户在线等不起，所以 serving 系统会有 "对话态小 batch、离线批处理大 batch" 两套配置，**平均单价可以差 10× 以上**。

参考：[Anyscale, *How continuous batching enables 23x throughput*](https://www.anyscale.com/blog/continuous-batching-llm-inference)、[vLLM blog](https://blog.vllm.ai/2023/06/20/vllm.html)。

### 4.2 ISL 和 OSL 的不对称影响

- **ISL 大（长 prompt）**：prefill 摊薄效果强（输入便宜），但 decode 每步 KV 更长（输出更贵）。
- **OSL 大（长输出）**：attention 项对 $$o = 1..O$$ 求和后是 $$4LH \cdot O(O-1)/2$$（带下三角常数 1/2），整体 ≈$$O^2$$ 增长；线性层项线性增长。**仅当 O 或 S 很大时 attention 才显著**——纯 $$O^2$$ 项要追平 $$2PO$$ 需要 $$O \approx P/(LH) \approx 10^5$$。
- 极端：如果每次都附 32k system prompt + 8k 对话历史，只要 100 token 回答，prefill 占总成本 90%+；output 单价要 "摊" 这一大笔 prefill。

总 decode 成本（生成 O 个 token，prompt S，B=1）：

$$F_{\mathrm{decode\_total}} \;=\; 2P\,O \;+\; 4\,L\,H\!\left[\,O\,S \;+\; \frac{O(O-1)}{2}\,\right]$$

按 §3 公式重算（P=70e9, L=80, H=8192；**单位 TFLOPs per sequence，prefill 一次性、decode 是 O 步总和**；attention 项 = $$4LH \cdot [OS + O(O-1)/2]$$）：

| O | S | F_prefill | F_decode_total | prefill 占比 | attention 项 / F_decode_total |
|---:|---:|---:|---:|---:|---:|
| 128 | 4 096 | **595 T** | **19.3 T** | 96.9% | **7.2%** |
| 1 024 | 4 096 | **595 T** | **156 T** | 79.3% | **7.9%** |
| 8 192 | 32 768 | **6.0 P** | **1.94 P** | 75.6% | **41%** |

S 和 O 都大时 prefill 仍是大头，但 attention 部分（OS + O(O-1)/2）随 O 二次增长——第三行 attention 已经占 decode 41%；如果继续往 O=32k、S=32k 推，attention 会进一步吃掉 decode 预算。

---

## 五、把 GPU 架构放回桌面上：哪些硬件选择决定了 "成本形状"

这一节把前面那张工作点图反过来看硬件——**GPU 设计时把哪些资源堆在哪个层级，决定了 decode 工作点的位置**。

### 5.1 HBM 是带宽的来源，但不是 GPU 的全部带宽

NVIDIA 架构在 compute die 和 HBM stack 之间走的是 CoWoS（**Chip-on-Wafer-on-Substrate）硅中介层 + PHY**——HBM stack 内部的垂直互连是 TSV（Through-Silicon Via），从 stack **底部 base die 通过 PHY** 出来到 CoWoS interposer 上的 RDL 走线，最终连到逻辑 die。H100 SXM 每张卡 5 颗 active HBM3 stack、5120-bit 总线、~5.2 Gbps 等效频率 → 3.35 TB/s。H200 升到 6.4 Gbps HBM3e，4.8 TB/s。Blackwell B200 用 **8 颗 HBM3e 8-Hi 24GB stack = 192 GB**，更宽总线，约 8 TB/s。

带宽增长曲线在加速：H100 → H200 是 +43%（同代 8-Hi，只是频率提升），H100 → B200 是 +139%（更宽 stack + 频率）。HBM3e 12-Hi 路径已在 B300 上落地（288 GB / 卡），核心瓶颈不在物理堆叠，而在良率与 TSV 工艺。

### 5.2 Tensor core 决定算力墙的高度

每代 tensor core 升级都会把峰值拉一截，但峰值是 *特定精度 + dense/sparse 结构* 下的——这两个口径必须分开标，否则"标称算力"的对比会失真（**所有数字截至 2026-07-20 NVIDIA 公开 brief/datasheet 口径**）：

- **FP16 with FP32 accumulate**：
  - H100 / H200：**989 TFLOPs dense** / **1979 TFLOPs sparse**（sparse 是 dense × 2，需要 2:4 结构化剪枝）
  - B200 / GB200：**2250 / 2500 TFLOPs dense** / **4500 / 5000 TFLOPs sparse**（**5th-gen Tensor Core**，仍支持 2:4 sparse）
- **FP8 (E4M3 / E5M2)**：H100 / H200 1979 TFLOPs dense / 3958 sparse；B200 / GB200 4500 / 5000 dense / 9000 / 10000 sparse。
- **FP4 (Blackwell 引入)**：B200 / GB200 9000 / 10000 TFLOPs dense / 18000 / 20000 sparse。

但 decode 实际工作的是 FP16（部分 FP8）——**FP4 tensor core 几乎用不上**。即使 FP16，**decode 也永远不会跑到 sparse 路径**：2:4 结构化稀疏**只作用于权重**（每 4 个权重剪掉 2 个，留下非零结构），而 KV cache 是 hidden state 线性投影得到的激活数据，每 step 都随输入动态生成、不存在权重意义上的"稀疏度"，所以结构性剪枝无定义；attention 主路径天然不稀疏。权重 GEMM 理论上能用 sparse 翻倍，但需要先做 2:4 剪枝（破坏 70B 类预训练权重的精度），实际 LLM serving 几乎不用。**标称的 1979 / 4500 是 sparse 营销值——真实能稳定跑到的工作峰值是 dense 989 / 2250；decode 连 dense 989 的 0.36%（B=1）都吃不到**。这正是 §2 observation 第一条想说的：标称 TFLOPs 对 decode 是无效指标，正确标尺是 TB/s × AI。

### 5.3 为什么 GQA / MQA 改变 attention 的内存占用方式

$$\mathrm{AI}_{\mathrm{attn}} = 2g/\sigma_{kv}$$ 跟 $$B$$ 无关、跟 $$T$$ 无关——这是 GPU 内存子系统的硬约束。但 GQA 把 $$g$$ 推高，等于把 "每 KV 字节对应的算力" 抬上去。NVIDIA A100 / H100 / B200 系列 GPU 没有专门给 attention 优化的 SRAM tiling，但 FlashAttention / FlashDecoding 这类软件 trick 在 SRAM 上复用 K/V tile——本质上是 **用 on-chip SRAM 把 HBM 带宽压力往上一层 "截流"**。

**区分 prefill 和 decode 两条路径的收益**：

- **Prefill**：FA 把 $$S \times T$$ 的 attention 矩阵**不 materialize 到 HBM**（直接 tile 化在 SRAM 上累加），把原本 ~$$O(S^2)$$ 量级的中间读写砍掉。这部分**真的**把 HBM 流量降一阶。
- **Decode**：FA 让 K/V tile 在 SRAM 上复用，但 decode 的 KV read 本来就只读一遍 HBM（因为矩阵小），FA 在 decode 收益主要来自 FlashDecoding 的**长 context 并行 reduce**（降低 latency，不直接省字节）。所谓 "GQA + FA + FP8 KV 改变 decode 工作点"，更准确的表述是："GQA + FP8 KV 改变 decode 工作点，FA 让实测逼近理想 roofline"。

### 5.4 为什么 MoE 模型从根本上更便宜

MoE 每 token 只激活 $$k/E$$ 的专家（典型 8 选 1）。需要分两种 regime：

- **小 B**（每专家命中 batch < 1，或说 routing 完美分散）：每序列只读激活专家的权重。**AI_lin 仍 = $$2B/\sigma_w$$ 不变**（按被激活的那部分参数算）；便宜是因为 **FLOPs 和权重流量都按 $$P_{\mathrm{act}} = P \cdot k/E$$ 缩**，自然 decode 更快。
- **大 B**（每专家被 B/E 个序列命中）：每张卡要为每个专家**完整地**读一次权重（即 "全量路由"），AI_lin = $$2B k/(E \sigma_w)$$，比同 FLOPs 的 dense **低 $$E/k$$ 倍**。这就是为什么 MoE serving 几乎必走 **expert parallelism**：把不同专家打到不同卡上，让每个专家的 effective batch 保持中等。

所以 "MoE 便宜" 的物理来源不是 "AI 涨 $$E/k$$ 倍"，而是 "FLOPs 和权重都按激活参数缩"——这两种解释只在小 B 极限下等价。

### 5.5 KV 容量天花板——decode 真正的硬约束

§3 的字节模型只算了 "流量"，但没算 "驻留"。实际 serving 中，**KV cache 占用的 HBM 容量往往是 decode batch 的硬天花板**：

- LLaMA-70B（g=8, FP16 KV, T=4096）单序列 KV ≈ $$2 \cdot L \cdot T \cdot H \cdot \sigma_{kv} / g = 2 \cdot 80 \cdot 4096 \cdot 8192 \cdot 2 / 8 = 1.34\,\mathrm{GB}$$。
- 70B 模型 FP16 权重本身就占 140 GB；H100 SXM 80GB **放不下单卡**，必须 **TP ≥ 2 或量化（INT8 / INT4）**——这是 "或" 不是 "且"。
- 假设 **TP=1 + INT4 权重（35 GB / 卡）+ 余下 45 GB 装 KV**：单卡可服务序列数 = 45 / 1.34 ≈ **33 条**——这就是 B=32（TP=1 配置）时 H100 真实的天花板，不是带宽。（注意：TP=4 下 INT4 权重只有 8.75 GB / 卡而非 35 GB / 卡；此时 KV 按 TP 分摊到各卡，上述 "45 / 1.34 ≈ 33" 的除法只在 TP=1 下成立。）

B=128 在 FP16 KV 下需要 1.34 × 128 = 172 GB KV，**单卡物理上放不下**——即使换 FP8 KV（0.67 GB / 序列）也需要 ~86 GB，加上 INT4 权重 35 GB = **单卡无论如何到不了 128**。要跑 B=128 必须 TP ≥ 2 + 进一步压缩（量化 KV、paged attention 复用 KV、short context）组合，或者换 H200/B200 拿更大容量。

H200（141GB HBM3e）和 B200（~192GB HBM3e，8 stack × 8-Hi 24GB = 192GB；更大堆叠到 288GB 已由 B300 落地）的真正卖点之一就是这层容量——不是带宽（虽然也涨），而是 "能装得下多少 B 的 KV"。这也是 §1 表格那个 "B=128" 数字的隐含前提：实际系统里，单卡 B 通常到不了 128；H200/B200 把这个上限推到 100+。

### 5.6 Prefill 与 decode 走了两条不同的硬件路径

- **Prefill 路径**：工作点在屋顶线右侧（算力墙）。GPU 这部分的核心瓶颈是 FLOPs 吞吐，需要 tensor core 升级、MoE 激活稀疏、混合精度算子融合。Blackwell 的 FP4 tensor core、Hopper 的 FP8 都首先服务这一段。
- **Decode 路径**：工作点在屋顶线左侧（带宽墙）。GPU 这部分的核心瓶颈是 HBM 带宽 + **KV cache 容量**（§5.5）。需要 GQA/FP8 KV/PagedAttention/Speculative Decoding 等软件层优化。

**把这两段合在一起的服务系统才叫 "LLM serving"**。vLLM、SGLang、TensorRT-LLM、LMDeploy 的工程努力都聚焦在这两段的并行调度、KV cache 复用、MoE 路由、量化、推测解码。

---

## 六、工程优化清单：每条解决什么

按 "对 FLOPs / 字节数 / 容量 的影响" 分类：

| 优化 | 改 FLOPs？ | 改字节数？ | 解决什么 |
|---|---|---|---|
| **Continuous batching** | 否 | 否 | 把 "等最慢序列" 的排队挤掉，提升整体 GPU 利用率 |
| **PagedAttention (vLLM)** | 否 | 否 | KV cache 内存碎片，提高有效 batch 上限（呼应 §5.5） |
| **Prefix caching** | **大幅减少** | 大幅减少 | 相同 prompt 段免去重复 **prefill 计算 + KV 写**，对 input 端是巨大折扣；代价是 KV 显存占用增加 |
| **FlashAttention / FlashDecoding** | 否 | **prefill 大量减少**（attention 矩阵不 materialize） | prefill 让实测逼近理想 roofline；decode 主要降低长 context 延迟（FlashDecoding 并行 reduce） |
| **Speculative decoding** | **总 FLOPs 略增**（draft + 被拒 token 白算） | **每 accepted token 大量减少**（一次权重读出验证 k 个 token） | 用小模型代笔+大模型验算，等效 output token 数放大 |
| **量化（FP8 / INT8 / INT4 权重）** | 否 | σ_w ↓ | AI_lin = 2B/σ_w 同样 B 下随 σ_w 下降而涨 |
| **FP8 KV cache** | 否 | σ_kv ↓ | attention AI 涨 |
| **GQA / MQA** | 否 | g ↑ | attention AI 涨 |
| **MoE 激活稀疏** | **减少** | **大幅减少** | FLOPs 和权重都按 P_act 缩，小 B 下 AI 不变；详见 §5.4 |
| **Linear attention / State-space (Mamba)** | **减少**（attention 从 O(T) 变 O(1)） | **大幅减少** | 替换 KV 为定长状态，decode AI 不再随 T 下降 |

参考：speculative decoding [Leviathan, Kalman & Matias 2023, *Fast Inference from Transformers via Speculative Decoding*](https://arxiv.org/abs/2211.17192)；PagedAttention [Kwon et al. 2023](https://arxiv.org/abs/2309.06180)；FlashAttention [Dao et al. 2022/2024](https://arxiv.org/abs/2205.14135)。

---

## 七、回到账单：理论下限、良好工程水平、公开 API 标价

### 7.1 纯硅成本：output / input 账

取 §4.1 量级区间的代表工况（B=1 → 24 tok/s，B=32 → 400 tok/s，B=128 → 480 tok/s——量级取自 §4.1 表的公开 benchmark 中位），按 §3.6 input 公式（prefill ≈ 145 GFLOPs/token，MFU 45%）反推 input 吞吐：

**Output**：

| 场景 | 吞吐 (tok/s/GPU) | $/GPU·h | GPU·h/1M tok | **$ / 1M tok** |
|---|--:|--:|--:|--:|
| 实时对话 B=1 H100 FP16 † | 24 | 2.4 (云) | 11.57 | **27.8** |
| 在线批量 B=32 H100 INT4 | 400 | 2.4 (云) | 0.694 | **1.67** |
| 离线批量 B=128 H100 INT4 | 480 | 2.4 (云) | 0.579 | **1.39** |
| 自建 (3y 摊销) B=32 H100 INT4 | 400 | 1.2 (自建) | 0.694 | **0.83** |
| 实时对话 B=1 GB200 FP8 ‡ | 60 (估) | 5.0 (云) | 4.63 | **23.1** |
| 在线批量 B=32 GB200 FP8 ‡ | 900 (估) | 5.0 (云) | 0.309 | **1.54** |

> † 70B FP16 单卡放不下（§5.5），本行是 per-GPU 折算（按 INT4 等效吞吐按 σ_w 比值折回 FP16 单卡），实际部署需 TP ≥ 2。
>
> ‡ GB200 吞吐为量级估测：B=1 按 H100 BW 单卡等效带宽比 8/3.35 折算，B=32 按 §3.5 INT4 理想值乘 FP8 增益 1.5×。精确数字以芯片级 benchmark 为准。

**Input**（prefill 工作点）：

| 场景 | 吞吐 (tok/s/GPU) | $/GPU·h | GPU·h/1M tok | **$ / 1M tok** |
|---|--:|--:|--:|--:|
| 在线 B=1 H100 FP16 (S=4096) | ~3070 | 2.4 (云) | 0.091 | **0.22** |
| 自建 B=1 H100 FP16 (S=4096) | ~3070 | 1.2 (自建) | 0.091 | **0.11** |

> 算式：prefill FLOPs = 2PS + 2LHS² ≈ 145 GFLOPs/token。H100 FP16 **dense 989 TFLOPs**（不用 sparse 1979：sparse inference 需 2:4 剪枝，prefill dense matmul 不走 sparse 路径；详见 §5.2），MFU 45% → 989 × 0.45 / 0.145 ≈ **3070 tok/s**。租价 $2.4/h → $0.217/1M input token。
>
> 云租赁参考 2024-2026 主要云厂商公开价（H100 SXM 80GB $2-3.5/·h，GB200 $4-8/·h）。自建用 3 年摊销 $30k/3y + 0.7 kW × $0.10/kWh ≈ $1.2/H100·h。

**Output / Input 纯硅成本比**。直接比 $/M tok 容易因 $/GPU·h 不同而错配——正确做法是比 **GPU·h / 1M tok**，租价自然约掉：

| 比对 | B 场景 | GPU·h/1M tok (out) | GPU·h/1M tok (in) | **纯硅比** |
|---|---|---:|---:|---:|
| B=1 对 B=1 | 实时对话 H100 FP16 | 11.57 | 0.091 | **~127×** |
| B=32 对 B=1 | 在线批量 H100 INT4 / 云 input FP16 | 0.694 | 0.091 | **~7.6×** |
| B=128 对 B=1 | 离线批量 H100 INT4 / 云 input FP16 | 0.579 | 0.091 | **~6.4×** |

也就是说：**batched serving 下纯硅成本差 ~6-8×**；**B=1 端到端场景（无任何摊薄）拉到 ~127×**——取决于 batch / caching 摊薄程度。修正 sparse/dense 口径后，B=32/128 的纯硅比从虚高 ~13-15× 降到 ~6-8×，与 §7.3 的公开 API 售价比对得上——见第三条观察。

### 7.2 换代卡的每美元账

H100 → H200 → B200 的 decode 吞吐收益等于带宽增幅本身（§2 已论证）。每美元改善 = 1 − 租价比 ÷ 带宽比，即**只取决于 "租价 ÷ 带宽" 这一比值，与 B 无关**——下面三行的百分比区间因此必然相同，可作表格自洽性自检。

| 比对 | 带宽变化 | 租价区间 | $/1M output 变化 | **净改善** |
|---|---:|---:|---:|---:|
| H100 → H200, B=1 | +43% | $2.4 → $3-4 | $27.8 → **$24.3-32.4** | **+13% ~ −17%** |
| H100 → B200, B=1 | +139% | $2.4 → $5-8 | $27.8 → **$24.2-38.8** | **+13% ~ −39%** |
| H100 → H200, B=32 | +43% | 同上 | $1.67 → **$1.46-1.95** | **+13% ~ −17%** |

> 算法：新 $/1M = 旧 $/1M × 租价比 ÷ (1 + 带宽增幅)。例 H100→H200 租价 $3：B=1 → $27.8 × 1.25/1.43 = $24.3；B=32 → $1.67 × 1.25/1.43 = $1.46。

**结论：带宽红利大致被租价吃掉甚至倒挂——区间跨零，"美元改善" 主要由供应商怎么定价决定，不是硬件决定的**。Blackwell + MoE + 量化 + 良好 batching 才能把 "理论下限" 压到 $1/1M 量级。

### 7.3 公开 API 标价对照

公开 API 标价（**截至 2026-07-20 官网价**）：

| 模型 | in $/M | out $/M | out/in |
|---|--:|--:|--:|
| OpenAI GPT-4.1 | 2.00 | 8.00 | 4.00× |
| Anthropic Claude Sonnet 4.6 | 3.00 | 15.00 | 5.00× |
| Anthropic Claude Opus 4.5 [2025-11-24] | 5.00 | 25.00 | 5.00× |
| Google Gemini 2.5 Pro (≤200k) | 1.25 | 10.00 | 8.00× |
| DeepSeek V3 (cache miss) | 0.27 | 1.10 | 4.07× |
| Moonshot Kimi K2 (cache miss) | 0.60 | 2.50 | 4.17× |
| Together / Fireworks（跑开源 LLaMA-70B）| ~0.9 | ~0.9 | ~1× |

> **绝对价格 vs 4-8× 出框警告**：GPT-4.1 / Claude / Gemini 是**闭源 frontier**模型，对 output 单 token 收高价（4-8× vs input）；Together / Fireworks 等跑开源 LLaMA-70B 的服务商却给出 in/out ≈ 1×（Llama-3-70B 在 Together 官网 in $0.88 / out $0.88 per 1M，2026-07）。这两套数据并不矛盾——前者把 frontier 模型当成"差异化产品"按 usage-based 溢价定价，后者按开源 blended 成本 + input 大流量 + 利润率压缩定价竞争。**本文开篇的"output 永远比 input 贵 4-8 倍"指的是闭源 frontier；开源 LLaMA-70B serving 的硅账反而是 §7.1 batched serving 6-8× 纯硅比的直接对照**——Together 的 $0.9/M 与 §7.1 算出的 $1.39-1.67/M output 纯硅在同量级，留 30-50% 毛利给服务商。

三个观察：

1. **B=1 与 B=32 的纯 GPU 成本差 ~17×**。这是 "延迟溢价" 的硅基解释：用户在线等时 GPU 90% 算力闲置。
2. **公开 API 标价夹在 B=1 和 B=32 之间**，但更接近 B=32。多数服务商在售出 output token 时，**实际硅成本远低于售价**——其中一部分来自批处理收益，一部分来自供应商毛利。
3. **4-8× out/in 售价 ≈ batched serving 纯硅比（~6-8×）**——以 §7.1 dense 一致口径算的纯硅 input/output 比 ~6-8×，与闭源 frontier API out/in 售价比 4-8× **基本吻合**。这意味着**主流定价大致就是按 batched 硅成本结构定的价**——闭源 frontier API 没有"压缩"成本差，开源 / 跑同模型的厂商（Together / Fireworks）则因 input 大流量 + 利润压缩直接把 out/in 打到 1×（与 §7.1 自建情形一致）。这是文章最强的反直觉观察之一：**4-8× 这个看似神秘的常数只是 batched 硅账的镜像，不是市场任意定的"延迟溢价"**。

---

## 八、硬件再审视：从这张图看未来

如果按上述纯 GPU 路径算，物理下限基本就到 $1/1M token 量级。再往下走必须换内存层级——把 "带宽墙" 从 HBM 这一层挪到别的位置。三条主要路线逐一展开：

### 8.1 PIM——把计算下沉到 DRAM

**代表产品**：SK Hynix AiM (GDDR6 PIM)、Samsung HBM-PIM、UPMEM CPU-PIM。

**思路**：把 ALU 单元塞进 DRAM bank 内部，让权重不出 DRAM。等效 "墙" 从 HBM I/O 推到 DRAM cell 层，权重摊薄项的字节需求被消除。

**现状 (2026-07)**：GDDR6 AiM 已量产；HBM-PIM 第二代在研。主要瓶颈在编程模型——CUDA / OpenCL 不直接对应 PIM 单元，需要专用 compiler 做 workload partitioning；KV attention 这类带 softmax 的算子一般仍要走外部 ALU。

→ 对 decode 工作点的影响：单 token 价格再降 3-5×，**前提是工作负载足够规整且 batch 较大**（PIM 的固定开销在低 batch 下摊不掉）。

参考：[SK Hynix AiM 白皮书](https://www.skhynix.com/sustainability/governance.do)。

### 8.2 统一内存架构（UMA — Unified Memory Architecture）——扩 KV 容量，不扩带宽

**代表产品**：AMD MI300A（CDNA3 GPU + Zen4 CPU + **128 GB HBM3 共享**）、NVIDIA Grace Hopper（Grace CPU + Hopper GPU 共享 LPDDR5X 缓存层，走 NVLink-C2C coherent memory）。

**思路**：让 CPU 和 GPU **访问同一块物理 HBM**（MI300A；Grace Hopper 走 NVLink-C2C，本质仍分层），去掉 PCIe/NVLink 跨设备拷贝。**对 decode 工作点：AI 不变**（仍卡 HBM 带宽墙），但**容量升到 ~1.6×（80 GB → 128 GB）且 CPU/GPU coherent**——直接消掉 §5.5 的 KV 容量硬约束：单卡可服务序列数从 33 条（H100 INT4）推到 ~70 条（MI300A INT4，余 93 GB 装 KV ÷ 1.34 GB/序列 ≈ 69），decode 实际 batch 能跑到理论上限的更大部分。

**现状 (2026-07)**：MI300A 已商用，ROCm + vLLM 跑 LLaMA-70B 可用；ROCm 软件生态成熟度仍是相对短板（vs CUDA）。Grace Hopper NVLink-C2C ~900 GB/s 双向，比 PCIe Gen5 ×16（~64 GB/s）快 14×，但比 HBM 慢一个数量级，仍卡带宽。

→ 对 decode 工作点的影响：**不动字节账里的带宽项，而是把容量项变大**——B=128 这种"理论上限"在 UMA 下变成可实现的"硬上限"，呼应 §5.5。**注意**：本文定位的"根治路线"是改内存层级本身（消掉带宽项），UMA 是这一类路线的次优解——容量问题解决但带宽问题仍在。

参考：[AMD MI300A 产品页](https://www.amd.com/en/products/accelerators/instinct/mi300/mi300a.html)。

### 8.3 LPU——用 SRAM 替代 DRAM

**代表产品**：Groq LPU、Cerebras WSE。

**思路**：用 on-chip SRAM 替代 HBM 作为权重存储，等效带宽比 HBM 高 ~10×（数十 TB/s vs 数 TB/s）。

**现状 (2026-07)**：Groq LPU Inference 已商用，主打**确定性低延迟**（编译期可预测每 token 时延）；每颗 LPU 230 MB SRAM、~80 TB/s 带宽（[Groq tech blog](https://wow.groq.com/2024-01-08-why-llama-2-runs-blazingly-fast-on-groq-lpu-inference-engine/)）。**关键代价：单芯片容量太小**——LLaMA-70B FP16 140 GB，需要 **~500-600 颗 LPU 联合 pipeline**；cluster overhead 吃掉相当一部分单卡带宽红利。Cerebras WSE 单芯片 40 GB SRAM、~20 PB/s 带宽，但同样面临 multi-chip 互联问题。

→ 对 decode 工作点的影响：AI 不变（仍带宽受限），但等效 "墙" 位上移到 SRAM 层，单**芯片** 吞吐涨 5-10×。代价是模型规模受限（70B 需要数百芯片协同），适用场景偏 "低延迟对话 + 中等模型"。

参考：[Groq LPU 架构博文](https://wow.groq.com/2024-01-08-why-llama-2-runs-blazingly-fast-on-groq-lpu-inference-engine/)、[Cerebras WSE 产品页](https://www.cerebras.net/product-chip/)。

这三类方案的 "理论下限" 比 GPU 再低 3-10×，但**实际能不能落地还要看软件栈成熟度**——PIM 缺编译器、UMA 缺生态、LPU 缺模型规模适配。这也是为什么 GPU 仍是主流，但新一代硬件的边际增长在向这几个方向集中。

硬件视角的总结：

- **GPU 的算力墙和带宽墙之间有 gap**：ridge 在 **206-312** FLOP/byte 之间（dense 一致口径），把 LLaMA-70B FP16 decode 工作点推到 **0.36% (B=1) ~ 22% (B=128)** 利用率（§2 最后一列）。要填这个 gap，要么提升带宽（昂贵但近几代在加速），要么改 AI（软件层）。
- **SRAM 已经在填补这个 gap**：FlashAttention/FlashDecoding 把 prefill attention 搬上 on-chip，等效把 "墙" 推到 SRAM 层级（~50 TB/s）。这是过去几年最显著的工程杠杆。
- **新硬件把内存层级再上一阶**：PIM 把计算下沉到 DRAM，UMA 把 CPU/GPU 边界打破，LPU 用 SRAM 替代 DRAM。每种方案都在不同方向上重写 roofline 的几何形状。

---

## 九、给读者的 takeaways

- **decode 时代标称 TFLOPs 是错误标尺，正确标尺是 TB/s × AI（dense）**。具体操作一句话：**评 decode 性价比看 TB/s per dollar，不看 TFLOPs per dollar**——H100 → B200 的 FP16 峰值从 989 涨到 2250（+127%），但 decode 工作点（AI_agg ≤ 62）依然深陷带宽段；只有带宽从 3.35 涨到 8 TB/s（+139%）那部分红利直接变成 decode 吞吐。HBM 容量也是这道算术的另一个输入——B=128 在 FP16 KV 下需要 172 GB 单卡放不下，H200/B200 真正卖点之一是 1.6× 容量。
- output 比 input 贵 4-8×（闭源 frontier API）的根因是数据复用结构：prefill 摊薄权重和 attention，decode 不摊；roofline 上 decode 永远卡在带宽侧。开源 LLaMA-70B serving 的 out/in ≈ 1×，说明 4-8× 不是物理常数，是闭源定价的"批处理结构镜像"。
- 想用 output 单价反推硬件能力不可靠：不同厂商 batching、cache、量化、MoE 激活率都不同，"市场均价" 是看不见的 mix。
- MoE、speculative decoding、FP8 KV、prefix caching 是目前最有效的几条——各自压低每 token 的字节数或计算量（详见 §6）。Linear attention、状态空间模型是从根本上让 attention 项不再随 T 二次增长的方向。
- **硬件升级的美元改善取决于供应商定价**：H200/B200 带宽红利大致被租价吃掉甚至倒挂（详见 §7.2）；想拿到 "理论下限" 必须配 MoE + 量化 + 良好 batching + 大 batch。

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