# linkedin-auto

Applies to LinkedIn **Easy Apply** jobs automatically, with a CV tailored to each job.

An agy agent (Gemini 3.8 Flash High) drives your own browser (Chrome by default; Brave, Edge or any Chromium-based one) through Playwright MCP. It searches
LinkedIn with your filters, then for each job it:

1. reads the job description,
2. generates a tailored CV from your `cv.yaml` (a separate Gemini call, rendered with RenderCV),
3. opens Easy Apply, uploads that PDF, fills the form from `answers.md`, submits,
4. logs the result in `applied.jsonl`.

It stops when it reaches `--max-applies`, `--max-time` or `--max-pages`, or when the results run out.

```
jobs.py run ──► agy -p (Gemini) ──► linkedin_playwright MCP = jobs.py mcp ──► your browser (.browser-profile/<browser>/)
                  │
                  ├─► uv run jobs.py tailor ──► agy -p (Gemini) ──► cv yaml ──► rendercv ──► CV.pdf
                  └─► uv run jobs.py log ──► applied.jsonl  (prints LIMIT REACHED to stop)
```

## Files

| Path | What it is | You edit it? |
|---|---|---|
| `jobs.py` | Everything: the `run`, `login`, `tailor`, `log` and `mcp` commands, and the agent prompt | Rarely |
| `cv.yaml` | Your base CV in RenderCV format. Its `design`, `locale` and `settings` are kept in every tailored CV | **Yes** |
| `answers.md` | Answers to Easy Apply questions. The agent only uses this file and `cv.yaml` | **Yes** |
| `search.toml` | Default search: position, location, limits, filters | **Yes** |
| `base_cv.pdf` | Untailored CV, uploaded when tailoring fails. Regenerate it after editing `cv.yaml` | No |
| `docs/filters.md` | Every search filter and its LinkedIn URL code | — |
| `applications/<company-title>/` | Per job: `CV.yaml` and `CV.pdf` | Generated |
| `applied.jsonl` | One line per processed job: `ts, company, title, url, status, note` | Generated |
| `.browser-profile/<browser>/` | One profile per browser, holding your LinkedIn login | Generated |
| `.run.json` | Start time, limits and browser of the current run | Generated |
| `run.log` | Every event of every run, the source of the live log | Generated |

Everything generated is gitignored. It contains personal data, so keep it that way.

## Setup (once)

Requirements: macOS, `uv`, `node`/`npx`, a Chromium-based browser (Chrome, Brave, Edge...), and `agy`
logged in to your Google account. Nothing gets downloaded for the browser: it uses the one you have.

```bash
uv sync                                   # rendercv

# Register the browser MCP in agy, once (agy only has a global MCP config).
# It points at `jobs.py mcp`, which starts Playwright MCP with the browser of the current run.
agy mcp add linkedin_playwright $PWD/.venv/bin/python -- $PWD/jobs.py mcp

# Log in to LinkedIn once per browser you want to use. Close the window when done.
uv run jobs.py login                      # Chrome
uv run jobs.py login --browser brave      # Brave, in its own profile
```

Then:

1. Put your CV in `cv.yaml` (RenderCV YAML) and check it renders:
   `uv run rendercv render cv.yaml --pdf-path base_cv.pdf -nomd -nohtml -nopng`
2. Fill every `TODO` in `answers.md`.

**Credentials:** none are stored in the project. The login lives in the browser profile as cookies,
the same as a normal browser. If LinkedIn logs you out, or shows a captcha or security check, the agent
stops and says so. Re-run `uv run jobs.py login` (with the same `--browser`) to log in again.

**Why `login` and not just opening the browser:** Playwright launches the browser with `--use-mock-keychain`,
so cookies are encrypted with a dummy key instead of the macOS keychain. `login` uses the same flag. A login
made by opening the browser normally is unreadable to the agent, and it will see a login page.

### Browsers

| `--browser` | Executable |
|---|---|
| `chrome` (default) | `/Applications/Google Chrome.app` |
| `brave` | `/Applications/Brave Browser.app` |
| `edge` | `/Applications/Microsoft Edge.app` |
| `chromium` | `/Applications/Chromium.app` |
| any path | Any other Chromium-based browser, e.g. `--browser "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi"` |

Each browser gets its own profile in `.browser-profile/<browser>/`, separate from your everyday one, so you
can keep using your browser while the agent runs. Firefox and Safari aren't supported (Playwright can only
drive its own builds of those).

