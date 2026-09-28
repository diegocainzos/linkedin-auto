# linkedin-auto

Applies to LinkedIn **Easy Apply** jobs automatically, with a CV tailored to each job.

An agent CLI (`agy` with Gemini 3.8 Flash High by default; also `claude`, `codex` or `pi`, see [Agents](#agents)) drives your own browser (Chrome by default; Brave, Edge or any Chromium-based one) through Playwright MCP. It searches
LinkedIn with your filters, then for each job it:

1. reads the job description and asks **Jev** (OpenRouter's decision model) whether it matches your CV; if not, it skips it,
2. generates a tailored CV from your `cv.yaml` (a separate text-only call to the same agent, rendered with RenderCV),
3. opens Easy Apply, uploads that PDF, fills the form from `answers.md`, submits,
4. logs the result in `jobs.db` (SQLite), and reports any problem it hits to `issues.log`,
5. for strong matches, finds the "Meet the hiring team" person's email (Hunter) and emails them (experimental, see [Hiring team email](#hiring-team-email)).

It stops when it reaches `--max-applies`, `--max-time` or `--max-pages`, or when the results run out.

Quick help: `uv run jobs.py -h` (see [Command reference](#command-reference)).

```
jobs.py run ──► agent CLI ──► linkedin_playwright MCP = jobs.py mcp ──► your browser (.browser-profile/<browser>/)
                  │
                  ├─► uv run jobs.py fit ──► Jev (OpenRouter Decisions API) ──► APPLY / SKIP
                  ├─► uv run jobs.py tailor ──► agent CLI, text only ──► cv yaml ──► rendercv ──► CV.pdf
                  ├─► uv run jobs.py log ──► jobs.db  (prints LIMIT REACHED to stop)
                  │        └─► strong match: Hunter email ──► agent writes email ──► SMTP (only saved without SMTP)
                  └─► uv run jobs.py issue ──► issues.log
```

## Files

| Path | What it is | You edit it? |
|---|---|---|
| `jobs.py` | Everything: the `run`, `login`, `fit`, `tailor`, `log`, `issue` and `mcp` commands, and the agent prompt | Rarely |
| `cv.yaml` | Your base CV in RenderCV format. Its `design`, `locale` and `settings` are kept in every tailored CV | **Yes** |
| `answers.md` | Answers to Easy Apply questions. The agent only uses this file and `cv.yaml` | **Yes** |
| `search.toml` | Default search: position, location, limits, filters, agent, browser | **Yes** |
| `base_cv.pdf` | Untailored CV, uploaded when tailoring fails. Regenerate it after editing `cv.yaml` | No |
| `docs/filters.md` | Every search filter and its LinkedIn URL code | — |
| `applications/<company-title>/` | Per job: `CV.yaml` and `CV.pdf` | Generated |
| `.env` | API keys and SMTP settings (see [Setup](#setup-once)) | **Yes** |
| `jobs.db` | SQLite, one row per job: status, note, Jev's fit, hiring team name, job description, tailored CV YAML, contact email, email subject/body/sent time | Generated |
| `applied.jsonl` | The old log. Imported into `jobs.db` when it's created, unused after that | Old |
| `.browser-profile/<browser>/` | One profile per browser, holding your LinkedIn login | Generated |
| `.run.json` | Start time, limits, agent and browser of the current run | Generated |
| `issues.log` | Problems the agent reported, all runs: `date time problem  job-url` | Generated |
| `run.log` | Every event of every run, the source of the live log | Generated |
| `logs/<date-time>/` | Everything about one run, for debugging (see [Debug logs](#debug-logs)) | Generated |

Everything generated is gitignored. It contains personal data, so keep it that way.

## Setup (once)

Requirements: macOS, `uv`, `node`/`npx`, a Chromium-based browser (Chrome, Brave, Edge...), and an agent
CLI, logged in: `agy` (default), `claude`, `codex` or `pi` (see [Agents](#agents)). Nothing gets downloaded for the browser: it uses the one you have.

```bash
uv sync                                   # rendercv

# agy only: register the browser MCP, once (agy only has a global MCP config; the others get it per run).
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
3. Optional: `cp .env.example .env` (gitignored) and fill it in, for the compatibility check and the hiring team email:
   ```bash
   OPENROUTER_API_KEY=sk-or-...        # Jev compatibility check; without it every job is applied to
   HUNTER_API_KEY=...                  # hiring team email lookup; without it no emails
   SMTP_HOST=smtp.gmail.com            # without it emails are written and saved in jobs.db, not sent
   SMTP_PORT=465                       # 465 = SSL, anything else = STARTTLS
   SMTP_USER=you@gmail.com
   SMTP_PASSWORD=...                   # Gmail: an app password
   SMTP_FROM=you@gmail.com             # optional, defaults to SMTP_USER
   ```

**Credentials:** only what you put in `.env` (gitignored). The login lives in the browser profile as cookies,
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

### Agents

| `--agent` | How it gets the browser | Text-only calls (`tailor`, answers fill) | Tested |
|---|---|---|---|
| `agy` (default) | Global MCP registered once (Setup above) | `--disable-slash-commands` | Yes |
| `claude` | `--mcp-config logs/<run>/mcp.json --strict-mcp-config` (ignores your other MCP servers) | `--tools ""` | Yes |
| `pi` | `--mcp-config logs/<run>/mcp.json`; needs the `pi-mcp-adapter` extension (`pi install npm:pi-mcp-adapter`) | `--no-tools` | Yes |
| `codex` | `-c mcp_servers.linkedin_playwright...` overrides | `--sandbox read-only` | No, codex wasn't installed |

`--model` / `model` picks the model, in the agent's own format: `sonnet` for claude, `gpt-5.5` for codex,
`provider/model` for pi, `agy models` lists agy's. Unset, each CLI uses its own default (Gemini 3.8 Flash
High for agy). All of them run with permission prompts off (`--dangerously-skip-permissions` or equivalent),
see [Risks and limits](#risks-and-limits). `tailor` uses the same agent and model as the run that called it.

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
uv run jobs.py run --agent claude --model sonnet
```

| `search.toml` key / flag | Meaning |
|---|---|
| `position` / 1st argument | Search keywords. Quote multi-word values: `"AI engineer"` |
| `location` / `-l`, `--location` | City, region or country: `-l "San Francisco"` |
| `agent` / `-a`, `--agent` | Agent CLI: `agy`, `claude`, `codex`, `pi`, see [Agents](#agents). Default `agy` |
| `model` / `-m`, `--model` | Model for that agent. Default: the agent's own |
| `browser` / `-b`, `--browser` | Browser to drive, see [Browsers](#browsers). Default `chrome` |
| `max_applies` / `--max-applies N` | Stop after N **successful** applications this run. `0` = unlimited |
| `max_time` / `--max-time H` | Stop after H hours (e.g. `1.5`); the agent finishes the current job first, and is killed 10 min later if it doesn't stop. `0` = unlimited |
| `max_pages` / `--max-pages N` | Result pages to go through (25 jobs each). `0` = until results run out |
| `posted`, `experience`, `workplace`, `job_type`, `sort`, `distance`, `under_10_applicants`, `filter` | Search filters, see [docs/filters.md](docs/filters.md). A `filter` given as a flag is added to the list in the file |
| `--dry-run` | Print the search URL and exit |
| `-v` / `-q` | Live progress log: `-v` adds timings, URLs and render errors, `-q` shows only application results. Everything also goes to `run.log` |

A typo'd key or an invalid value in `search.toml` stops the run with an error rather than being silently ignored.

Before the browser starts, `run` does a quick preflight: the agent CLI is installed (for agy, also that
the `linkedin_playwright` MCP is registered; it prints the register command if not), the browser exists and you have logged in with it once, and
`cv.yaml` renders. It re-renders `base_cv.pdf` if
`cv.yaml` changed. If `answers.md` still has TODOs, it lists them and asks:
`[f]` fill what your CV answers with the agent (shows a diff, asks before saving, keeps `answers.md.bak`),
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

Green = applied, yellow = skipped or warning, red = failed or error. `-v` adds grey debug lines (model timings,
job URLs, RenderCV errors), `-q` keeps only the application results. `tailor` and `log` are run by the agent,
so their events go through `run.log`, which `run` follows; the file keeps every event of every run. Between
"agent started" and the first job it's quiet: that's the agent browsing LinkedIn. The agent's own summary is
printed when it finishes.

`Ctrl+C` stops at any time. Jobs already logged are skipped on the next run, and so are jobs LinkedIn
marks as "Applied".

### Debug logs

Every `run` writes a folder `logs/<YYYYMMDD-HHMMSS>/`, whose path is printed at the end:

| File | What it is | Look here when |
|---|---|---|
| `prompt.txt` | The command line and the exact prompt the agent got | The agent ignored an instruction or a filter |
| `mcp.json` | How the agent CLI starts the browser MCP (not used by agy) | The agent had no browser tools |
| `agent.txt` | Everything the agent printed, including its final summary | It stopped early or said something odd |
| `events.log` | This run's events (all levels, including debug) | You want the timeline |
| `browser/session-*/session.md` | Every browser action the agent took, with its arguments and the page snapshot it got back | It got stuck on a popup or clicked the wrong thing |
| `cli-*.log` | agy's own logs for the agent and each `tailor` call (agy only) | agy or Gemini errors (auth, quota, crashes) |

Problems the agent noticed itself are also in `issues.log`, one line each across all runs, and show up
as yellow `issue:` lines in the live log. The agent reports them with `uv run jobs.py issue "<what happened>" --url <job>`
whenever a tool or command fails, a popup won't close, a page doesn't load, a field can't be filled, an upload
fails or a filter isn't applied. Read it after a run: `cat issues.log`.

`session.md` is the most useful one: search it for the last `### Tool call` to see where the agent was when
it stopped. The folders hold page contents from LinkedIn and your answers, so they're gitignored, and nothing
deletes them: `rm -rf logs/` from time to time.

## Command reference

Everything is in `-h`: `uv run jobs.py -h` for the overview, `uv run jobs.py <command> -h` for each command.
The `[now: ...]` values come from `search.toml`, so yours may differ from the ones below.

<details open><summary><code>uv run jobs.py -h</code></summary>

```
usage: jobs.py [-h] command ...

Applies to LinkedIn Easy Apply jobs for you, uploading a CV tailored to each job.
An agent CLI (agy, claude, codex or pi) drives your browser; answers come from answers.md, the CV from cv.yaml.
Problems the agent hits go to issues.log; everything about a run goes to logs/<date-time>/.

positional arguments:
  command
    run       search LinkedIn and apply (the main command)
    login     log in to LinkedIn in the browser the agent uses (once per browser)
    tailor    write a CV for one job (the agent calls this)
    log       record a job's result (the agent calls this)
    fit       ask Jev if a job matches the CV (the agent calls this)
    issue     report a problem during a run (the agent calls this)
    mcp       start the browser MCP (the agent CLI runs this)

options:
  -h, --help  show this help message and exit

first time:
  agy mcp add linkedin_playwright $PWD/.venv/bin/python -- $PWD/jobs.py mcp     (agy only)
  uv run jobs.py login                   log in to LinkedIn (Chrome); -b brave for Brave

every day:
  uv run jobs.py run --dry-run           preview the search from search.toml
  uv run jobs.py run                     apply with search.toml
  uv run jobs.py run "ai engineer" -l "San Francisco" -b brave --max-time 1.5 -v
  uv run jobs.py run --agent claude --model sonnet

more:  uv run jobs.py run -h    ·    README.md    ·    docs/filters.md
```
</details>

<details><summary><code>uv run jobs.py run -h</code></summary>

```
usage: jobs.py run [-h] [-l LOCATION] [--posted X[,Y]] [--experience X[,Y]] [--workplace X[,Y]]
                   [--job-type X[,Y]] [--sort X] [--distance MILES] [--under-10-applicants]
                   [--filter KEY=VALUE] [--max-applies N] [--max-time HOURS] [--max-pages N]
                   [-a {agy,claude,codex,pi}] [-m MODEL] [-b BROWSER] [--dry-run] [-v] [-q]
                   [position]

Search LinkedIn with Easy Apply only and apply to each job with a tailored CV.

options:
  -h, --help            show this help message and exit

search:
  position              search keywords; quote several words: "ai engineer" [now: python developer]
  -l, --location LOCATION
                        city, region or country: Valencia, "San Francisco" [now: Valencia]
  --posted X[,Y]        posted: 24h, week, month [now: week]
  --experience X[,Y]    experience: internship, entry, associate, mid-senior, director, executive [now:
                        internship,entry,associate]
  --workplace X[,Y]     workplace: onsite, remote, hybrid [now: none]
  --job-type X[,Y]      job type: full-time, part-time, contract, temporary, internship, volunteer, other
                        [now: full-time]
  --sort X              sort: recent, relevant [now: recent]
  --distance MILES      radius around --location: 10, 25, 50, 100 [now: none]
  --under-10-applicants
                        only jobs with fewer than 10 applicants
  --filter KEY=VALUE    raw LinkedIn URL param, repeatable: f_C=1441, geoId=105646813 (docs/filters.md)
                        [now: none]

limits (0 = unlimited):
  --max-applies N       stop after N successful applications [now: 10]
  --max-time HOURS      stop after HOURS (e.g. 1.5), finishing the current job first [now: 0]
  --max-pages N         result pages to go through, 25 jobs each [now: 3]

agent, browser and output:
  -a, --agent {agy,claude,codex,pi}
                        agent CLI that drives the browser and writes the CVs [now: agy]
  -m, --model MODEL     model for --agent; none = its default (gemini-3.8-flash-high for agy) [now: none]
  -b, --browser BROWSER
                        chrome, brave, edge, chromium or the path of any Chromium-based browser [now:
                        chrome]
  --dry-run             print the search URL and exit, no browser
  -v, --verbose         live log + debug: model timings, job URLs, render errors
  -q, --quiet           live log with application results only

Every option's default comes from search.toml ([now: ...] shows it); a flag overrides it for this run.
Stops at whichever comes first: --max-applies, --max-time, --max-pages, or no more results.
Ctrl+C stops at any time; processed jobs are skipped next run (jobs.db).

examples:
  uv run jobs.py run "python developer" -l Valencia
  uv run jobs.py run "ml engineer" -l "European Union" --workplace remote --posted 24h
  uv run jobs.py run "data scientist" -l Madrid --max-applies 0 --max-time 2 -q
  uv run jobs.py run "backend" -l Berlin --filter f_C=1441 --dry-run
  uv run jobs.py run "python developer" --agent pi --model openrouter/xiaomi/mimo-v2.6-pro
```
</details>

<details><summary><code>uv run jobs.py login -h</code></summary>

```
usage: jobs.py login [-h] [-b BROWSER]

Opens the browser on the agent's own profile (.browser-profile/<browser>/). Log in to LinkedIn and close the
window. Use this, not your normal browser: the agent can't read a login made there (macOS keychain).

options:
  -h, --help            show this help message and exit
  -b, --browser BROWSER
                        chrome, brave, edge, chromium or an executable path [now: chrome]
```
</details>

`fit`, `tailor`, `log`, `issue` and `mcp` are called by the agent, not by you; their `-h` says what they do.

## How it behaves

**Choosing jobs.** It takes every Easy Apply job in the results, in order, skipping any job that's
already logged or marked "Applied". For each one it runs `jobs.py fit`, which sends your `cv.yaml` and the job
description to Jev (`typesafe/jev-1.13`, [OpenRouter Decisions API](https://openrouter.ai/docs/guides/community/jev-tutorial),
~$0.00002 a call) with one yes/no question: does the CV meet the job's must-haves (stack, seniority, languages,
location or work permit)? Jev answers with a probability. Below 0.5 (`FIT_APPLY` in `jobs.py`) the job is logged
as `skipped` with note `not compatible (fit 0.23)`. Without `OPENROUTER_API_KEY`, or if the API fails (it's
alpha), the job is applied to anyway. The fit of every job is in `jobs.db`: raise the threshold if it lets bad
jobs through.

**CV tailoring** (`jobs.py tailor`). The agent (a text-only call, no tools) gets your `cv.yaml` and the job description and returns a new
YAML file. The rules it's given:

- It picks the job's 5-10 key keywords. Those your CV supports go first and loud, in the job's own wording:
  a summary rewritten for the role, skills reordered, **bold** in the bullets that prove them, the relevant
  bullets expanded and the rest cut. An AI/LLM job leads with your LLM, agent, RAG and voice-AI work; a
  LangChain + AWS job puts every LangChain and AWS fact up front in bold.
- Keywords your CV doesn't support never appear, not even as "familiar with": a Java job gets no Java, it
  gets your closest real strengths (backend, APIs, Python) instead.
- Generous wording, no invented facts: no new employers, dates, titles, tools, metrics, projects or
  credentials, no invented scale ("enterprise", "scalable", "proven"), and a tool that's only in your Skills
  isn't claimed for a job or project that doesn't mention it.
- Identity, `design`, `locale` and `settings` stay unchanged. The CV must fit on one page.

If RenderCV rejects the YAML, the error goes back to the agent for one retry. If that fails too, the agent
uploads `base_cv.pdf`. Before relying on it, spot-check a few `applications/*/CV.yaml` against `cv.yaml`.

**Form filling.** Answers come only from `answers.md` and `cv.yaml`. If a required question isn't
covered, the agent discards that application and logs it as `skipped` with the question as the note. It
unticks "Follow company" before submitting.

**Popups and getting stuck.** The agent closes unexpected popups (Premium ads, "Save this application?",
surveys...) with Escape or their close button, and handles native browser dialogs. If it's stuck on the same
job after 3 attempts, it logs that job as `failed` and moves on.

**Guardrails in the prompt.** The agent must never pay for anything, message anyone on LinkedIn, or edit your
profile or the project files, and it stops on a login page or captcha.

### Hiring team email

Experimental. After applying, `jobs.py log` checks the job's row: if Jev's fit is at least 0.9 (`FIT_EMAIL`)
and the job showed someone in "Meet the hiring team", it asks Hunter's Email Finder for that person's email
at the company. Only a verified email (status `valid` or score ≥ 90) is used; otherwise nothing happens. The
run's agent then writes the email (text-only call): the job's language, at most 300 characters, plain prose
with no lists or special characters, one or two real facts from your CV, a request for a short call. The
subject is `<job title> - <your name>` and the tailored PDF is attached.

Without `SMTP_HOST` in `.env` the email is only saved in `jobs.db`. Nobody is emailed twice: an address
that already got an email is skipped. Read what was written:

```bash
sqlite3 jobs.db "SELECT company, contact_email, email_body, email_sent FROM jobs WHERE email_body IS NOT NULL"
```

## Improving over time

```bash
# Which questions blocked applications? Add the answers to answers.md
sqlite3 jobs.db "SELECT note FROM jobs WHERE status = 'skipped'"

# What got applied, and how well it fit
sqlite3 jobs.db "SELECT company, title, fit FROM jobs WHERE status = 'applied' ORDER BY ts"
```

To retry a job, delete its row: `sqlite3 jobs.db "DELETE FROM jobs WHERE url LIKE '%/123/'"`.

## Risks and limits

- **Account risk.** Automated use breaks LinkedIn's terms and it can restrict accounts. Keep runs small
  (tens a day, not hundreds) and watch the first ones.
- **Shell access.** Every agent runs with its permission prompts off (`--dangerously-skip-permissions`,
  codex `--dangerously-bypass-approvals-and-sandbox`, pi has none), so it can run any shell command as you
  for the whole run. The prompt forbids editing files, but that's an instruction, not a
  sandbox.
- **Global MCP (agy).** `linkedin_playwright` is visible to every agy session. Remove it with
  `agy mcp remove linkedin_playwright`.
- **Easy Apply only.** Jobs that apply on the company's own site are filtered out by `f_AL=true`.
- **One page isn't checked.** Tailored CVs are asked to fit one page, but nothing verifies it. Check
  with `pdfinfo applications/*/CV.pdf | grep Pages`.
- **Cold email.** Emails go out automatically once SMTP is set. Read the first ones in `jobs.db` before
  adding `SMTP_HOST`.
- **Quota.** Each job is one long agent turn plus one tailoring call, against the chosen agent's quota or API bill.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Anything odd | Open the run's `logs/<date-time>/browser/session-*/session.md` ([Debug logs](#debug-logs)) |
| Agent says it's not logged in, or hits a captcha | Re-run `uv run jobs.py login` and log in |
| Agent sees a login page right after `login` | You logged in by opening the browser yourself: use `uv run jobs.py login --browser <same one>` |
| "Profile in use" / browser won't start | Close the `login` window, or any window using `.browser-profile/<browser>/` |
| Upload fails or the path is refused | Add `"--allow-unrestricted-file-access"` to the `npx` args in `mcp()` in `jobs.py` |
| Preflight says the MCP is missing or outdated | Run the `agy mcp remove/add` command it prints |
| Many `skipped` entries | Add the logged questions to `answers.md` |
| A filter doesn't apply | Set it by hand on LinkedIn, copy the param from the URL, pass it with `--filter` |
| `CV render failed` | Check `applications/<job>/CV.yaml`, and check that `cv.yaml` itself renders |
