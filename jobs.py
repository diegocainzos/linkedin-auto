"""LinkedIn Easy Apply automation: a LangChain agent (OpenRouter model) + Playwright MCP, with a tailored RenderCV CV per job.

  uv run jobs.py login --browser brave                                    # once per browser
  uv run jobs.py run "python developer" -l Valencia --max-applies 5 -v    # start the agent
  uv run jobs.py tailor --company X --title Y --jd jd.txt                  # one CV by hand
"""
import argparse, asyncio, datetime, difflib, json, os, re, shutil, subprocess, sys, threading, time, tomllib, warnings
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode

warnings.filterwarnings("ignore", "Core Pydantic V1")  # langchain-core on py3.14, harmless
from langchain_core._api import suppress_langchain_beta_warning

with suppress_langchain_beta_warning():  # langchain.mcp is beta
    from langchain.mcp import MCPAdapter
from langchain.agents import create_agent
from langchain.agents.middleware import ClearToolUsesEdit, ContextEditingMiddleware
from langchain.tools import tool
from langchain_openrouter import ChatOpenRouter

ROOT = Path(__file__).parent.resolve()
BASE_CV, ANSWERS, LOG, APPS, SEARCH, EVENTS, LOGS = (ROOT / p for p in (
    "cv.yaml", "answers.md", "applied.jsonl", "applications", "search.toml", "run.log", "logs"))
MODEL = "~deepseek/deepseek-flash-latest"  # OpenRouter model id
ISSUES = ROOT / "issues.log"
BROWSERS = {  # macOS; any other Chromium-based browser works by passing its executable path
    "chrome": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "brave": "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "edge": "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "chromium": "/Applications/Chromium.app/Contents/MacOS/Chromium",
}


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def logged() -> list[dict]:
    out = []
    for l in LOG.read_text().splitlines() if LOG.exists() else []:
        try:
            out.append(json.loads(l))
        except ValueError:  # half-written line: skip rather than brick every later run
            pass
    return out


def job_url(u: str) -> str:  # canonical /jobs/view/<id>/ so the skip list matches however the agent copied it
    m = re.search(r"(?:currentJobId=|/jobs/view/(?:[^/?]*-)?)(\d+)", u)
    return f"https://www.linkedin.com/jobs/view/{m[1]}/" if m else u


def applied_since(started: float) -> int:
    return sum(1 for e in logged() if e["status"] == "applied" and e.get("ts", 0) >= started)


COLORS = {"ok": 32, "warn": 33, "err": 31, "info": 36, "debug": 90}


def say(msg: str, kind: str = "info") -> None:
    """Append an event to run.log. `run` prints it live, even when the agent is the caller."""
    with EVENTS.open("a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {kind} {' | '.join(msg.strip().splitlines())}\n")


def follow(stop: threading.Event, level: int, offset: int) -> None:
    """Print new run.log events until `stop`. level 0 = results, 1 = + progress, 2 = + debug."""
    shown = ("ok", "warn", "err", "info", "debug")[:3 + level]
    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    buf = ""
    with EVENTS.open("a+") as f:
        f.seek(offset)
        while True:
            buf += f.readline()
            if not buf.endswith("\n"):  # nothing new, or a half-written line
                if stop.is_set():
                    return
                time.sleep(0.3)
                continue
            ts, kind, msg = (buf.rstrip("\n").split(" ", 2) + ["", ""])[:3]
            buf = ""
            if kind in shown:
                print(f"\033[2m{ts}\033[0m \033[{COLORS[kind]}m{msg}\033[0m" if color else f"{ts} {msg}", flush=True)


def llm(model: str) -> ChatOpenRouter:  # reads OPENROUTER_API_KEY
    return ChatOpenRouter(model=model or MODEL, max_retries=3, request_timeout=300_000)  # ms


def ask(prompt: str, model: str) -> str:
    return llm(model).invoke(prompt).text


