"""LinkedIn Easy Apply automation driven by agy (Gemini) + Playwright MCP, with a tailored RenderCV CV per job.

  uv run jobs.py login --browser brave                                    # once per browser
  uv run jobs.py run "python developer" -l Valencia --max-applies 5 -v    # start the agent
  uv run jobs.py tailor --company X --title Y --jd jd.txt                  # called by the agent
  uv run jobs.py log --company X --title Y --url U --status applied        # called by the agent
  jobs.py mcp                                                             # started by agy
"""
import argparse, datetime, difflib, json, os, re, shutil, subprocess, sys, tempfile, threading, time, tomllib
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).parent.resolve()
BASE_CV, ANSWERS, LOG, RUN, APPS, SEARCH, EVENTS, LOGS = (ROOT / p for p in (
    "cv.yaml", "answers.md", "applied.jsonl", ".run.json", "applications", "search.toml", "run.log", "logs"))
AGY_LOGS = Path.home() / ".gemini/antigravity-cli/log"
MODEL = os.environ.get("AGY_MODEL", "gemini-3.8-flash-high")
MCP = "linkedin_playwright"  # registered in agy, see README
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


def agy(prompt: str, cwd: Path, timeout="0s") -> tuple[str, str]:
    r = subprocess.run(["agy", "-p", prompt, "--model", MODEL, "--dangerously-skip-permissions",
                        "--disable-slash-commands", "--print-timeout", timeout],
                       cwd=cwd, capture_output=True, text=True)
    return r.stdout, r.stderr


TAILOR_PROMPT = """Do not use any tools. Reply with text only.
You tailor a RenderCV v2 YAML CV to a job description.
- Truth first: never invent employers, dates, titles, technologies, metrics or credentials. Only reorder, reword, emphasize or drop what is already in the base CV.
- Mirror the job's keywords only where the base CV truthfully supports them (ATS).
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


def tailor(a) -> int:
    out = APPS / slug(f"{a.company}-{a.title}")
    out.mkdir(parents=True, exist_ok=True)
    yml, pdf, error = out / "CV.yaml", out / "CV.pdf", ""
    fallback = f"CV tailoring failed, upload the base CV instead:\n{ROOT / 'base_cv.pdf'}"
    say(f"tailoring CV: {a.company} — {a.title}")
    try:
        jd = Path(a.jd).read_text()
    except OSError as e:
        say(f"can't read the job description: {e}", "err")
        print(f"{e}\n{fallback}")
        return 1
    start = time.time()
    for attempt in (1, 2):  # one retry with the render error fed back
        t = time.time()
        with tempfile.TemporaryDirectory() as tmp:  # empty cwd so the model can't touch the project
            reply, err = agy(TAILOR_PROMPT.format(cv=BASE_CV.read_text(), title=a.title, company=a.company, jd=jd, error=error),
                        Path(tmp), "300s")
        say(f"attempt {attempt}: Gemini replied in {time.time() - t:.0f}s, {len(reply)} chars (jd {len(jd)} chars)", "debug")
        fenced = re.search(r"```\w*\n(.*?)\n```", reply, re.S)  # models like to wrap YAML in prose + fences
        yml.write_text((fenced[1] if fenced else reply).strip() + "\n")
        r = render(yml, pdf)
        if r.returncode == 0 and pdf.exists():
            say(f"CV ready in {time.time() - start:.0f}s: {pdf.relative_to(ROOT)}")
            print(pdf)
            return 0
        if not reply.strip():
            say(f"Gemini returned nothing: {err.strip()[-300:]}", "err")
            print(f"agy returned nothing: {err.strip()[-500:]}", file=sys.stderr)
            break  # quota/auth error: a retry won't help
        error = f"\nYour previous YAML failed to render, fix it:\n{(r.stdout + r.stderr)[-3000:]}"
        say(f"attempt {attempt}: RenderCV rejected the YAML", "warn")
        say((r.stdout + r.stderr)[-800:], "debug")
    say("tailoring failed, the agent will upload base_cv.pdf", "warn")
    print(error[-1500:], file=sys.stderr)
    print(fallback)  # last stdout line is always a PDF path
    return 1


def log(a) -> int:
    torn = LOG.exists() and LOG.stat().st_size and not LOG.read_bytes().endswith(b"\n")
    with LOG.open("a") as f:
        f.write("\n" * bool(torn) + json.dumps({"ts": time.time(), **{k: getattr(a, k) for k in ("company", "title", "status", "note")}, "url": job_url(a.url)}) + "\n")
    run = json.loads(RUN.read_text()) if RUN.exists() else {"started": 0, "max": 0}
    n = applied_since(run["started"])
    out_of_time = run.get("hours") and time.time() >= run["started"] + run["hours"] * 3600
    say(f"{a.status}: {a.company} — {a.title}" + (f" ({a.note})" if a.note else "") + f"  [{n}/{run['max'] or '∞'} applied]",
        {"applied": "ok", "skipped": "warn", "failed": "err"}[a.status])
    say(job_url(a.url), "debug")
    print(f"{n} applied this run." + (" LIMIT REACHED: stop now and summarize." if run["max"] and n >= run["max"] or out_of_time else ""))
    return 0


SYSTEM = """You apply to LinkedIn jobs with Easy Apply. Drive the browser ONLY with the `{mcp}` MCP tools (never invisible_playwright_mcp).
Project directory: {root}. Run shell commands from there.