## Running

Your default search lives in **`search.toml`** (position, location, limits, filters), so normally you just run:

```bash
uv run jobs.py run --dry-run          # preview the search URL
uv run jobs.py run                    # apply with the defaults in search.toml
```

Any flag overrides `search.toml` for that run only:

```bash
uv run jobs.py run "AI engineer" --location Germany --workplace remote --max-applies 1
uv run jobs.py run "python developer" -l "San Francisco" --browser brave --max-time 1.5 -v
```

| `search.toml` key / flag | Meaning |
|---|---|
| `position` / 1st argument | Search keywords. Quote multi-word values: `"AI engineer"` |
| `location` / `-l`, `--location` | City, region or country: `-l "San Francisco"` |
| `browser` / `-b`, `--browser` | Browser to drive, see [Browsers](#browsers). Default `chrome` |
| `max_applies` / `--max-applies N` | Stop after N **successful** applications this run. `0` = unlimited |
| `max_time` / `--max-time H` | Stop after H hours (e.g. `1.5`); the agent finishes the current job first, and is killed 10 min later if it doesn't stop. `0` = unlimited |
| `max_pages` / `--max-pages N` | Result pages to go through (25 jobs each). `0` = until results run out |
| `posted`, `experience`, `workplace`, `job_type`, `sort`, `distance`, `under_10_applicants`, `filter` | Search filters, see [docs/filters.md](docs/filters.md). A `filter` given as a flag is added to the list in the file |
| `--dry-run` | Print the search URL and exit |
| `-v` / `-q` | Live progress log: `-v` adds timings, URLs and render errors, `-q` shows only application results. Everything also goes to `run.log` |
| `AGY_MODEL` env var | Model for the agent and CV tailoring. Default `gemini-3.8-flash-high` (`agy models` lists them) |

A typo'd key or an invalid value in `search.toml` stops the run with an error rather than being silently ignored.

Before the browser starts, `run` does a quick preflight: agy and the `linkedin_playwright` MCP are set up
(it prints the register command if not), the browser exists and you have logged in with it once, and
`cv.yaml` renders. It re-renders `base_cv.pdf` if
`cv.yaml` changed. If `answers.md` still has TODOs, it lists them and asks:
`[f]` fill what your CV answers with Gemini (shows a diff, asks before saving, keeps `answers.md.bak`),
`[c]` continue anyway, or `[q]` quit.

### Live log

While the agent works, `run` prints what's happening, in color:

```
22:16:32 searching "ai engineer" in San Francisco, stop after 2 applies
22:16:32 agent started (gemini-3.8-flash-high, chrome), browsing LinkedIn... (-v for more detail)
22:17:05 tailoring CV: ACME — AI Engineer
22:17:29 CV ready in 24s: applications/acme-ai-engineer/CV.pdf
22:18:40 applied: ACME — AI Engineer  [1/2 applied]
22:19:02 skipped: Foo — ML Engineer (required: German)  [1/2 applied]
22:25:13 agent finished (exit 0): 2 applied this run
```

Green = applied, yellow = skipped or warning, red = failed or error. `-v` adds grey debug lines (Gemini timings,
job URLs, RenderCV errors), `-q` keeps only the application results. `tailor` and `log` are run by the agent,
so their events go through `run.log`, which `run` follows; the file keeps every event of every run. Between
"agent started" and the first job it's quiet: that's the agent browsing LinkedIn. The agent's own summary is
printed when it finishes.

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
| Agent says it's not logged in, or hits a captcha | Re-run `uv run jobs.py login` and log in |
| Agent sees a login page right after `login` | You logged in by opening the browser yourself: use `uv run jobs.py login --browser <same one>` |
| "Profile in use" / browser won't start | Close the `login` window, or any window using `.browser-profile/<browser>/` |
| Upload fails or the path is refused | Add `"--allow-unrestricted-file-access"` to the `npx` args in `mcp()` in `jobs.py` |
| Preflight says the MCP is missing or outdated | Run the `agy mcp remove/add` command it prints |
| Many `skipped` entries | Add the logged questions to `answers.md` |
| A filter doesn't apply | Set it by hand on LinkedIn, copy the param from the URL, pass it with `--filter` |
| `CV render failed` | Check `applications/<job>/CV.yaml`, and check that `cv.yaml` itself renders |