TAILOR_PROMPT = """You tailor a RenderCV v2 YAML CV to a job description, to pass ATS keyword filters and impress a recruiter in 10 seconds.

First, silently pick the 5-10 most important keywords of the job (stack, tools, domain, seniority signals).
Then sell hard the parts of the base CV that match them:
- Keywords the base CV truthfully supports: use the job's exact wording, put them first and make them loud:
  in the summary's first sentence, first in the skills, and in **bold** in the bullets that prove them.
- Rewrite the Professional Summary for this job: open with the job's role and its top matching keywords.
- Reorder sections, entries, bullets and skills so the most relevant come first. Expand the relevant bullets with
  more concrete detail that the base CV already implies (what was built, with what, for whom); shorten or drop the rest.
  E.g. an AI/LLM job leads with LLMs, agents, RAG, the voice agent, local model serving and AI projects;
  a job asking for LangChain and AWS puts every LangChain and AWS fact up front and in bold.
- Keywords the base CV does NOT support (e.g. Java for someone who never used Java): never write them anywhere,
  not in skills, summary or bullets, not as "familiar with" or "learning". Instead emphasize the closest true
  strengths (e.g. backend development, OOP, APIs in Python).
- Embellish generously in wording, never in facts: never invent employers, dates, titles, technologies, tools,
  metrics, projects or credentials, and never raise a level of expertise the base CV doesn't state.
  No invented scale, clients or results either ("enterprise", "high-throughput", "scalable", "proven", "led",
  user counts) unless the base CV says so. A tool listed only under Skills stays a skill: don't claim it
  was used in a job or project that doesn't mention it.
- Keep `cv` identity/contact data and the `design`, `locale`, `settings` blocks unchanged.
- Must fit on ONE page: cut the least relevant content. One entry type per section, valid RenderCV schema.
Output ONLY the full YAML, no code fences, no commentary.

<base_cv>
{cv}
</base_cv>

<job title="{title}" company="{company}">
{jd}
</job>
{error}"""


def render(yml: Path, pdf: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "rendercv", "render", str(yml), "--pdf-path", str(pdf),
                           "-nomd", "-nohtml", "-nopng"], capture_output=True, text=True, cwd=pdf.parent,
                          env=os.environ | {"COLUMNS": "200", "NO_COLOR": "1"})  # no -q: it hides the error


def tailor(company: str, title: str, jd: str, model: str) -> tuple[bool, str]:
    """Tailored CV for one job. Returns (ok, message); the message's last line is always a PDF path to upload."""
    out = APPS / slug(f"{company}-{title}")
    out.mkdir(parents=True, exist_ok=True)
    yml, pdf, error = out / "CV.yaml", out / "CV.pdf", ""
    say(f"tailoring CV: {company} — {title}")
    start = time.time()
    for attempt in (1, 2):  # one retry with the render error fed back
        t = time.time()
        try:
            reply = ask(TAILOR_PROMPT.format(cv=BASE_CV.read_text(), title=title, company=company, jd=jd, error=error), model)
        except Exception as e:  # quota/auth/network: a retry won't help
            say(f"model call failed: {e}", "err")
            break
        say(f"attempt {attempt}: {model or MODEL} replied in {time.time() - t:.0f}s, {len(reply)} chars (jd {len(jd)} chars)", "debug")
        fenced = re.search(r"```\w*\n(.*?)\n```", reply, re.S)  # models like to wrap YAML in prose + fences
        yml.write_text((fenced[1] if fenced else reply).strip() + "\n")
        r = render(yml, pdf)
        if r.returncode == 0 and pdf.exists():
            say(f"CV ready in {time.time() - start:.0f}s: {pdf.relative_to(ROOT)}")
            return True, str(pdf)
        error = f"\nYour previous YAML failed to render, fix it:\n{(r.stdout + r.stderr)[-3000:]}"
        say(f"attempt {attempt}: RenderCV rejected the YAML", "warn")
        say((r.stdout + r.stderr)[-800:], "debug")
    say("tailoring failed, the agent will upload base_cv.pdf", "warn")
    return False, f"CV tailoring failed, upload the base CV instead:\n{ROOT / 'base_cv.pdf'}"


