# CLAUDE.md

Guidance for Claude when working in this repository.

## Project

Personal site / digital garden for `kaisxu`, served via GitHub Pages from `main`.

- **Stack:** Jekyll on GitHub Pages (`build_type: legacy`)
- **URL:** https://kaisxu.github.io/
- **Source branch:** `main` — pushing to `main` triggers a Pages build automatically.

## Layout

```
index.md        # homepage — uses Liquid to auto-list all posts
_posts/         # one markdown file per blog post
README.md       # one-line project description
assets/         # static assets
```

## Writing a new blog post

**Do not edit `index.md`.** It already iterates all posts via:

```liquid
{% for post in site.posts %}
  **[{{ post.title }}]({{ post.url }})**
  *{{ post.date | date_to_string }}*
  {{ post.excerpt }}
{% endfor %}
```

So a new file under `_posts/` is enough — it will appear on the homepage on its own.

### Naming

`_posts/YYYY-MM-DD-slug.md` (Jekyll requires `YEAR-MONTH-DAY-` prefix).

### Front matter (copy from any existing post)

```yaml
---
layout: post
title: "Your title here"
date: YYYY-MM-DD HH:MM:SS +0800
categories: tech <topic>
---
```

- Date must be in the past or present — Jekyll/the site may drop future-dated posts.
- `+0800` timezone is the convention used across existing posts.
- Categories are free-form (`tech security`, `tech gpu`, …); pick something that matches the topic.

### Commit message style

Match the existing style — short, sentence-case, declarative:

```
Publish new blog post: <Title>
Add new post about <topic>
Fix blog post date to be in the past
```

Avoid prefixes like `feat:` / `chore:` — the repo's history does not use them.

## Publishing & monitoring

After committing and pushing to `main`, GitHub Pages kicks off a `pages build and deployment` workflow.

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

A successful run is typically `success` in well under a minute. Always verify by fetching the rendered post URL (it will live at `https://kaisxu.github.io/<categories>/<YYYY>/<MM>/<DD>/<slug>.html`) — the workflow succeeding does not guarantee the post is publicly visible.

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