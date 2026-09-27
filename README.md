# linkedin-auto

Applies to LinkedIn **Easy Apply** jobs automatically, with a CV tailored to each job.

An agy agent (Gemini 3.8 Flash High) drives a real Chromium browser through Playwright MCP. It searches
LinkedIn with your filters, then for each job it:

1. reads the job description,
2. generates a tailored CV from your `cv.yaml` (a separate Gemini call, rendered with RenderCV),
3. opens Easy Apply, uploads that PDF, fills the form from `answers.md`, submits,
4. logs the result in `applied.jsonl`.

It stops when it reaches `--max-applies` or `--max-pages`, or when the results run out.

```
jobs.py run ──► agy -p (Gemini) ──► linkedin_playwright MCP ──► Chromium (.browser-profile/)
                  │
                  ├─► uv run jobs.py tailor ──► agy -p (Gemini) ──► cv yaml ──► rendercv ──► CV.pdf
                  └─► uv run jobs.py log ──► applied.jsonl  (prints LIMIT REACHED to stop)
```

## Files

| Path | What it is | You edit it? |
|---|---|---|
| `jobs.py` | Everything: the `run`, `tailor` and `log` commands, and the agent prompt | Rarely |
| `cv.yaml` | Your base CV in RenderCV format. Its `design`, `locale` and `settings` are kept in every tailored CV | **Yes** |
| `answers.md` | Answers to Easy Apply questions. The agent only uses this file and `cv.yaml` | **Yes** |
| `search.toml` | Default search: position, location, limits, filters | **Yes** |
| `base_cv.pdf` | Untailored CV, uploaded when tailoring fails. Regenerate it after editing `cv.yaml` | No |
| `docs/filters.md` | Every search filter and its LinkedIn URL code | — |
| `applications/<company-title>/` | Per job: `CV.yaml` and `CV.pdf` | Generated |
| `applied.jsonl` | One line per processed job: `ts, company, title, url, status, note` | Generated |
| `.browser-profile/` | Chromium profile holding your LinkedIn login | Generated |
| `.run.json` | Start time and limit of the current run | Generated |

Everything generated is gitignored. It contains personal data, so keep it that way.

## Setup (once)

Requirements: `uv`, `node`/`npx`, and `agy` logged in to your Google account.

```bash
uv sync                                   # rendercv
npx playwright install chromium           # browser used by the MCP

# Register the browser MCP in agy (agy only has a global MCP config)
agy mcp add linkedin_playwright npx -- -y @playwright/mcp@latest \
  --browser chromium --user-data-dir $PWD/.browser-profile

# Log in to LinkedIn once, by hand, in that same profile. Close the window when done.
npx playwright open -b chromium --user-data-dir .browser-profile https://www.linkedin.com/login
```

Then:

1. Put your CV in `cv.yaml` (RenderCV YAML) and check it renders:
   `uv run rendercv render cv.yaml --pdf-path base_cv.pdf -nomd -nohtml -nopng`
2. Fill every `TODO` in `answers.md`.

**Credentials:** none are stored in the project. The login lives in the browser profile as cookies,
the same as a normal browser. If LinkedIn logs you out, or shows a captcha or security check, the agent
stops and says so. Re-run the `playwright open` command above to log in again.

## Running

Your default search lives in **`search.toml`** (position, location, limits, filters), so normally you just run:

```bash
uv run jobs.py run --dry-run          # preview the search URL
uv run jobs.py run                    # apply with the defaults in search.toml
```

Any flag overrides `search.toml` for that run only:

```bash
uv run jobs.py run "AI engineer" --location Germany --workplace remote --max-applies 1
```

| `search.toml` key / flag | Meaning |
|---|---|
| `position` / 1st argument | Search keywords |
| `max_applies` / `--max-applies N` | Stop after N **successful** applications this run. `0` = unlimited |
| `max_pages` / `--max-pages N` | Result pages to go through (25 jobs each). `0` = until results run out |
| `location`, `posted`, `experience`, `workplace`, `job_type`, `sort`, `distance`, `under_10_applicants`, `filter` | Search filters, see [docs/filters.md](docs/filters.md). A `filter` given as a flag is added to the list in the file |
| `--dry-run` | Print the search URL and exit |
| `AGY_MODEL` env var | Model for the agent and CV tailoring. Default `gemini-3.8-flash-high` (`agy models` lists them) |