def log(company: str, title: str, url: str, status: str, note: str, started: float, max_applies: int, hours: float) -> str:
    torn = LOG.exists() and LOG.stat().st_size and not LOG.read_bytes().endswith(b"\n")
    with LOG.open("a") as f:
        f.write("\n" * bool(torn) + json.dumps({"ts": time.time(), "company": company, "title": title, "status": status,
                                                "note": note, "url": job_url(url)}) + "\n")
    n = applied_since(started)
    out_of_time = hours and time.time() >= started + hours * 3600
    say(f"{status}: {company} — {title}" + (f" ({note})" if note else "") + f"  [{n}/{max_applies or '∞'} applied]",
        {"applied": "ok", "skipped": "warn", "failed": "err"}[status])
    say(job_url(url), "debug")
    return f"{n} applied this run." + (" LIMIT REACHED: stop now and summarize." if max_applies and n >= max_applies or out_of_time else "")


def issue(text: str, url: str = "") -> str:
    """The agent reports a problem it hit: one line in issues.log (all runs), and a warning in the live log."""
    url = f"  {job_url(url)}" if url else ""
    with ISSUES.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(text.split())}{url}\n")
    say(f"issue: {text}{url}", "warn")
    return "noted, carry on"


SYSTEM = """You apply to LinkedIn jobs with Easy Apply. Drive the browser with the browser_* tools.

For each search results page (search URL + &start=0, 25, 50, ...), for each job card:
1. Skip cards marked "Applied" and URLs in the already-processed list.
2. Open the job and read the full description (click "see more" if present).
3. Call tailor_cv with the company, title and full description text (~1 min). The last line it returns is the absolute PDF path to upload.
4. Click "Easy Apply". At the resume step click "Upload resume", then call browser_file_upload with that PDF path, and make sure it is selected.
5. Fill every step truthfully from <candidate_answers> and <cv>. Never invent facts.
   If a required question cannot be answered truthfully from them, discard the application and log it as skipped with the question as note.
6. Uncheck "Follow company" if present, Submit, dismiss the confirmation dialog.
7. Call log_job with company, title, job url (e.g. https://www.linkedin.com/jobs/view/123/), status and a short note.
   If it returns LIMIT REACHED, stop immediately.

Unexpected popups (premium ads, "Save this application?", surveys, cookie banners, "Share your profile?", tooltips):
- Close them: press Escape, or click Dismiss / Close / X / "Not now" / "No thanks" / "Discard". Never click the upsell button.
- Native browser dialogs (alert/confirm/beforeunload): use browser_handle_dialog.
- If an action has no effect, take a fresh snapshot before retrying; the page probably changed.
If you are stuck on the same job after 3 attempts, re-open the search URL, log that job as failed with what blocked you as note, and move on.

Report every execution problem as soon as it happens with report_issue (what happened, where, what you tried; job url if any), then carry on.
Problems = a tool error, a popup you couldn't close, a page that didn't load or looked wrong, a form field you
couldn't fill, an upload that failed, a filter that wasn't applied, anything unexpected. Not needed for normal skips.

On the first results page, check the filter pills at the top show the requested filters as active; if one is not, say which and continue.
If LinkedIn shows a login page, a captcha or a security check, stop and say so.
Never click anything that pays, messages people, or edits the LinkedIn profile.
Keep snapshots small: prefer targeted clicks over repeated full-page snapshots."""


# Human-readable filter values -> LinkedIn search URL codes. See docs/filters.md.
FILTERS = {
    "posted": ("f_TPR", {"24h": "r86400", "week": "r604800", "month": "r2592000"}),
    "experience": ("f_E", {"internship": "1", "entry": "2", "associate": "3", "mid-senior": "4", "director": "5", "executive": "6"}),
    "workplace": ("f_WT", {"onsite": "1", "remote": "2", "hybrid": "3"}),
    "job_type": ("f_JT", {"full-time": "F", "part-time": "P", "contract": "C", "temporary": "T",
                          "internship": "I", "volunteer": "V", "other": "O"}),
    "sort": ("sortBy", {"recent": "DD", "relevant": "R"}),
}


