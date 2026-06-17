# Intake Questionnaire

Fill this out and we build you a weekly intelligence monitor tuned to exactly
what you need to track — no software to learn, just a briefing you open and read.

Your answers become a structured `intake.json`, which the scaffolder turns into
the deterministic parts of your config; the source-discovery agent then finds
and verifies the live feeds for everything you list under **Coverage**.

> Each section notes the config field it feeds (in *italics*) — for the operator,
> not the client.

---

## 1. About your monitor
- **What should we call it?** *(→ `branding.name`)*
- **Brand color?** A hex code, or we'll pick a sensible default. *(→ `branding.accent_color`)*
- **Who should be able to see it?** Just you / your team / public link. *(→ deployment: repo visibility)*

## 2. Your audiences (1–4)
For each distinct kind of reader, answer the three questions below. Each audience
becomes a switchable "edition" with its own relevance lens. *(→ `editions[]`)*
- **Label** — e.g. "Policy", "Sales", "Compliance". *(→ `editions[].label` / `id`)*
- **Their role** — what do they do, what decisions do they make? *(→ `audience_description`)*
- **What makes something matter to them?** What would they act on; what would they
  ignore? Be specific. *(→ `analysis_instructions`)*
- **Topic buckets** — how do they mentally sort this news? 3–6 short labels.
  *(→ `editions[].categories`)*

## 3. Never-miss triggers
Words or events that are **always** critical — surface them even if nothing else
qualifies (e.g. "warning letter", a recall class, your company name, a key program).
*(→ `scoring_rubric.never_discard`)*

## 4. Noise to cut
What reliably wastes your time and should be dropped? (e.g. obituaries, sports,
horoscopes). *(→ `keyword_prefilter.exclude`)*

## 5. Names that matter
The specific organizations, agencies, programs, competitors, or people you care
about. These sharpen relevance and help the agent find the right sources.
*(→ `keyword_prefilter.include` + source discovery)*

## 6. Coverage — what should it watch?
List what you read today or wish you could keep up with: agencies, regulators,
publications, databases, competitor newsrooms. **You don't need URLs** — names and
a one-line "what" are enough; the agent finds and verifies the live feeds.
*(→ `source_briefs` → agent → `sources[]`)*
For each: **name**, **what it covers**, **kind** (official / news / database /
social / web), and a **URL hint** if you happen to know one.

## 7. Cadence & delivery
- **How often?** Weekly / weekdays / daily. *(→ `cadence.frequency`)*
- **What day & hour** should it be ready, in **what time zone**? *(→ `cadence`)*

## 8. Depth on click
When you expand an item, what do you want to see? Pick any:
background · key stakeholders · scenarios to watch · recommended actions.
*(→ `deep_analysis.sections`)*

## 9. Access & security
- **Any paid or login-only sources** you want included? Name them; we'll request
  the API key separately and store it as a secret — **never paste keys here.**
  *(→ `sources[].auth_env_var` + repo secret)*

---

### What happens next
1. Your answers → `intake.json` (validated against `docs/intake/intake.schema.json`).
2. Scaffolder fills the deterministic config (`python -m tooling.scaffold intake.json`).
3. Source-discovery agent resolves **Coverage** into working, connectivity-tested sources.
4. We verify the first brief with you, then it goes live on a schedule.
