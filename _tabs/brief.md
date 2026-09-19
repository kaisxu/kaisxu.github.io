---
layout: page
title: 简报
icon: fas fa-newspaper
order: 2
---

<!--
  Index for the `brief` collection (_brief/*.md).

  Tag filtering is client-side on purpose. jekyll-archives only ever walks
  `site.posts`, so it builds no /tags/ page for a brief-only tag; generating
  real pages for a daily cadence would also mean hundreds of extra archive
  pages a year. One page that filters in place suits a stream better.

  Inline JS below avoids `//` comments and relies on semicolons — Chirpy
  renders through the `compress` layout, which collapses whitespace outside
  <pre>.
-->

{% assign briefs = site.brief | sort: 'date' | reverse %}

{% if briefs.size == 0 %}

还没有简报条目。生成器往 `_brief/` 放入第一个文件后，这里就会自动出现。

{% else %}

{% assign all_tags = '' | split: '' %}
{% for entry in briefs %}
  {% for tag in entry.tags %}
    {% assign all_tags = all_tags | push: tag %}
  {% endfor %}
{% endfor %}
{% assign all_tags = all_tags | uniq | sort_natural %}

<p class="text-muted small mb-3">
  <i class="fas fa-bookmark fa-fw me-1"></i>
  想收藏？用
  <a href="{{ '/brief/latest/' | relative_url }}"><code>/brief/latest/</code></a>
  —— 这个地址永远指向最新一期。
</p>

<div id="brief-filter" class="d-flex flex-wrap align-items-center mb-4">
  <a class="post-tag btn btn-outline-primary active" href="#" data-tag="">
    全部 <span class="text-muted">{{ briefs.size }}</span>
  </a>
  {% for tag in all_tags %}
    {% assign hits = briefs | where_exp: 'd', 'd.tags contains tag' %}
    <a class="post-tag btn btn-outline-primary" href="#{{ tag | slugify }}" data-tag="{{ tag | slugify }}">
      {{ tag }} <span class="text-muted">{{ hits.size }}</span>
    </a>
  {% endfor %}
</div>

<p id="brief-empty" class="text-muted d-none">没有符合该标签的条目。</p>

<div id="brief-list">
  {% assign current_month = '' %}
  {% for entry in briefs %}
    {% assign month = entry.date | date: '%Y-%m' %}
    {% if month != current_month %}
      {% unless forloop.first %}</ul>{% endunless %}
      <h2 class="brief-month" data-month="{{ month }}">{{ entry.date | date: '%Y 年 %-m 月' }}</h2>
      <ul class="brief-entries ps-0" style="list-style: none;">
      {% assign current_month = month %}
    {% endif %}

    {% capture tag_slugs %}{% for tag in entry.tags %}{{ tag | slugify }}{% unless forloop.last %}|{% endunless %}{% endfor %}{% endcapture %}
    {%- comment -%}
      Resolve the title with if/else, not `default:`. Chaining
      `entry.title | default: entry.date | date: '%Y-%m-%d'` would apply the
      date filter to the *title* whenever one exists, and Ruby's Time.parse
      is lenient enough to turn "2026-09-18 简报" into "2026-09-18".
    {%- endcomment -%}
    {% if entry.title %}
      {% assign entry_title = entry.title %}
    {% else %}
      {% assign entry_title = entry.date | date: '%Y-%m-%d' %}
    {% endif %}
    <li class="brief-entry mb-2" data-tags="{{ tag_slugs }}">
      <a href="{{ entry.url | relative_url }}">
        <span class="text-muted">{{ entry.date | date: '%m-%d' }}</span>
        {{ entry_title }}
      </a>
      {% if entry.tags.size > 0 %}
        <span class="brief-entry-tags text-muted small">
          {% for tag in entry.tags %}{{ tag }}{% unless forloop.last %} · {% endunless %}{% endfor %}
        </span>
      {% endif %}
    </li>

    {% if forloop.last %}</ul>{% endif %}
  {% endfor %}
</div>

<script>
  (function () {
    var list = document.getElementById('brief-list');
    if (!list) { return; }
    var chips = Array.prototype.slice.call(
      document.querySelectorAll('#brief-filter [data-tag]')
    );
    var entries = Array.prototype.slice.call(list.querySelectorAll('.brief-entry'));
    var months = Array.prototype.slice.call(list.querySelectorAll('.brief-month'));
    var empty = document.getElementById('brief-empty');

    function apply(tag) {
      var shown = 0;
      entries.forEach(function (li) {
        var tags = (li.getAttribute('data-tags') || '').split('|');
        var hit = !tag || tags.indexOf(tag) !== -1;
        li.classList.toggle('d-none', !hit);
        if (hit) { shown += 1; }
      });

      /* Hide a month heading when every entry under it is filtered out. */
      months.forEach(function (h) {
        var ul = h.nextElementSibling;
        if (!ul) { return; }
        var visible = ul.querySelectorAll('.brief-entry:not(.d-none)').length;
        h.classList.toggle('d-none', visible === 0);
        ul.classList.toggle('d-none', visible === 0);
      });

      chips.forEach(function (chip) {
        chip.classList.toggle('active', chip.getAttribute('data-tag') === tag);
      });

      if (empty) { empty.classList.toggle('d-none', shown !== 0); }
    }

    chips.forEach(function (chip) {
      chip.addEventListener('click', function (event) {
        event.preventDefault();
        var tag = chip.getAttribute('data-tag');
        /* Keep the hash shareable and the back button meaningful. */
        history.replaceState(null, '', tag ? '#' + tag : location.pathname);
        apply(tag);
      });
    });

    function fromHash() {
      apply(decodeURIComponent((location.hash || '').replace(/^#/, '')));
    }

    window.addEventListener('hashchange', fromHash);
    fromHash();
  })();
</script>

{% endif %}
