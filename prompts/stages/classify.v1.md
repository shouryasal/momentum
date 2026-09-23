<!-- prompts/stages/classify.v1.md — TIER 1. Rendered by runs/ingest.py:classify_news.
Placeholders: {{EVENT_CLASSES}} {{ASSETS}} {{ITEMS}} (JSON). The classifier NEVER
escalates: any failure leaves the deterministic keyword labels standing. -->

Label each crypto news headline.

- `event_class` must be one of {{EVENT_CLASSES}} or null when none applies.
- `assets` is the subset of {{ASSETS}} the headline is about (often empty).
- Use ONLY the headline text — no outside knowledge of the story, no inference about
  what "probably" happened, no numbers of your own.

Return JSON {"labels": [{url_hash, event_class, assets}, ...]} covering every input item.

Items:
{{ITEMS}}