def search_url(a) -> str:
    q = {"keywords": a.position, "location": a.location, "f_AL": "true"}  # f_AL = Easy Apply only
    for name, (key, codes) in FILTERS.items():
        if v := getattr(a, name):
            q[key] = ",".join(codes[x] for x in v.split(","))  # multi-select = comma list
    if a.distance:
        q["distance"] = a.distance
    if a.under_10_applicants:
        q["f_JIYN"] = "true"
    q.update(kv.split("=", 1) for kv in a.filter)
    return "https://www.linkedin.com/jobs/search/?" + urlencode({k: v for k, v in q.items() if v})


FILL_PROMPT = """Today is {today}.
Below is a job-application answers file with TODO placeholders, and the candidate's CV (RenderCV YAML).
Replace a TODO only when the CV states the answer or it follows directly from it (years of experience from dates: integers, rounded down).
Leave every other TODO exactly as is: never guess preferences, salary, notice period, relocation, consents or legal answers.
Change nothing else. Output ONLY the full file, no code fences.

<answers_md>
{answers}
</answers_md>

<cv>
{cv}
</cv>"""


def preflight(a) -> None:
    """Cheap checks before the browser starts. Exits on anything that would break the run."""
    problems, name = [], a.browser
    if not os.environ.get("OPENROUTER_API_KEY"):
        problems.append("OPENROUTER_API_KEY is not set: export it (https://openrouter.ai/keys)")
    if not shutil.which("npx"):
        problems.append("npx not found on PATH: install node, it runs Playwright MCP")
    exe, profile = browser(name)
    if not Path(exe).exists():
        problems.append(f"browser {exe} not found: pass --browser {'|'.join(BROWSERS)} or an executable path")
    elif not profile.is_dir():
        problems.append(f"not logged in with {name} yet: run `uv run jobs.py login --browser {name}` once")
    for f in (BASE_CV, ANSWERS):
        if not f.exists():
            problems.append(f"{f.name} is missing")
    base = ROOT / "base_cv.pdf"
    if BASE_CV.exists() and (not base.exists() or base.stat().st_mtime < BASE_CV.stat().st_mtime):
        print("preflight: cv.yaml changed, re-rendering base_cv.pdf")
        if (r := render(BASE_CV, base)).returncode:
            problems.append(f"cv.yaml does not render:\n{(r.stdout + r.stderr)[-1500:]}")
    if problems:
        sys.exit("preflight failed:\n- " + "\n- ".join(problems))
    while todos := [l.strip() for l in ANSWERS.read_text().splitlines() if "TODO" in l]:
        print(f"\nanswers.md still has {len(todos)} TODOs. Jobs that ask about them get skipped:")
        print("\n".join(f"  {t}" for t in todos[:10]) + ("\n  ..." if len(todos) > 10 else ""))
        if not sys.stdin.isatty():
            return
        c = input(f"[f] fill what the CV answers with {a.model}  [c] continue anyway  [q] quit > ").strip().lower()
        if c == "c":
            return
        if c != "f":
            sys.exit(0)
        old = ANSWERS.read_text()
        print(f"asking {a.model} (~30s)...")
        try:
            new = ask(FILL_PROMPT.format(today=datetime.date.today(), answers=old, cv=BASE_CV.read_text()), a.model).strip() + "\n"
        except Exception as e:
            print(f"model call failed, answers.md unchanged: {e}")
            continue
        if "## Agent policy" not in new:  # empty or mangled reply: never overwrite
            print(f"{a.model} reply unusable, answers.md unchanged.")
            continue
        diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), "answers.md", "filled", lineterm="", n=0))
        if not diff:
            print(f"{a.model} found nothing in the CV for the remaining TODOs.")
            continue
        print("\n".join(diff))
        if input("Save these changes? (old copy -> answers.md.bak) [y/N] > ").strip().lower() == "y":
            (ROOT / "answers.md.bak").write_text(old)
            ANSWERS.write_text(new)


