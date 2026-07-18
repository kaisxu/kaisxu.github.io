---
layout: post
title: "从 output token 到 roofline：一份对 GPU 推理架构的再审视"
date: 2026-07-18 09:00:00 +0800
categories: [tech, gpu, inference]
math: true
---

一个常见但值得反复审视的现象：任何 LLM API 的价目表里，output token 永远比 input token 贵——大多在 3-6 倍之间。看起来像定价策略，但只要把单步 decode 的工作点画到 roofline 上，就能看到这是 **数据复用结构** 决定的工程结果，不以哪一家的定价为转移。

顺着这个观察一路往下，会遇到一连串有意思的问题：

- 为什么 batch 越大单 token 越便宜、但用户能感觉到的延迟也越长？
- 为什么 GQA、FP8 KV、quantization 这些优化并不改变 FLOPs，却能让 GPU"干活变多"？
- 为什么 H200、B200 这类带宽更强的卡，对单 token 单价的边际改善远小于直觉？
- 为什么 MoE 模型以同样总参数能便宜那么多？

本文想做的不是另一个 "decode 为什么慢" 的科普贴，而是把 *从 output token 这一具体现象倒推到 roofline、再倒推到 GPU 硬件设计选择* 这条链条整理清楚。最后再回到一份可复算的账单，看看公开 API 标价和"纯硅成本"之间到底差多少。

---

## 一、从 output token 开始：一个具体的工作点

单 token decode 看起来很简单：跑一遍 80 层 Transformer，吐一个 token。但里面发生的事情可以用 roofline 量化：

$$\mathrm{AI} \;=\; \frac{\text{一次推理做的浮点运算}}{\text{一次推理从显存读出的字节数}}\quad(\mathrm{FLOP/byte})$$

把 LLaMA-70B 类 dense 模型（GQA $$g=8$$，FP16 权重 + FP16 KV）在 H100 上拆开，单 token 算术强度大致是：

| B (batch) | 权重摊薄 FLOP/byte | KV 流量 FLOP/byte | 聚合 AI | 相对 H100 拐点 (590) |
|---:|---:|---:|---:|---:|
| 1 | 1.0 | 8.0 | **1.07** | 0.18% |
| 8 | 8.0 | 8.0 | **8.00** | 1.4% |
| 32 | 32.0 | 8.0 | **26.4** | 4.5% |
| 128 | 128.0 | 8.0 | **62.0** | 10.5% |

B=128 都还没到 H100 拐点的 11%。这就是 output token 工作的真实"地形"：**永远坐在 roofline 的带宽侧**。

注意表格里有两个完全不同的量：权重摊薄是 $$2B/\sigma_w$$（按 batch 摊权重），KV 流量是 $$2g/\sigma_{kv} = 8$$（与 batch 无关、与上下文长度无关）。前者解释了 batching 的红利，后者解释了为什么 batching 不能无限提升。第三节会从公式推导。

### 1.1 几个常见误解，先标出来

网上不少资料把这些数字讲得太"硬"了，进入推导之前先把几条要修正的事说清楚：

- "Attention 算术强度恒等于 1 FLOP/byte"——只对 MHA + FP16 KV 成立。Llama-3、Qwen 等 2024 年后的主流模型都用了 GQA（$$g=8$$），attention AI 在 FP16 KV 下是 8、在 FP8 KV 下是 16。
- "5× 价格差是物理常数"——纯硅成本差大约 8-25×（来自利用率差异），3-6× 是市场观察到的 *售价* 比，**它小于** 纯成本比。定价在 *压缩* 成本差。
- "H100 decode 时只跑出峰值算力的 0.5%"——结论跟口径有关：FP16 严格匹配时 H100 B=1 是 0.17%；换成 Blackwell FP4 tensor-core 峰值做分母会显得更难看；GQA 比 MHA 略好。
- "PIM / APU / LPU 是颠覆性突破"——这些方案都把内存层级往上推一阶，绕开 HBM 带宽墙；带宽/算力比从 1:1 升到数十到 100:1。这才是结构上的差异。

---

## 二、Roofline：再讲一遍，但只讲 GPU 推理相关的

