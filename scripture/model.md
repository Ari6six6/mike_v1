# Model Protocol: Synthesizing Recon into an AppModel

## What This Mode Does

Model mode takes raw recon output and distills it into a typed, sourced, gap-documented
`AppModel` — the formal contract that build mode operates from. You are not gathering new
data. You are reasoning over data that already exists.

**Precision is the metric.** A model entry you cannot source is not an entry — it is a
guess, and guesses corrupt the build. Document what you know. Document what you don't.
Leave nothing implied.

---

## Input Sources

In order of authority:

1. `targets/<domain>.md` — structured recon output, updated across sessions
2. `recon/raw.jsonl` — verbatim tool results, auto-saved on every recon run
3. `models/<name>-<version>.json` — an existing model, if you are revising

Read all three before writing anything.

---

## Output

One file: `models/<name>-<version>.json`

Where `<name>` is the system identifier (domain, service name, or slug) and `<version>` is
the software version confirmed during recon (e.g. `1.4.2`, `2024-Q1`, `unknown` if not
pinned).

The schema is defined in `michael/appmodel.py`. Required fields:

```json
{
  "name": "api.example.com",
  "version": "2.3.1",
  "discovered_at": "2026-05-28T14:00:00+00:00",
  "base_url": "https://api.example.com",
  "auth": {
    "type": "bearer",
    "header": "Authorization",
    "notes": "JWT, 1h expiry observed in response headers"
  },
  "endpoints": [
    {
      "method": "POST",
      "path": "/v1/sessions",
      "status": 200,
      "content_type": "application/json",
      "notes": "unauthenticated — returns token"
    }
  ],
  "stack": ["nginx/1.25.3", "Node.js", "Cloudflare"],
  "notes": "Free-form synthesis — interesting patterns, confirmed behaviours, anomalies.",
  "source_files": ["targets/api.example.com.md", "recon/raw.jsonl"]
}
```

---

## Source Every Field

If a field's value came from a recon tool, note it. If it came from a banner, a header,
a cookie name, or a source_map match — say so in the `notes` of that field or in the
top-level `notes` string.

If a field is genuinely unknown, set it to an empty string or empty list and add an entry
to the `notes` field:

```
"notes": "auth type unknown — login path found at /login but no request was made. Stack: nginx confirmed, backend language not determined."
```

Never fill a field with a plausible guess. Unknown is more useful than wrong.

---

## Handoff Convention

Before calling `commit_changes`:

1. Copy the model file to the results directory:
   `run_shell("cp models/<name>-<version>.json /path/to/results/<slug>-model.json")`

2. Call `commit_changes(summary="model: <name> v<version>")`.

The model file will be picked up by H2 on the next run and available to build mode via
`load_model(name, version)`.

---

## Revision Pattern

If a model already exists and recon has produced new data:

1. `load_model(name, version)` — load current model
2. `read_file("recon/raw.jsonl")` or `read_file("targets/<domain>.md")` — read new data
3. Identify what changed, what was confirmed, what was wrong
4. `write_file("models/<name>-<version>.json", ...)` — overwrite with updated model
5. Increment version if the change is significant (new endpoints, auth change, stack revision)
6. Commit

---

## What Makes a Good Model

**Good `auth` entry:**
```json
{
  "type": "apikey",
  "header": "X-API-Key",
  "notes": "Observed in web_security_posture output: header present on all /v2/ responses"
}
```

**Bad `auth` entry:**
```json
{
  "type": "apikey"
}
```

**Good `endpoints` entry:**
```json
{
  "method": "GET",
  "path": "/api/v2/users",
  "status": 401,
  "content_type": "application/json",
  "notes": "Returns {\"error\": \"unauthorized\"} — confirms endpoint exists and requires auth"
}
```

**Bad `endpoints` entry:**
```json
{
  "path": "/api/v2/users"
}
```

Specific beats vague. Observed beats inferred. Sourced beats assumed.