async def agent_loop(a, prompt: str, out: Path, started: float) -> None:
    """The agent: OpenRouter model + Playwright MCP browser tools + our own tools. Transcript -> out/agent.txt."""
    exe, profile = browser(a.browser)
    pw = {"command": "npx", "args": ["-y", "@playwright/mcp@latest", "--executable-path", exe, "--user-data-dir", str(profile),
                                     "--save-session", "--output-dir", str(out / "browser")]}  # every tool call + page snapshot

    @tool
    def tailor_cv(company: str, title: str, job_description: str) -> str:
        """Write a CV tailored to this job (~1 min). The last line returned is the absolute PDF path to upload."""
        return tailor(company, title, job_description, a.model)[1]

    @tool
    def log_job(company: str, title: str, url: str, status: Literal["applied", "skipped", "failed"], note: str = "") -> str:
        """Record a processed job's result. Says LIMIT REACHED when the run must stop."""
        return log(company, title, url, status, note, started, a.max_applies, a.max_time)

    @tool
    def report_issue(text: str, url: str = "") -> str:
        """Report an execution problem: what happened, where, what you tried; the job url if any."""
        return issue(text, url)

    async with MCPAdapter({"mcpServers": {"browser": pw}}) as mcp:
        agent = create_agent(llm(a.model), [*await mcp.list_tools(), tailor_cv, log_job, report_issue], system_prompt=prompt,
                             # ponytail: old page snapshots are the bulk of the context; clearing them keeps a run
                             # going for hours. Add SummarizationMiddleware if the AI messages alone ever overflow.
                             middleware=[ContextEditingMiddleware(edits=[ClearToolUsesEdit(exclude_tools=("log_job",))])])
        last = None
        with (out / "agent.txt").open("w") as f:
            async for step in agent.astream({"messages": [{"role": "user", "content": "Start."}]},
                                            {"recursion_limit": 100_000}, stream_mode="updates"):
                for update in step.values():
                    for m in (update or {}).get("messages", []):
                        f.write(m.pretty_repr() + "\n")
                        f.flush()
                        last = m
        print(last.text if last else "")  # the agent's final summary


def run(a) -> int:
    url = search_url(a)
    print(url)
    if a.dry_run:
        return 0
    preflight(a)
    started, offset = time.time(), EVENTS.stat().st_size if EVENTS.exists() else 0
    out = LOGS / time.strftime("%Y%m%d-%H%M%S")  # everything about this run, for debugging afterwards
    out.mkdir(parents=True)
    stop = threading.Event()
    tail = threading.Thread(target=follow, args=(stop, 1 + a.verbose - a.quiet, offset), daemon=True)
    tail.start()
    say(f"searching \"{a.position}\" in {a.location or 'anywhere'}, stop after {a.max_applies or '∞'} applies"
        + (f" or {a.max_time:g}h" if a.max_time else ""))
    say(url, "debug")
    pages = f"at most {a.max_pages} results pages" if a.max_pages else "all results pages until there are no more"
    prompt = (SYSTEM +
              f"\n\nSearch URL: {url}\nProcess {pages}.\n"
              f"Already processed job URLs: {json.dumps([e['url'] for e in logged()][-300:])}\n\n"
              f"<candidate_answers>\n{ANSWERS.read_text()}\n</candidate_answers>\n\n<cv>\n{BASE_CV.read_text()}\n</cv>")
    say(f"agent started ({a.model}, {a.browser}), browsing LinkedIn... (-v for more detail)")
    say(f"prompt: {len(prompt)} chars", "debug")
    (out / "prompt.txt").write_text(" ".join(sys.argv) + "\n\n" + prompt)
    rc = 1
    try:
        # the soft limit is LIMIT REACHED from log_job; stop 10 min later in case the agent never logs again
        asyncio.run(asyncio.wait_for(agent_loop(a, prompt, out, started), a.max_time * 3600 + 600 if a.max_time else None))
        rc = 0
    except TimeoutError:
        say(f"--max-time {a.max_time:g}h passed and the agent didn't stop: stopped it", "warn")
    except Exception as e:
        say(f"agent crashed: {e!r}", "err")
    finally:  # also on Ctrl+C
        say(f"agent finished (exit {rc}): {applied_since(started)} applied this run", "ok" if rc == 0 else "err")
        say(f"debug logs: {out}/")
        stop.set()
        tail.join(timeout=2)
        with EVENTS.open() as f:
            f.seek(offset)
            (out / "events.log").write_text(f.read())
    return rc