For each search results page (search URL + &start=0, 25, 50, ...), for each job card:
1. Skip cards marked "Applied" and URLs in the already-processed list.
2. Open the job and read the full description (click "see more" if present).
3. Save the description text to {root}/applications/jd.txt, then run (in the foreground, wait for it to finish, ~1 min):
   uv run jobs.py tailor --company "<company>" --title "<title>" --jd applications/jd.txt
   The last line printed is the absolute PDF path to upload.
4. Click "Easy Apply". At the resume step click "Upload resume", then call browser_file_upload with that PDF path, and make sure it is selected.
5. Fill every step truthfully from <candidate_answers> and <cv>. Never invent facts.
   If a required question cannot be answered truthfully from them, discard the application and log it as skipped with the question as note.
6. Uncheck "Follow company" if present, Submit, dismiss the confirmation dialog.
7. Run: uv run jobs.py log --company "<company>" --title "<title>" --url "<job url, e.g. https://www.linkedin.com/jobs/view/123/>" --status applied|skipped|failed --note "<short note>"
   If it prints LIMIT REACHED, stop immediately.

Unexpected popups (premium ads, "Save this application?", surveys, cookie banners, "Share your profile?", tooltips):
- Close them: press Escape, or click Dismiss / Close / X / "Not now" / "No thanks" / "Discard". Never click the upsell button.
- Native browser dialogs (alert/confirm/beforeunload): use browser_handle_dialog.
- If an action has no effect, take a fresh snapshot before retrying; the page probably changed.
If you are stuck on the same job after 3 attempts, re-open the search URL, log that job as failed with what blocked you as note, and move on.

On the first results page, check the filter pills at the top show the requested filters as active; if one is not, say which and continue.
If LinkedIn shows a login page, a captcha or a security check, stop and say so.
Never click anything that pays, messages people, or edits the LinkedIn profile. Do not modify project files.
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


