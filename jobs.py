"""LinkedIn Easy Apply automation driven by an agent CLI (agy, claude, codex, pi) + Playwright MCP, with a tailored RenderCV CV per job.

  uv run jobs.py login --browser brave                                    # once per browser
  uv run jobs.py run "python developer" -l Valencia --max-applies 5 -v    # start the agent
  uv run jobs.py fit --company X --title Y --url U --jd jd.txt            # called by the agent
  uv run jobs.py tailor --company X --title Y --jd jd.txt                  # called by the agent
  uv run jobs.py log --company X --title Y --url U --status applied        # called by the agent
  uv run jobs.py issue "what went wrong" --url U                          # called by the agent
  jobs.py mcp                                                             # started by the agent CLI
"""
import argparse, datetime, difflib, json, os, re, shutil, smtplib, sqlite3, subprocess, sys, tempfile, threading, time, tomllib
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).parent.resolve()
BASE_CV, ANSWERS, LOG, RUN, APPS, SEARCH, EVENTS, LOGS = (ROOT / p for p in (
    "cv.yaml", "answers.md", "applied.jsonl", ".run.json", "applications", "search.toml", "run.log", "logs"))
AGY_LOGS = Path.home() / ".gemini/antigravity-cli/log"
AGENTS = ("agy", "claude", "codex", "pi")  # agent CLIs, see agent_cmd()
AGY_MODEL = "gemini-3.8-flash-high"  # agy's own default isn't Flash
MCP = "linkedin_playwright"  # the browser MCP server's name in every agent CLI
ISSUES, DB = ROOT / "issues.log", ROOT / "jobs.db"
FIT_APPLY, FIT_EMAIL = 0.5, 0.9  # Jev's "compatible" probability: apply from 0.5, also email the hiring team from 0.9
JEV = "typesafe/jev-1.13"  # pinned: ~typesafe/jev-latest could change answers under us
for _l in (ROOT / ".env").read_text().splitlines() if (ROOT / ".env").exists() else []:  # KEY=VALUE  # comment, no dependency
    if "=" in _l and not _l.lstrip().startswith("#"):
        os.environ.setdefault(_l.split("=", 1)[0].strip(), _l.split("=", 1)[1].split(" #")[0].strip().strip("'\""))
BROWSERS = {  # macOS; any other Chromium-based browser works by passing its executable path
    "chrome": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "brave": "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "edge": "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "chromium": "/Applications/Chromium.app/Contents/MacOS/Chromium",
}


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def db() -> sqlite3.Connection:
    """jobs.db: one row per job the agent opened. Imports applied.jsonl (the old log) on creation."""
    new = not DB.exists()
    c = sqlite3.connect(DB, isolation_level=None, timeout=30)  # autocommit
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE IF NOT EXISTS jobs (url TEXT PRIMARY KEY, ts REAL, company TEXT, title TEXT, status TEXT, "
              "note TEXT, fit REAL, poster TEXT, jd TEXT, cv_yaml TEXT, contact_email TEXT, email_subject TEXT, "
              "email_body TEXT, email_sent REAL)")
    for l in LOG.read_text().splitlines() if new and LOG.exists() else []:
        try:
            e = json.loads(l)
        except ValueError:  # half-written line
            continue
        save(e["url"], c=c, **{k: e.get(k) for k in ("ts", "company", "title", "status", "note")})
    return c


def save(url: str, c: sqlite3.Connection | None = None, **cols) -> None:
    """Insert or update the job's row, only the columns given."""
    q = f"INSERT INTO jobs (url, {', '.join(cols)}) VALUES ({', '.join('?' * (len(cols) + 1))}) ON CONFLICT(url) DO UPDATE SET "
    (c or db()).execute(q + ", ".join(f"{k} = excluded.{k}" for k in cols), (job_url(url), *cols.values()))


def logged() -> list[dict]:
    return [dict(r) for r in db().execute("SELECT * FROM jobs WHERE status IN ('applied', 'skipped', 'failed') ORDER BY ts")]


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