A typo'd key or an invalid value in `search.toml` stops the run with an error rather than being silently ignored.

Before the browser starts, `run` does a quick preflight: agy and the `linkedin_playwright` MCP are set up,
you have logged in once (`.browser-profile/` exists), and `cv.yaml` renders. It re-renders `base_cv.pdf` if
`cv.yaml` changed. If `answers.md` still has TODOs, it lists them and asks:
`[f]` fill what your CV answers with Gemini (shows a diff, asks before saving, keeps `answers.md.bak`),
`[c]` continue anyway, or `[q]` quit.

`Ctrl+C` stops at any time. Jobs already logged are skipped on the next run, and so are jobs LinkedIn
marks as "Applied".

## How it behaves

**Choosing jobs.** It takes every Easy Apply job in the results, in order, skipping any job that's
already logged or marked "Applied". There's no fit scoring: narrow the search with filters.

**CV tailoring** (`jobs.py tailor`). Gemini gets your `cv.yaml` and the job description and returns a new
YAML file. The rules it's given:

- Truth first: it may reorder, reword, emphasize or drop content, but never invent employers, dates,
  titles, technologies, metrics or credentials.
- It mirrors the job's keywords only where your CV supports them.
- Identity, `design`, `locale` and `settings` stay unchanged. The CV must fit on one page.

If RenderCV rejects the YAML, the error goes back to Gemini for one retry. If that fails too, the agent
uploads `base_cv.pdf`. Before relying on it, spot-check a few `applications/*/CV.yaml` against `cv.yaml`.

**Form filling.** Answers come only from `answers.md` and `cv.yaml`. If a required question isn't
covered, the agent discards that application and logs it as `skipped` with the question as the note. It
unticks "Follow company" before submitting.

**Guardrails in the prompt.** The agent must never pay for anything, message anyone, or edit your
profile or the project files, and it stops on a login page or captcha.

## Improving over time

```bash
# Which questions blocked applications? Add the answers to answers.md
grep '"skipped"' applied.jsonl | python3 -c "import sys,json; [print(json.loads(l)['note']) for l in sys.stdin]"

# What got applied
grep '"applied"' applied.jsonl | python3 -c "import sys,json; [print(e['company'],'-',e['title']) for e in map(json.loads,sys.stdin)]"
```

To retry a job, delete its line from `applied.jsonl`.

## Risks and limits

- **Account risk.** Automated use breaks LinkedIn's terms and it can restrict accounts. Keep runs small
  (tens a day, not hundreds) and watch the first ones.
- **Shell access.** agy runs with `--dangerously-skip-permissions`, so the Gemini agent can run any shell
  command as you for the whole run. The prompt forbids editing files, but that's an instruction, not a
  sandbox.
- **Global MCP.** `linkedin_playwright` is visible to every agy session. Remove it with
  `agy mcp remove linkedin_playwright`.
- **Easy Apply only.** Jobs that apply on the company's own site are filtered out by `f_AL=true`.
- **One page isn't checked.** Tailored CVs are asked to fit one page, but nothing verifies it. Check
  with `pdfinfo applications/*/CV.pdf | grep Pages`.
- **Quota.** Each job is one long agent turn plus one tailoring call against your agy (Google) quota.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Agent says it's not logged in, or hits a captcha | Re-run the `playwright open ... /login` command and log in |
| "Profile in use" / browser won't start | Close other Chromium windows using `.browser-profile/` |
| Upload fails or the path is refused | Re-register the MCP with `--allow-unrestricted-file-access` added |
| Many `skipped` entries | Add the logged questions to `answers.md` |
| A filter doesn't apply | Set it by hand on LinkedIn, copy the param from the URL, pass it with `--filter` |
| `CV render failed` | Check `applications/<job>/CV.yaml`, and check that `cv.yaml` itself renders |
