---
layout: post
title: "How Claude Code Guards Its API: Fingerprints, Billing Headers, and a Native Attestation Kill-Switch"
date: 2026-04-02 19:55:00 +0800
categories: tech security
---
*A deep dive into the authentication mechanisms uncovered from Claude Code's leaked source, and what they mean for third-party tooling.*
---
## The Leak
On March 31, 2026, security researcher [Chaofan Shou](<https://x.com/Fried_rice/status/2038894956459290963>) noticed that Anthropic's `@anthropic-ai/claude-code` npm package shipped with a `.map` file pointing to unobfuscated TypeScript sources hosted on Anthropic's R2 storage bucket. The entire `src/` directory — roughly 1,900 files and 512,000 lines of TypeScript — was publicly downloadable.
This post isn't about the leak itself. It's about what the source reveals: a multi-layered authentication system designed to ensure that only genuine Claude Code clients get preferential API access — and a dormant kill-switch that could shut out every third-party proxy overnight.
---
## The Problem Anthropic Needed to Solve
Claude Code is Anthropic's official terminal-based coding assistant. It ships as a standalone binary (a custom Bun fork) and talks to the same `<http://api.anthropic.com|api.anthropic.com>` endpoint as any other API consumer.
But there's a catch: Claude Code users on API billing plans get **dramatically better rate limits** than raw API callers. An API key that can barely sustain one Haiku conversation can power multiple concurrent Opus 4.6 sessions through Claude Code.
How? The API needs to distinguish "this request came from Claude Code" from "this request came from some random script." And it needs to do this without a separate auth endpoint or API key scope — the same `sk-ant-*` key is used everywhere.
Anthropic's solution is a three-layer authentication scheme baked into every API request.
---
## Layer 1: The Attribution Header (System Prompt Injection)
Every Claude Code request includes a special string as the **first text block of the system prompt**:
```
x-anthropic-billing-header: cc_version=2.1.72.fb8; cc_entrypoint=cli;
```
This isn't an HTTP header — it's literally injected into the conversation's system prompt, where the API server can parse it before routing the request.
The `cc_version` field contains the Claude Code version number followed by a three-character **fingerprint**. This fingerprint is computed as:
```javascript
SHA256("59cf53e54c78" + msg[4] + msg[7] + msg[20] + version).slice(0, 3)
```
Where:
- `59cf53e54c78` is a hardcoded salt (must match backend validation)
- `msg[4]`, `msg[7]`, `msg[20]` are characters at indices 4, 7, and 20 of the first user message (or `"0"` if out of bounds)
- `version` is the Claude Code version string
The source code comments explicitly warn: *"Do not change this method without careful coordination with 1P and 3P (Bedrock, Vertex, Azure) APIs."*
**What this achieves:** The server can verify that the request was constructed by software that knows the salt and the algorithm. The fingerprint changes with every message, so you can't just replay a captured header.
**How hard is it to forge?** Trivial — the salt is hardcoded in the source, and the algorithm is five lines of JavaScript. We confirmed this by crafting requests with computed fingerprints and successfully calling Sonnet and Opus models that were otherwise rate-limited.
---
## Layer 2: Beta Headers and the `?beta=true` Endpoint
Claude Code doesn't call `/v1/messages` — it calls `/v1/messages?beta=true` through the SDK's `client.beta.messages.create()` method. Along with this, it sends a specific set of beta headers:
```
anthropic-beta: claude-code-20250219, interleaved-thinking-2025-05-14, prompt-caching-scope-2026-01-05
```
The critical one is `claude-code-20250219`. Without it, even with a valid fingerprint, requests to Sonnet and Opus models return `rate_limit_error`. This header acts as a **routing tag** that tells the API server to apply Claude Code's preferential rate limit tier.
We verified this experimentally:
| Request | Haiku | Sonnet | Opus |
|---------|-------|--------|------|
| Raw API key, no special headers | :white_check_mark: | :x: 429 | :x: 429 |
| + `claude-code-20250219` beta header | :white_check_mark: | :x: 429 | :x: 429 |
| + attribution fingerprint in system prompt | :white_check_mark: | :white_check_mark: | :white_check_mark: |
Both the beta header AND the attribution fingerprint are required. Neither alone is sufficient for Sonnet/Opus access on API billing.
---
## Layer 3: The Dormant Kill-Switch — `cch` Native Attestation
This is where it gets interesting. In the source, there's a feature flag called `NATIVE_CLIENT_ATTESTATION`. When enabled, the attribution header includes an additional field:
```
x-anthropic-billing-header: cc_version=2.1.72.fb8; cc_entrypoint=cli; cch=00000;
```
The source comments explain:
&gt; *When NATIVE_CLIENT_ATTESTATION is enabled, includes a `cch=00000` placeholder. Before the request is sent, Bun's native HTTP stack finds this placeholder in the request body and overwrites the zeros with a computed hash. The server verifies this token to confirm the request came from a real Claude Code client. See `bun-anthropic/src/http/Attestation.zig` for implementation.*
The key insight: Claude Code doesn't ship with stock Bun. It runs on **`bun-anthropic`**, a private fork that includes a Zig module (`Attestation.zig`) compiled into the HTTP layer. This module:
1. Intercepts outgoing HTTP requests at the native level (below JavaScript)
2. Scans the serialized request body for the byte sequence `cch=00000`
3. Computes an HMAC (or similar) over the request body using a key baked into the binary
4. Overwrites the five zeros with the resulting hash — same length, no Content-Length change
5. The server validates this hash to confirm the request came from an unmodified Claude Code binary
### But Does It Actually Work?
We performed binary analysis on the Claude Code v2.1.72 Linux aarch64 binary (221 MB ELF executable) and found:
- **The `cch=00000` placeholder appears 3 times** — all within the embedded JavaScript bundle, none in native `.text` or `.rodata` ELF sections
- **No attestation-related strings** (`"attest"`, `"Attestation"`, `"cch="`) exist in the native ELF sections
- **The native `.text` section is 6.2 MB larger** than stock Bun 1.3.11, but the extra code doesn't contain attestation logic
- **Zero `Attestation.zig` symbols** in the symbol table (stripped or not present)
The conclusion: **`NATIVE_CLIENT_ATTESTATION` is compiled into the JS bundle as a feature flag (set to `true`), but the native Zig code that would actually compute the hash is not present in this build.** The placeholder `cch=00000` is sent verbatim. The server accepts it.
We verified by sending requests with `cch=00000` — they work fine. We also tested sending requests with no `cch` field at all — also works.
### What This Means
The attestation infrastructure exists in the codebase. The TypeScript layer generates the placeholder. The comments reference a Zig implementation. But the actual enforcement — both client-side (hash computation) and server-side (hash validation) — **is not yet active**.
This is a **dormant kill-switch**. When Anthropic decides to flip it:
- The `bun-anthropic` fork will include the real `Attestation.zig` module
- The native HTTP layer will replace `00000` with a real hash
- The server will reject requests without valid hashes
- Every third-party proxy, CLI wrapper, and SDK shim will stop working for Claude Code rate limits
The hash depends on the full request body and a key embedded in the compiled binary. Without the exact binary, you can't compute it. Reverse-engineering would require disassembling the Bun fork's HTTP stack — feasible but non-trivial, and Anthropic could rotate the key with every release.
---
## The Complete Authentication Flow
Putting it all together, here's what a Claude Code API request looks like:
```
POST /v1/messages?beta=true HTTP/1.1
Host: <http://api.anthropic.com|api.anthropic.com>
x-api-key: sk-ant-api03-...
anthropic-version: 2023-06-01
anthropic-beta: claude-code-20250219,interleaved-thinking-2025-05-14,...
x-app: cli
User-Agent: claude-cli/2.1.72 (external, cli)
x-client-request-id: &lt;uuid&gt;
Content-Type: application/json
{
 "model": "claude-opus-4-20250514",
 "max_tokens": 16000,
 "system": [
 {
 "type": "text",
 "text": "x-anthropic-billing-header: cc_version=2.1.72.fb8; cc_entrypoint=cli; cch=00000;"
 },
 {
 "type": "text",
 "text": "You are Claude Code, Anthropic's official CLI for Claude."
 }
 ],
 "messages": [...],
 "stream": true
}
```
The server processes this by:
1. Checking the `anthropic-beta` header for `claude-code-20250219` → routes to Claude Code rate limit tier
2. Parsing the first system prompt block for `x-anthropic-billing-header`
3. Extracting `cc_version` and validating the fingerprint against the message content
4. (Future) Validating the `cch` hash against the request body
5. If all checks pass → apply Claude Code's generous rate limits instead of the default API tier
---
## Implications for Third-Party Tools
### Today
Third-party tools (like [OpenCode](<https://github.com/opencode-ai/opencode>), custom proxies, or SDK wrappers) can currently access Claude Code's rate limit tier by:
1. Computing the fingerprint (trivial — 5 lines of code)
2. Injecting the attribution header as the first system prompt block
3. Sending the `claude-code-20250219` beta header
4. Appending `?beta=true` to the endpoint URL
We built a working proxy that does exactly this in ~170 lines of JavaScript, and verified it works for Haiku, Sonnet, and Opus.
### Tomorrow
When Anthropic activates `cch` enforcement:
- Only the official Claude Code binary (running on `bun-anthropic`) will be able to compute valid hashes
- Third-party tools will fall back to standard API rate limits
- The only workaround would be reverse-engineering the Bun fork's attestation module — which Anthropic can invalidate with each release
This is a well-designed defense-in-depth strategy:
- **Layer 1** (fingerprint) is easy to forge but blocks casual misuse
- **Layer 2** (beta header) is a simple routing tag, easy to add but narrows the surface
- **Layer 3** (native attestation) is the real enforcement mechanism, currently dormant but architecturally complete
---
## Other Interesting Findings
While exploring the authentication code, we also discovered:
- **A Datadog client token** (`pubbbf48e6d78dae54bceaa4acf463299bf`) embedded in the binary for analytics
- **16+ hidden slash commands** for Anthropic employees (gated behind `USER_TYPE=ant`), including `/mock-limits` and `/reset-limits`
- **Auth bypass environment variables** (`CLAUDE_CODE_SKIP_BEDROCK_AUTH`, etc.) for testing
- **An anti-distillation system** that injects fake tools into API requests to prevent model cloning
- **Full telemetry pipeline** sending events to four backends (1P API, Datadog, BigQuery, GrowthBook)
- **An "undercover mode"** that automatically strips internal model codenames when working in public repos
---
## Conclusion
Anthropic has built a layered authentication system that currently relies on security through obscurity (the fingerprint salt and algorithm) plus a routing tag (the beta header). This is sufficient today because the system wasn't designed to be secret — it was designed to be **upgradeable**.
The `cch` native attestation is the endgame. It shifts the trust boundary from "knows the algorithm" to "runs the official binary" — a much harder bar to clear. The infrastructure is in place, the feature flag is set, and the server-side parsing is ready. It's just waiting to be turned on.
For anyone building third-party Claude Code integrations: enjoy the current openness while it lasts, and design your architecture to gracefully fall back to standard API rate limits when the switch flips.
---
*This analysis is based on the publicly exposed Claude Code v2.1.72 source snapshot and binary. All findings are from defensive security research on publicly available artifacts.*