def browser(name: str) -> tuple[str, Path]:
    """Executable + its own profile dir: a profile isn't portable between browsers."""
    return BROWSERS.get(name, name), ROOT / ".browser-profile" / slug(Path(name).stem)


def login(a) -> int:
    exe, profile = browser(a.browser)
    if not Path(exe).exists():
        sys.exit(f"{exe} not found: pass one of {', '.join(BROWSERS)} or an executable path")
    print(f"Log in to LinkedIn, then close the {a.browser} window. Profile: {profile.relative_to(ROOT)}")
    # --use-mock-keychain like Playwright: cookies saved with the real macOS keychain are unreadable to the agent
    return subprocess.run([exe, "--use-mock-keychain", "--no-first-run", f"--user-data-dir={profile}",
                           "https://www.linkedin.com/login"], capture_output=True).returncode


def tailor_cmd(a) -> int:
    try:
        jd = Path(a.jd).read_text()
    except OSError as e:
        sys.exit(str(e))
    ok, msg = tailor(a.company, a.title, jd, a.model)
    print(msg)
    return 0 if ok else 1


def choices(codes):
    def check(v):
        v = ",".join(x.strip() for x in v.split(","))
        if bad := [x for x in v.split(",") if x not in codes]:
            raise argparse.ArgumentTypeError(f"{bad} not in: {', '.join(codes)}")
        return v
    return check


def key_value(v):
    if "=" not in v:
        raise argparse.ArgumentTypeError(f"{v!r} is not KEY=VALUE")
    return v


HELP = """Applies to LinkedIn Easy Apply jobs for you, uploading a CV tailored to each job.
A LangChain agent (an OpenRouter model) drives your browser; answers come from answers.md, the CV from cv.yaml.
Problems the agent hits go to issues.log; everything about a run goes to logs/<date-time>/."""

HELP_EPILOG = """first time:
  export OPENROUTER_API_KEY=sk-or-...    in your shell profile
  uv run jobs.py login                   log in to LinkedIn (Chrome); -b brave for Brave

every day:
  uv run jobs.py run --dry-run           preview the search from search.toml
  uv run jobs.py run                     apply with search.toml
  uv run jobs.py run "ai engineer" -l "San Francisco" -b brave --max-time 1.5 -v
  uv run jobs.py run --model anthropic/claude-sonnet-4.6

more:  uv run jobs.py run -h    ·    README.md    ·    docs/filters.md"""

RUN_EPILOG = """Every option's default comes from search.toml ([now: ...] shows it); a flag overrides it for this run.
Stops at whichever comes first: --max-applies, --max-time, --max-pages, or no more results.
Ctrl+C stops at any time; processed jobs are skipped next run (applied.jsonl).

examples:
  uv run jobs.py run "python developer" -l Valencia
  uv run jobs.py run "ml engineer" -l "European Union" --workplace remote --posted 24h
  uv run jobs.py run "data scientist" -l Madrid --max-applies 0 --max-time 2 -q
  uv run jobs.py run "backend" -l Berlin --filter f_C=1441 --dry-run
  uv run jobs.py run "python developer" --model google/gemini-3-flash-preview"""


