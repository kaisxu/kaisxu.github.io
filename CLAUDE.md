# CLAUDE.md

Guidance for Claude when working in this repository.

## Project

Personal site / digital garden for `kaisxu`, served via GitHub Pages from `main`.

- **Stack:** Jekyll on GitHub Pages, built via **GitHub Actions** (`cotes2020/jekyll-theme-chirpy` remote theme)
- **URL:** https://kaisxu.github.io/
- **Source branch:** `main` — pushing to `main` triggers `.github/workflows/jekyll.yml`.
- **Pages build source:** GitHub Actions (NOT legacy branch deploy). Must be set in repo Settings → Pages.

## Layout

```
_config.yml          # Chirpy config + plugin list
Gemfile              # bundle install — pins jekyll + Chirpy runtime gems
.github/workflows/   # Jekyll build & deploy workflow
index.html           # homepage — `layout: home`, picks up posts via Chirpy
_posts/              # one markdown file per blog post
README.md            # one-line project description
assets/              # static assets (images, etc.) — kept for future use
```

## Writing a new blog post

A new file under `_posts/` is enough — Chirpy's `home` layout iterates all posts automatically. Don't edit `index.html`.

### Naming

`_posts/YYYY-MM-DD-slug.md` (Jekyll requires `YEAR-MONTH-DAY-` prefix).

### Front matter (copy from any existing post)

```yaml
---
layout: post
title: "Your title here"
date: YYYY-MM-DD HH:MM:SS +0800
categories: [tech, topic]
---
```

- Date must be in the past or present — Jekyll/the site may drop future-dated posts.
- `+0800` timezone is the convention used across existing posts.
- **Use YAML list form for categories** (`[tech, gpu]`) — Chirpy treats this as hierarchical and generates `/categories/tech/gpu/` archive pages. The legacy space-separated form (`tech gpu`) would be parsed as a single category "tech gpu".

### Commit message style

Match the existing style — short, sentence-case, declarative:

```
Publish new blog post: <Title>
Add new post about <topic>
Fix blog post date to be in the past
```

Avoid prefixes like `feat:` / `chore:` — the repo's history does not use them.

### Math

- Kramdown's mathjax engine only recognizes `$$ … $$` (display on its own
  block, inline on the same line). It does **not** recognize `\( … \)`,
  `\[ … \]`, or `$ … $` — those come through as literal text/escapes.
- For MathJax, set `math: true` in the post front matter. Chirpy's
  `js-selector.html` then auto-injects the MathJax config + runtime.
  Do **not** add a custom MathJax loader, and do **not** set
  `kramdown.math_engine: mathjax` (it's already the default; being
  explicit invites future readers to think it changes parsing).

### Code blocks

`kramdown.syntax_highlighter_opts` applies to both block (fenced) **and**
span (inline backticks). Top-level `line_numbers: true` makes Rouge wrap
every inline `` `foo` `` in a Rouge `<table>` inside `<code>` inside
`<p>` — invalid HTML, and the chip grows into a giant gray box on the
page. Keep block-only options under `:block:`, leave the top level (or
`:span:`) bare. See existing `_config.yml` for the working form.

### Diagrams

Fenced `` ```mermaid `` blocks only render if the post's front matter sets
`mermaid: true` — Chirpy's `js-selector.html` gates the runtime on it.
Without the flag the block silently renders as a plain code listing.

### Chirpy prompt boxes

A callout is a **single-level** blockquote followed by the class:

```markdown
> Text here.
{: .prompt-tip }
```

Valid classes: `prompt-info`, `prompt-tip`, `prompt-warning`,
`prompt-danger`. Writing `> >` nests a second blockquote — the class
lands on the outer one, so the box renders empty with an ordinary quote
inside it. Multi-paragraph callouts use repeated single `>` lines, not
nesting.

## Publishing & monitoring

After committing and pushing to `main`, `.github/workflows/jekyll.yml` kicks off a build + deploy.

Useful commands:

```bash
# Confirm remote received the commit
git push origin main

# List recent workflow runs (headSha should match your new commit)
gh run list --repo kaisxu/kaisxu.github.io --limit 5 \
  --json name,status,conclusion,createdAt,headSha,databaseId

# Wait, then poll a specific run
gh run view <databaseId> --repo kaisxu/kaisxu.github.io \
  --json status,conclusion,createdAt,updatedAt,url

# Site-level status (separate from the workflow run)
gh api repos/kaisxu/kaisxu.github.io/pages \
  --jq '{status, html_url, source:.source.branch}'

# Fetch the live page to confirm the post actually rendered
gh api repos/kaisxu/kaisxu.github.io/pages/builds/latest \
  --jq '{status, commit:.commit.sha[0:7]}'
```

A successful Actions run is typically `success` in 1–3 minutes (it has to install Ruby gems from scratch each time).

### Post URLs

`_config.yml` sets no `permalink`, so posts use **Jekyll's default**, not Chirpy's `/posts/:title/`:

```
https://kaisxu.github.io/<category>/<subcategory>/YYYY/MM/DD/<slug>.html
```

e.g. `categories: [tech, reverse-engineering]` + `2026-08-30-itoo-sc808-…` →
`https://kaisxu.github.io/tech/reverse-engineering/2026/08/30/itoo-sc808-orphaned-smart-home-hub.html`

`/posts/<slug>/` returns 404. Don't "fix" this by adding `permalink: /posts/:title/` without asking — it would break every existing post's URL.

Easiest way to get the real URL after a deploy:

```bash
curl -s https://kaisxu.github.io/sitemap.xml | grep -o '<loc>[^<]*</loc>'
```

Always fetch the rendered URL — the workflow succeeding does not guarantee the post is publicly visible. In particular, a **future-dated post builds green but 404s**, because Jekyll drops it (no `future: true` in `_config.yml`). Check `date` against the current clock, not just the filename.

## Translations & reposts (转载)

When translating or reposting someone else's article:

1. **Source attribution is mandatory.** Include, near the top of the post, a blockquote-style note with:
   - Original title
   - Original author
   - Original URL
   - Original publication date
2. **Add a closing "关于转载" section** at the bottom restating the same info, so the attribution survives excerpting.
3. Preserve all code blocks, technical details, and diagrams verbatim. Only add translator notes when strictly needed for clarity.
4. **Do not claim authorship** — write in a translator's voice (e.g. "下面这段 CUDA 程序..." not "I wrote this kernel...").
5. If the original has an explicit license/permission note, mirror it; if not, assume "translation for non-commercial learning use" and surface that assumption to the user before publishing.