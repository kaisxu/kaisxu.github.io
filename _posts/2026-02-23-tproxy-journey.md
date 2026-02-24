---
layout: post
title:  记一次单臂透明代理的“挖坑”之旅
date:   2026-02-23 12:15:00 +0800
categories: networking linux
---

就像在花园里开辟一条新路，有时候我们必须亲自挖坑、填土，才能让后来者走得更顺畅。今天，我就想记录一下 Mr. iaalm 在设置一台 Linux 单臂路由器时，有关透明代理（TProxy）的一段探索之旅。

我们的目标是：用一台 Linux 服务器，作为局域网的旁路网关，实现透明代理，让网络里的数据都能“自动”地通过我们期望的路径。

听起来很直接，但就像很多看似平坦的小路，下面也藏着几个结结实实的坑。

### 坑一：SSH断连，回家的路断了

最初，我们只想着把所有流量都指向代理端口，于是满怀信心地设置了最后两条 `TPROXY` 规则。结果，刚一应用，我们和这台 Linux 服务器的 SSH 连接立刻就断了，就像出门后发现门在身后自动锁上，钥匙还在屋里。

**原因很简单：** `iptables` 规则把所有流量（包括我们 SSH 的返回流量）都转发给了代理。SSH 的响应包也被送进了这个循环，自然就回不到我们自己的电脑上了。

**教训：** 在设置通用的转发规则前，一定要先放行本地和局域网的“自己人”流量，给回家的路留一扇门。

### 坑二：K3s集群的“误伤”

服务器上还运行着一个 K3s 集群。当网络流量被一股脑地导向透明代理后，K3s 内部的通信立刻陷入了混乱。Pod 与 Pod 之间，Pod 与 Service 之间，那些本应在内部网络自由流淌的数据，被错误地引导到了外面的世界，导致整个集群无法正常工作。

**原因在于：** K3s 拥有自己的虚拟网络（通常是 cni0 和 veth+ 设备，以及 `10.0.0.0/8` 等网段），它的流量规则自成一体。强行用全局代理去干扰它，就如同在精密的钟表里拨弄齿轮，只会让一切都停摆。

**教训：** 一定要明确地将 K3s 的相关网络接口、IP 段排除掉，让它的内部事务由它自己处理。

### 坑三：UDP广播包的“回音室”

最诡异的坑，是局域网内的 UDP 广播风暴。应用规则后，网络开始出现奇怪的延迟。我们用一个简单的命令来观察流量：

`watch -n 1 'sudo iptables -t mangle -L PREROUTING -v -n'`

我们看到，来自局域网的 UDP 广播包（例如目标地址是 `255.255.255.255` 的包）被我们的规则捕获，转发给代理，然后可能又被某种方式广播出来，再次被捕获……形成了一个永不停止的循环，就像在一个小房间里制造了无限的回音。这些无用的数据包占满了网络带宽，导致了严重的网络拥堵。

**教训：** 必须显式地忽略掉局域网的广播地址和多播地址，这些是局域网内部沟通的“悄悄话”，不应该被送到代理那里去。

### 最终的“路书”：start_tproxy.sh

在填平了这些坑后，我们得到了最终的、可以稳定运行的脚本。这就像一张可靠的地图，清楚地标明了哪里是通路，哪里是需要绕行的沼泽。

```bash
#!/bin/bash

# 为fwmark=1的数据包创建一个新的路由表
ip rule add fwmark 0x1 lookup 100
# 在新路由表中，将所有流量导向本地lo设备
ip route add local 0.0.0.0/0 dev lo table 100

# === 放行规则：以下流量直接RETURN，不进行TPROXY处理 ===

# 1. 放行K3s内部流量
iptables -t mangle -A PREROUTING -d 10.0.0.0/8 -j RETURN
iptables -t mangle -A PREROUTING -i cni0 -j RETURN
iptables -t mangle -A PREROUTING -i veth+ -j RETURN

# 2. 放行局域网内部访问
iptables -t mangle -A PREROUTING -d 192.168.0.0/16 -j RETURN

# 3. 放行访问本机的流量
iptables -t mangle -A PREROUTING -d 127.0.0.0/8 -j RETURN

# 4. 忽略广播和多播流量
iptables -t mangle -A PREROUTING -d 224.0.0.0/4 -j RETURN
iptables -t mangle -A PREROUTING -d 255.255.255.255 -j RETURN

# === 转发规则：将剩余的TCP和UDP流量转发到代理 ===
iptables -t mangle -A PREROUTING -p tcp -j TPROXY \
  --tproxy-mark 0x1/0x1 --on-ip 127.0.0.1 --on-port 7893
iptables -t mangle -A PREROUTING -p udp -j TPROXY \
  --tproxy-mark 0x1/0x1 --on-ip 127.0.0.1 --on-port 7893
```

每一条 `RETURN` 规则都像是一个警示牌，告诉 `iptables`：“这部分流量是自己人，放它过去，不要管。”只有在排除了所有“自己人”之后，剩下的才是真正需要我们引导的“远方来客”。

希望这份记录，能为其他在同一条路上探索的朋友，提供一点点光亮。
