# Security policy

Do not open a public issue containing credentials, cookies, personal source data
or operational logs. Use GitHub private vulnerability reporting if enabled; if it
is unavailable, ask the maintainer for a private reporting channel without posting
the sensitive material. No public reporting email is specified yet.

This is a pet-project, not a hosted service. Only the current public snapshot is
intended for review; no security response time or production SLA is promised.

## Operator checklist

- Keep `.env`, DPAPI stores, SQLite, browser sessions, caches and logs out of Git.
- Use least-privilege platform credentials and a separate development account.
- Keep Ollama local; do not expose an unauthenticated endpoint publicly.
- Restrict source URLs to reviewed HTTPS endpoints and treat SOURCE text as hostile.
- Review dependency advisories and install packages only from trusted registries.
- Keep dry-run enabled until explicit deployment review; never retry ambiguous sends.
- Before sharing an export, run secret/privacy scanners on both files and history.
- If a credential was exposed, revoke and reissue it even after removing the content.

Source-consistency heuristics do not independently establish factual truth. Model
outputs and browser-derived content remain untrusted. The public sample excludes
operator credentials, archived chat exports, production databases and media.
