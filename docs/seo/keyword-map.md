# Ollama Herd SEO keyword map

The canonical keyword phrases and length limits every Ollama Herd surface must satisfy. Rules only — each one is a machine-checkable predicate.

## How to read this file

Every rule is a numeric or string constraint that a linter can decide without judgement. There is no advice here; if a line cannot be turned into `assert`, it does not belong in this file.

- **Rule IDs** are stable (`SEO-R01`, `SEO-P01`, `SEO-S01`, `SEO-D01`, `SEO-M01`). Downstream nodes reference them by ID.
- **The canonical form is the single `yaml` fence** at the bottom of this file. Parse the first ```` ```yaml ```` block; the prose tables above it are a rendering of the same values and must not be read programmatically.
- **Baseline counts** were measured on 2026-09-07 against commit `7980c96`. They record how many files violated the rule the day it was written, so a node knows the size of its job. They are not part of the constraint.

## Scope

Indexed (rules apply, 33 docs as of the baseline): `README.md`, `pyproject.toml`, `skills/*/SKILL.md`, `skills/README.md`, and every `.md` under `docs/` except the exempt paths below.

Exempt, with reason:

| Path | Reason |
|------|--------|
| `docs/issues.md`, `docs/observations.md`, `docs/issues/` | Append-only operational logs; headings are dated entries, not page titles |
| `docs/plans/`, `docs/handoffs/`, `docs/experiments/`, `docs/upstream-issues/`, `docs/examples/` | Internal working files, not reader-facing pages |
| `docs/ollama-fleet-manager.md`, `docs/ollama-fleet-manager-v2.md`, `docs/ollama-fleet-manager-research.md` | Superseded design documents kept for history |
| `docs/seo/` | This ruleset (self-reference) |
| `CHANGELOG.md`, `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md` | Machine/contributor files, not search surfaces |

## Definitions

These fix the meaning of "title", "lead", and "character" so two implementations agree.

- **Body** — the file with fenced code blocks removed. A line whose first non-space characters are ` ``` ` toggles the fence; fenced lines count as empty.
- **Title** — the text of the single `# ` heading in the body, minus the leading `# ` and surrounding whitespace.
- **Lead** — the first of these found after the title, scanning the body downward: (a) a `### ` deck line immediately following the title (used by `docs/fleet-manager-routing-engine.md` and `docs/openclaw-integration.md`), or (b) the first paragraph, where these lines are skipped rather than treated as the paragraph: blank lines, horizontal rules (`---`), badge-only lines (a line containing only markdown image links and whitespace), blockquote lines (`>`), table rows (`|`), list items (`-`, `*`, `+`), and bold-only metadata lines (`**Date:** …`).
- **Prose length** — character count of a string after: joining its lines with single spaces, rewriting `![alt](url)` to `alt` and `[text](url)` to `text`, deleting `` ` ``, `**`, `__`, `*`, `_`, and collapsing runs of whitespace to one space. Unicode characters, not bytes.
- **Normalized text** — lowercased with every non-alphanumeric character deleted (`Gemma 3` → `gemma3`). Used for phrase containment, so hyphenation and spacing never decide a check.
- **Slug tokens** — the hyphen-separated parts of a file's directory name (`skills/ollama-load-balancer/` → `ollama`, `load`, `balancer`) or of a doc filename without extension (`docs/guides/mlx-setup.md` → `mlx`, `setup`), minus the stopword list in the YAML block.

## Surface: README

| Field | Value |
|-------|-------|
| Primary phrase | `ollama fleet router` |
| Secondary phrases | `load balancer`, `mDNS`, `OpenAI-compatible`, `Apple Silicon`, `local LLM inference` |
| Title max | 60 characters |
| Lead length | 60–160 characters |

| ID | Rule | Baseline |
|----|------|----------|
| SEO-R01 | Exactly one `# ` heading in the body, and it is the first non-blank body line | 0 violations |
| SEO-R02 | Title prose length ≤ 60 | 0 violations |
| SEO-R03 | Normalized title contains normalized primary phrase `ollamafleetrouter` | 1 violation (title is `Ollama Herd`) |
| SEO-R04 | Lead prose length ≥ 60 and ≤ 160 | 1 violation (lead is 307) |
| SEO-R05 | Normalized lead contains the normalized primary phrase | 1 violation |
| SEO-R06 | At least 4 of the 5 secondary phrases appear (normalized) anywhere in the body | 0 violations (4 of 5 present; `local LLM inference` absent) |
| SEO-R07 | Every markdown image in the file has non-empty alt text | 0 violations |
| SEO-R08 | At least 6 relative links to `docs/**.md` targets that exist on disk | count at build time |

## Surface: PyPI metadata (`pyproject.toml`)

| Field | Value |
|-------|-------|
| Primary phrase | `ollama fleet router` |
| Secondary keywords (required, exact strings) | `load-balancer`, `inference-router`, `mdns`, `openai-compatible`, `local-llm` |
| Description length | 120–200 characters |
| Keyword list size | 12–25 entries |

| ID | Rule | Baseline |
|----|------|----------|
| SEO-P01 | `project.description` prose length ≥ 120 and ≤ 200 | 0 violations (154) |
| SEO-P02 | Normalized `project.description` starts with normalized primary phrase | 1 violation (starts `Smart multimodal router`) |
| SEO-P03 | `project.keywords` has 12–25 entries, all lowercase, all unique, each ≤ 30 characters | 1 violation (`mDNS` is not lowercase; 17 entries) |
| SEO-P04 | `project.keywords` contains all 5 required secondary keywords as exact strings | 3 missing (`inference-router`, `mdns`, `local-llm`) |
| SEO-P05 | `project.urls` defines the keys `Homepage`, `Documentation`, `Changelog`, `Issues` | 1 violation (`Changelog` absent) |
| SEO-P06 | Every `project.urls` value is an `https://github.com/geeks-accelerator/` URL or an existing repo-relative path | 0 violations |

## Surface: skills

Per-skill phrases are derived, not hand-listed: a skill's own slug is its primary phrase. This is what ClawHub's scoring rewards — slug and display-name matches add up to +2.5 on top of the vector score (see [optimizing-skills-for-clawhub.md](../guides/optimizing-skills-for-clawhub.md)).

| Field | Value |
|-------|-------|
| Primary phrase | the skill's slug tokens (per skill) |
| Secondary phrases | `Llama`, `Qwen`, `DeepSeek`, `Mistral`, `Apple Silicon` (model/hardware terms) |
| Display-name (H1) max | 60 characters |
| `description` length | 120–500 characters |
| SKILL.md body max | 12000 characters (ClawHub `DEFAULT_EMBEDDING_MAX_CHARS`; text past it is not embedded) |

| ID | Rule | Baseline |
|----|------|----------|
| SEO-S01 | Every slug token appears in the normalized first 120 characters of `description` | 3 violations (`mflux-image-router`, `ollama-herd`, `ollama-manager`) |
| SEO-S02 | Every slug token appears in the normalized H1 | 4 violations (`deepseek-deepseek-coder`, `mlx-apple-silicon-mlx`, `qwen-qwen3`, `stable-diffusion-sd3`) |
| SEO-S03 | `description` prose length ≥ 120 and ≤ 500 | 2 violations (`local-llm-router` 537, `ollama-herd` 550) |
| SEO-S04 | H1 prose length ≤ 60 | 0 violations |
| SEO-S05 | No two skills share the same first 40 characters of `description` (case-insensitive) | 0 violations |
| SEO-S06 | `description` names at least 3 distinct terms from the secondary list or the model table in the ClawHub guide | count at build time |
| SEO-S07 | SKILL.md length ≤ 12000 characters | 0 violations (max 11128) |
| SEO-S08 | `skills/README.md` links to every `skills/*/` directory exactly once | count at build time |

## Surface: docs

| Field | Value |
|-------|-------|
| Primary phrase | `Ollama Herd` |
| Secondary phrases | `local LLM inference`, `fleet router`, `mDNS`, `OpenAI-compatible`, `Apple Silicon` |
| Title max | 70 characters |
| Lead length | 60–160 characters |

| ID | Rule | Baseline |
|----|------|----------|
| SEO-D01 | Exactly one `# ` heading in the body, and it is the first non-blank body line | 0 violations |
| SEO-D02 | Title prose length ≤ 70 | 1 violation (`claude-code-proxy-techniques-survey` 73) |
| SEO-D03 | Lead prose length ≥ 60 and ≤ 160 | 21 of 33 indexed docs |
| SEO-D04 | Normalized body contains the normalized primary phrase `ollamaherd` at least once | 6 violations |
| SEO-D05 | Normalized title contains at least 2 of the page's slug tokens | count at build time |
| SEO-D06 | Each of the 5 secondary phrases appears (normalized) in at least 2 indexed docs | 0 violations (lowest is `local LLM inference` and `fleet router`, 3 docs each) |
| SEO-D07 | No duplicate H2 slugs within a file (GitHub anchor slugging: lowercase, spaces to `-`, drop other punctuation) | count at build time |
| SEO-D08 | Every relative link in an indexed doc resolves to an existing path | count at build time |