FILL_PROMPT = """Do not use any tools. Reply with text only. Today is {today}.
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


def preflight(name: str) -> None:
    """Cheap checks before the browser starts. Exits on anything that would break the run."""
    problems = []
    if not shutil.which("agy"):
        sys.exit("preflight: agy not found on PATH")
    mcps = subprocess.run(["agy", "mcp", "list"], capture_output=True, text=True).stdout.splitlines()
    if not any(l.startswith(MCP + " ") and "jobs.py mcp" in l for l in mcps):
        problems.append(f"MCP `{MCP}` is missing or outdated in agy. Register it with:\n"
                        f"  agy mcp remove {MCP}; agy mcp add {MCP} {ROOT}/.venv/bin/python -- {ROOT}/jobs.py mcp")
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
        c = input("[f] fill what the CV answers with Gemini  [c] continue anyway  [q] quit > ").strip().lower()
        if c == "c":
            return
        if c != "f":
            sys.exit(0)
        old = ANSWERS.read_text()
        print("asking Gemini (~30s)...")
        new, err = agy(FILL_PROMPT.format(today=datetime.date.today(), answers=old, cv=BASE_CV.read_text()),
                       Path(tempfile.gettempdir()), "300s")
        new = new.strip() + "\n"
        if not new.strip() or "## Agent policy" not in new:  # empty or mangled reply: never overwrite
            print(f"Gemini reply unusable, answers.md unchanged. {err.strip()[-300:]}")
            continue
        diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), "answers.md", "filled", lineterm="", n=0))
        if not diff:
            print("Gemini found nothing in the CV for the remaining TODOs.")
            continue
        print("\n".join(diff))
        if input("Save these changes? (old copy -> answers.md.bak) [y/N] > ").strip().lower() == "y":
            (ROOT / "answers.md.bak").write_text(old)
            ANSWERS.write_text(new)


def run(a) -> int:
    url = search_url(a)
    print(url)
    if a.dry_run:
        return 0
    preflight(a.browser)
    started, offset = time.time(), EVENTS.stat().st_size if EVENTS.exists() else 0
    out = LOGS / time.strftime("%Y%m%d-%H%M%S")  # everything about this run, for debugging afterwards
    out.mkdir(parents=True)
    RUN.write_text(json.dumps({"started": started, "max": a.max_applies, "hours": a.max_time, "browser": a.browser, "dir": str(out)}))
    stop = threading.Event()
    tail = threading.Thread(target=follow, args=(stop, 1 + a.verbose - a.quiet, offset), daemon=True)
    tail.start()
    say(f"searching \"{a.position}\" in {a.location or 'anywhere'}, stop after {a.max_applies or '∞'} applies"
        + (f" or {a.max_time:g}h" if a.max_time else ""))
    say(url, "debug")
    pages = f"at most {a.max_pages} results pages" if a.max_pages else "all results pages until there are no more"
    prompt = (SYSTEM.format(mcp=MCP, root=ROOT) +
              f"\n\nSearch URL: {url}\nProcess {pages}.\n"
              f"Already processed job URLs: {json.dumps([e['url'] for e in logged()][-300:])}\n\n"
              f"<candidate_answers>\n{ANSWERS.read_text()}\n</candidate_answers>\n\n<cv>\n{BASE_CV.read_text()}\n</cv>")
    say(f"agent started ({MODEL}, {a.browser}), browsing LinkedIn... (-v for more detail)")
    say(f"prompt: {len(prompt)} chars", "debug")
    (out / "prompt.txt").write_text(" ".join(sys.argv) + "\n\n" + prompt)
    rc = 1
    try:
        # the soft limit is LIMIT REACHED from `log`; kill 10 min later in case the agent never logs again
        with (out / "agent.txt").open("w") as f:
            rc = subprocess.run(["agy", "-p", prompt, "--model", MODEL, "--dangerously-skip-permissions"], cwd=ROOT,
                                stdout=f, stderr=subprocess.STDOUT,
                                timeout=a.max_time * 3600 + 600 if a.max_time else None).returncode
    except subprocess.TimeoutExpired:
        say(f"--max-time {a.max_time:g}h passed and the agent didn't stop: killed it", "warn")
    finally:  # also on Ctrl+C
        n = applied_since(json.loads(RUN.read_text())["started"])
        say(f"agent finished (exit {rc}): {n} applied this run", "ok" if rc == 0 else "err")
        say(f"debug logs: {out}/")
        stop.set()
        tail.join(timeout=2)
        print((out / "agent.txt").read_text(), end="")
        with EVENTS.open() as f:
            f.seek(offset)
            (out / "events.log").write_text(f.read())
        for p in AGY_LOGS.glob("cli-*.log"):  # the agent's and every `tailor` call's agy log
            if p.stat().st_mtime >= started:
                shutil.copy2(p, out / p.name)
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


def mcp(a) -> int:
    """agy starts the browser MCP through this (README > Setup), so each run can pick its browser."""
    run = json.loads(RUN.read_text()) if RUN.exists() else {}
    exe, profile = browser(run.get("browser", "chrome"))
    save = ["--save-session", "--output-dir", run["dir"] + "/browser"] if run.get("dir") else []  # every tool call + page snapshot
    os.execvp("npx", ["npx", "-y", "@playwright/mcp@latest", "--executable-path", exe, "--user-data-dir", str(profile), *save])


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
An agy (Gemini) agent drives your browser; answers come from answers.md, the CV from cv.yaml."""

