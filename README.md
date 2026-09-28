# linkedin-auto

Applies to LinkedIn **Easy Apply** jobs automatically, with a CV tailored to each job.

A [LangChain](https://docs.langchain.com) agent, running an [OpenRouter](https://openrouter.ai) model (`~deepseek/deepseek-flash-latest` by default, see [Model](#model)), drives your own browser (Chrome by default; Brave, Edge or any Chromium-based one) through Playwright MCP. It searches
LinkedIn with your filters, then for each job it:

1. reads the job description,
2. generates a tailored CV from your `cv.yaml` (a separate text-only call to the same agent, rendered with RenderCV),
3. opens Easy Apply, uploads that PDF, fills the form from `answers.md`, submits,
4. logs the result in `applied.jsonl`, and reports any problem it hits to `issues.log`.

It stops when it reaches `--max-applies`, `--max-time` or `--max-pages`, or when the results run out.

Quick help: `uv run jobs.py -h` (see [Command reference](#command-reference)).

```
jobs.py run ──► LangChain agent (OpenRouter model) ──► Playwright MCP (npx) ──► your browser (.browser-profile/<browser>/)
                  │  tools, in-process:
                  ├─► tailor_cv ──► model, text only ──► cv yaml ──► rendercv ──► CV.pdf
                  ├─► log_job ──► applied.jsonl  (returns LIMIT REACHED to stop)
                  └─► report_issue ──► issues.log
```

## Files

| Path | What it is | You edit it? |
|---|---|---|
| `jobs.py` | Everything: the `run`, `login` and `tailor` commands, the agent, its tools and its prompt | Rarely |
| `cv.yaml` | Your base CV in RenderCV format. Its `design`, `locale` and `settings` are kept in every tailored CV | **Yes** |
| `answers.md` | Answers to Easy Apply questions. The agent only uses this file and `cv.yaml` | **Yes** |
| `search.toml` | Default search: position, location, limits, filters, agent, browser | **Yes** |
| `base_cv.pdf` | Untailored CV, uploaded when tailoring fails. Regenerate it after editing `cv.yaml` | No |
| `docs/filters.md` | Every search filter and its LinkedIn URL code | — |
| `applications/<company-title>/` | Per job: `CV.yaml` and `CV.pdf` | Generated |
| `applied.jsonl` | One line per processed job: `ts, company, title, url, status, note` | Generated |
| `.browser-profile/<browser>/` | One profile per browser, holding your LinkedIn login | Generated |
| `issues.log` | Problems the agent reported, all runs: `date time problem  job-url` | Generated |
| `run.log` | Every event of every run, the source of the live log | Generated |
| `logs/<date-time>/` | Everything about one run, for debugging (see [Debug logs](#debug-logs)) | Generated |

Everything generated is gitignored. It contains personal data, so keep it that way.

## Setup (once)

Requirements: macOS, `uv`, `node`/`npx`, a Chromium-based browser (Chrome, Brave, Edge...), and an
[OpenRouter API key](https://openrouter.ai/keys) in `OPENROUTER_API_KEY`. Nothing gets downloaded for the browser: it uses the one you have.

```bash
uv sync                                   # langchain, langchain-openrouter, rendercv
export OPENROUTER_API_KEY=sk-or-...       # put it in your shell profile

# Log in to LinkedIn once per browser you want to use. Close the window when done.
uv run jobs.py login                      # Chrome
uv run jobs.py login --browser brave      # Brave, in its own profile
```

Then:

1. Put your CV in `cv.yaml` (RenderCV YAML) and check it renders:
   `uv run rendercv render cv.yaml --pdf-path base_cv.pdf -nomd -nohtml -nopng`
2. Fill every `TODO` in `answers.md`.

**Credentials:** none are stored in the project (the OpenRouter key comes from the environment). The login lives in the browser profile as cookies,
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

### Model

`--model` / `model` takes any [OpenRouter model id](https://openrouter.ai/models); it must support tool calling.
The default, `~deepseek/deepseek-flash-latest`, always points at DeepSeek's latest Flash model. The same model drives
the browser, tailors the CVs and fills `answers.md`.

The agent is LangChain's `create_agent` with the Playwright MCP tools (through `langchain.mcp.MCPAdapter`) plus
three of its own: `tailor_cv`, `log_job` and `report_issue`. It has no shell and no file access.

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
uv run jobs.py run --model anthropic/claude-sonnet-4.6
```

| `search.toml` key / flag | Meaning |
|---|---|
| `position` / 1st argument | Search keywords. Quote multi-word values: `"AI engineer"` |
| `location` / `-l`, `--location` | City, region or country: `-l "San Francisco"` |
| `model` / `-m`, `--model` | OpenRouter model id, see [Model](#model). Default `~deepseek/deepseek-flash-latest` |
| `browser` / `-b`, `--browser` | Browser to drive, see [Browsers](#browsers). Default `chrome` |
| `max_applies` / `--max-applies N` | Stop after N **successful** applications this run. `0` = unlimited |
| `max_time` / `--max-time H` | Stop after H hours (e.g. `1.5`); the agent finishes the current job first, and is killed 10 min later if it doesn't stop. `0` = unlimited |
| `max_pages` / `--max-pages N` | Result pages to go through (25 jobs each). `0` = until results run out |
| `posted`, `experience`, `workplace`, `job_type`, `sort`, `distance`, `under_10_applicants`, `filter` | Search filters, see [docs/filters.md](docs/filters.md). A `filter` given as a flag is added to the list in the file |
| `--dry-run` | Print the search URL and exit |
| `-v` / `-q` | Live progress log: `-v` adds timings, URLs and render errors, `-q` shows only application results. Everything also goes to `run.log` |

A typo'd key or an invalid value in `search.toml` stops the run with an error rather than being silently ignored.

Before the browser starts, `run` does a quick preflight: `OPENROUTER_API_KEY` is set, `npx` is installed,
the browser exists and you have logged in with it once, and
`cv.yaml` renders. It re-renders `base_cv.pdf` if
`cv.yaml` changed. If `answers.md` still has TODOs, it lists them and asks:
`[f]` fill what your CV answers with the model (shows a diff, asks before saving, keeps `answers.md.bak`),
`[c]` continue anyway, or `[q]` quit.

### Live log

While the agent works, `run` prints what's happening, in color:

```
22:16:32 searching "ai engineer" in San Francisco, stop after 2 applies
22:16:32 agent started (~deepseek/deepseek-flash-latest, chrome), browsing LinkedIn... (-v for more detail)
22:17:05 tailoring CV: ACME — AI Engineer
22:17:29 CV ready in 24s: applications/acme-ai-engineer/CV.pdf
22:18:40 applied: ACME — AI Engineer  [1/2 applied]
22:19:02 skipped: Foo — ML Engineer (required: German)  [1/2 applied]
22:25:13 agent finished (exit 0): 2 applied this run
```

Green = applied, yellow = skipped or warning, red = failed or error. `-v` adds grey debug lines (model timings,
job URLs, RenderCV errors), `-q` keeps only the application results. Events go through `run.log`, which `run` follows; the file keeps every event of every run. Between
"agent started" and the first job it's quiet: that's the agent browsing LinkedIn. The agent's own summary is
printed when it finishes.

`Ctrl+C` stops at any time. Jobs already logged are skipped on the next run, and so are jobs LinkedIn
marks as "Applied".

### Debug logs

Every `run` writes a folder `logs/<YYYYMMDD-HHMMSS>/`, whose path is printed at the end:

| File | What it is | Look here when |
|---|---|---|
| `prompt.txt` | The command line and the exact prompt the agent got | The agent ignored an instruction or a filter |
| `agent.txt` | The agent's transcript: every model message and tool call/result | It stopped early or said something odd |
| `events.log` | This run's events (all levels, including debug) | You want the timeline |
| `browser/session-*/session.md` | Every browser action the agent took, with its arguments and the page snapshot it got back | It got stuck on a popup or clicked the wrong thing |

Problems the agent noticed itself are also in `issues.log`, one line each across all runs, and show up
as yellow `issue:` lines in the live log. The agent reports them with its `report_issue` tool
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
A LangChain agent (an OpenRouter model) drives your browser; answers come from answers.md, the CV from cv.yaml.
Problems the agent hits go to issues.log; everything about a run goes to logs/<date-time>/.

positional arguments:
  command
    run       search LinkedIn and apply (the main command)
    login     log in to LinkedIn in the browser the agent uses (once per browser)
    tailor    write a CV for one job (the agent does this for each job)

options:
  -h, --help  show this help message and exit

first time:
  export OPENROUTER_API_KEY=sk-or-...    in your shell profile
  uv run jobs.py login                   log in to LinkedIn (Chrome); -b brave for Brave

every day:
  uv run jobs.py run --dry-run           preview the search from search.toml
  uv run jobs.py run                     apply with search.toml
  uv run jobs.py run "ai engineer" -l "San Francisco" -b brave --max-time 1.5 -v
  uv run jobs.py run --model anthropic/claude-sonnet-4.6

more:  uv run jobs.py run -h    ·    README.md    ·    docs/filters.md
```
</details>

<details><summary><code>uv run jobs.py run -h</code></summary>

```
usage: jobs.py run [-h] [-l LOCATION] [--posted X[,Y]] [--experience X[,Y]] [--workplace X[,Y]]
                   [--job-type X[,Y]] [--sort X] [--distance MILES] [--under-10-applicants]
                   [--filter KEY=VALUE] [--max-applies N] [--max-time HOURS] [--max-pages N] [-m MODEL]
                   [-b BROWSER] [--dry-run] [-v] [-q]
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

model, browser and output:
  -m, --model MODEL     OpenRouter model id that drives the browser and writes the CVs [now:
                        ~deepseek/deepseek-flash-latest]
  -b, --browser BROWSER
                        chrome, brave, edge, chromium or the path of any Chromium-based browser [now:
                        chrome]
  --dry-run             print the search URL and exit, no browser
  -v, --verbose         live log + debug: model timings, job URLs, render errors
  -q, --quiet           live log with application results only

Every option's default comes from search.toml ([now: ...] shows it); a flag overrides it for this run.
Stops at whichever comes first: --max-applies, --max-time, --max-pages, or no more results.
Ctrl+C stops at any time; processed jobs are skipped next run (applied.jsonl).

examples:
  uv run jobs.py run "python developer" -l Valencia
  uv run jobs.py run "ml engineer" -l "European Union" --workplace remote --posted 24h
  uv run jobs.py run "data scientist" -l Madrid --max-applies 0 --max-time 2 -q
  uv run jobs.py run "backend" -l Berlin --filter f_C=1441 --dry-run
  uv run jobs.py run "python developer" --model google/gemini-3-flash-preview
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

`uv run jobs.py tailor --company X --title Y --jd jd.txt` writes one CV by hand, the same way the agent does for each job.

## How it behaves

**Choosing jobs.** It takes every Easy Apply job in the results, in order, skipping any job that's
already logged or marked "Applied". There's no fit scoring: narrow the search with filters.

**CV tailoring** (`tailor_cv` tool). The model (a plain text call, no tools) gets your `cv.yaml` and the job description and returns a new
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

If RenderCV rejects the YAML, the error goes back to the model for one retry. If that fails too, the agent
uploads `base_cv.pdf`. Before relying on it, spot-check a few `applications/*/CV.yaml` against `cv.yaml`.

**Form filling.** Answers come only from `answers.md` and `cv.yaml`. If a required question isn't
covered, the agent discards that application and logs it as `skipped` with the question as the note. It
unticks "Follow company" before submitting.

**Popups and getting stuck.** The agent closes unexpected popups (Premium ads, "Save this application?",
surveys...) with Escape or their close button, and handles native browser dialogs. If it's stuck on the same
job after 3 attempts, it logs that job as `failed` and moves on.

**Guardrails in the prompt.** The agent must never pay for anything, message anyone, or edit your
profile, and it stops on a login page or captcha.

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
- **Browser code.** Playwright MCP includes `browser_evaluate` and `browser_run_code_unsafe`, so the agent can run
  JavaScript in the page. It has no shell or file access of its own.
- **Easy Apply only.** Jobs that apply on the company's own site are filtered out by `f_AL=true`.
- **One page isn't checked.** Tailored CVs are asked to fit one page, but nothing verifies it. Check
  with `pdfinfo applications/*/CV.pdf | grep Pages`.
- **Cost.** Each job is many model calls (every browser step) plus one tailoring call, billed to your OpenRouter account.
- **Long runs.** Old page snapshots are cleared from the agent's context to keep it small; the `log_job` results stay.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Anything odd | Open the run's `logs/<date-time>/browser/session-*/session.md` ([Debug logs](#debug-logs)) |
| Agent says it's not logged in, or hits a captcha | Re-run `uv run jobs.py login` and log in |
| Agent sees a login page right after `login` | You logged in by opening the browser yourself: use `uv run jobs.py login --browser <same one>` |
| "Profile in use" / browser won't start | Close the `login` window, or any window using `.browser-profile/<browser>/` |
| Upload fails or the path is refused | Add `"--allow-unrestricted-file-access"` to the `npx` args in `agent_loop()` in `jobs.py` |
| Many `skipped` entries | Add the logged questions to `answers.md` |
| A filter doesn't apply | Set it by hand on LinkedIn, copy the param from the URL, pass it with `--filter` |
| `CV render failed` | Check `applications/<job>/CV.yaml`, and check that `cv.yaml` itself renders |