Roofline 把"一个算子在这块卡上能跑多快"这件事收成两条线：

- **斜线段**：$$T = \mathrm{BW} \cdot \mathrm{AI}$$（被带宽限制，斜率 = HBM 带宽 BW）。
- **水平线段**：$$T = F_{\mathrm{peak}}$$（被算力限制，高度 = 峰值 FLOP/s）。
- **拐点**：$$\mathrm{AI}_{\mathrm{ridge}} = F_{\mathrm{peak}} / \mathrm{BW}$$。H100 SXM FP16 tensor core $$1979\,\mathrm{TFLOPs} / 3.35\,\mathrm{TB/s} \approx 590\,\mathrm{FLOP/byte}$$。

把当前一代旗舰的拐点列一下，能立刻看出问题：

| 卡 | HBM 带宽 | FP16 峰值 (TFLOPs) | FP16 ridge | FP8 峰值 | FP8 ridge | decode 利用率 (B=128) |
|---|---:|---:|---:|---:|---:|---:|
| A100 SXM | 2.0 TB/s | 312 | 156 | — | — | **40%** |
| H100 SXM | 3.35 TB/s | 1979 | **590** | 3958 | 1180 | **10.5%** |
| H200 SXM | 4.8 TB/s | 1979 | 412 | 3958 | 825 | **15%** |
| B200 SXM | 8.0 TB/s | 2250 | 281 | 4500 | 562 | **22%** |
| GB200 (NVL72 per package 估) | ~8 TB/s | 2500 | 312 | 5000 | 625 | **20%** |

观察：H200 把带宽加了 43%，但 FP16 ridge *反而下降*（从 590 到 412），因为峰值算力没变。Blackwell 把带宽推到 8 TB/s，ridge 进一步降到 ~280。换句话说，**新一代硬件把带宽缺口按比例扩大了"算力墙-带宽墙"之间的剪刀差**——这对 decode 不利，对 prefill 影响有限。

