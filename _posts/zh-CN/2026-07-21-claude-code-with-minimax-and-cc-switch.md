---
layout: post
title: "用 Claude Code 跑 MiniMax 模型：踩坑、配置与思考"
date: 2026-07-21 09:30:00 +0800
categories: [tech, claude-code]
---

过去这一周，我把 Claude Code 接到了 [MiniMax](https://api.minimaxi.com) 上，整体跑通之后回头复盘，发现其中藏着的"坑"比看上去多。本文把这套当前在用的设置和当初踩过的几个关键问题都写下来，留给以后回看。

## 背景

我平时写代码、跑 agent 的主力机是一台海外服务器（Linux），身边没有什么顺手的图形界面，几乎所有事都得在终端里办。这就给"用 Claude Code"这件事定下了几个硬约束：

- 不能用 GUI 类的配置工具（开 VNC 那种方案太重）；
- 走国内访问受限的 Anthropic 官方 API 不可行，必须换供应商；
- 偶尔还得让模型帮我搜点东西、读点网页，所以 WebSearch / WebFetch 必须能 work。

在这三个前提之下，可走的路其实只有一条：用 **cc-switch（或它的 CLI）** 把 Claude Code 的请求路由到 **MiniMax（MiniMax）** 的 Anthropic 兼容 API 上。这一步说起来简单，做起来一路红灯。下面分四个坑讲。

---

## 坑一：cc-switch 在服务器上怎么装

[cc-switch](https://github.com/farion1231/cc-switch)（star 2 万多，作者 farion1231）本质上是个 GUI 工具——Tauri + 系统 WebView 写的跨平台桌面应用，主打"一键切换 Claude Code / Codex / Gemini CLI 的 API 端点和 key"。在 Windows 和 macOS 上很顺，但在 headless 的 Linux 服务器上直接装是会炸的，最常见的报错：

```
Failed to initialize gtk backend!
```

或者：

```
cc-switch : Depends: libwebkit2gtk-4.1-0 but it is not installed
```

桌面环境没有、GUI 库也没装，强行 `apt install cc-switch` 然后启动就是一片空白。我现在的解法是直接用 cc-switch 提供的 **CLI**——它在仓库里和 GUI 是同一套二进制，只不过在服务器上不启动图形界面，只跑它的命令面板。常用的子命令大致是这几个形态：

```bash
# 列出当前已经配好的 provider（也就是一个个 provider profile）
cc-switch list

# 加一个 MiniMax（MiniMax）的 profile，名字随便起，后面会用到
cc-switch add minimax \
  --base-url https://api.minimaxi.com/anthropic \
  --model MiniMax-M3 \
  --haiku-model MiniMax-M2.7 \
  --sonnet-model MiniMax-M3 \
  --opus-model MiniMax-M3

# 把当前 shell 的环境切到 minimax 上（这一步会写出 ANTHROPIC_BASE_URL 等 env）
cc-switch use minimax
```

`cc-switch use` 这个动作的实质是改 `~/.claude/settings.json`（或者按它的说法，叫"同步到 Claude Code 的 settings"）。改完之后跑 `claude` 命令，Claude Code 自己读这个文件，看到 `ANTHROPIC_BASE_URL` 不是官方 `api.anthropic.com`、不是 `bedrock`/`vertex` 这种 1P 路由，就会把请求打到我指定的 MiniMax 上。

> 这里为什么是改 settings 而不是 export 环境变量？因为 Claude Code 读 `~/.claude/settings.json` 的优先级比 shell env 要高一些，且跨 shell 会话保留，server 上重启 shell 不会丢。

## 坑二（其实不算坑）：代理

既然走国内供应商 https://api.minimaxi.com/ ，反向墙也就几乎不存在——无论我在国内 vps 还是海外节点，都能稳定打到。这一点一带而过，懂的人都懂。

唯一相关的：很多人习惯性地会给终端也设上 `HTTPS_PROXY=https://...`，这反而会让 Claude Code 把请求先扔到自己的代理、再绕一圈回国内，徒增延迟。`cc-switch` 切到 MiniMax 之后，把所有 `*_PROXY` 环境变量 unset 掉最干脆。

---

## 坑三：WebSearch 为什么"用不了"，以及两种解法

这个坑比想象的深，要先讲机制再讲解法。

Claude Code 里的 `/search` 和 agent 模式下模型自己决定的 WebSearch 调用，**并不是经过 `ANTHROPIC_BASE_URL` 路由到我设的 MiniMax 的**。它走的是 Claude Code SDK 里硬编码的一条独立路径：

```
WebSearchTool  →  https://api.anthropic.com/v1/messages   （带 anthropic-version 头）
                                  ↓
              官方服务端识别到 WebSearch 工具被启用后，
              自己跑搜索、把结果拼回 messages 流
```

换句话说，WebSearch 是 Anthropic 服务端自己"包"起来的能力——模型只发一个 tool_use，服务端看到这个工具就内置处理掉，根本不会下到我设的 MiniMax 那里。MiniMax 收到的请求体里可能根本不出现 WebSearch 这个工具，因为 Claude Code 的 SDK 在生成请求时就把这条管线排除掉了。

那么 MiniMax 这种**兼容方**要想"也支持 WebSearch"，就得自己实现一整套：要么自己跑搜索引擎，要么调第三方搜索 API，然后把这个能力挂到自己的 Anthropic 兼容层上。截至目前 MiniMax 没做——所以你在 Claude Code 里 `/search`，会直接报 `tool not available` 之类。

### 解法一：让 MiniMax 自己接好

如果哪天 MiniMax 上线了 WebSearch（很多类似供应商都在排队做这个），最省事。但现在 2026/07 这篇文章时点上还没。

### 解法二：自己挂一个搜索工具

更现实的做法，是**自己写一个 MCP server**，把搜索能力塞进 Claude Code 的工具表里，从而绕开它对官方 WebSearch 的依赖。Claude Code 支持 MCP 工具，所以一个极简的 ddg 搜索 server 长这样：

```python
# ddg_search_server.py
from ddgs import DDGS            # https://github.com/deedy5/ddgs
from mcp.server import Server

app = Server("ddg-search")

@app.tool()
def web_search(query: str, max_results: int = 5) -> str:
    """DuckDuckGo text search; returns a short result list."""
    with DDGS() as d:
        results = list(d.text(query, max_results=max_results))
    return "\n".join(
        f"- {r['title']} — {r['href']}\n  {r['body']}" for r in results
    )

if __name__ == "__main__":
    app.run()
```

然后在 `~/.claude/settings.json` 里挂上：

```json
{
  "mcpServers": {
    "ddg": {
      "command": "python",
      "args": ["/home/simon/.claude/mcp/ddg_search_server.py"]
    }
  }
}
```

之后跟 Claude Code 说"用 ddg 搜一下 xxx"，它会调 `web_search` 而不是 `/search`，完全绕开官方 WebSearch 通道。

> 顺带一提：本仓库的 `skills/websearch` 走的也是这个路线——`ddgr` + 自定义包装，没用 Claude 官方的搜索。

## 坑四：WebFetch 必须关掉官方预检

WebSearch 之外还有个看起来相似其实完全不同的坑：**WebFetch**。

Claude Code 在抓取一个 URL 之前，会先发一个 **preflight 请求**去 `api.anthropic.com/v1/messages`（是的，又回到官方站点了），目的是让官方服务判断"这个 URL 我能不能替你抓、要不要脱敏"。这个 preflight 是硬编码的，不看 `ANTHROPIC_BASE_URL`。

但 MiniMax 当然没接 Anthropic 官方那一套脱敏/抓取服务，于是 preflight 直接返回非 200，WebFetch 工具就废了——你让它读任何页面，它要么 `403`、`preflight failed`，要么返回一堆空 body。

解法也是关掉这个预检。这就是为什么当前我的 `~/.claude/settings.json` 顶部有这个字段：

```json
{
  "skipWebFetchPreflight": true,
  ...
}
```

打开之后，Claude Code 跳过去官方那一步，直接用 SDK 自带的 fetcher 拉 URL。代价是丢掉官方那一层 URL 脱敏（所谓 "isolated session"），但对一个本地 agent 来说不痛不痒。

> 注意 `skipWebFetchPreflight` 只是关掉 WebFetch 这一项。如果你看到 Claude Code 仍然报奇怪的 preflight error，请确认你的版本够新——这个开关是在最近的版本里加的，老版本要么没这个键，要么得通过 `CLAUDE_CODE_SKIP_FETCH_PREFLIGHT=1` 环境变量去开。

---

## 把四个坑串起来：最终的配置长什么样

`~/.claude/settings.json` 长这样（敏感字段已脱敏）：

```json
{
  "skipWebFetchPreflight": true,
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "sk-cp-...你的 MiniMax key...",
    "ANTHROPIC_BASE_URL":   "https://api.minimaxi.com/anthropic",
    "ANTHROPIC_MODEL":                "MiniMax-M3",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL":  "MiniMax-M2.7",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "MiniMax-M3",
    "ANTHROPIC_DEFAULT_OPUS_MODEL":   "MiniMax-M3"
  },
  "mcpServers": {
    "ddg": {
      "command": "python",
      "args": ["/home/simon/.claude/mcp/ddg_search_server.py"]
    }
  },
  "attribution": { "commit": "", "pr": "" }
}
```

对应到工具：

| 能力        | Claude Code 原生路径               | 我的接法                                 |
|-------------|------------------------------------|------------------------------------------|
| 主对话      | `ANTHROPIC_BASE_URL`               | MiniMax M3（默认）/M2.7（Haiku 子任务） |
| WebSearch   | 官方硬编码 `api.anthropic.com`     | 自建 MCP `ddg` 工具                      |
| WebFetch    | 官方 preflight + SDK fetcher       | `skipWebFetchPreflight: true` + 直拉     |
| 切供应商    | GUI（cc-switch）                   | cc-switch CLI：`cc-switch use minimax`   |

---

## 为什么这两个"三方坑"是结构性的

最后讲一句稍微抽象的话。

Claude Code 把 WebSearch、WebFetch、attribution fingerprint、`anthropic-beta: claude-code-...` 这一大堆东西都跟 `api.anthropic.com` 这个域名绑死了——其中甚至**有些路径根本不接受 `ANTHROPIC_BASE_URL` 重写**（preflight、native attestation 都是）。这意味着任何走"兼容 Anthropic 协议"的供应商，在协议层上是 80% 像、20% 缺；缺的那 20% 恰恰是 Claude Code 体验里最亮眼的那几样。

所以对供应商来说，"兼容 Anthropic"从来不是一个 API 形状对齐就完事的工程——它得自己重新实现 WebSearch 自己重新实现 WebFetch 自己重新实现 thinking 自己重新实现 tool_use 路由，每少实现一项，Claude Code 上就少一个工具能用。

而对用户来说，结论很简单：**用三方兼容供应商之前，先确认它实现了 WebSearch 和 WebFetch；没实现的话，要么自己用 MCP 补，要么接受"这个 agent 不能上网"这件事。** 把这两个坑绕过去，再谈别的优化才有意义。

---

## 参考

- [cc-switch 仓库 (farion1231)](https://github.com/farion1231/cc-switch)
- [MiniMax（MiniMax） Anthropic 兼容 API](https://api.minimaxi.com)
- [Claude Code 源码泄露分析（含 attribution / preflight 细节）](https://kaisxu.github.io/tech/security/2026/04/02/how-claude-code-guards-its-api.html)
- [本仓库 `skills/websearch`](https://github.com/kaisxu/kaisxu.github.io) — `ddgr` 包装的搜索 skill
