---
layout: post
title: "运行 CUDA 内核时发生了什么"
date: 2026-07-07 21:00:00 +0800
categories: tech gpu
---
> *本文翻译并转载自 [Fergus Finn 的博客](https://fergusfinn.com/blog/what-happens-when-you-run-a-gpu-kernel/)，原文标题："What Happens When You Run a CUDA Kernel"，发表于 2026 年 6 月 29 日。译文已获得原作者授权转载，转载请保留原文链接与作者署名。*

---

下面这段 CUDA 程序把两个向量相加：

```cuda
__global__ void vadd(const float* a, const float* b, float* c, int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) c[i] = a[i] + b[i];
}

int main() {
    int n = 1 << 20;
    size_t bytes = n * sizeof(float);

    float *a = (float*)malloc(bytes), *b = (float*)malloc(bytes),
          *c = (float*)malloc(bytes);
    for (int i = 0; i < n; i++) a[i] = b[i] = 1.0f;

    float *da, *db, *dc;
    cudaMalloc(&da, bytes);
    cudaMalloc(&db, bytes);
    cudaMalloc(&dc, bytes);
    cudaMemcpy(da, a, bytes, cudaMemcpyHostToDevice);
    cudaMemcpy(db, b, bytes, cudaMemcpyHostToDevice);

    vadd<<<4096, 256>>>(da, db, dc, n);

    cudaMemcpy(c, dc, bytes, cudaMemcpyDeviceToHost);
    printf("c[0]=%f c[n-1]=%f\n", c[0], c[n-1]);
}
```

编译后在 RTX 4090 上运行，它会正确地告诉你 1+1=2。

不过正如原文所说，这件事「背后涉及了数千万条 CPU 指令、几个设备文件、九百次 ioctl，以及对一处内存映射门铃寄存器的写入」。下面把这条路径逐段拆开来看。

---

## 用 nvcc 编译

`nvcc` 是一个驱动程序，它会调用多个编译器并把产物拼起来。加上 `--keep` 可以让它保留中间产物：

```
vadd.ptx            # 设备端代码，PTX 形态
vadd.sm_89.cubin    # 设备端代码，SASS 形态
vadd.fatbin         # cubin + PTX，打包在一起
vadd.cudafe1.stub.c # 宿主端的 launch stub 与内核注册
vadd.o              # 最终的宿主端目标文件，内嵌 fatbin
```

设备端代码先经过 `cicc`（基于 LLVM）变成 PTX，再由 `ptxas` 把 PTX 翻译成 SASS。

PTX 是一种虚拟 ISA，拥有「无穷多个」带类型的寄存器。原文里给出的示例 PTX 片段是：

```ptx
mad.lo.s32      %r1, %r3, %r4, %r5;        // ctaid*ntid + tid
setp.ge.s32     %p1, %r1, %r2;             // if i >= n
@%p1 bra        $L__BB0_2;                // 跳到出口
```

PTX 比想象中「啰嗦」，因为它要做到设备无关。

接下来 `ptxas` 把 PTX 翻译成 SASS：

```
/*0000*/  MOV R1, c[0x0][0x28];
/*0010*/  S2R R6, SR_CTAID.X ;
/*0020*/  S2R R3, SR_TID.X ;
/*0030*/  IMAD R6, R6, c[0x0][0x0], R3 ;
/*0040*/  ISETP.GE.AND P0, PT, R6, c[0x0][0x178], PT ;
/*0050*/  @P0 EXIT ;
...
/*00a0*/  LDG.E R4, [R4.64] ;
/*00b0*/  LDG.E R3, [R2.64] ;
/*00d0*/  FADD R9, R4, R3 ;
/*00e0*/  STG.E [R6.64], R9 ;
```

其中的 `c[0x0][…]` 操作数来自常量 bank 0，装着内核参数和启动配置。

cubin 是一个 ELF 文件。fatbinary 把 cubin 和 PTX 打包在一起，作为 JIT 回退方案。

---

## 宿主端如何触发 GPU

在 `main` 启动之前，一个隐藏的构造函数会把这个 fatbinary 注册到 CUDA 运行时。launch stub 把参数按特定偏移（0、8、16、24）放进缓冲区，这些偏移正好与 SASS 里常量 bank 的偏移一一对应。

```c
void __device_stub__Z4vaddPKfS0_Pfi(...) {
    __cudaLaunchPrologue(4);
    __cudaSetupArgSimple(__par0,  0UL);
    __cudaSetupArgSimple(__par1,  8UL);
    __cudaSetupArgSimple(__par2, 16UL);
    __cudaSetupArgSimple(__par3, 24UL);
    __cudaLaunch((char*)vadd);
}
```

运行时接着穿过 `libcuda.so.1`，把这次启动发出去。

从 CUDA 12.2 开始，模块加载默认是延迟的——直到第一次 launch 才会真正发生。

---

## 把任务送进 GPU

GPU 不像 CPU 那样接收函数调用。它坐在 PCIe 那一头，从宿主内存里读一条驱动命令流。这一切都涉及跨主机与设备两侧的内存写入结构体——也就是*通道*（channel）。

有两个关键结构体常驻于宿主 RAM 中：

- **Pushbuffer**：驱动把命令（method）写到这里，喂给 GPU
- **GPFIFO**：一个由指针构成的环形缓冲区，用来协调 GPU 的工作

GPU 上的 host engine 在现代卡上已经不再轮询游标，所以驱动改为「按门铃」：把一个 token 写到门铃寄存器，告诉 GPU 哪条通道有新工作。

这次启动由两个 method 触发：`SET_INLINE_QMD_ADDRESS_A/B`，紧接着是 `LOAD_INLINE_QMD_DATA`。它们把 Queue Meta Data（QMD）按流式方式送进 pushbuffer。

QMD 里装着 grid 和 block 的维度、每个线程占用的寄存器、需要的共享内存，以及 program 与常量 bank 的地址。

---

## 一条指令一条指令地看

Host engine 把 QMD 交给 compute work distributor，后者把任务分发给 RTX 4090 上的 128 个 SM。

编译后的机器码在全局内存里是一段线性的序列。每个 SM 各自有一个本地 I-cache，每个活跃的 warp 也都有自己的 PC。

每个 SM 最多容纳 48 个常驻 warp，由四个调度器负责发射，每个周期最多发射一条指令。

### 一个 warp 何时才有资格被调度？

> 「每条 128-bit 的 SASS 指令都带有一组由 `ptxas` 写好的、压缩在一起的调度控制字段。」

这些控制位决定硬件的调度时机：

1. **静态停顿计数**：用于固定时延的指令
2. **让出提示**：是否让出当前调度优先级
3. **依赖屏障索引**：每个 warp 拥有 6 个物理 scoreboard 屏障（0–5），用于变时延操作

两次 LDG 各置位屏障 B2，FADD 则等待 B2 释放，直到两个 load 都返回。

### 加载数据

当 SM 的 load/store 单元发起 LDG.E 时，它会发现连续的访问模式，自动做 *请求合并*——把 32 个线程各 4 字节的请求合并成 4 个 32 字节的 sector 请求。

请求会先查 L1，再查 L2（RTX 4090 上是 72 MB），最后才落到 GDDR6X VRAM。

Ncu profiling 的结果显示：82.77% 的 warp 处于活跃状态，5.17% 的时间真正在发射指令，DRAM 达到了峰值的 79.65%。

这个内核的「算术强度极低」——每搬运 12 字节才做一次浮点加法。

---

## 回到 CPU

门铃响起的那一刻，启动调用就把控制权交还给了 CPU。GPU 通过 QMD 里的一个信号量（semaphore）来通知完成。

cudaMemcpy 在默认流上排在 kernel 之后，它要等这个信号量。结果还留在 L2 cache 里，所以这次读取直接从 L2 服务，无需绕回 DRAM。

`printf` 接着把结果打出来。

---

## 整条路径

「内核源码先经过 `cicc` 变成 PTX，再经过 `ptxas` 变成 SASS；`fatbinary` 把 SASS 和一份 PTX 备份一起打包成一个带 cubin 的 fatbin，最后链接器把它焊进一个普通的 Linux 可执行文件。」

`cuLaunchKernel` 构建好 QMD，把它写进 pushbuffer，前移 `GP_PUT`，再用一次 MMIO store 敲响门铃。GPU 的 host engine 取到这份工作，交给 compute work distributor。Distributor 把 4096 个 block 全占满地摊到 128 个 SM 上，warp 调度器按编译器写好的停顿计数发射 128-bit 指令，一条合并过的访存路径穿过 DRAM，带宽跑到峰值的五分之四，最后在 100 万条 lane 上各自算出一次加法。

输出：`c[0]=2.000000 c[n-1]=2.000000`。

---

## 附录：如何窥探一次启动的内部

原文描述了若干手段：用 `LD_PRELOAD` 注入的拦截钩子抓取 pushbuffer 的 method、解码命令流的格式、通过一个 peek kernel 直接读设备内存、用 strace 解码 ioctl，以及分析 SASS 控制字。

SASS 控制字的布局：

```
┌────────┬───────────┬──────┬──────┬─┬──────┐
│ reuse  │ wait mask │ read │write │Y│stall │
│  (4)   │    (6)    │ barr │ barr │ │ (4)  │
└────────┴───────────┴──────┴──────┴─┴──────┘
```

原文最后还讨论了 NVCC 在宿主端注册的那些回调，并提到作者就职于 Doubleword——一家「致力于让推理效率产生数量级提升」的公司。

---

> **关于转载**
> - 原文标题：*What Happens When You Run a CUDA Kernel*
> - 原文作者：Fergus Finn
> - 原文链接：<https://fergusfinn.com/blog/what-happens-when-you-run-a-gpu-kernel/>
> - 原文发布时间：2026 年 6 月 29 日
> - 译者注：本文为翻译转载，仅做学习交流之用。译文保留了原文的全部代码片段、技术细节与图表结构，仅在必要处添加了少量译注以辅助中文读者理解。如有错漏，欢迎指正。