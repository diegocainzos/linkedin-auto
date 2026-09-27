"""LinkedIn Easy Apply automation driven by agy (Gemini) + Playwright MCP, with a tailored RenderCV CV per job.

  uv run jobs.py run "python developer" --location Spain --max-applies 5   # start the agent
  uv run jobs.py tailor --company X --title Y --jd jd.txt                  # called by the agent
  uv run jobs.py log --company X --title Y --url U --status applied        # called by the agent
"""
import argparse, datetime, difflib, json, os, re, shutil, subprocess, sys, tempfile, time, tomllib
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).parent.resolve()
BASE_CV, ANSWERS, LOG, RUN, APPS, SEARCH = (ROOT / p for p in (
    "cv.yaml", "answers.md", "applied.jsonl", ".run.json", "applications", "search.toml"))
MODEL = os.environ.get("AGY_MODEL", "gemini-3.8-flash-high")
MCP = "linkedin_playwright"  # registered in agy, see README


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
    try:
        jd = Path(a.jd).read_text()
    except OSError as e:
        print(f"{e}\n{fallback}")
        return 1
    for _ in range(2):  # one retry with the render error fed back
        with tempfile.TemporaryDirectory() as tmp:  # empty cwd so the model can't touch the project
            reply, err = agy(TAILOR_PROMPT.format(cv=BASE_CV.read_text(), title=a.title, company=a.company, jd=jd, error=error),
                        Path(tmp), "300s")
        fenced = re.search(r"```\w*\n(.*?)\n```", reply, re.S)  # models like to wrap YAML in prose + fences
        yml.write_text((fenced[1] if fenced else reply).strip() + "\n")
        r = render(yml, pdf)
        if r.returncode == 0 and pdf.exists():
            print(pdf)
            return 0
        if not reply.strip():
            print(f"agy returned nothing: {err.strip()[-500:]}", file=sys.stderr)
            break  # quota/auth error: a retry won't help
        error = f"\nYour previous YAML failed to render, fix it:\n{(r.stdout + r.stderr)[-3000:]}"
    print(error[-1500:], file=sys.stderr)
    print(fallback)  # last stdout line is always a PDF path
    return 1


def log(a) -> int:
    torn = LOG.exists() and LOG.stat().st_size and not LOG.read_bytes().endswith(b"\n")
    with LOG.open("a") as f:
        f.write("\n" * bool(torn) + json.dumps({"ts": time.time(), **{k: getattr(a, k) for k in ("company", "title", "status", "note")}, "url": job_url(a.url)}) + "\n")
    run = json.loads(RUN.read_text()) if RUN.exists() else {"started": 0, "max": 0}
    n = sum(1 for e in logged() if e["status"] == "applied" and e.get("ts", 0) >= run["started"])
    print(f"{n} applied this run." + (" LIMIT REACHED: stop now and summarize." if run["max"] and n >= run["max"] else ""))
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


def preflight() -> None:
    """Cheap checks before the browser starts. Exits on anything that would break the run."""
    problems = []
    if not shutil.which("agy"):
        sys.exit("preflight: agy not found on PATH")
    if MCP not in subprocess.run(["agy", "mcp", "list"], capture_output=True, text=True).stdout:
        problems.append(f"MCP `{MCP}` is not registered in agy (README > Setup)")
    if not (ROOT / ".browser-profile").is_dir():
        problems.append("no .browser-profile/: log in to LinkedIn once (README > Setup)")
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
    RUN.write_text(json.dumps({"started": time.time(), "max": a.max_applies}))
    url = search_url(a)
    print(url)
    if a.dry_run:
        return 0
    preflight()
    pages = f"at most {a.max_pages} results pages" if a.max_pages else "all results pages until there are no more"
    prompt = (SYSTEM.format(mcp=MCP, root=ROOT) +
              f"\n\nSearch URL: {url}\nProcess {pages}.\n"
              f"Already processed job URLs: {json.dumps([e['url'] for e in logged()][-300:])}\n\n"
              f"<candidate_answers>\n{ANSWERS.read_text()}\n</candidate_answers>\n\n<cv>\n{BASE_CV.read_text()}\n</cv>")
    return subprocess.run(["agy", "-p", prompt, "--model", MODEL, "--dangerously-skip-permissions"], cwd=ROOT).returncode


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


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("position", nargs="?")
    r.add_argument("--location", default="")
    r.add_argument("--max-applies", type=int, default=10, help="0 = unlimited")
    r.add_argument("--max-pages", type=int, default=0, help="0 = unlimited")
    for name, (_, codes) in FILTERS.items():
        r.add_argument(f"--{name.replace('_', '-')}", type=choices(codes), help=f"comma list of: {', '.join(codes)}")
    r.add_argument("--distance", help="radius in miles around --location, e.g. 10, 25, 50, 100")
    r.add_argument("--under-10-applicants", action="store_true")
    r.add_argument("--filter", action="append", default=[], type=key_value, metavar="KEY=VALUE",
                   help="raw LinkedIn URL param, repeatable, e.g. f_C=1441 or geoId=105646813")
    r.add_argument("--dry-run", action="store_true", help="print the search URL and exit")
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
                      isinstance(v, (int, str)) and not isinstance(v, bool))
            except argparse.ArgumentTypeError:
                ok = False
            if not ok:
                sys.exit(f"search.toml: bad value for {x.dest}: {v!r}")
        r.set_defaults(**defaults)
    t = sub.add_parser("tailor")
    t.add_argument("--company", required=True)
    t.add_argument("--title", required=True)
    t.add_argument("--jd", required=True, help="file with the job description")
    l = sub.add_parser("log")
    for k in ("company", "title", "url"):
        l.add_argument(f"--{k}", required=True)
    l.add_argument("--status", required=True, type=lambda s: s.strip().lower(), choices=("applied", "skipped", "failed"))
    l.add_argument("--note", default="")
    a = p.parse_args()
    if a.cmd == "run" and not a.position:
        p.error("no position: pass one or set `position` in search.toml")
    sys.exit({"run": run, "tailor": tailor, "log": log}[a.cmd](a))


if __name__ == "__main__":
    main()