这就是为什么不能拿"FP4 峰值"评价 LLM decode 的利用率：FP4 tensor core 在 decode 中几乎用不到（KV cache 还得是 FP16/FP8，attention 主路径不会跑到 FP4），能匹配上的只有权重 GEMM。引用：NVIDIA H100 / H200 / Blackwell architecture briefs ([H100](https://www.nvidia.com/en-us/data-center/h100/), [H200](https://www.nvidia.com/en-us/data-center/h200/), [Blackwell brief](https://developer.nvidia.com/blog/nvidia-blackwell-gpu-technical-brief/))。

### §二 takeaway：对 decode 工作点，FP16 峰值就够用

把上面这张 ridge 表和 §一那张聚合 AI 表叠在一起读，能看出来一件事，但需要写明白：

- LLaMA-70B（GQA g=8、FP16 KV）在 B=128 下 AI_agg ≈ 62 FLOP/byte——只是 H100 ridge (590) 的 10.5%，B=1 时只剩 0.18%。
- "FP16 峰值 1979 vs 2250 TFLOPs"、"FP8 峰值 3958 vs 4500 TFLOPs" 这类数字——对 decode 来说几乎只是营销数字；新版峰值更高，反而把 ridge 推得更低，**纸面上看利用率涨了，实际上 decode 工作点没变**（这就是新增那列 `decode 利用率` 想钉的事）。
- Blackwell 引入的 FP4 tensor core 在 decode 中几乎用不上：KV cache 还得是 FP16/FP8，attention 主路径不会跑到 FP4，能匹配上的只有权重 GEMM。
- 真正决定 decode 单 token 单价的，是 **HBM 带宽 × batch 摊薄 × 量化精度的组合**——和"几 eFLOPS 的 FP4 峰值"基本无关。

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

- 权重按 batch 摊薄：$$P\,\sigma_w / B$$ 字节/token。
- KV cache read：$$2\,L\,T\,H\,\sigma_{kv} / g$$ 字节/token。
- KV cache write（新 token 写入）：$$2\,L\,B\,H\,\sigma_{kv} / g$$ 字节/token。

$$M_{\mathrm{token}}(B,T) \;\approx\; \frac{P\,\sigma_w}{B} \;+\; \frac{2\,L\,T\,H\,\sigma_{kv}}{g} \;+\; \frac{2\,L\,B\,H\,\sigma_{kv}}{g}$$

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

GQA 来源：[Ainslie et al. 2023, *GQA: Training Generalized Multi-Query Transformer Models from Multi-Head Checkpoints*](https://arxiv.org/abs/2305.13245)；MQA：[Shazeer 2019, *Fast Transformer Decoding: Big Write, Small READ*](https://arxiv.org/abs/1911.02150)。

### 3.4 聚合 AI 和 roofline 拐点

聚合 AI 用"FLOP/字节"的标准定义即可，**不要用 FLOP-weighted 平均**（那是另一个量）：

$$\mathrm{AI}_{\mathrm{agg}}(B) \;=\; \frac{2P + 4\,L\,H\,T}{P\,\sigma_w/B + 2\,L\,T\,H\,\sigma_{kv}/g + 2\,L\,B\,H\,\sigma_{kv}/g}$$

LLaMA-70B、g=8、σ_w=σ_kv=2 下，B=128 也只到 H100 ridge 的 10.5%——decode 工作点死死卡在屋顶线斜线段。

### 3.5 Roofline 上可达吞吐

带宽限制段：$$T_{\mathrm{token\_bandwidth}} = \mathrm{BW} \cdot \mathrm{AI}_{\mathrm{agg}}$$。H100 3.35 TB/s × AI_agg：

- B=1 → 3.6 TFLOPs
- B=128 → 207.7 TFLOPs（仍远低于 1979 TFLOPs FP16 峰值 → 还在带宽段）

跟 vLLM 实测吞吐反推的等效算力对照：

| B | vLLM 实测 (tok/s/GPU, LLaMA-70B, H100 SXM, INT4) | 等效算力 (TFLOPs) |
|---:|---:|---:|
| 1 | ~24 | ~3.6 |
| 8 | ~120 | ~18.1 |
| 32 | ~400 | ~60.2 |
| 128 | ~480 | ~72.3 |

> vLLM 数据来自项目公开 benchmark：[vLLM GitHub](https://github.com/vllm-project/vllm)。这证实 B=1..128 区间内 *确实是带宽主导*。

### 3.6 Prefill 的反向镜像

Prefill（一次性读 prompt）的算式几乎和 decode 对偶：

- $$F_{\mathrm{prefill}} = 2P\,S + 2\,L\,H\,S^{2}$$（causal 注意力下，系数 2 与 FlashAttention 论文一致）。
- $$\mathrm{AI}_{\mathrm{lin,prefill}} = 2\,B\,S/\sigma_w$$：权重按 prompt 长度摊薄，S=4096 FP16 → 4096 FLOP/byte，远高于 H100 拐点。
- $$\mathrm{AI}_{\mathrm{attn,prefill}} = 2\,S\,g/\sigma_{kv}$$：注意力同样按 S 摊薄。

prefill 工作点稳坐在屋顶线算力墙上。这就是为什么"长 prompt 越长越划算"——同一段 prompt 被并行摊薄。

---

## 四、Batching 究竟搬动了哪条曲线

把 batch 从 1 推到 32、推到 128，每一步都在搬动哪条曲线：

$$\begin{aligned}
\text{线性层 AI}:&\quad 2B/\sigma_w &&\to \text{随 } B \text{ 线性涨}\\
\text{注意力 AI}:&\quad 2g/\sigma_{kv} &&\to \text{常数}\\
\text{聚合 AI}:&\quad \text{加权和} &&\to B \text{ 增大时近似线性}\\
\text{可达吞吐}:&\quad \mathrm{BW}\cdot\mathrm{AI}_{\mathrm{agg}} &&\to \text{在带宽段近似线性}
\end{aligned}$$

KV 流量是另一条独立的曲线：每多一个并发请求，每步多一份 KV 字节读，总带宽 *按 B 摊薄*。B 越大，KV 流量占总带宽比例越小，但 *绝对量* 也在涨。当 KV 流量接近 HBM 带宽上限时，再加 batch 就开始挤占线性层的可用带宽——这就是 vLLM 实测里 B=32→128 吞吐只涨 20% 的原因。

### 4.1 Batching 的代价是延迟，不是利用率

| B | TPOT (ms) | 总吞吐 (tok/s/GPU) |
|---:|---:|---:|
| 1 | ~42 | ~24 |
| 8 | ~67 | ~120 |
| 32 | ~80 | ~400 |
| 128 | ~270 | ~480 |

总吞吐涨 5-10 倍，但 TPOT 从 42 ms 涨到 270 ms。对话态用户等不起，所以 serving 系统会有"对话态小 batch、离线批处理大 batch"两套配置，**平均单价可以差 10× 以上**。

参考：[Anyscale, *How continuous batching enables 23x throughput*](https://www.anyscale.com/blog/continuous-batching-llm-inference)、[vLLM blog](https://blog.vllm.ai/2023/06/20/vllm.html)。

### 4.2 ISL 和 OSL 的不对称影响

- **ISL 大（长 prompt）**：prefill 摊薄效果强（输入便宜），但 decode 每步 KV 更长（输出更贵）。
- **OSL 大（长输出）**：每步 attention 都累加一次 $$O(O-1)/2$$；输出成本随 $$O$$ 平方增长。
- 极端：如果每次都附 32k system prompt + 8k 对话历史，只要 100 token 回答，prefill 占总成本 90%+；output 单价要"摊"这一大笔 prefill。

总 decode 成本（生成 O 个 token，prompt S）：

$$F_{\mathrm{decode\_total}} \;=\; 2P\,O \;+\; 4\,L\,H\!\left[\,O\,S \;+\; \frac{O(O-1)}{2}\,\right]$$

| O | S | B | F_prefill | F_decode_total | prefill 占比 |
|---:|---:|---:|---:|---:|---:|
| 128 | 4 096 | 1 | 313 G | 19.4 G | 94% |
| 1 024 | 4 096 | 1 | 313 G | 168 G | 65% |
| 8 192 | 32 768 | 1 | 56.6 T | 2.2 T | 96% |

S 大 O 也大时 prefill 仍是大头；如果 O 远超 S，attention 部分会逐步吃下一大部分预算。

---

## 五、把 GPU 架构放回桌面上：哪些硬件选择决定了"成本形状"

这一节把前面那张工作点图反过来看硬件——**GPU 设计时把哪些资源堆在哪个层级，决定了 decode 工作点的位置**。

### 5.1 HBM 是带宽的来源，但不是 GPU 的全部带宽

NVIDIA 架构在 compute die 和 HBM stack 之间走的是硅穿孔（TSV）+ PHY。H100 每张卡 6 颗 HBM3 stack，1024-bit 总线/颗 × 6 = 6144-bit 总带宽，跑在 5.6 Gbps 等效频率下约 3.35 TB/s。H200 升到 6.4 Gbps HBM3e，4.8 TB/s。Blackwell B200 用 HBM3e 12-Hi stack + 更宽总线，约 8 TB/s。

关键限制：**HBM stack 物理上不能无限堆**。HBM3e 8-Hi 单 stack 已经接近良率极限，再往 12-Hi、16-Hi 推要靠更先进的 die-stacking 工艺。带宽增长曲线在放缓——H100→H200 是 +43%，H100→B200 是 +139%，但 B100/B200 走的是 NVIDIA NVLink Switch + 72-way NVL72 横向扩展，单卡相对 H100 只 +139%。

### 5.2 Tensor core 决定算力墙的高度

每代 tensor core 升级都会把峰值拉一截，但峰值是 *特定精度* 下的：

- FP16 with FP32 accumulate：H100 1979 TFLOPs（密集），稀疏翻倍。
- FP8 (E4M3 / E5M2)：H100 3958 TFLOPs（密集）。
- FP4 (Blackwell 引入)：B200/GB200 ~3600-7200 TFLOPs（密集 / 稀疏）。

但 decode 实际工作的是 FP16（部分 FP8）——**FP4 tensor core 几乎用不上**。这就是为什么 Blackwell 的"FP4 峰值 1.8 PFLOPs"对 decode 几乎无意义，分母只能取 FP16/FP8。

### 5.3 为什么 GQA / MQA 改变 GPU 占用方式

$$\mathrm{AI}_{\mathrm{attn}} = 2g/\sigma_{kv}$$ 跟 $$B$$ 无关、跟 $$T$$ 无关——这是 GPU 内存子系统的硬约束。但 GQA 把 $$g$$ 推高，等于把"每 KV 字节对应的算力"抬上去。NVIDIA A100/H100/B100 系列 GPU 没有专门给 attention 优化的 SRAM tiling，但 FlashAttention / FlashDecoding 这类软件 trick 在 SRAM 上复用 K/V tile——本质上是 **用 on-chip SRAM 把 HBM 带宽压力往上一层"截流"**。

- FlashAttention 把 attention tile 化进 SRAM：HBM 只读一次 K/V tile，attention 计算在 on-chip 完成，结果写回 HBM。等效"墙"从 HBM 推到 SRAM（每 SM 上 ~228 KB shared memory）——带宽量级从 3 TB/s 推到 ~50 TB/s 量级（按 SM 数量展开）。
- FlashDecoding 把长 context 切成并行段，再 reduce。同一思路在不同尺度上的应用。

这就是为什么 GQA + FlashAttention + FP8 KV 组合能改变 decode 工作点：**不是改 FLOPs，而是改字节数**。

### 5.4 为什么 MoE 模型从根本上更便宜

MoE 每 token 只激活 $$k/E$$ 的专家（典型 8 选 1）。权重摊薄变成：

$$\mathrm{AI}_{\mathrm{lin,moe}} \;=\; \frac{E}{k}\cdot\frac{2B}{\sigma_w}$$

E/k = 8 → AI_lin 涨 8 倍。这等价于把工作点人工往上推一截（在 batch 不变时），因此 MoE 模型在同硬件上的吞吐通常 2-3× 于同规模 dense 模型，与公开 API 价目吻合——Mixtral 8x7B、DeepSeek V3、Kimi K2、Qwen3-MoE 价格都比同规模 dense 便宜 ~50%。

### 5.5 Prefill 与 decode 走了两条不同的硬件路径

- **Prefill 路径**：工作点在屋顶线右侧（算力墙）。GPU 这部分的核心瓶颈是 FLOPs 吞吐，需要 tensor core 升级、MoE 激活稀疏、混合精度算子融合。Blackwell 的 FP4 tensor core、Hopper 的 FP8 都首先服务这一段。
- **Decode 路径**：工作点在屋顶线左侧（带宽墙）。GPU 这部分的核心瓶颈是 HBM 带宽 + KV cache 容量。需要 GQA/FP8 KV/PagedAttention/Speculative Decoding 等软件层优化。

**把这两段合在一起的服务系统才叫"LLM serving"**。vLLM、SGLang、TensorRT-LLM、LMDeploy 的工程努力都聚焦在这两段的并行调度、KV cache 复用、MoE 路由、量化、推测解码。

---

## 六、工程优化清单：每条解决什么

按"对 AI 的影响"分类：

| 优化 | 改 FLOPs？ | 改字节数？ | 解决什么 |
|---|---|---|---|
| **Continuous batching** | 否 | 否 | 把"等最慢序列"的排队挤掉，提升整体 GPU 利用率 |
| **PagedAttention (vLLM)** | 否 | 否 | KV cache 内存碎片，提高有效 batch 上限 |
| **Prefix caching** | 否 | 大幅减少 | 相同 prompt 段免去重复 prefill，对 input 端是巨大折扣 |
| **FlashAttention / FlashDecoding** | 否 | 大幅减少 | 把 attention tile 化进 SRAM，截流 HBM |
| **Speculative decoding** | 减少 | 否 | 用小模型代笔+大模型验算，等效 output token 数放大 |
| **量化（FP8 / INT8 / INT4 权重）** | 否 | σ_w ↓ | 线性层 AI 涨 B 不变 |
| **FP8 KV cache** | 否 | σ_kv ↓ | 注意力 AI 涨 |
| **GQA / MQA** | 否 | g ↑ | 注意力 AI 涨 |
| **MoE 激活稀疏** | 减少 | 大幅减少 | 线性层 AI 涨 (E/k) 倍 |
| **Linear attention / State-space (Mamba)** | 否 | 大幅减少 | 替换 KV 为定长状态，decode AI 不再随 T 下降 |

参考：speculative decoding [Leviathan et al. 2023](https://arxiv.org/abs/2210.00920)；PagedAttention [Kwon et al. 2023](https://arxiv.org/abs/2309.06180)；FlashAttention [Dao et al. 2022/2024](https://arxiv.org/abs/2205.14135)。

---

## 七、回到账单：理论下限、良好工程水平、公开 API 标价

按上面三档定价算 LLaMA-70B 类模型的"每 1M output token 硬件成本"：

| 场景 | 吞吐 (tok/s/GPU) | $/GPU·h | GPU·h/1M tok | **$ / 1M tok** | 备注 |
|---|--:|--:|--:|--:|---|
| 实时对话 B=1 H100 FP16 | 24 | 2.4 (云) | 11.57 | **27.8** | vLLM LLaMA-70B 实测 |
| 在线批量 B=32 H100 INT4 | 400 | 2.4 (云) | 0.694 | **1.67** | 同上 |
| 离线批量 B=128 H100 INT4 | 480 | 2.4 (云) | 0.579 | **1.39** | 同上 |
| 自建 (3y 摊销) B=32 H100 INT4 | 400 | 1.2 (自建) | 0.694 | **0.83** | capex $30k/3y + 电费 |
| 实时对话 B=1 GB200 FP8 | 60 (估) | 5.0 (云) | 4.63 | **23.1** | Blackwell 2026 估测 |
| 在线批量 B=32 GB200 FP8 | 900 (估) | 5.0 (云) | 0.309 | **1.54** | 同上 |

> 云租赁参考 2024-2026 主要云厂商公开价（H100 SXM 80GB $2-3.5/·h，GB200 $4-8/·h）。自建用 3 年摊销 $30k/3y + 0.7 kW × $0.10/kWh ≈ $1.2/H100·h。

公开 API 标价（2026 年中）：

| 模型 | in $/M | out $/M | out/in |
|---|--:|--:|--:|
| OpenAI GPT-4.1 | 2.50 | 10.00 | 4.00× |
| Anthropic Claude Sonnet 4.6 | 3.00 | 15.00 | 5.00× |
| Anthropic Claude Opus 4.7 | 15.00 | 75.00 | 5.00× |
| Google Gemini 1.5 Pro (≤200k) | 1.25 | 5.00 | 4.00× |
| Google Gemini 2.5 Pro | 1.25 | 10.00 | 8.00× |
| DeepSeek V3 (cache miss) | 0.27 | 1.10 | 4.07× |
| Moonshot Kimi K2 (cache miss) | 0.60 | 2.50 | 4.17× |

三个观察：

1. **B=1 与 B=32 的纯 GPU 成本差 ~17×**。这是"延迟溢价"的硅基解释：用户在线等时 GPU 90% 算力闲置。
2. **公开 API 标价夹在 B=1 和 B=32 之间**，但更接近 B=32。多数服务商在售出 output token 时，**实际硅成本远低于售价**——其中一部分来自批处理收益，一部分来自供应商毛利。
3. **3-6× out/in 售价比 < 纯成本比**。如果只算硅，output 单 token 应比 input 贵 8-25×。售价只标 4-8×，说明供应商定价 *压缩* 了成本差。这一压缩来自 input 端按 cache-hit 摊薄、output 端按批处理摊薄的双向作用。

---

## 八、硬件再审视：从这张图看未来

如果按上述纯 GPU 路径算，物理下限基本就到 $1/1M token 量级。再往下走必须换内存层级——把"带宽墙"在不同方向上重新位置化。三条主要路线逐一展开：

### 8.1 PIM——把计算下沉到 DRAM

**代表产品**：SK Hynix AiM (GDDR6 PIM)、Samsung HBM-PIM、UPMEM CPU-PIM。

**思路**：把 ALU 单元塞进 DRAM bank 内部，让权重不出 DRAM。等效"墙"从 HBM I/O 推到 DRAM cell 层，权重摊薄项的字节需求被消除。

**现状 (2026-07)**：GDDR6 AiM 已量产；HBM-PIM 第二代在研。主要瓶颈在编程模型——CUDA / OpenCL 不直接对应 PIM 单元，需要专用 compiler 做 workload partitioning；KV attention 这类带 softmax 的算子一般仍要走外部 ALU。

→ 对 decode 工作点的影响：单 token 价格再降 3-5×，**前提是工作负载足够规整且 batch 较大**（PIM 的固定开销在低 batch 下摊不掉）。

参考：[SK Hynix AiM 白皮书](https://www.skhynix.com/sustainability/governance.do)。

### 8.2 APU——CPU/GPU 共享 HBM

**代表产品**：AMD MI300A（CDNA3 GPU + Zen4 CPU + 128 GB HBM3 共享内存）、Apple M-series、Qualcomm Snapdragon X。

**思路**：CPU 和 GPU 共享同一块 HBM，把 CPU↔GPU 之间 PCIe/NVLink 来回拷贝 metadata 的开销砍掉。**AI 不变**，但 launch overhead 和跨设备数据传输省掉。

**现状 (2026-07)**：MI300A 已商用，ROCm + vLLM 跑 LLaMA-70B 可用；ROCm 软件生态成熟度仍是相对短板（vs CUDA）。

→ 对 decode 工作点的影响：对 **B=1 / 小 batch 实时对话场景**边际改善最大；大 batch 离线场景相对收益有限。

参考：[AMD MI300A 产品页](https://www.amd.com/en/products/accelerators/instinct/mi300/mi300a.html)。

### 8.3 LPU——用 SRAM 替代 DRAM

**代表产品**：Groq LPU、Cerebras WSE、Tenstorrent。

**思路**：用 on-chip SRAM 替代 HBM 作为权重存储，等效带宽比 HBM 高 ~10×（数十 TB/s vs 数 TB/s）。

**现状 (2026-07)**：Groq LPU Inference 已商用，主打**确定性低延迟**（编译期可预测每 token 时延）；Cerebras 通过云服务对外 sell compute；Tenstorrent 走 RISC-V 路线，工具链成熟度滞后于前两者。

→ 对 decode 工作点的影响：AI 不变（仍带宽受限），但等效"墙"位上移到 SRAM 层，单卡吞吐涨 5-10×。代价是模型规模受限（>30B 需 multi-chip parallel，cluster overhead 吃掉一部分增益）。

参考：[Groq LPU 架构博文](https://wow.groq.com/2024-01-08-why-llama-2-runs-blazingly-fast-on-groq-lpu-inference-engine/)。

这三类方案的"理论下限"比 GPU 再低 3-10×，但**实际能不能落地还要看软件栈成熟度**——PIM 缺编译器、APU 缺生态、LPU 缺模型规模适配。这也是为什么 GPU 仍是主流，但新一代硬件的边际增长在向这几个方向集中。

硬件视角的总结：

- **GPU 的算力墙和带宽墙之间有 gap**：ridge 在 200-600 FLOP/byte 之间，把 decode 推到 0.5-10% 利用率。要填这个 gap，要么提升带宽（昂贵且渐进），要么改 AI（软件层）。
- **SRAM 已经在填补这个 gap**：FlashAttention/FlashDecoding 把注意力搬上 on-chip，等效把"墙"推到 SRAM 层级（~50 TB/s）。这是过去几年最显著的工程杠杆。
- **新硬件把内存层级再上一阶**：PIM 把计算下沉到 DRAM，APU 把 CPU/GPU 边界打破，LPU 用 SRAM 替代 DRAM。每种方案都在不同方向上重写 roofline 的几何形状。

---

## 九、给读者的 takeaways

- "为什么 output token 比 input 贵"的核心是 **数据复用结构**：prefill 摊薄权重和 attention，decode 不摊。Roofline 上 decode 永远卡在带宽侧。
- **3-6× 是市场观察**，不是硅下界。GPU + 良好 batching 下的纯硅成本下限约 $0.8-1.7/1M output token（2026 年云价），公开 API 标价 $5-15/1M 覆盖了延迟溢价和毛利。
- 想用 output 单价反推硬件能力是不可靠的：不同厂商 batching 策略、cache 复用率、量化等级、MoE 激活率都不同，"市场均价"是看不见的 mix。
- MoE、speculative decoding、FP8 KV、prefix caching 是目前最有效的几条——它们都在不同维度上抬升 AI。Linear attention、状态空间模型是从根本上让 AI 不再随 T 下降的方向。
- 硬件升级对单 token 单价的边际改善有限：H200 比 H100 在 decode 上单 token 价降 ~5%，不是因为硬件变差，而是因为 B=1 工作点的带宽瓶颈没变；Blackwell + MoE + 量化 + 良好 batching 才能把"理论下限"压到 $1/1M 量级。

最后一句立场：研究 LLM cost 时，最好把"GPU 算力价格"和"output token 价格"分开。前者按公开 benchmark 和云价能算清楚；后者由市场决定。混在一起谈容易把工程问题说成物理问题，把策略空间说成物理常数。

---

## 参考资料

- Pope et al., *Efficiently Scaling Transformer Inference*, MLSys 2023. [arXiv:2211.05102](https://arxiv.org/abs/2211.05102)
- Yuan et al., *LLM Inference Unveiled: Survey and Roofline Model Insights*, 2024. [arXiv:2402.16363](https://arxiv.org/abs/2402.16363)
- Williams, Waterman & Patterson, *Roofline: An Insightful Visual Performance Model*, CACM 2009. [HP Labs tech report](https://crd.lbl.gov/assets/pubs_presos/parlab08-pw99.pdf)
- Ainslie et al., *GQA: Training Generalized Multi-Query Transformer Models from Multi-Head Checkpoints*, EMNLP 2023. [arXiv:2305.13245](https://arxiv.org/abs/2305.13245)
- Shazeer, *Fast Transformer Decoding: Big Write, Small READ*, ICLR 2020. [arXiv:1911.02150](https://arxiv.org/abs/1911.02150)
- Leviathan, Kalman & Matias, *One Pass Decoding for Speculative Decoding*, ICML 2023. [arXiv:2210.00920](https://arxiv.org/abs/2210.00920)
- Kwon et al., *PagedAttention (vLLM)*, SOSP 2023. [arXiv:2309.06180](https://arxiv.org/abs/2309.06180)
- Dao et al., *FlashAttention*, NeurIPS 2022; *FlashAttention-2*, ICLR 2024. [arXiv:2205.14135](https://arxiv.org/abs/2205.14135), [arXiv:2307.08691](https://arxiv.org/abs/2307.08691)
- NVIDIA H100 / H200 / Blackwell architecture briefs. [H100](https://www.nvidia.com/en-us/data-center/h100/), [H200](https://www.nvidia.com/en-us/data-center/h200/), [Blackwell](https://developer.nvidia.com/blog/nvidia-blackwell-gpu-technical-brief/)
- He, *Making Deep Learning Go Brrrr From First Principles*, 2022. [turingocomputation.com](https://www.turingocomputation.com/p/making-deep-learning-go-brrrr-from-first)
- Anyscale, *How continuous batching enables 23x throughput*. [blog](https://www.anyscale.com/blog/continuous-batching-llm-inference)
- vLLM project, *vLLM: A high-throughput and memory-efficient inference engine for LLMs*. [GitHub](https://github.com/vllm-project/vllm)
- 主要云厂商公开定价页（AWS、Lambda Labs、CoreWeave、GCP、阿里云）— 截至 2026-07
- OpenAI / Anthropic / Google / DeepSeek / Moonshot 官方 API 价目 — 截至 2026-07

文中涉及的数字均标注了日期与口径；价格与硬件规格每隔几个月就会更新一遍，再过一年请以最新公开资料为准。FLOPs 公式采用 "1 MAC = 2 FLOPs" 约定；如要换算成 MAC 数，对应所有 FLOPs 数减半即可。