## Canonical rules

```yaml
version: 1
measured_at: 2026-09-07
measured_commit: 7980c96
stopwords: [a, an, and, for, of, the, to, via, with, your]
exempt_paths:
  - docs/issues.md
  - docs/observations.md
  - docs/issues/
  - docs/plans/
  - docs/handoffs/
  - docs/experiments/
  - docs/upstream-issues/
  - docs/examples/
  - docs/ollama-fleet-manager.md
  - docs/ollama-fleet-manager-v2.md
  - docs/ollama-fleet-manager-research.md
  - docs/seo/
surfaces:
  readme:
    files: [README.md]
    primary_phrase: "ollama fleet router"
    secondary_phrases: ["load balancer", "mDNS", "OpenAI-compatible", "Apple Silicon", "local LLM inference"]
    secondary_min_present: 4
    title_max_chars: 60
    lead_min_chars: 60
    lead_max_chars: 160
    min_docs_links: 6
  pypi:
    files: [pyproject.toml]
    primary_phrase: "ollama fleet router"
    required_keywords: ["load-balancer", "inference-router", "mdns", "openai-compatible", "local-llm"]
    description_min_chars: 120
    description_max_chars: 200
    keywords_min: 12
    keywords_max: 25
    keyword_max_chars: 30
    required_url_keys: [Homepage, Documentation, Changelog, Issues]
    url_prefix: "https://github.com/geeks-accelerator/"
  skills:
    files: ["skills/*/SKILL.md"]
    index_file: skills/README.md
    primary_phrase_source: slug_tokens
    primary_phrase_window_chars: 120
    secondary_phrases: [Llama, Qwen, DeepSeek, Mistral, "Apple Silicon"]
    secondary_min_present: 3
    title_max_chars: 60
    description_min_chars: 120
    description_max_chars: 500
    description_prefix_collision_chars: 40
    file_max_chars: 12000
  docs:
    files: ["docs/**/*.md"]
    primary_phrase: "Ollama Herd"
    secondary_phrases: ["local LLM inference", "fleet router", "mDNS", "OpenAI-compatible", "Apple Silicon"]
    secondary_min_files_each: 2
    title_max_chars: 70
    title_min_slug_tokens: 2
    lead_min_chars: 60
    lead_max_chars: 160
  meta:
    files: [docs/seo/keyword-map.md]
    id_pattern: "SEO-[A-Z][0-9]{2}"
rules:
  - {id: SEO-R01, surface: readme, predicate: "body contains exactly one '# ' heading and it is the first non-blank body line", baseline_violations: 0}
  - {id: SEO-R02, surface: readme, predicate: "prose_len(title) <= 60", baseline_violations: 0}
  - {id: SEO-R03, surface: readme, predicate: "norm(primary_phrase) in norm(title)", baseline_violations: 1}
  - {id: SEO-R04, surface: readme, predicate: "60 <= prose_len(lead) <= 160", baseline_violations: 1}
  - {id: SEO-R05, surface: readme, predicate: "norm(primary_phrase) in norm(lead)", baseline_violations: 1}
  - {id: SEO-R06, surface: readme, predicate: "count(p for p in secondary_phrases if norm(p) in norm(body)) >= 4", baseline_violations: 0}
  - {id: SEO-R07, surface: readme, predicate: "every markdown image has non-empty alt text", baseline_violations: 0}
  - {id: SEO-R08, surface: readme, predicate: "count(relative links to existing docs/**.md) >= 6", baseline_violations: null}
  - {id: SEO-P01, surface: pypi, predicate: "120 <= prose_len(project.description) <= 200", baseline_violations: 0}
  - {id: SEO-P02, surface: pypi, predicate: "norm(project.description).startswith(norm(primary_phrase))", baseline_violations: 1}
  - {id: SEO-P03, surface: pypi, predicate: "12 <= len(keywords) <= 25 and all lowercase and all unique and max(len(k)) <= 30", baseline_violations: 1}
  - {id: SEO-P04, surface: pypi, predicate: "set(required_keywords) <= set(keywords)", baseline_violations: 3}
  - {id: SEO-P05, surface: pypi, predicate: "set(required_url_keys) <= set(project.urls)", baseline_violations: 1}
  - {id: SEO-P06, surface: pypi, predicate: "every project.urls value startswith url_prefix or is an existing repo path", baseline_violations: 0}
  - {id: SEO-S01, surface: skills, predicate: "every slug token in norm(description[:120])", baseline_violations: 3}
  - {id: SEO-S02, surface: skills, predicate: "every slug token in norm(title)", baseline_violations: 4}
  - {id: SEO-S03, surface: skills, predicate: "120 <= prose_len(description) <= 500", baseline_violations: 2}
  - {id: SEO-S04, surface: skills, predicate: "prose_len(title) <= 60", baseline_violations: 0}
  - {id: SEO-S05, surface: skills, predicate: "no two descriptions share their first 40 characters, case-insensitive", baseline_violations: 0}
  - {id: SEO-S06, surface: skills, predicate: "count(distinct model/hardware terms in description) >= 3", baseline_violations: null}
  - {id: SEO-S07, surface: skills, predicate: "len(file) <= 12000", baseline_violations: 0}
  - {id: SEO-S08, surface: skills, predicate: "skills/README.md links each skills/*/ directory exactly once", baseline_violations: null}
  - {id: SEO-D01, surface: docs, predicate: "body contains exactly one '# ' heading and it is the first non-blank body line", baseline_violations: 0}
  - {id: SEO-D02, surface: docs, predicate: "prose_len(title) <= 70", baseline_violations: 1}
  - {id: SEO-D03, surface: docs, predicate: "60 <= prose_len(lead) <= 160", baseline_violations: 21}
  - {id: SEO-D04, surface: docs, predicate: "norm(primary_phrase) in norm(body)", baseline_violations: 6}
  - {id: SEO-D05, surface: docs, predicate: "count(slug tokens in norm(title)) >= 2", baseline_violations: null}
  - {id: SEO-D06, surface: docs, predicate: "each secondary phrase appears in >= 2 indexed docs", baseline_violations: 0}
  - {id: SEO-D07, surface: docs, predicate: "no duplicate H2 anchor slugs within a file", baseline_violations: null}
  - {id: SEO-D08, surface: docs, predicate: "every relative link resolves to an existing path", baseline_violations: null}
  - {id: SEO-M01, surface: meta, predicate: "the set of rule ids in the prose tables equals the set of ids in this yaml list, each appearing exactly once", baseline_violations: 0}
```

## Surface: this ruleset

| ID | Rule | Baseline |
|----|------|----------|
| SEO-M01 | The set of `SEO-` IDs in the prose tables equals the set in the YAML fence, each appearing exactly once in each | 0 violations (31 IDs) |

A rule is added by adding its ID to both the YAML fence and a prose row; a rule is removed by deleting both. IDs match `SEO-[A-Z][0-9]{2}` and are never reused after removal — SEO-M01 fails on any commit that changes one representation without the other.