def main():
    fmt = argparse.RawDescriptionHelpFormatter
    p = argparse.ArgumentParser(prog="jobs.py", description=HELP, epilog=HELP_EPILOG, formatter_class=fmt)
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")
    r = sub.add_parser("run", help="search LinkedIn and apply (the main command)", formatter_class=fmt,
                       description="Search LinkedIn with Easy Apply only and apply to each job with a tailored CV.",
                       epilog=RUN_EPILOG)
    now = " [now: %(default)s]"
    g = r.add_argument_group("search")
    g.add_argument("position", nargs="?", help="search keywords; quote several words: \"ai engineer\"" + now)
    g.add_argument("-l", "--location", default="", help="city, region or country: Valencia, \"San Francisco\"" + now)
    for name, (_, codes) in FILTERS.items():
        g.add_argument(f"--{name.replace('_', '-')}", type=choices(codes), metavar="X" if name == "sort" else "X[,Y]",
                       help=f"{name.replace('_', ' ')}: {', '.join(codes)}" + now)
    g.add_argument("--distance", metavar="MILES", help="radius around --location: 10, 25, 50, 100" + now)
    g.add_argument("--under-10-applicants", action="store_true", help="only jobs with fewer than 10 applicants")
    g.add_argument("--filter", action="append", default=[], type=key_value, metavar="KEY=VALUE",
                   help="raw LinkedIn URL param, repeatable: f_C=1441, geoId=105646813 (docs/filters.md)" + now)
    g = r.add_argument_group("limits (0 = unlimited)")
    g.add_argument("--max-applies", type=int, default=10, metavar="N", help="stop after N successful applications" + now)
    g.add_argument("--max-time", type=float, default=0, metavar="HOURS",
                   help="stop after HOURS (e.g. 1.5), finishing the current job first" + now)
    g.add_argument("--max-pages", type=int, default=0, metavar="N", help="result pages to go through, 25 jobs each" + now)
    g = r.add_argument_group("model, browser and output")
    g.add_argument("-m", "--model", default=MODEL, help="OpenRouter model id that drives the browser and writes the CVs" + now)
    g.add_argument("-b", "--browser", default="chrome",
                   help=f"{', '.join(BROWSERS)} or the path of any Chromium-based browser" + now)
    g.add_argument("--dry-run", action="store_true", help="print the search URL and exit, no browser")
    g.add_argument("-v", "--verbose", action="store_true", help="live log + debug: model timings, job URLs, render errors")
    g.add_argument("-q", "--quiet", action="store_true", help="live log with application results only")
    if SEARCH.exists():  # search.toml = defaults, CLI flags override (filter lists are added to)
        defaults = tomllib.loads(SEARCH.read_text())
        if unknown := set(defaults) - {x.dest for x in r._actions}:
            sys.exit(f"search.toml: unknown keys {sorted(unknown)}")
        for x in r._actions:  # argparse only type-checks string defaults, so check the rest here
            v = defaults.get(x.dest)
            if v is None or isinstance(v, str) and x.type and x.dest != "filter":
                continue
            try:
                ok = (isinstance(v, bool) if x.nargs == 0 else
                      isinstance(v, list) and all(key_value(i) for i in v) if x.dest == "filter" else
                      isinstance(v, (int, float, str)) and not isinstance(v, bool))
            except argparse.ArgumentTypeError:
                ok = False
            if not ok:
                sys.exit(f"search.toml: bad value for {x.dest}: {v!r}")
        r.set_defaults(**defaults)
    lg = sub.add_parser("login", help="log in to LinkedIn in the browser the agent uses (once per browser)",
                        description="Opens the browser on the agent's own profile (.browser-profile/<browser>/). "
                                    "Log in to LinkedIn and close the window. Use this, not your normal browser: "
                                    "the agent can't read a login made there (macOS keychain).")
    lg.add_argument("-b", "--browser", default="chrome",
                    help=f"{', '.join(BROWSERS)} or an executable path" + now)
    if SEARCH.exists():  # same default browser as `run`
        lg.set_defaults(browser=tomllib.loads(SEARCH.read_text()).get("browser", "chrome"))
    for x in r._actions + lg._actions:  # fill [now: ...] with readable values
        if x.help and now in x.help:
            v = ", ".join(x.default) if isinstance(x.default, list) else x.default
            x.help = x.help.replace(now, f" [now: {v if v not in (None, '') else 'none'}]".replace("%", "%%"))
    t = sub.add_parser("tailor", help="write a CV for one job (the agent does this for each job)",
                       description="Tailors cv.yaml to a job description and renders "
                                   "applications/<company-title>/CV.pdf. The last line printed is the PDF to upload.")
    t.add_argument("--company", required=True)
    t.add_argument("--title", required=True)
    t.add_argument("--jd", required=True, metavar="FILE", help="text file with the job description")
    t.add_argument("-m", "--model", default=MODEL, help="OpenRouter model id [default: %(default)s]")
    a = p.parse_args()
    if a.cmd == "run" and not a.position:
        p.error("no position: pass one or set `position` in search.toml")
    sys.exit({"run": run, "tailor": tailor_cmd, "login": login}[a.cmd](a))


if __name__ == "__main__":
    main()
