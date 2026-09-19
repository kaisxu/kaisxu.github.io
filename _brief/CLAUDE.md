# _brief/ — 每日简报

本目录存放每日简报条目，由生成程序自动写入。**每天一个文件**。

本目录与 `_posts/` 完全独立：简报不属于 `site.posts`，因此不会出现在首页时间线、`/archives/`、`/categories/`、站内搜索和主 RSS 中。

导航方式：左侧边栏的「简报」**始终指向最新一期**，每篇简报底部有「查看全部简报」链接通往列表页 <https://kaisxu.github.io/brief/>。首页不设简报入口。

固定书签地址：

| URL | 指向 |
|---|---|
| `/brief/latest/` | **永远是最新一期**，适合存书签 |
| `/brief/` | 全部条目列表（带标签筛选） |
| `/brief/YYYY-MM-DD/` | 某一期的固定链接 |

`/brief/latest/` 由根目录的 `brief-latest.html` 在**构建期**解析目标、客户端跳转实现（GitHub Pages 是纯静态，没有服务端 301）。每次新增简报触发重建时目标会自动更新，无需手工维护。

## 发布流程

1. 在本目录新建 `YYYY-MM-DD.md`
2. 按下面的格式写入内容
3. `git add _brief/ && git commit && git push origin main`

推送后 GitHub Actions 自动构建部署，1–3 分钟生效。不需要修改任何其他文件——列表页会自动收录，标签筛选器会自动出现新标签。

## 文件格式

文件名：`_brief/YYYY-MM-DD.md`（对应 URL `/brief/YYYY-MM-DD/`）

```markdown
---
title: "2026-09-19 简报"
date: 2026-09-19 08:00:00 +0800
tags: [AI, 芯片, 网络]
---

## [标题一](https://example.com/article-1)

一到三句话的摘要，说明这条新闻讲了什么、为什么值得关注。

## [标题二](https://example.com/article-2)

同上。
```

### 字段说明

| 字段 | 必填 | 说明 |
|---|---|---|
| `title` | 建议填 | 省略时列表和详情页回退显示日期 |
| `date` | **必填** | 见下方警告 |
| `tags` | 可选 | YAML 列表形式，用于列表页筛选 |

不需要写 `layout`，`_config.yml` 的 `defaults` 已自动套用 `layout: brief`。
不需要写 `categories`——简报不进分类归档。

## 三个必须注意的点

> **`date` 必须显式写在 front matter 里，且带 `+0800`。**
> Jekyll 只对 `_posts/` 从文件名推断日期。collection 文档缺少 `date` 时会回退到**构建时间**，导致列表排序错乱。
{: .prompt-warning }

> **日期不能是未来。**
> `_config.yml` 没有开 `future: true`，Jekyll 的 publisher 对所有带 `date` 的文档生效（不限于 posts）。未来日期的条目会被静默丢弃——构建照样成功，但页面 404。写入前请核对当前时钟。
{: .prompt-warning }

> **标签直接复用，不要造同义词。**
> 筛选器按标签精确匹配。`AI` 和 `人工智能` 会变成两个互不相干的标签。新增标签前先看一眼 `/brief/` 页面已有哪些。
{: .prompt-tip }

## 格式约定

- 摘要用 Markdown 即可，标准语法都支持。
- 每条目建议用 `## [标题](URL)` 形式，便于扫读。
- **务必保留来源链接**——简报的价值在于可追溯。
- 数学公式、Mermaid 图表在简报里**不可用**（`layout: brief` 未加载对应运行时）。需要这些请写成正式文章放 `_posts/`。

## 本目录的文件

`CLAUDE.md`（本文件）已在 `_config.yml` 的 `exclude` 中列出，不会被发布，也不会被当作简报条目。**除此之外，本目录内的所有 `.md` 文件都会成为线上页面**——草稿、临时文件请勿留在这里。