HELP_EPILOG = f"""first time:
  agy mcp add {MCP} $PWD/.venv/bin/python -- $PWD/jobs.py mcp
  uv run jobs.py login                   log in to LinkedIn (Chrome); -b brave for Brave

every day:
  uv run jobs.py run --dry-run           preview the search from search.toml
  uv run jobs.py run                     apply with search.toml
  uv run jobs.py run "ai engineer" -l "San Francisco" -b brave --max-time 1.5 -v

more:  uv run jobs.py run -h    ·    README.md    ·    docs/filters.md"""

RUN_EPILOG = """Every option's default comes from search.toml ([now: ...] shows it); a flag overrides it for this run.
Stops at whichever comes first: --max-applies, --max-time, --max-pages, or no more results.
Ctrl+C stops at any time; processed jobs are skipped next run (applied.jsonl).

examples:
  uv run jobs.py run "python developer" -l Valencia
  uv run jobs.py run "ml engineer" -l "European Union" --workplace remote --posted 24h
  uv run jobs.py run "data scientist" -l Madrid --max-applies 0 --max-time 2 -q
  uv run jobs.py run "backend" -l Berlin --filter f_C=1441 --dry-run"""


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
    g = r.add_argument_group("browser and output")
    g.add_argument("-b", "--browser", default="chrome",
                   help=f"{', '.join(BROWSERS)} or the path of any Chromium-based browser" + now)
    g.add_argument("--dry-run", action="store_true", help="print the search URL and exit, no browser")
    g.add_argument("-v", "--verbose", action="store_true", help="live log + debug: Gemini timings, job URLs, render errors")
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
    t = sub.add_parser("tailor", help="write a CV for one job (the agent calls this)",
                       description="Tailors cv.yaml to a job description with Gemini and renders "
                                   "applications/<company-title>/CV.pdf. The last line printed is the PDF to upload.")
    t.add_argument("--company", required=True)
    t.add_argument("--title", required=True)
    t.add_argument("--jd", required=True, metavar="FILE", help="text file with the job description")
    l = sub.add_parser("log", help="record a job's result (the agent calls this)",
                       description="Appends to applied.jsonl and prints LIMIT REACHED when the run must stop.")
    for k in ("company", "title", "url"):
        l.add_argument(f"--{k}", required=True)
    l.add_argument("--status", required=True, type=lambda s: s.strip().lower(), choices=("applied", "skipped", "failed"))
    l.add_argument("--note", default="", help="why, e.g. the question that blocked it")
    sub.add_parser("mcp", help="start the browser MCP (agy runs this, see README > Setup)",
                   description="Starts Playwright MCP with the browser of the current run (.run.json).")
    a = p.parse_args()
    if a.cmd == "run" and not a.position:
        p.error("no position: pass one or set `position` in search.toml")
    sys.exit({"run": run, "tailor": tailor, "log": log, "login": login, "mcp": mcp}[a.cmd](a))


if __name__ == "__main__":
    main()