def agent_cmd(agent: str, prompt: str, model: str = "", mcp: Path | None = None) -> list[str]:
    """One-shot command line for an agent CLI. With `mcp` (an mcp.json) it gets the browser and no permission
    prompts; without it, it's a text-only call (tailor, answers.md fill)."""
    if agent == "agy":  # agy only has a global MCP config: registered once, see README > Setup
        cmd = ["agy", "-p", prompt, "--dangerously-skip-permissions"] + (
            [] if mcp else ["--disable-slash-commands", "--print-timeout", "300s"])
        model = model or AGY_MODEL
    elif agent == "claude":  # --tools is variadic, so the prompt goes right after -p
        cmd = ["claude", "-p", prompt, "--dangerously-skip-permissions"] + (
            ["--mcp-config", str(mcp), "--strict-mcp-config"] if mcp else ["--tools", ""])
    elif agent == "codex":  # no MCP file flag: pass the server as config overrides
        srv = json.loads(mcp.read_text())["mcpServers"][MCP] if mcp else {}
        cmd = ["codex", "exec", "--skip-git-repo-check"] + (
            ["--dangerously-bypass-approvals-and-sandbox",
             "-c", f"mcp_servers.{MCP}.command={json.dumps(srv['command'])}",
             "-c", f"mcp_servers.{MCP}.args={json.dumps(srv['args'])}",
             "-c", f"mcp_servers.{MCP}.startup_timeout_sec=120"] if mcp else ["--sandbox", "read-only"])
    else:  # pi, MCP through the pi-mcp-adapter extension
        cmd = ["pi", "-p", "--no-session"] + (["--mcp-config", str(mcp)] if mcp else ["--no-tools"])
    return cmd + (["--model", model] if model else []) + ([prompt] if agent in ("codex", "pi") else [])


