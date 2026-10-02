# AGENTS.md

Guidance for coding agents working in this repository.

`CLAUDE.md` is a symlink to this file — one document, two names, so tools
looking for either find the same content. Edit `AGENTS.md`.

## Project

Personal site / digital garden for `kaisxu`, served via GitHub Pages from `main`.

- **Stack:** Jekyll on GitHub Pages, built via **GitHub Actions** (`cotes2020/jekyll-theme-chirpy` remote theme)
- **URL:** https://kaisxu.github.io/
- **Source branch:** `main` — pushing to `main` triggers `.github/workflows/jekyll.yml`.
- **Pages build source:** GitHub Actions (NOT legacy branch deploy). Must be set in repo Settings → Pages.

## Layout

```
_config.yml          # Chirpy config + plugin list + polyglot settings
Gemfile              # bundle install — pins jekyll + Chirpy runtime gems
.github/workflows/   # Jekyll build & deploy workflow
index.html           # homepage — `layout: home`, picks up posts via Chirpy
_posts/<lang>/       # blog posts, one directory per language
_brief/<lang>/       # daily digest entries — see _brief/AGENTS.md before writing one
_includes/           # local overrides of Chirpy includes — see each file's header
tools/               # repo scripts (not published; excluded in _config.yml)
README.md            # one-line project description
assets/              # static assets (images, local theme SCSS override)
```

## Writing a new blog post

A new file under `_posts/<lang>/` is enough — Chirpy's `home` layout iterates
all posts automatically. Don't edit `index.html`.

**Write in Chinese by default**, i.e. `_posts/zh-CN/`. Chinese is the site's
default language and is served without a url prefix. Translation is a separate,
optional, after-the-fact step — see § 多语言 below. Never hold a post back
because its translation isn't ready.

### Naming

`_posts/<lang>/YYYY-MM-DD-slug.md` (Jekyll requires the `YEAR-MONTH-DAY-` prefix).

The slug must be **identical across languages** — that is how polyglot pairs
translations. `_posts/zh-CN/2026-01-01-foo.md` and `_posts/en/2026-01-01-foo.md`
are the same post in two languages; they produce the same url and each falls
back to the other when missing.

Subdirectories under `_posts/` do not become categories and do not appear in
urls — that is a Jekyll rule, which is exactly why the language directory is
free of consequences.

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

## 多语言

The site is bilingual via [jekyll-polyglot](https://github.com/untra/polyglot).
Chinese (`zh-CN`) is the default language and carries **no url prefix**;
English is served under `/en/`. Every url that existed before polyglot was
added still resolves unchanged.

### The one rule

> **A post has exactly one source language — the directory it lives in.**
> A translation is a generated artifact. Never hand-edit a translation.
{: .prompt-warning }

Most posts are written in Chinese, so `_posts/zh-CN/` is the source and
`_posts/en/` is the artifact. Two posts were drafted in English
(`the-gardeners-error`, `how-claude-code-guards-its-api`) and for those the
direction is reversed — the English file is the source. `tools/i18n-status.py`
works this out per document; it does not assume Chinese.

If a translation reads badly, fix the **source** or fix the translating
prompt, then regenerate. Editing the artifact directly means the next
regeneration silently discards the fix, and the two files drift apart with no
record of which is right.

### Nothing blocks on translation

A document that exists in only one language still resolves in the other:
polyglot serves the source-language content as a fallback rather than 404ing.
So an untranslated post is **not a broken page** and **not a pending task** —
it is a page that happens to be in one language. Publish Chinese, push, done.
Translate whenever, or never.

### Which languages a document is translated into

Two different mechanisms, easy to confuse:

| Key | Read by | Effect |
|---|---|---|
| `translate: false` | `tools/i18n-status.py` only | Build unaffected. The page still exists in every language, falling back to the source. Just stops the script reporting it. |
| `lang-exclusive: ['zh-CN']` | polyglot itself | The page is **not generated** for other languages — `/en/<url>` returns 404. |

**Default to `translate: false`.** It is a note to the tooling, not a change
to the site. `lang-exclusive` is for content that genuinely must not exist in
a language, and nothing in this repo currently uses it; reach for it only
after deciding a 404 is the correct outcome.

Both are set per-directory in `_config.yml`'s `defaults:` block, so documents
need no front matter of their own. One post is currently marked
`translate: false`: `what-happens-when-you-run-a-gpu-kernel` is a Chinese
translation of an English original, and back-translating it would manufacture
a degraded copy of someone else's article.

### Finding what needs translating

```bash
tools/i18n-status.py            # posts only — the default view
tools/i18n-status.py --all      # include _brief/ (~1 new entry per day)
tools/i18n-status.py --skipped  # also show translate: false documents
tools/i18n-status.py --json     # machine-readable, for a translating agent
```

It reports two states. **缺失** is the obvious one. **过期** is the one that
matters: both files exist, but the source has a newer commit than the
translation — i.e. the translation is serving outdated content. A plain
file-existence check never catches that.

`_brief/` is hidden by default on purpose. The digest is daily, and letting
~30 entries a month into the report would bury the handful of posts actually
worth translating. They are still translatable; they just don't nag.

### Where the local overrides are

Polyglot knows nothing about Chirpy and vice versa, so a few theme files are
overridden locally. Each carries a header explaining the diff. **When bumping
the chirpy gem, re-diff all of them:**

- `_includes/lang.html` — makes UI chrome follow `site.active_lang`. Without
  it, the English site renders its sidebar and dates in Chinese.
- `_includes/sidebar.html` — hosts the language switcher (plus the older
  `/brief/latest/` tweak).
- `_includes/lang-selector.html` — new, not a theme file. Note every href is
  wrapped in a `static_href` block: polyglot rewrites relative links to keep
  visitors inside the current language, which is precisely wrong for the
  control whose job is to leave it. (Don't write that tag literally inside a
  Liquid comment — see the warning in the file itself.)
- `assets/css/jekyll-theme-chirpy.scss` — the theme's stylesheet entry point,
  recreated so the switcher's styles can be appended. The two `@use` rules
  must stay byte-identical to the theme's.

### Gotchas

- **Keep `exclude:` entries literal.** Polyglot compiles that list into a
  regex. Globs like `"*.gem"` used to raise `RegexpError` or hang the build
  (untra/polyglot#204, #260). Fixed in 1.11.0, but literals sidestep the whole
  class of bug.
- `parallel_localization: false` is deliberate — polyglot's fork-per-language
  mode has an open race that can render a page in the wrong language
  (untra/polyglot#190). With two languages the parallel win is nil.
- `sass.sourcemap: never` is required by Jekyll 4 + polyglot.
- Local preview needs Docker (no Ruby on this machine):
  ```bash
  docker run --rm -v "$PWD":/srv/jekyll -w /srv/jekyll ruby:3.2 \
    bash -c "bundle install && bundle exec jekyll build"
  ```

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