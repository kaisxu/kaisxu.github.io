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

A successful Actions run is typically `success` in 1–3 minutes (it has to install Ruby gems from scratch each time). Posts live at `https://kaisxu.github.io/posts/<slug>/` (Chirpy permalink). Always fetch the rendered URL — the workflow succeeding does not guarantee the post is publicly visible.

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