def ask(prompt: str, cwd: Path, agent: str, model: str) -> tuple[str, str]:
    r = subprocess.run(agent_cmd(agent, prompt, model), cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    return r.stdout, r.stderr


TAILOR_PROMPT = """Do not use any tools. Reply with text only.
You tailor a RenderCV v2 YAML CV to a job description, to pass ATS keyword filters and impress a recruiter in 10 seconds.

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
    run = json.loads(RUN.read_text()) if RUN.exists() else {}  # same agent as the run that called us
    agent = run.get("agent", "agy")
    for attempt in (1, 2):  # one retry with the render error fed back
        t = time.time()
        with tempfile.TemporaryDirectory() as tmp:  # empty cwd so the model can't touch the project
            reply, err = ask(TAILOR_PROMPT.format(cv=BASE_CV.read_text(), title=a.title, company=a.company, jd=jd, error=error),
                             Path(tmp), agent, run.get("model", ""))
        say(f"attempt {attempt}: {agent} replied in {time.time() - t:.0f}s, {len(reply)} chars (jd {len(jd)} chars)", "debug")
        fenced = re.search(r"```\w*\n(.*?)\n```", reply, re.S)  # models like to wrap YAML in prose + fences
        yml.write_text((fenced[1] if fenced else reply).strip() + "\n")
        r = render(yml, pdf)
        if r.returncode == 0 and pdf.exists():
            say(f"CV ready in {time.time() - start:.0f}s: {pdf.relative_to(ROOT)}")
            print(pdf)
            return 0
        if not reply.strip():
            say(f"{agent} returned nothing: {err.strip()[-300:]}", "err")
            print(f"{agent} returned nothing: {err.strip()[-500:]}", file=sys.stderr)
            break  # quota/auth error: a retry won't help
        error = f"\nYour previous YAML failed to render, fix it:\n{(r.stdout + r.stderr)[-3000:]}"
        say(f"attempt {attempt}: RenderCV rejected the YAML", "warn")
        say((r.stdout + r.stderr)[-800:], "debug")
    say("tailoring failed, the agent will upload base_cv.pdf", "warn")
    print(error[-1500:], file=sys.stderr)
    print(fallback)  # last stdout line is always a PDF path
    return 1


def jev(cv: str, title: str, company: str, jd: str) -> float:
    """OpenRouter Decisions API: probability (0-1) that the CV is a real match for the job."""
    body = {"model": JEV, "state": {"candidate_cv": cv, "job_offer": f"{title} at {company}\n\n{jd}"},
            "questions": {"compatible": {
                "type": "noul",
                "instructions": "Judging only by the candidate's CV against the job's requirements, is the candidate a real match for this job?",
                "criteria": {"true": "The CV meets the job's must-have requirements: main tech stack, seniority or years "
                                     "of experience, required languages, location or work permit.",
                             "false": "The CV lacks at least one must-have requirement, or the job is in another field or seniority."}}}}
    req = Request("https://openrouter.ai/api/alpha/decisions", json.dumps(body).encode(),
                  {"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}", "Content-Type": "application/json"})
    with urlopen(req, timeout=60) as r:
        return json.load(r)["answers"]["compatible"]["noul"]


def fit(a) -> int:
    """The agent asks before applying: Jev decides whether the job matches the base CV. Prints APPLY or SKIP."""
    try:
        jd = Path(a.jd).read_text()
    except OSError as e:
        sys.exit(str(e))
    p = None
    if not os.environ.get("OPENROUTER_API_KEY"):
        say("no OPENROUTER_API_KEY in .env: applying without a compatibility check", "debug")
    else:
        try:
            p = jev(BASE_CV.read_text(), a.title, a.company, jd)
        except Exception as e:  # alpha API: never block the run on it
            say(f"Jev failed, applying without a compatibility check: {e}", "warn")
    ok = p is None or p >= FIT_APPLY
    note = "" if ok else f"not compatible (fit {p:.2f})"
    save(a.url, ts=time.time(), company=a.company, title=a.title, fit=p, poster=a.poster or None, jd=jd,
         **({} if ok else {"status": "skipped", "note": note}))
    say(f"fit {'?' if p is None else f'{p:.2f}'}: {a.company} — {a.title}" + ("" if ok else "  → skipped"), "info" if ok else "warn")
    print(f"APPLY (fit {'unknown' if p is None else f'{p:.2f}'})" if ok else f"SKIP: {note}. Already logged, go to the next job.")
    return 0


EMAIL_PROMPT = """Do not use any tools. Reply with text only.
Write a short cold email from the candidate to {poster}, who posted the job "{title}" at {company}.
The candidate just applied through LinkedIn and attaches the CV.
- Write in the same language as the job description.
- At most 300 characters in total, greeting and sign-off included.
- Plain prose only: no bullet points, no lists, no markdown, no emojis, no dashes or other special characters.
- Name the role, give one or two facts from the CV that match the job's key requirements, ask for a short call,
  sign with the candidate's first name. Only facts from the CV, nothing invented.
Output ONLY the email body.

<cv>
{cv}
</cv>

<job>
{jd}
</job>"""


def contact(url: str, pdf: Path) -> None:
    """After applying to a strong match: find the hiring team member's email (Hunter), write and send an email.
    Anything missing (poster, key, a verified email) = silently nothing. No SMTP_HOST = the email is only saved."""
    c = db()
    r = c.execute("SELECT * FROM jobs WHERE url = ?", (job_url(url),)).fetchone()
    if not r or (r["fit"] or 0) < FIT_EMAIL or not r["poster"] or not os.environ.get("HUNTER_API_KEY"):
        return
    try:
        q = urlencode({"full_name": r["poster"], "company": r["company"], "api_key": os.environ["HUNTER_API_KEY"]})
        with urlopen(f"https://api.hunter.io/v2/email-finder?{q}", timeout=30) as h:
            d = json.load(h)["data"]
    except Exception as e:
        say(f"Hunter: no email for {r['poster']} ({r['company']}): {e}", "debug")
        return
    to = d.get("email")
    if not to or not ((d.get("verification") or {}).get("status") == "valid" or (d.get("score") or 0) >= 90):
        say(f"Hunter: no verified email for {r['poster']} ({r['company']})", "debug")
        return
    if c.execute("SELECT 1 FROM jobs WHERE contact_email = ? AND email_sent IS NOT NULL", (to,)).fetchone():
        say(f"already emailed {to}, not again", "debug")
        return
    run = json.loads(RUN.read_text()) if RUN.exists() else {}
    with tempfile.TemporaryDirectory() as tmp:
        body, _ = ask(EMAIL_PROMPT.format(poster=r["poster"], title=r["title"], company=r["company"], cv=BASE_CV.read_text(),
                                          jd=r["jd"] or ""), Path(tmp), run.get("agent", "agy"), run.get("model", ""))
    body = re.sub(r"[^\w\s.,;:!?¿¡()'@/+]", "", re.sub(r"\s*[—–]\s*", ", ", body)).strip()  # no special characters
    name = re.search(r"^\s+name:\s*(.+)$", BASE_CV.read_text(), re.M)[1].strip()
    subject = f"{r['title']} - {name}"
    save(url, contact_email=to, email_subject=subject, email_body=body)
    if not body or len(body) > 300:
        say(f"email to {to} not sent: {len(body)} chars, want 1-300 (saved in jobs.db)", "warn")
        return
    if not os.environ.get("SMTP_HOST"):
        say(f"email to {r['poster']} <{to}> saved, not sent (no SMTP_HOST in .env)")
        return
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = os.environ.get("SMTP_FROM") or os.environ["SMTP_USER"], to, subject
    m.set_content(body)
    m.add_attachment(pdf.read_bytes(), maintype="application", subtype="pdf", filename=f"CV {name}.pdf")
    port = int(os.environ.get("SMTP_PORT", 465))
    try:
        with (smtplib.SMTP_SSL if port == 465 else smtplib.SMTP)(os.environ["SMTP_HOST"], port, timeout=30) as s:
            if port != 465:
                s.starttls()
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            s.send_message(m)
    except Exception as e:
        say(f"email to {to} failed: {e}", "err")
        return
    save(url, email_sent=time.time())
    say(f"emailed {r['poster']} <{to}>", "ok")


def log(a) -> int:
    yml = APPS / slug(f"{a.company}-{a.title}") / "CV.yaml"
    save(a.url, ts=time.time(), company=a.company, title=a.title, status=a.status, note=a.note,
         **({"cv_yaml": yml.read_text()} if a.status == "applied" and yml.exists() else {}))
    run = json.loads(RUN.read_text()) if RUN.exists() else {"started": 0, "max": 0}
    n = applied_since(run["started"])
    out_of_time = run.get("hours") and time.time() >= run["started"] + run["hours"] * 3600
    say(f"{a.status}: {a.company} — {a.title}" + (f" ({a.note})" if a.note else "") + f"  [{n}/{run['max'] or '∞'} applied]",
        {"applied": "ok", "skipped": "warn", "failed": "err"}[a.status])
    say(job_url(a.url), "debug")
    if a.status == "applied":
        pdf = yml.with_suffix(".pdf")
        contact(a.url, pdf if pdf.exists() else ROOT / "base_cv.pdf")
    print(f"{n} applied this run." + (" LIMIT REACHED: stop now and summarize." if run["max"] and n >= run["max"] or out_of_time else ""))
    return 0


def issue(a) -> int:
    """The agent reports a problem it hit: one line in issues.log (all runs), and a warning in the live log."""
    url = f"  {job_url(a.url)}" if a.url else ""
    with ISSUES.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(a.text.split())}{url}\n")
    say(f"issue: {a.text}{url}", "warn")
    print("noted, carry on")
    return 0


SYSTEM = """You apply to LinkedIn jobs with Easy Apply. Drive the browser ONLY with the `{mcp}` MCP tools (no other browser or Playwright tools).
Project directory: {root}. Run shell commands from there.

For each search results page (search URL + &start=0, 25, 50, ...), for each job card:
1. Skip cards marked "Applied" and URLs in the already-processed list.
2. Open the job and read the full description (click "see more" if present). If a "Meet the hiring team" section
   shows a person, note their full name.
3. Save the description text to {root}/applications/jd.txt, then run:
   uv run jobs.py fit --company "<company>" --title "<title>" --url "<job url>" --jd applications/jd.txt --poster "<hiring team name, or empty>"
   If it prints SKIP, the job is already logged: go to the next job. If it prints APPLY, go on.
   Then run (in the foreground, wait for it to finish, ~1 min):
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

Report every execution problem as soon as it happens, then carry on:
   uv run jobs.py issue "<what happened, where, what you tried>" --url "<job url, if any>"
Problems = a tool or command error, a popup you couldn't close, a page that didn't load or looked wrong, a form field you
couldn't fill, an upload that failed, a filter that wasn't applied, anything unexpected. Not needed for normal skips.

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


def preflight(a) -> None:
    """Cheap checks before the browser starts. Exits on anything that would break the run."""
    problems, name = [], a.browser
    if not shutil.which(a.agent):
        sys.exit(f"preflight: {a.agent} not found on PATH")
    mcps = subprocess.run(["agy", "mcp", "list"], capture_output=True, text=True).stdout.splitlines() if a.agent == "agy" else []
    if a.agent == "agy" and not any(l.startswith(MCP + " ") and "jobs.py mcp" in l for l in mcps):
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
        c = input(f"[f] fill what the CV answers with {a.agent}  [c] continue anyway  [q] quit > ").strip().lower()
        if c == "c":
            return
        if c != "f":
            sys.exit(0)
        old = ANSWERS.read_text()
        print(f"asking {a.agent} (~30s)...")
        new, err = ask(FILL_PROMPT.format(today=datetime.date.today(), answers=old, cv=BASE_CV.read_text()),
                       Path(tempfile.gettempdir()), a.agent, a.model)
        new = new.strip() + "\n"
        if not new.strip() or "## Agent policy" not in new:  # empty or mangled reply: never overwrite
            print(f"{a.agent} reply unusable, answers.md unchanged. {err.strip()[-300:]}")
            continue
        diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), "answers.md", "filled", lineterm="", n=0))
        if not diff:
            print(f"{a.agent} found nothing in the CV for the remaining TODOs.")
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
    preflight(a)
    started, offset = time.time(), EVENTS.stat().st_size if EVENTS.exists() else 0
    out = LOGS / time.strftime("%Y%m%d-%H%M%S")  # everything about this run, for debugging afterwards
    out.mkdir(parents=True)
    RUN.write_text(json.dumps({"started": started, "max": a.max_applies, "hours": a.max_time, "browser": a.browser,
                               "agent": a.agent, "model": a.model, "dir": str(out)}))
    mcp_json = out / "mcp.json"  # how the agent CLI starts the browser (agy uses its global config instead)
    mcp_json.write_text(json.dumps({"mcpServers": {MCP: {"command": sys.executable, "args": [str(ROOT / "jobs.py"), "mcp"],
                                                         "directTools": True}}}, indent=2))  # directTools: pi
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
    say(f"agent started ({a.agent} {a.model or (AGY_MODEL if a.agent == 'agy' else 'default model')}, {a.browser}), browsing LinkedIn... (-v for more detail)")
    say(f"prompt: {len(prompt)} chars", "debug")
    (out / "prompt.txt").write_text(" ".join(sys.argv) + "\n\n" + prompt)
    rc = 1
    try:
        # the soft limit is LIMIT REACHED from `log`; kill 10 min later in case the agent never logs again
        with (out / "agent.txt").open("w") as f:
            rc = subprocess.run(agent_cmd(a.agent, prompt, a.model, mcp_json), cwd=ROOT,
                                stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT,
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
An agent CLI (agy, claude, codex or pi) drives your browser; answers come from answers.md, the CV from cv.yaml.
Problems the agent hits go to issues.log; everything about a run goes to logs/<date-time>/."""

HELP_EPILOG = f"""first time:
  agy mcp add {MCP} $PWD/.venv/bin/python -- $PWD/jobs.py mcp     (agy only)
  uv run jobs.py login                   log in to LinkedIn (Chrome); -b brave for Brave

every day:
  uv run jobs.py run --dry-run           preview the search from search.toml
  uv run jobs.py run                     apply with search.toml
  uv run jobs.py run "ai engineer" -l "San Francisco" -b brave --max-time 1.5 -v
  uv run jobs.py run --agent claude --model sonnet

more:  uv run jobs.py run -h    ·    README.md    ·    docs/filters.md"""

RUN_EPILOG = """Every option's default comes from search.toml ([now: ...] shows it); a flag overrides it for this run.
Stops at whichever comes first: --max-applies, --max-time, --max-pages, or no more results.
Ctrl+C stops at any time; processed jobs are skipped next run (jobs.db).

examples:
  uv run jobs.py run "python developer" -l Valencia
  uv run jobs.py run "ml engineer" -l "European Union" --workplace remote --posted 24h
  uv run jobs.py run "data scientist" -l Madrid --max-applies 0 --max-time 2 -q
  uv run jobs.py run "backend" -l Berlin --filter f_C=1441 --dry-run
  uv run jobs.py run "python developer" --agent pi --model openrouter/xiaomi/mimo-v2.6-pro"""


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
    g = r.add_argument_group("agent, browser and output")
    g.add_argument("-a", "--agent", default="agy", choices=AGENTS, help="agent CLI that drives the browser and writes the CVs" + now)
    g.add_argument("-m", "--model", default="", help=f"model for --agent; none = its default ({AGY_MODEL} for agy)" + now)
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
    t = sub.add_parser("tailor", help="write a CV for one job (the agent calls this)",
                       description="Tailors cv.yaml to a job description with the run's agent and renders "
                                   "applications/<company-title>/CV.pdf. The last line printed is the PDF to upload.")
    t.add_argument("--company", required=True)
    t.add_argument("--title", required=True)
    t.add_argument("--jd", required=True, metavar="FILE", help="text file with the job description")
    l = sub.add_parser("log", help="record a job's result (the agent calls this)",
                       description="Records the result in jobs.db, emails the hiring team of strong matches, "
                                   "and prints LIMIT REACHED when the run must stop.")
    for k in ("company", "title", "url"):
        l.add_argument(f"--{k}", required=True)
    l.add_argument("--status", required=True, type=lambda s: s.strip().lower(), choices=("applied", "skipped", "failed"))
    l.add_argument("--note", default="", help="why, e.g. the question that blocked it")
    f = sub.add_parser("fit", help="ask Jev if a job matches the CV (the agent calls this)",
                       description=f"Asks the OpenRouter Decisions API ({JEV}) whether the job matches cv.yaml. "
                                   f"Prints APPLY (fit >= {FIT_APPLY}) or SKIP (logged as skipped). "
                                   "No OPENROUTER_API_KEY or an API error = APPLY.")
    for k in ("company", "title", "url"):
        f.add_argument(f"--{k}", required=True)
    f.add_argument("--jd", required=True, metavar="FILE", help="text file with the job description")
    f.add_argument("--poster", default="", help="name in the job's \"Meet the hiring team\" section, if any")
    i = sub.add_parser("issue", help="report a problem during a run (the agent calls this)",
                       description="Appends a line to issues.log and shows it as a warning in the live log.")
    i.add_argument("text", help="what happened, where, what was tried")
    i.add_argument("--url", default="", help="the job it happened on")
    sub.add_parser("mcp", help="start the browser MCP (the agent CLI runs this)",
                   description="Starts Playwright MCP with the browser of the current run (.run.json).")
    a = p.parse_args()
    if a.cmd == "run" and not a.position:
        p.error("no position: pass one or set `position` in search.toml")
    sys.exit({"run": run, "fit": fit, "tailor": tailor, "log": log, "issue": issue, "login": login, "mcp": mcp}[a.cmd](a))


if __name__ == "__main__":
    main()
