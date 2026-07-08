---
layout: post
title: "运行 CUDA 内核时发生了什么"
date: 2026-07-07 21:00:00 +0800
categories: tech gpu
---
> *本文为**非商业学习用途**的中文翻译，原文来自 [Fergus Finn 的博客](https://fergusfinn.com/blog/what-happens-when-you-run-a-gpu-kernel/)，原文标题："What Happens When You Run a CUDA Kernel"，原文发表于 2026 年 6 月 29 日（最后修改于 2026 年 7 月 2 日）。封面：Salomon de Caus 的「钉柱式水风琴」，出自其 1615 年出版的《[Les Raisons des Forces Mouvantes](https://commons.wikimedia.org/wiki/File:Les_raisons_des_forces_mouuantes_auec_diuerses_machines_tant_vtilles_que_plaisantes_aus_quelles_sont_adioints_plusieurs_desseings_de_grotes_et_fontaines_%281615%29_%2814740673966%29.jpg)）。**译者未与原作者取得联系，原站点未声明任何转载或翻译许可。**译者为尊重原作者权益，已在文末详细标注原文出处与作者信息。如需再行转载，请联系原作者或译者。*

---

下面是一段简短的 CUDA 程序，把两个向量相加。

```cuda
__global__ void vadd(const float* a, const float* b, float* c, int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) c[i] = a[i] + b[i];
}

int main() {
    int n = 1 << 20;                 // 一百万个 float（1,048,576）
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

    vadd<<<4096, 256>>>(da, db, dc, n);   // 4096 * 256 = n 个线程，每个 float 一个

    cudaMemcpy(c, dc, bytes, cudaMemcpyDeviceToHost);
    printf("c[0]=%f c[n-1]=%f\n", c[0], c[n-1]);
}
```

为 RTX 4090 编译并运行后，它确实正确地算出了 1+1=2，整整一百万次（作者说他没全检查一遍）：

```
$ nvcc -arch=sm_89 -o vadd vadd.cu && ./vadd
c[0]=2.000000 c[n-1]=2.000000
```

为了把这件事讲清楚，原文说「背后涉及了数千万条 CPU 指令、几个设备文件、九百次 ioctl，以及对一处内存映射门铃寄存器的一次写入」。本文就把这一个内核，从代码一直追到 warp，再回到答案。

> **译注（关于「可读性转变」）**：原文有一段旁注，说这篇文章本身正是「可读性转变（legibility transition）」的一个实例——有了 AI 助手，凡是带点好奇心和（机器增强的）毅力的，几乎没有不能搞清楚的计算机细节。原文还附了 [Res Obscura 上关于「可读性」对 AI 帮我们认识世界的影响的讨论](https://resobscura.substack.com/p/ai-legibility-archives-future-of-research)。

---

## 用 nvcc 编译

先说怎么把这段 CUDA 程序变成设备能真正读的东西。这里需要的不是一个编译器，而是很多个编译器。

`nvcc` 是个驱动程序，会调用多个编译器并把产物拼起来。加 `--keep` 可以让整个流水线都留在磁盘上供你查看：

```
$ nvcc --keep -arch=sm_89 -o vadd vadd.cu && ls
...
vadd.ptx            # 设备端代码，PTX 形态     （来自 cicc）
vadd.sm_89.cubin    # 设备端代码，SASS 形态    （来自 ptxas）
vadd.fatbin         # cubin + PTX，打包在一起  （来自 fatbinary）
vadd.cudafe1.stub.c # 宿主端 launch stub 与内核注册
vadd.o              # 最终的宿主端目标文件，内嵌 fatbin
...
```

宿主端代码交给宿主编译器。设备端代码（`vadd`）步骤要多一些：先由基于 [LLVM](https://en.wikipedia.org/wiki/LLVM) 的 `cicc` 编译成 [PTX](https://developer.nvidia.com/blog/understanding-ptx-the-assembly-language-of-cuda-gpu-computing/)，再由 `ptxas` 把 PTX 翻译成 [SASS](https://modal.com/gpu-glossary/device-software/streaming-assembler)。

PTX 是一种虚拟 [ISA](https://en.wikipedia.org/wiki/Instruction_set_architecture)。它有无穷多个带类型的寄存器，也完全不知道硬件实际有多少。下面是 `vadd` 的（节选过的）PTX 函数体：

```
$ cat vadd.ptx
...
mad.lo.s32      %r1, %r3, %r4, %r5;        // 让 r1 等于 ctaid*ntid + tid
setp.ge.s32     %p1, %r1, %r2;             // 若 i >= n，则置位谓词 p1
@%p1 bra        $L__BB0_2;                 // 若越界，跳到出口
cvta.to.global.u64  %rd4, %rd1;            // 把泛型指针 %rd1 转换为 global 地址，结果存到 %rd4
mul.wide.s32    %rd5, %r1, 4;              // 把 r1 乘以 4，结果存到 %rd5
add.s64         %rd6, %rd4, %rd5;          // %rd4 + %rd5 → %rd6
ld.global.f32   %f2, [%rd6];               // 把 a[i] 装入 %f2
...
add.f32         %f3, %f2, %f1;             // %f1 + %f2 → %f3
st.global.f32   [%rd10], %f3;              // c[i] = ... 写回 global 内存
```

这里的虚拟寄存器形如 `%rd1`–`%rd10`、`%f1`–`%f3`（前缀表示类型：`%r` 是 32 位整数，`%rd` 是 64 位整数，`%f` 是 32 位 float，`%p` 是一位谓词）。

PTX 比想象中「啰嗦」。比如在 `%rd6` 里形成一个地址就要花三条 PTX 指令。原因是 PTX 必须做到设备无关。

为什么偏偏是三条？

CUDA 指针默认是「泛型」的，意味着它可能指向 global、shared 或 local 内存。`cvta.to.global` 断言这个指针位于 global 窗口，这样后面就可以用更便宜的 `ld.global`。`mul.wide.s32` 把索引 `i` 乘以 4（也就是 `sizeof(float)`），同时把位宽从 32 位扩到 64 位，一气呵成。`add.s64` 再把字节偏移加到基指针上。

接下来 `ptxas` 把设备无关的 PTX 翻译成你目标架构的 SASS——这次就不是设备无关了。它产出的 SASS 看起来很不一样：

```
$ cuobjdump -sass vadd
/*0000*/  MOV R1, c[0x0][0x28] ;                      // 设定栈指针（ABI 约定；这里没用上）
/*0010*/  S2R R6, SR_CTAID.X ;                        // R6 = blockIdx.x
/*0020*/  S2R R3, SR_TID.X ;                          // R3 = threadIdx.x
/*0030*/  IMAD R6, R6, c[0x0][0x0], R3 ;              // i = ctaid*ntid + tid
/*0040*/  ISETP.GE.AND P0, PT, R6, c[0x0][0x178], PT ;// P0 = (i >= n)
/*0050*/  @P0 EXIT ;                                  // 是的话，直接退出
/*0060*/  MOV R7, 0x4 ;                               // 把字面量 4（sizeof(float)）装入 R7 作为乘数
/*0070*/  ULDC.64 UR4, c[0x0][0x118] ;                // 从驱动提供的系统值做一次 uniform load
/*0080*/  IMAD.WIDE R4, R6, R7, c[0x0][0x168] ;       // &b[i]
/*0090*/  IMAD.WIDE R2, R6, R7, c[0x0][0x160] ;       // &a[i]
/*00a0*/  LDG.E R4, [R4.64] ;                         // b[i]
/*00b0*/  LDG.E R3, [R2.64] ;                         // a[i]
/*00c0*/  IMAD.WIDE R6, R6, R7, c[0x0][0x170] ;       // &c[i]
/*00d0*/  FADD R9, R4, R3 ;                           // a[i] + b[i]
/*00e0*/  STG.E [R6.64], R9 ;                         // c[i] = ...
/*00f0*/  EXIT ;
```

> **S2R 究竟在做什么**：S2R 是「special register to register」——把硬件为每个线程维护的某个*特殊寄存器*拷贝到普通寄存器。这里取的是 `SR_CTAID.X`（块索引，也就是 `blockIdx.x`）和 `SR_TID.X`（线程在块内的 lane 索引，也就是 `threadIdx.x`），这样 `IMAD` 才能拿它们做算术。

十来个虚拟寄存器在这里被压缩到了 7 个真实寄存器（`ncu` 报告 `launch__registers_per_thread = 16`。反汇编里只命名到了 `R9`，但分配器还为 ABI 和对齐预留了几个）。两条 `mul.wide` 加上 `add`，融合成了一条 `IMAD.WIDE`。`cvta` 那几次转换被整个吞进了寻址里。

那些 `c[0x0][…]` 操作数来自**常量 bank 0**——一块很小的、驱动管理的区域。里面装着内核的参数——也就是指针 `a`、`b`、`c` 和长度 `n`，以及启动配置。填这块 bank 是后面那个叫 QMD 的结构的职责，驱动会在启动时把它交给 GPU。等启动本身送到卡上时再细说。

> **为什么参数放在常量 bank 0？放哪儿了？**：放在常量内存是因为这是一次*广播*读：grid 中的每个线程都需要同一组指针，常量缓存可以一次性喂给全部 32 个 lane。布局是固定的——`0x160`、`0x168`、`0x170` 是指针 `a`、`b`、`c`，`0x178` 是 `n`，启动配置跟它们挨在一起，位于 `0x0`（也就是 `blockDim.x`）。Bank 0 还装着 ABI 参数，例如 `c[0x0][0x28]`，那是栈基址，由入口处的 `MOV R1, c[0x0][0x28]` 读出。等后面看宿主端 stub 打包参数时会再次碰到这些偏移。

装这段 SASS 的「cubin」是一个 [ELF](https://en.wikipedia.org/wiki/Executable_and_Linkable_Format) 文件——和 Linux 上普通可执行文件、动态库用的容器是同一种（`cuobjdump -elf` 会显示符号表、装机器码的 `.text.vadd` 段，以及像 `.nv.callgraph` 这样的 CUDA 专用段）。`fatbinary` 工具把 cubin 和 PTX 一起打包成一个「fatbin」，对它跑 `cuobjdump` 就会看到我们二进制里嵌的 fatbin 同时包含两份代码：

```
$ cuobjdump vadd
...
Fatbin elf code:  arch = sm_89        # 也就是我们刚读到的 SASS
Fatbin ptx code:  arch = sm_89  compressed   # PTX 也一起带着
```

SASS 才是真正在这张 4090 上跑的，但 PTX 跟着作为前向兼容的回退方案。如果将来把这个二进制拿到一个 cubin 没覆盖到的架构上跑，驱动可以在加载时把 PTX 现场编译成新的 SASS。

最后，这个 fatbin 又被嵌进宿主可执行文件里。`readelf -S` 会发现它独占好几个段：

```
$ readelf -S vadd
...
[18] .nv_fatbin        PROGBITS   ...
[19] __nv_module_id    PROGBITS   ...
[29] .nvFatBinSegment  PROGBITS   ...
...
```

`nvcc` 吐出来的 `vadd` 二进制是个单一可执行文件，里面同时装着宿主代码、一份完整包含 Ada SASS 的 ELF 对象、以及一份 PTX。因为 PTX 是啰嗦的纯文本，`nvcc` 默认会压缩它来缩减体积；只有当二进制被运行在 cubin 没覆盖的架构上时，驱动才会解压并 JIT 编译它。

---

## 宿主端如何触发 GPU

编译后的 GPU 机器码此刻正静静地躺在 `./vadd` 可执行文件的 `.nv_fatbin` 段里。在宿主上启动程序时，得把两个世界连起来：宿主 CPU，以及坐在 PCIe 那一头的 GPU。

为了让宿主二进制知道怎么跨过这道桥，前端编译器（`cudafe++`）会在你的代码里偷偷塞一个构造函数，在 `main` 跑之前先跑起来。它的任务是把这个嵌入的 fatbin 注册到 CUDA 运行时，并记录一张表——把宿主侧的函数指针 `vadd` 跟 fatbin 里编译出来的设备内核的 mangled 名字对应起来。

编译器遇到 `vadd<<<4096, 256>>>(da, db, dc, n)` 时，会把这一整段高级表达式替换成生成出来的宿主端 launch stub。这个 stub 把我们的内核参数打包到宿主内存的一段缓冲区里。指针 `da`、`db`、`dc` 和整数 `n` 各自按字节偏移 `0`、`8`、`16`、`24` 对齐（这些偏移就是前面 SASS 从常量 bank 0 读的 `0x160`、`0x168`、`0x170`、`0x178`）：

```c
// from vadd.cudafe1.stub.c
void __device_stub__Z4vaddPKfS0_Pfi(const float *__par0, const float *__par1,
                                     float *__par2, int __par3) {
    __cudaLaunchPrologue(4);
    __cudaSetupArgSimple(__par0,  0UL);   // arg 缓冲区偏移 0
    __cudaSetupArgSimple(__par1,  8UL);   // 偏移 8
    __cudaSetupArgSimple(__par2, 16UL);   // 偏移 16
    __cudaSetupArgSimple(__par3, 24UL);   // 偏移 24
    __cudaLaunch((char*)(void(*)(const float*, const float*, float*, int))vadd);
}
```

参数打包好之后，stub 调用 `__cudaLaunch`，把宿主侧那个 dummy `vadd` 函数的内存地址传过去。因为这个宿主函数在 CPU 上只是个空壳，它的内存地址就当 lookup key 用。运行时拿这个地址去查它那张注册表，找到对应的设备端符号名，然后穿过边界进入闭源的用户态驱动（`libcuda.so.1`）（用户态驱动这块是跟着 GPU 的内核驱动一起装上的，不属于 CUDA toolkit：`strace` 看到 `libcuda.so.1` 解析到了 `libcuda.so.590.48.01`，也就是本机当前装的驱动版本）发起内核启动。

运行时会在程序里第一次 GPU 调用时动态打开这个驱动——用 `strace` 可以抓到：

```
$ strace -f -e trace=openat ./vadd
...
openat(..., "/lib/x86_64-linux-gnu/libcuda.so.1", O_RDONLY|O_CLOEXEC) = 3
...
```

在这第一次调用进行时，会创建一个「上下文（context）」，里面装着驱动跟设备对话所需的全部基础设施，包括 CPU 与 GPU 通话的*通道（channel）*。下一节再细说通道。

到现在为止，编译好的机器码还没真正到达 GPU。从 CUDA 12.2 起，模块加载默认是惰性的（由 `CUDA_MODULE_LOADING` 控制。这个选项 11.7 里以可选方式出现，多年默认是 `EAGER`；12.x 系列把默认翻成了 `LAZY`，需要的话可以覆盖回去）——驱动会一直拖到那个特定内核真正被启动时，才把它的 SASS cubin 上传到卡上的内存。

`libcuda` 底下是内核态驱动 `nvidia.ko`，`libcuda` 通过对设备文件做 `ioctl` 来到达它。当 `cuLaunchKernel` 终于要把工作送进 GPU 时，这场对话就变成了和那个内核模块的对话。下面就是这场对话的机制。

---

## 把任务送进 GPU

GPU 接收命令的方式跟 CPU 完全不同。它没有入口地址可以跳过去，也没有栈可以由 CPU 来压参数。GPU 坐在 PCIe 那一头，从宿主内存里读一条驱动命令流。从 `cuLaunchKernel` 之后做的所有事，都是为了让一条完整的 launch 命令进入这条流，然后把「做完了」这件事告诉 GPU。

首先要做的是把 GPU 代码装到设备上。第一次跑 `vadd` 时，驱动把内核的代码拷过去：分配一块缓冲区，把 SASS 拷进去。

代码到了 GPU 上之后，CPU 还得让 GPU 读它并开始执行。它要穿过多段跨宿主内存和设备内存的复杂协作。宿主和 GPU 都能映射对方地址空间里的一部分区域，但跨 PCIe 的访问是要付代价的。要完成一次内核启动，宿主和设备都得往好几个结构体里写——这些结构体分居两端，合起来就是*通道*——也就是 GPU 操作所使用的工作队列。

其中有两个重要结构体住在宿主 RAM 里：**pushbuffer** 和 **GPFIFO**，它们合起来代表 GPU 要做的工作清单。

**pushbuffer** 是一段内存区域，驱动往里写发给 GPU 的命令，这些命令叫 *method*。一个 method 是一对「GPU 原生命令编码下的寄存器地址 + 值」，由这对值告诉 GPU 该执行什么动作。

**GPFIFO** 是一个由指针构成的环形缓冲区，GPU 和 CPU 用它来协调「GPU 还要读什么」和「已经读过什么」。GPFIFO 的每一项由两个 32 位字组成，描述 pushbuffer 的一小段范围（这里 base 是一个指向宿主内存的 GPU 虚拟地址，形如 `(base, length)`）。

GPU 不停地走 GPFIFO 找活干。驱动和 GPU 各自维护两个游标：`GP_GET`（GPU 消费到哪儿了）和 `GP_PUT`（驱动生产到哪儿了）。两个游标都住在 USERD 里——一个很小的、每通道一个的结构体，这里它位于设备内存。要启动一个内核，驱动会用相关的 method 填一段 pushbuffer 范围，把 GPFIFO 的一项指过去，然后推进 `GP_PUT`。GPU 消费掉那一项之后，再推进 `GP_GET`。

> **各部分住在哪里**（译者按图重画）：
>
> **CPU（宿主 RAM）**：pushbuffer（method + QMD）、GPFIFO 环形、USERD（GP_GET / GP_PUT）。Doorbell 是 MMIO 映射的寄存器。
>
> **GPU**：Host engine、Compute work distributor、SMs、VRAM（指令流、QMD、计算结果）。
>
> **PCIe**：driver→doorbell 是 MMIO 写，GPU→host 是 DMA 拉 pushbuffer，CPU↔GPU 互相可映射部分内存。

这次启动由一段 method 突发触发，先是 [`SET_INLINE_QMD_ADDRESS_A/B`](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/590.48.01/src/common/sdk/nvidia/inc/class/clc6c0.h#L403-L409)（原文注：「我怎么会知道是这个 method，毕竟 `libcuda` 是闭源的」——见[附录](#附录如何窥探一次启动的内部)），接着是一连串 [`LOAD_INLINE_QMD_DATA`](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/590.48.01/src/common/sdk/nvidia/inc/class/clc6c0.h#L409-L410)。这些 method 把一个叫「Queue Meta Data」（**QMD**）的对象以流式方式送进 pushbuffer。

QMD 就是这次计算的启动描述符。它装着 grid 和 block 的维度——也就是我们 `.cu` 代码里的 4096 和 256——每个线程占的寄存器数、需要的共享内存，以及两个地址：程序的起始地址（第一次启动时装进 GPU 内存的 SASS）和装着内核参数的常量 bank。那个 bank 就是宿主 stub 打包好的参数要去的地方：驱动把参数拷进去，再把 bank 的地址记到 QMD 里。QMD 告诉 GPU SASS 在哪儿、怎么把 SASS 变成一个并行程序、以及在哪里通知程序已经跑完。

万事俱备，只欠 GPU 上场。问题是 GPU 的 **host engine**（GPU 控制逻辑里负责对接宿主的那部分）还没动：在现代卡上它不再盯着游标（老的 GPU 曾经会——它们会[窥探 USERD](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/590.48.01/src/common/unix/nvidia-push/src/nvidia-push.c#L421-L438)，所以写一次 `GP_PUT` 就够了；Turing 之后不这么干了，驱动改为按门铃），所以 `GP_PUT` 的变化就停在那儿，得有人告诉引擎去看。

告诉它去看的东西叫 **doorbell**（门铃）。GPU 把一小块寄存器窗口映射进进程，其中之一就是门铃；驱动把通道的*工作提交 token*写到那里。Token 告诉引擎「这条通道有活儿」。

门铃一响，host engine 就读最新的 `GP_PUT`，顺着新的 GPFIFO 项找到 pushbuffer 那段范围，通过 DMA 把 method 拉过来。等它走到承载我们 QMD 的那条 compute method 时，就把描述符转交给「compute work distributor」，我们等一下再看这个分发器。

从 CPU 的角度看，启动已经完成了：`cuLaunchKernel` 在门铃响起那一刻就返回了。这个调用是异步的，所以控制权交回程序，CPU 继续往前跑，GPU 同时在干活。我们等 kernel 跑完再回到宿主端的故事。

现在轮到 GPU 真正开始做活了。

---

## 一条指令一条指令地看

host engine 把 QMD 交给 **compute work distributor**（有时仍叫 GigaThread Engine。整张 GPU 上只有一个分发器。整个 VRAM 里只有一份线性的 SASS 指令序列，compute work distributor 加上 QMD 就是第一步——告诉硬件怎么把这条线性的线程指令列表变成跨所有 **Streaming Multiprocessor**（SM）的大规模并行程序）。

沿着这条栈一路往下走，compute work distributor 现在手里有一份 QMD，描述了 4096 个 block、每 block 256 个线程。我们目标卡的芯片是 GeForce RTX 4090，有 **128 个 SM**（NVIDIA 的 AD102-300-A1 SKU 把满晶 144 个 SM 关掉了 16 个以提升良率，详见 [NVIDIA Ada GPU 架构白皮书](https://images.nvidia.com/aem-dam/Solutions/geforce/ada/nvidia-ada-gpu-architecture.pdf)）。分发器的任务是把所有 128 个 SM 都喂饱。

编译后的机器码在全局内存里是一段线性序列。每个 SM 各自带一个本地 Instruction Cache（I-cache），GPU 上每个活跃的 warp 也各自维护一个私有 [Program Counter](https://en.wikipedia.org/wiki/Program_counter)（PC）（从 Volta 开始模型更细——每个*线程*都有自己的程序计数器和调用栈（[Independent Thread Scheduling](https://docs.nvidia.com/cuda/volta-tuning-guide/index.html#independent-thread-scheduling)），可以让一个 warp 里的线程自由分叉与重聚。Issue 还是按 warp 算的：每个周期调度器选一个 warp，issue 给当前处在同一 PC 的那些 lane）。SM 上的调度器于是独立地从这段线性序列里取指，让不同的 warp 可以用不同的速度执行同一段 SASS，甚至走不同的分支。

> **译者按图重画**：
>
> VRAM 里的指令流（SASS）：`S2R R6, CTAID.X` → `S2R R3, TID.X` → `IMAD R6, R6, ntid, R3` → `ISETP.GE P0, R6, n` → `@P0 EXIT` → `IMAD.WIDE R4, …` → `LDG.E R4, [R4]` → `LDG.E R3, [R2]` → `FADD R9, R4, R3` → `STG.E [R6], R9` → `EXIT`
>
> × 128 SM，每个 SM：I-cache 缓存上面的指令流；48 个常驻 warp；4 个调度器，每周期最多发射 1 条指令。
>
> 在跑 `vadd` 这个时刻，几乎每个 warp 都停在 `LDG.E`（橙色），只有一个 slot 在发射 `FADD`（绿色）。

我们 SM 的硬件约束决定了同一时刻能跑多少个 block（`cudaGetDeviceProperties` 会告诉你这些信息）：

```
      +------------------------------------------------------------+
      |                   AD102 SM Resource Caps                   |
      +------------------------------------------------------------+
      |  Max Active Threads/SM |  1,536 threads (48 warps)         |
      |  Register File/SM      |  65,536 32-bit registers (256 KB) |
      |  Shared Memory/SM      |  100 KB                           |
      +------------------------------------------------------------+
```

我们这个启动配置里，每个 block 是 **256 个线程（8 个 warp）**，而 `ptxas` 为每个线程预留了 **16 个寄存器**。

1. **寄存器容量**：每个 block 需要 256×16=4096 个寄存器。光看寄存器，一个 SM 可以塞 65536/4096=16 个常驻 block。
2. **线程容量**：硬件把每个 SM 的活跃线程数硬限制在 1536。拿 block 大小去除，得到 1536/256=6 个常驻 block。

因为线程容量是更紧的瓶颈，每个 SM 同一时刻最多容纳 **6 个 block（48 个 warp）**。

分发器把这 6 个常驻 block 分配给 SM。每个 SM 进一步被分成 **四个处理块（sub-partition）**。每个 sub-partition 是一条自洽的执行流水线。

SM 把 48 个常驻 warp 平均分给这 4 个 sub-partition，所以 SM 满载时每个 warp 调度器手上有 **12 个活跃 warp**（48/4）需要管。每个周期，调度器从自己的 12 个候选里挑一个*有资格*的 warp，把它下一条指令分发到它那条执行 slice 的 32 个物理 lane 上。

### 一个 warp 的「有资格」是什么意思？

GPU 判断一条指令何时就绪的方式跟 CPU 是不一样的。现代乱序 CPU 是[运行时动态发现依赖](https://en.wikipedia.org/wiki/Tomasulo%27s_algorithm)，靠 [reorder buffer](https://en.wikipedia.org/wiki/Re-order_buffer) 和[寄存器重命名](https://en.wikipedia.org/wiki/Register_renaming) 这些硅去榨单线程里的并行。GPU 不需要这些——它靠常驻一大堆 warp、遇到阻塞就换别的 warp 来藏时延。在并行俯拾皆是的情况下，搞太重的依赖跟踪硬件是划不来的。所以硬件把那些它能预测时序的部分全部推给编译器来调度，剩下的再由轻量的硬件 scoreboard 兜底。

每条 128-bit 的 SASS 指令都带一组由 `ptxas` 写好的、压缩在一起的调度控制字段（最清晰的公开重建资料是 Citadel 那批 microbenchmarking 论文——[Jia 等人「Dissecting the NVIDIA Volta GPU Architecture via Microbenchmarking」](https://arxiv.org/abs/1804.06826)——以及 Maxwell 上 [maxas 控制码笔记](https://github.com/NervanaSystems/maxas/wiki/Control-Codes)）。这些调度控制位直接决定硬件时序，包含三条关键指示：

1. **静态停顿计数**：对于固定时延的指令——比如标准的整数或浮点运算——编译器精确知道 ALU 什么时候会回写。它编码一个精确的周期数，告诉调度器在发出下一条指令前把这个 warp 停多久。
2. **让出提示**：一位，告诉调度器这个 warp 是否要让出调度优先级。如果编译器知道这个 warp 马上要撞瓶颈，就置位这个提示，让调度器在下一个时钟周期优先安排别的活跃 warp。
3. **依赖屏障索引**：对于编译期无法预测时长的*变时延*操作——主要是 global 内存 load（`LDG`）和特殊函数（`MUFU`）——硬件提供**六个物理 scoreboard 屏障（编号 0 到 5）**，每个 warp 一份。

> **为什么反汇编里看不到这些位**：用 NVIDIA 标准的 `nvdisasm` 工具反汇编时，默认会把这些裸的控制码藏起来，只显示干净的标准 SASS 助记符。但它们其实就跟在指令旁边。如果用 `cuobjdump -sass` 看原始二进制、再细看十六进制的指令注释（例如 `/* 0x... */`），就能看到承载这些控制位的、压缩的裸 64-bit 字。
>
> 关于它们的具体布局，都是 microbenchmarking 社区逆向出来的。虽然 Maxwell、Volta、Ampere 和 Ada Lovelace 之间的位域一直有演化，但背后的架构思想没变：把编译期的静态调度元数据直接塞进指令流，让 SM 硬件尽可能简单、省电。

跑 `cuobjdump -sass` 看我们的 `vadd`，每条指令都带着它的裸 128-bit 编码，形如两个 64-bit 字；*第二个*字装着控制负载：

```
$ cuobjdump -sass vadd                              # 控制负载
/*00a0*/  LDG.E R4, [R4.64]                       /* 0x000ea8000c1e1900 */
/*00b0*/  LDG.E R3, [R2.64]                       /* 0x000ea2000c1e1900 */
/*00c0*/  IMAD.WIDE R6, R6, R7, c[0x0][0x170]     /* 0x000fe200078e0207 */
/*00d0*/  FADD R9, R4, R3                         /* 0x004fca0000000000 */
/*00e0*/  STG.E [R6.64], R9                       /* 0x000fe2000c101904 */
```

把控制负载拆开来看（具体位域在[附录](#附录sass-控制字的解码)），就能看到 `ptxas` 写下的调度表，每条指示都各就各位：

| 指令 | stall | yield | sets | waits-on |
|------|------:|------:|------|----------|
| `LDG.E` | 4 | yes | `B2` | — |
| `LDG.E` | 1 | yes | `B2` | — |
| `IMAD.WIDE` | 1 | yes | — | — |
| `FADD` | 5 | no | — | `B2` |
| `STG.E` | 1 | yes | — | — |

两条 load 用的是上面第 3 条指示，各自「set」了**同一个** scoreboard 屏障 `B2`。`FADD` 是第一条要用到 `R4`、`R3` 的指令，所以它对 `B2` 等待：直到两个 load 都返回、屏障清掉之前，这个 warp 都是**无资格**的，调度器会跳过它，去挑这个 sub-partition 里其他 11 个 warp 之一。

`FADD`→`STG` 的衔接是上面第 1 条指示。浮点加法时延是固定的，所以没有屏障：`FADD` 仅仅带了 `stall=5`，让这个 warp 停几个周期等 `R9` 落定，然后 `STG` 再去读它。

第 2 条「yield」位则在这串指令里忽明忽暗——编译器在那些马上要等的操作周围调整调度优先级。

每个周期调度器读 warp 的 6 位屏障状态和一个小停顿计数器，再对每个 warp 做出资格决定。GPU 就是用这种方式，用几乎为零的硬件调度开销，把时延藏了起来。

### 加载数据

当 warp 调度器找到一个有资格的 warp 并把 `LDG.E` 发射出去之后，我们可以顺着硬件请求往下追访存层级。warp 里 32 个线程各自算出一个地址。因为我们这些线程访问的是 `float` 数组的相邻元素（每个 4 字节），warp 一次请求的就是连续 128 字节（32×4 字节）。

SM 的 load/store 单元识别出这种连续访问模式，做**请求合并**：把 32 个线程各 4 字节的请求合并成 4 个 32 字节的 sector 请求。取数的单位本来就是 32 字节，所以刚好完美——如果不是连续合并的话，就得多装不少用不上的数据。

合并后的请求先去 SM 本地的 L1 Data Cache 查。如果 miss，就走一条高带宽的 crossbar 互联，连到分散在 L2 Cache（72 MB）各处的片。如果 L2 也 miss，就再下沉到内存控制器，穿过显存总线到物理的 GDDR6X VRAM 芯片上（RTX 4090 用的是 GDDR6X 而不是 A100/H100 那种数据中心卡上的 HBM）。把 `c[i]` 写回去的 `STG.E` 走的是一模一样的反向路径（原则上如此，后面会看到 `c[i]` 实际上根本没到 VRAM）。

用 NVIDIA Nsight Compute profiler（`ncu`）跑一下编译好的内核，能拿到一组很有说明性的指标：

```
$ ncu --metrics \
    launch__grid_size,launch__block_size,launch__registers_per_thread,\
    launch__waves_per_multiprocessor,sm__warps_active.avg.pct_of_peak,\
    smsp__issue_active.avg.pct_of_peak,dram__throughput.avg.pct_of_peak,\
    gpu__time_duration.sum \
    ./vadd
...
----------------------------------------------------------
Metric Name                                   Unit   Value
----------------------------------------------------------
launch__grid_size                                    4,096
launch__block_size                                     256
launch__registers_per_thread                            16
launch__waves_per_multiprocessor                      5.33
sm__warps_active.avg.pct_of_peak              %      82.77
smsp__issue_active.avg.pct_of_peak            %       5.17
dram__throughput.avg.pct_of_peak              %      79.65
gpu__time_duration.sum                        us     10.78
----------------------------------------------------------
```

整个过程里 82.77% 的 warp 处于活跃状态。Warp 真正在发射指令的时间是 5.17%。DRAM 跑到了峰值的 79.65%。

这个内核的**算术强度**极低：每搬运 12 字节的数据，就只做一次浮点加法（`FADD`）加一点点指针算术。

所以这 `10.78 μs` 完全取决于 DRAM 总线喂数据给内核的速度——这里大概是峰值的五分之四（只有两个输入走过总线，不是全部 12 MB。`ncu` 显示从 DRAM 读了 8.4 MB，写出去基本为 0：4 MB 的输出 `c` 在 72 MB 的 L2 里放得下，要等到稍后 device-to-host 那次 memcpy 把它读回来时才会被刷下去。「五分之四峰值」算的是读端——8.4 MB / 10.78 μs ≈ 780 GB/s）。

---

## 回到 CPU

结果现在坐在 GPU 的 L2 缓存里。终端是 CPU 跑的，所以得把结果搬过来才能显示。我们切回 CPU 视角。

门铃一响，启动调用就把控制权交还给 CPU 了。所以 GPU 得告诉 CPU 它做完了。等 4096 个 block 全部退场时，GPU 通过提交 QMD 里带过来的一个完成信号量（[fence 字段](#附录读取设备内存与-qmd-布局)在字 23–24）来通知完成。

device-to-host 的 `cudaMemcpy(c, dc, …)`（用 pinned memory 的 `cudaMemcpyAsync` 就可以不等，让宿主先往前跑）在默认流上排在 kernel 后面，所以 GPU 的 copy engine 要等那个信号量。信号量一出，GPU 就做 DMA。因为 `c` 还脏着坐在 72 MB 的 L2 里——`STG.E` 那几次 store 根本没需要把它挤回 DRAM——copy engine 的读直接从 L2 供数，数据穿过 PCIe 时连 DRAM 那一趟都省了。

copy 完成后，它再 post 一个自己的信号量，宿主在 `cudaMemcpy` 里一直等的就是它。`cudaMemcpy` 在宿主端完成，`c` 又回到普通宿主内存，`printf` 把 `c[0]` 和 `c[n-1]` 从 RAM 里读出来，格式化成字符串，通过 stdout 上的 `write` syscall 交出去。

---

## 整条路径

内核源码经过 `cicc` 编译成 PTX，再经过 `ptxas` 编译成 SASS；`fatbinary` 把 SASS 和一份 PTX 备份一起打包成一个带 cubin 的 fatbin，链接器把它焊进一个普通的 Linux 可执行文件。一个构造函数在 `main` 之前把这个 fatbin 注册掉，把宿主 stub 跟一个 mangled 的设备名字对应起来。第一次启动时，cubin 被惰性地上传到 GPU。`cuLaunchKernel` 根据启动配置构建好一份 QMD，把它作为 GPU method 写进 pushbuffer，推进 `GP_PUT`，再用一次 MMIO store 敲响门铃——GPU 的 host engine 取到这份工作，把 QMD 交给 compute work distributor。分发器把 4096 个 block 以满占用的方式摊到 128 个 SM 上，每个 SM 4 个 warp 调度器发射那些 128-bit 的指令——停顿计数是编译器写好的——一条合并过的访存路径把输入以五分之四峰值带宽穿过 DRAM 拉进来，在一百万条 lane 上各自算一次加法。然后一个完成信号量和一个 copy engine 把结果搬回总线那一头，`printf` 已经在等着了，于是我们看到：

```
c[0]=2.000000 c[n-1]=2.000000
```

---

## 附录：如何窥探一次启动的内部

原文作者和 Claude 用了一堆奇技淫巧去看这次内核启动不同环节的发生。有些来自对 [open kernel modules](https://github.com/nvidia/open-gpu-kernel-modules) 的细致阅读。

文中有些论断没法从开源代码里看出来，因为 `libcuda` 是闭源的。为了搞清楚那些事，有几个有用的诊断钩子可以用。

### 一个 interposition 钩子

驱动的 method 写从来没走过 syscall（驱动直接写进一块已经映射好的 write-combined 缓冲区），所以要找到它们得直接读内存。原文用了一个 `LD_PRELOAD` 垫片，包住 `mmap`，把驱动从 `/dev/nvidia*` 文件映射上来的每块区域都记下来，再暴露一个函数，让测试程序在 launch 返回后立刻调用，把这些区域 dump 出来：

```c
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <dlfcn.h>
#include <sys/mman.h>
#include <unistd.h>
#include <string.h>

// Dynamic linker function pointers
static void* (*orig_mmap)(void*, size_t, int, int, int, off_t) = NULL;

// Store captured channel mappings
struct Map {
    void* addr;
    size_t length;
    off_t offset;
    char path[256];
} maps[128];
static int map_count = 0;

void* mmap(void* addr, size_t length, int prot, int flags, int fd, off_t offset) {
    if (!orig_mmap) {
        orig_mmap = dlsym(RTLD_NEXT, "mmap");
    }
    void* ret = orig_mmap(addr, length, prot, flags, fd, offset);
    if (ret != MAP_FAILED && fd != -1 && map_count < 128) {
        char proclink[256];
        char path[256];
        sprintf(proclink, "/proc/self/fd/%d", fd);
        ssize_t len = readlink(proclink, path, sizeof(path) - 1);
        if (len != -1) {
            path[len] = '\0';
            // We care about NVIDIA device files
            if (strstr(path, "/dev/nvidia")) {
                maps[map_count].addr = ret;
                maps[map_count].length = length;
                maps[map_count].offset = offset;
                strcpy(maps[map_count].path, path);
                map_count++;
            }
        }
    }
    return ret;
}

// Expose a function to dump memory ranges holding the pushbuffer
void dump_pushbuffer() {
    printf("\n=== [Shim] Dump of Mapped Pushbuffers ===\n");
    for (int i = 0; i < map_count; i++) {
        // User-space channels/pushbuffers are mapped at large sizes
        if (maps[i].length >= 0x1000) {
            unsigned int* ptr = (unsigned int*)maps[i].addr;
            printf("Mapping %d: %s, at %p (%zu bytes), offset 0x%lx\n",
                   i, maps[i].path, maps[i].addr, maps[i].length, (long)maps[i].offset);

            // Walk the words looking for a method-header burst
            for (size_t j = 0; j < maps[i].length / 4; j++) {
                unsigned int word   = ptr[j];
                unsigned int opcode = (word >> 29) & 0x7;     // 1 = INC
                unsigned int count  = (word >> 16) & 0x1FFF;  // payload words
                unsigned int method = (word & 0xFFF) << 2;    // register offset

                // 0x318 is SET_INLINE_QMD_ADDRESS_A, the start of the inline burst
                if (opcode == 1 && method == 0x318) {
                    printf("  [+] Method burst at word %zu: header = 0x%08X\n", j, word);
                    printf("      INC, count %d, offset 0x%04X\n", count, method);
                    for (unsigned int k = 1; k <= count && (j + k) < (maps[i].length / 4); k++) {
                        printf("      word %02u: 0x%08X\n", k, ptr[j + k]);
                    }
                }
            }
        }
    }
}
```

编译成共享库：

```
$ gcc -shared -fPIC -o shim.so shim.c -ldl
```

然后让测试程序在内核 launch 之后立刻调用 `dump_pushbuffer()`，跑的时候把这个 shim preload 进来，原来的 `mmap` 就被这个版本替换掉：

```
$ LD_PRELOAD=./shim.so ./vadd
```

驱动会为通道映射一块 write-combined 缓冲区；dump 程序走这块缓冲区，把这次启动的 method 突发打出来。然后我们还得把这段流解码。

### pushbuffer 命令流的解码

一条 pushbuffer method 由一个 header word 加若干 data word 组成。header 里塞了四个字段（在 `clc46f.h` 里以 `NVC46F_DMA_INCR_*` 宏的形式定义）：

* **位 31:29**——opcode：`0x1` 是 increasing-method write（`INC_METHOD`/`INCR_OPCODE_VALUE`），`0x3` 是 non-increasing-method write（`NON_INC_METHOD`），`0x4` 是 immediate-data write（`IMMD_DATA_METHOD`）。
* **位 28:16**——count：payload word 的数量（`NVC46F_DMA_INCR_COUNT`）。
* **位 15:13**——subchannel index：把命令路由到某个专用的后端引擎上下文（`NVC46F_DMA_INCR_SUBCHANNEL`）。
* **位 11:0**——method 的寄存器偏移，除以 4（`NVC46F_DMA_INCR_ADDRESS`），shim 里又把它左移回来。

看起来相关的 launch 路径有两条。Method 在 `src/common/sdk/nvidia/inc/class/` 下按 compute class 各自定义——`clc3c0.h`（Volta）、`clc5c0.h`（Turing）、`clc6c0.h`/`clc7c0.h`（Ampere）、`clc9c0.h`（Ada）、`clcbc0.h`（Hopper）、`clcdc0.h`（Blackwell）。Ada 的 header（`clc9c0.h`）是个 29 行的 stub，只定义 class number `0xC9C0`，剩下全部继承 Ampere 的 method 集合，所以真正在读的定义都在 Ampere 的 header 里：

* `0x0318`——`SET_INLINE_QMD_ADDRESS_A`（在 Ampere header 里定义为 `NVC6C0_SET_INLINE_QMD_ADDRESS_A`，Ada 不变地继承下来），打开 inline-QMD 突发，紧接着通过 `LOAD_INLINE_QMD_DATA(i)`（偏移 `0x0320 + i * 4`）流式写入 pushbuffer。
* `0x02b4`——`SEND_PCAS_A`，out-of-line 路径，只带一个指向 VRAM 中别处的 QMD 的指针。

从 dump 结果里，可以推断出真正用的是哪条路径。Dump 显示走的是 inline 路径：一次 increasing-method 突发，count 为 66，开头就是 `SET_INLINE_QMD_ADDRESS_A`。这 66 个字是两条地址 word（`SET_INLINE_QMD_ADDRESS_A`/`_B`，`0x0318`/`0x031c`），紧跟 64 个 `LOAD_INLINE_QMD_DATA` 字（从 `0x0320` 往后）——也就是一份 256 字节的 inline QMD。其中字 12 是 `0x1000`、字 18 是 `0x100`：就是 `vadd<<<4096, 256>>>` 里的 4096 和 256。

### 读取设备内存与 QMD 布局

Queue Meta Data（QMD）这个结构以多字（multi-word）布局表示，字段在 `src/common/sdk/nvidia/inc/class/cla0c0qmd.h` 里以跨越 32-bit 边界的 MW 位定义。QMD 装着若干地址类的字段，但它们并不是同一种值：

* `PROGRAM_OFFSET`——MW(287:256)（Word 8）是一个**32 位**的入口偏移，相对于通道的 code base，而不是 64 位指针。
* `CONSTANT_BUFFER_ADDR_LOWER(i)` / `ADDR_UPPER(i)`——MW(959+i_64:928+i_64)（例如装参数的 Constant Bank 0 就在 Words 29–30）。
* `RELEASE0_ADDRESS_LOWER/UPPER`——MW(767:736)（Words 23–24），用作 fence/semaphore。
* `CIRCULAR_QUEUE_ADDR_LOWER/UPPER`——MW(319:288)（Words 9–10）。

这些指向 CPU 不能直接读的设备内存：普通 load 会 fault，`cudaMemcpy` 和 `cuMemcpyDtoH` 也会拒绝这个地址。

所以得让 GPU 来读。一个小内核把 512 字节从一个裸指针拷进一块宿主能取回的缓冲区：

```cuda
__global__ void peek(const unsigned char* src, unsigned char* dst) {
    for (int i = blockIdx.x * blockDim.x + threadIdx.x;
         i < 512;
         i += blockDim.x * gridDim.x) {
        dst[i] = src[i];
    }
}
```

把 `peek` 指向 QMD 的各个字段，结果只有一处能完整取出 512 字节的 SASS——那就是要找的字段。如果再跑一个内存扫描 shim 去 QMD 里找合法的 GPU 虚拟地址，会在 Word 48 上命中一次：

```
qmd[48] -> 0x74167b272300   512 / 512 bytes match
```

为什么 SASS 是在 Word 48（`qmd[48]`）命中的，而驱动的 program 字段是 Word 8 的 `PROGRAM_OFFSET`？

Word 8 只是驱动填的 32 位偏移，而 Word 48/49 是硬件自己持有的 `HW_ONLY_INNER_GET`（`MW(1566:1536)`）和 `HW_ONLY_INNER_PUT`（`MW(1598:1568)`）字段。Launch 之后的 dump 里，这两个字装着一个完整的 64 位 GPU 虚拟地址，对 Word 48 这个值解引用就拿到内核 SASS。最直白的解释是：调度器在 launch 时把 program offset 解析进了这两个调度器持有的字段。

### 解码驱动的 ioctl

命令流得从内存里读，但 `libcuda` 用的是最普通的办法来准备它的内存和 GPU 对象：对驱动的设备文件做 `ioctl`（参见 Michael Kerrisk 的《[The Linux Programming Interface](https://www.amazon.com/Linux-Programming-Interface-System-Handbook/dp/1593272200)》第 4、15 章）。对这个一核程序跑 `strace`，会录到 948 次 ioctl（绝大多数是一次性 setup；稳态 launch 循环里的 ioctl 远远更少），几乎全发生在两个文件描述符上——`/dev/nvidiactl` 和 `/dev/nvidia-uvm`：

```
$ strace -f -e trace=ioctl ./vadd
...
ioctl(8, _IOC(_IOC_READ|_IOC_WRITE, 0x46, 0x2a, 0x900), ...)   # /dev/nvidiactl
ioctl(8, _IOC(_IOC_READ|_IOC_WRITE, 0x46, 0x2b, 0x30),  ...)   # /dev/nvidiactl
ioctl(9, ...)                                                  # /dev/nvidia-uvm
...
```

那个魔数 `0x46` 是 `'F'`，是 NVIDIA 资源管理器 ioctl 的 magic number（'magic' 字节是每个 NVIDIA ioctl 都带的一个 sanity check；见 [Linux 内核文档](https://kernel.org/doc/html/v5.4/process/magic-number.html)）。命令号对应到开源 kernel module 的 [`nv_escape.h`](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/590.48.01/src/nvidia/arch/nvalloc/unix/include/nv_escape.h#L27-L31)：`0x2A` 是 `NV_ESC_RM_CONTROL`，`0x2B` 是 `NV_ESC_RM_ALLOC`。

### SASS 控制字的解码

上面 [「一个有资格的 warp」](#一个-warp-的有资格是什么意思) 一节里的停顿计数、屏障、yield 位，来自 `ptxas` 塞进每条指令第二个 64-bit 字高位的 21 位控制字段——`cuobjdump -sass` 会把它打在助记符旁边：

```
   20    17 16       11 10   8 7    5 4 3    0
  ┌────────┬───────────┬──────┬──────┬─┬──────┐
  │ reuse  │ wait mask │ read │write │Y│stall │
  │  (4)   │    (6)    │ barr │ barr │ │ (4)  │
  └────────┴───────────┴──────┴──────┴─┴──────┘
```

两个 3 位的索引指明指令 set 的 scoreboard 屏障，6 位的 mask 是它要等的屏障，`Y` 是 yield 位，`stall` 是静态周期数。这个布局没有公开文档，是 microbenchmarking 逆向出来的（最清晰的公开重建资料仍然是 Citadel 那篇 microbenchmarking 论文——[Jia 等人「Dissecting the NVIDIA Volta GPU Architecture via Microbenchmarking」](https://arxiv.org/abs/1804.06826)——以及 Maxwell 上 [maxas 控制码笔记](https://github.com/NervanaSystems/maxas/wiki/Control-Codes)）。

### NVCC 宿主端注册回调

想看编译器具体生成了什么代码来在启动时注册 GPU 代码的话，加 `nvcc --keep` 编译，可以去看 `vadd.cudafe1.stub.c`。

进程一开头的注册是由编译器自动生成的构造函数完成的：

```c
// from vadd.cudafe1.stub.c
static void __sti____cudaRegisterAll(void) __attribute__((__constructor__));

static void __nv_cudaEntityRegisterCallback(void **__T4) {
    __cudaRegisterEntry(__T4, (void(*)(const float*, const float*, float*, int))vadd,
                        _Z4vaddPKfS0_Pfi, -1);
}

static void __sti____cudaRegisterAll(void) {
    __cudaRegisterBinary(__nv_cudaEntityRegisterCallback);
}
```

`__attribute__((__constructor__))` 让链接器在 `main` 启动之前先执行 `__sti____cudaRegisterAll`。它把设备 binary 注册到 CUDA 运行时并排好回调。等回调跑起来，`__cudaRegisterEntry` 把宿主函数指针 `vadd` 跟 mangled 后的设备入口 `_Z4vaddPKfS0_Pfi` 对上号，建成一张哈希表——`cudaLaunchKernel` 在 launch 时就来查这张表。

---

> *原文最后修改于 2026 年 7 月 2 日。*
>
> 作者 Fergus Finn 在 [Doubleword](https://app.doubleword.ai) 做这一类工作。Doubleword 致力于让推理效率发生数量级提升——给 agent、批量、嵌入场景提供大规模、低成本的 token。如果这跟你有关，或者你想一起做，**他们在招人**（[投递链接](https://blog.doubleword.ai/what-happens-when-you-run-a-cuda-kernel)）。
>
> 也可以在 [GitHub](https://github.com/fergusfinn)、[LinkedIn](https://www.linkedin.com/in/fergusfinn/)、[X (Twitter)](https://x.com/finn_fergus)、[Google Scholar](https://scholar.google.com/citations?user=tAp5bZ8AAAAJ&hl=en) 找到作者，或在 [Doubleword 的 About 页](https://www.doubleword.ai/about-doubleword) 了解更多。

---

> **关于转载**
> - 原文标题：*What Happens When You Run a CUDA Kernel*
> - 原文作者：Fergus Finn
> - 原文链接：<https://fergusfinn.com/blog/what-happens-when-you-run-a-gpu-kernel/>
> - 原文发布时间：2026 年 6 月 29 日；最后修改 2026 年 7 月 2 日
> - 原文另址：[Doubleword 博客](https://blog.doubleword.ai/what-happens-when-you-run-a-cuda-kernel)
> - **授权状态**：原站点及其全文未声明任何明示的转载或翻译许可。译者未与原作者取得联系，也未获得任何形式的授权。本文按"未经授权、仅作非商业学习交流用途"处理。如需将译文再行转载、出版或用于其他用途，请**先联系原作者 Fergus Finn**（联系方式见原文页脚及[其个人主页](https://www.doubleword.ai/about-doubleword)）取得明确许可。
> - 译注：本文为非商业学习用途的中文翻译。译文保留原文的全部代码片段（含 CUDA、PTX、SASS、NCU 输出、C 钩子）、ASCII 图表、表格与脚注；非英文引文（封面出处、题图、参考链接等）一并保留原文 URL 以便读者回溯。原文个别 SASS 控制字表头被 GitHub Markdown 渲染错误地写成 `t0e`、`teReuseFlags` 等乱码（位于 [「一个有资格的 warp」](#一个-warp-的有资格是什么意思) 一节），译文按文档惯例恢复为 `reuse / wait mask / read / write / Y / stall`。代码注释与变量名均保留英文以方便与原文对照。如有错漏，欢迎指正。