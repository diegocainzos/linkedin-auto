"""LinkedIn Easy Apply automation, a pipeline:
  1. scrape   the public (logged-out) job pages, one request at a time: never touches the LinkedIn account
  2. prepare  Jev fit check + a tailored RenderCV CV, several jobs in parallel (only APIs)
  3. apply    one job at a time in the logged-in browser: a fresh LangChain agent (OpenRouter model) + Playwright MCP per job

  uv run jobs.py login --browser brave                                    # once per browser
  uv run jobs.py run "python developer" -l Valencia --max-applies 5 -v    # run the pipeline
  uv run jobs.py tailor --company X --title Y --jd jd.txt                  # one CV by hand
"""
import argparse, asyncio, datetime, difflib, html, json, os, re, shutil, smtplib, sqlite3, subprocess, sys, threading, time, tomllib, warnings
from collections.abc import Iterator
from email.message import EmailMessage
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

warnings.filterwarnings("ignore", "Core Pydantic V1")  # langchain-core on py3.14, harmless
from langchain_core._api import suppress_langchain_beta_warning

with suppress_langchain_beta_warning():  # langchain.mcp is beta
    from langchain.mcp import MCPAdapter
from langchain.agents import create_agent
from langchain.agents.middleware import ClearToolUsesEdit, ContextEditingMiddleware, ModelCallLimitMiddleware
from langchain.tools import tool
from langchain_openrouter import ChatOpenRouter

ROOT = Path(__file__).parent.resolve()
BASE_CV, ANSWERS, LOG, APPS, SEARCH, EVENTS, LOGS = (ROOT / p for p in (
    "cv.yaml", "answers.md", "applied.jsonl", "applications", "search.toml", "run.log", "logs"))
MODEL = "~deepseek/deepseek-flash-latest"  # OpenRouter model id
ISSUES, DB = ROOT / "issues.log", ROOT / "jobs.db"
FIT_APPLY, FIT_EMAIL = 0.5, 0.9  # Jev's "compatible" probability: apply from 0.5, also email the hiring team from 0.9
JEV = "typesafe/jev-1.13"  # pinned: ~typesafe/jev-latest could change answers under us
GUEST = "https://www.linkedin.com/jobs-guest/jobs/api"  # logged-out job pages: scraping them never touches the account
DELAY = 1.0  # seconds between requests to LinkedIn's public pages
WORKERS = 4  # jobs being fit-checked and tailored at once (OpenRouter calls, no LinkedIn)
JOB_TIMEOUT = 15 * 60  # seconds the agent gets for one application
MAX_MODEL_CALLS = 80  # per application; a normal Easy Apply takes 15-40
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
    """jobs.db: one row per job found. status: new (scraped) -> ready (CV done) -> applied | skipped | failed.
    Imports applied.jsonl (the old log) on creation."""
    new = not DB.exists()
    c = sqlite3.connect(DB, isolation_level=None, timeout=30)  # autocommit
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE IF NOT EXISTS jobs (url TEXT PRIMARY KEY, ts REAL, company TEXT, title TEXT, status TEXT, "
              "note TEXT, fit REAL, poster TEXT, jd TEXT, cv_yaml TEXT, contact_email TEXT, email_subject TEXT, "
              "email_body TEXT, email_sent REAL, pdf TEXT)")
    try:  # jobs.db from before the pipeline
        c.execute("ALTER TABLE jobs ADD COLUMN pdf TEXT")
    except sqlite3.OperationalError:  # already there
        pass
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


def queued(status: str) -> list[dict]:
    """Jobs a previous run left half-way (new or ready), oldest first."""
    return [dict(r) for r in db().execute("SELECT * FROM jobs WHERE status = ? ORDER BY ts", (status,))]


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


def tailor(company: str, title: str, jd: str, model: str, folder: str = "") -> tuple[bool, str]:
    """Tailored CV for one job. Returns (ok, message); the message's last line is always a PDF path to upload."""
    out = APPS / (folder or slug(f"{company}-{title}"))
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


def prepare(j: dict, model: str) -> dict | None:
    """Jev fit check, then a tailored CV. Returns the job ready to apply, or None when it's skipped."""
    p = None
    try:
        p = jev(BASE_CV.read_text(), j["title"], j["company"], j["jd"] or "")
    except Exception as e:  # alpha API: never block the run on it
        say(f"Jev failed, applying without a compatibility check: {e}", "warn")
    if p is not None and p < FIT_APPLY:
        note = f"not compatible (fit {p:.2f})"
        save(j["url"], ts=time.time(), status="skipped", note=note, fit=p)
        say(f"skipped: {j['company']} — {j['title']} ({note})", "warn")
        return None
    say(f"fit {'?' if p is None else f'{p:.2f}'}: {j['company']} — {j['title']}")
    # the job id in the folder: the same title at the same company can be tailored twice at once
    pdf = tailor(j["company"], j["title"], j["jd"] or "", model, f"{slug(j['company'] + '-' + j['title'])}-{j['url'].split('/')[-2]}")[1]
    pdf = pdf.splitlines()[-1]
    save(j["url"], status="ready", fit=p, pdf=pdf)
    return j | {"status": "ready", "fit": p, "pdf": pdf}


def fetch(url: str) -> str:
    """GET a public LinkedIn page, politely. Rate limited (429) = wait longer and retry, twice."""
    for attempt in range(3):
        time.sleep(DELAY * (1 + 10 * attempt))
        try:
            with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as r:
                return r.read().decode()
        except HTTPError as e:
            if e.code != 429 or attempt == 2:
                raise


def guest_search(search: str, start: int) -> str:
    """The logged-in search URL as the public search endpoint: same filters, one page of results from `start`."""
    return search.replace("https://www.linkedin.com/jobs/search/?", f"{GUEST}/seeMoreJobPostings/search?") + f"&start={start}"


def parse_search(page: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"urn:li:jobPosting:(\d+)", page)))


def text(fragment: str) -> str:
    """HTML fragment -> plain text, one line per paragraph or list item."""
    lines = re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>|</(?:p|li|h\d|ul|ol)>", "\n", fragment)).splitlines()
    return "\n".join(s for s in (html.unescape(l).strip() for l in lines) if s)


def parse_job(page: str) -> dict | None:
    """A public job page -> title, company, description and the hiring team member's name ("" if none shown)."""
    if not (title := re.search(r"<h2[^>]*top-card-layout__title[^>]*>(.*?)</h2>", page, re.S)):
        return None
    company = re.search(r"topcard__org-name-link[^>]*>(.*?)</a>", page, re.S)
    jd = re.search(r"show-more-less-html__markup[^>]*>(.*?)</div>", page, re.S)
    poster = re.search(r'aria-label="Message ([^"]+)"', page)
    return {"title": text(title[1]), "company": text(company[1]) if company else "", "jd": text(jd[1]) if jd else "",
            "poster": html.unescape(poster[1]) if poster else ""}


def scrape(search: str, max_pages: int) -> Iterator[dict]:
    """New jobs from the public search pages, one request at a time, saved with status new. Known jobs cost no request."""
    known = {r[0] for r in db().execute("SELECT url FROM jobs WHERE status IS NOT NULL")}
    start = pages = 0
    while not max_pages or pages < max_pages:
        try:
            ids = parse_search(fetch(guest_search(search, start)))
        except Exception as e:
            say(f"search page failed, no more new jobs this run: {e}", "warn")
            return
        if not ids:
            return
        pages, start = pages + 1, start + len(ids)
        for i in ids:
            url = f"https://www.linkedin.com/jobs/view/{i}/"
            if url in known:
                continue
            known.add(url)
            try:
                j = parse_job(fetch(f"{GUEST}/jobPosting/{i}"))
            except Exception as e:  # 404 = taken down since the search
                say(f"job page failed, skipped: {url} ({e})", "warn")
                continue
            if not j:
                say(f"job page without a job, skipped: {url}", "warn")
                continue
            save(url, ts=time.time(), status="new", **j)
            say(f"found: {j['company']} — {j['title']}", "debug")
            yield {"url": url, **j}


EMAIL_PROMPT = """Write a short cold email from the candidate to {poster}, who posted the job "{title}" at {company}.
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


def contact(url: str, pdf: Path, model: str) -> None:
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
    try:
        body = ask(EMAIL_PROMPT.format(poster=r["poster"], title=r["title"], company=r["company"], cv=BASE_CV.read_text(),
                                       jd=r["jd"] or ""), model)
    except Exception as e:
        say(f"email to {to} not written, model call failed: {e}", "err")
        return
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


def log(j: dict, status: str, note: str, model: str) -> None:
    """A job's final result. Applied = keep the CV that was sent and email the hiring team if it's a strong match."""
    pdf = Path(j["pdf"])
    yml = pdf.with_suffix(".yaml")  # base_cv.pdf has none: cv_yaml stays empty
    save(j["url"], ts=time.time(), status=status, note=note,
         **({"cv_yaml": yml.read_text()} if status == "applied" and yml.exists() else {}))
    say(f"{status}: {j['company']} — {j['title']}" + (f" ({note})" if note else ""),
        {"applied": "ok", "skipped": "warn", "failed": "err"}[status])
    say(j["url"], "debug")
    if status == "applied":
        contact(j["url"], pdf, model)


def issue(text: str, url: str = "") -> str:
    """The agent reports a problem it hit: one line in issues.log (all runs), and a warning in the live log."""
    url = f"  {job_url(url)}" if url else ""
    with ISSUES.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(text.split())}{url}\n")
    say(f"issue: {text}{url}", "warn")
    return "noted, carry on"


SYSTEM = """You apply to ONE LinkedIn job with Easy Apply. Drive the browser with the browser_* tools.

1. Open the job URL. If the page says you already applied, the job no longer accepts applications, or there is
   no "Easy Apply" button, call log_job with status skipped and the reason as note, and stop.
2. Click "Easy Apply". At the resume step click "Upload resume", then call browser_file_upload with the given PDF path, and make sure it is selected.
3. Fill every step truthfully from <candidate_answers> and <cv>. Never invent facts.
   If a required question cannot be answered truthfully from them, discard the application and log it as skipped with the question as note.
4. Uncheck "Follow company" if present, Submit, dismiss the confirmation dialog.
5. Call log_job with the status and a short note, then reply with one line. Never open another job.

Unexpected popups (premium ads, "Save this application?", surveys, cookie banners, "Share your profile?", tooltips):
- Close them: press Escape, or click Dismiss / Close / X / "Not now" / "No thanks" / "Discard". Never click the upsell button.
- Native browser dialogs (alert/confirm/beforeunload): use browser_handle_dialog.
- If an action has no effect, take a fresh snapshot before retrying; the page probably changed.
If you are stuck on the same step after 3 attempts, log the job as failed with what blocked you as note.

Report every execution problem as soon as it happens with report_issue (what happened, where, what you tried), then carry on.
Problems = a tool error, a popup you couldn't close, a page that didn't load or looked wrong, a form field you
couldn't fill, an upload that failed, anything unexpected. Not needed for normal skips.

If LinkedIn shows a login page, a captcha or a security check, call stop_run with what you saw.
Never click anything that pays, messages people, or edits the LinkedIn profile.
Keep snapshots small: prefer targeted clicks over repeated full-page snapshots."""


def system_prompt() -> str:
    return f"{SYSTEM}\n\n<candidate_answers>\n{ANSWERS.read_text()}\n</candidate_answers>\n\n<cv>\n{BASE_CV.read_text()}\n</cv>"


class Stop(Exception):
    """Ends the run for every job: LinkedIn showed a login page, a captcha or a security check."""


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
        problems.append("OPENROUTER_API_KEY is not set: export it or put it in .env (https://openrouter.ai/keys)")
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


async def apply_one(j: dict, model: str, browser_tools: list, out: Path) -> None:
    """One job, one fresh agent: its context starts clean and dies with the job. Transcript -> out/<job id>.txt.
    The agent logs the result; if it ends without one (model call cap, gave up), the job is logged failed."""
    stop = []

    @tool
    def log_job(status: Literal["applied", "skipped", "failed"], note: str = "") -> str:
        """Record this job's result, once, at the end."""
        log(j, status, note, model)
        return "logged. Reply with one line, you're done."

    @tool
    def report_issue(text: str) -> str:
        """Report an execution problem: what happened, where, what you tried."""
        return issue(text, j["url"])

    @tool
    def stop_run(reason: str) -> str:
        """LinkedIn shows a login page, a captcha or a security check: stops the whole run."""
        stop.append(reason)
        return "stopping"

    agent = create_agent(llm(model), [*browser_tools, log_job, report_issue, stop_run], system_prompt=system_prompt(),
                         middleware=[ContextEditingMiddleware(edits=[ClearToolUsesEdit()]),  # old page snapshots
                                     ModelCallLimitMiddleware(run_limit=MAX_MODEL_CALLS, exit_behavior="end")])
    task = f"Apply to {j['title']} at {j['company']}: {j['url']}\nResume PDF to upload: {j['pdf']}"
    with (out / f"{j['url'].split('/')[-2]}.txt").open("w") as f:
        async for step in agent.astream({"messages": [{"role": "user", "content": task}]},
                                        {"recursion_limit": 10 * MAX_MODEL_CALLS}, stream_mode="updates"):
            for update in step.values():
                for m in (update or {}).get("messages", []):
                    f.write(m.pretty_repr() + "\n")
                    f.flush()
            if stop:
                raise Stop(stop[0])
    if db().execute("SELECT status FROM jobs WHERE url = ?", (j["url"],)).fetchone()[0] == "ready":
        log(j, "failed", "the agent stopped without logging a result", model)


def limit_reached(a, started: float) -> bool:
    return bool(a.max_applies and applied_since(started) >= a.max_applies
                or a.max_time and time.time() >= started + a.max_time * 3600)


async def pipeline(a, search: str, started: float, apply) -> None:
    """scrape (one request at a time) -> prepare (WORKERS at once) -> apply (one job at a time: one browser, one account).
    The queues are small, so when applying stops only a few jobs were scraped and tailored ahead.
    Jobs a crashed run left new or ready go first."""
    todo, ready = asyncio.Queue(WORKERS), asyncio.Queue(WORKERS)
    resumed = queued("ready")  # before the workers start adding to it

    async def scraper():
        for j in queued("new"):
            await todo.put(j)
        jobs = scrape(search, a.max_pages)
        while j := await asyncio.to_thread(next, jobs, None):
            await todo.put(j)
        for _ in range(WORKERS):
            await todo.put(None)

    async def worker():
        while j := await todo.get():
            try:
                if r := await asyncio.to_thread(prepare, j, a.model):
                    await ready.put(r)
            except Exception as e:
                save(j["url"], ts=time.time(), status="failed", note=f"preparing failed: {e!r}"[:500])
                say(f"preparing {j['company']} — {j['title']} failed: {e!r}", "err")

    async def producers():
        async with asyncio.TaskGroup() as g:
            g.create_task(scraper())
            for _ in range(WORKERS):
                g.create_task(worker())
        await ready.put(None)

    prod = asyncio.create_task(producers())
    try:
        while not limit_reached(a, started):
            if resumed:
                j = resumed.pop(0)
            else:
                left = started + a.max_time * 3600 - time.time() if a.max_time else None
                try:
                    j = await asyncio.wait_for(ready.get(), left)
                except TimeoutError:
                    break
            if j is None:
                say("no more jobs to apply to")
                break
            try:
                await asyncio.wait_for(apply(j), JOB_TIMEOUT)
            except Stop as e:
                say(f"run stopped: {e}", "err")
                break
            except TimeoutError:
                log(j, "failed", f"timed out after {JOB_TIMEOUT / 60:g} min", a.model)
            except Exception as e:  # a crashed agent or browser call: next job
                log(j, "failed", f"crashed: {e!r}"[:500], a.model)
            say(f"{applied_since(started)}/{a.max_applies or '∞'} applied")
    finally:
        prod.cancel()  # ponytail: a CV being tailored right now still finishes (threads can't be cancelled), ~1 min


async def browse(a, search: str, out: Path, started: float) -> None:
    """The pipeline with a real browser: Playwright MCP on the agent's logged-in profile, shared by every job."""
    exe, profile = browser(a.browser)
    pw = {"command": "npx", "args": ["-y", "@playwright/mcp@latest", "--executable-path", exe, "--user-data-dir", str(profile),
                                     "--save-session", "--output-dir", str(out / "browser")]}  # every tool call + page snapshot
    async with MCPAdapter({"mcpServers": {"browser": pw}}) as mcp:
        tools = await mcp.list_tools()
        await pipeline(a, search, started, lambda j: apply_one(j, a.model, tools, out))


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
    say(f"pipeline started ({a.model}, {a.browser}): scraping public job pages, {WORKERS} CVs at a time, "
        "one application at a time (-v for more detail)")
    (out / "prompt.txt").write_text(" ".join(sys.argv) + "\n\n" + system_prompt())
    rc = 1
    try:
        asyncio.run(browse(a, url, out, started))
        rc = 0
    except Exception as e:
        say(f"run crashed: {e!r}", "err")
    finally:  # also on Ctrl+C
        say(f"run finished (exit {rc}): {applied_since(started)} applied this run", "ok" if rc == 0 else "err")
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
Jobs are found on LinkedIn's public pages (not your account), CVs are tailored a few at a time, then a
LangChain agent (an OpenRouter model) applies to one job at a time in your browser, starting fresh for each.
Answers come from answers.md, the CV from cv.yaml.
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
Ctrl+C stops at any time; processed jobs are skipped next run (jobs.db).

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
    g.add_argument("--max-pages", type=int, default=0, metavar="N", help="result pages to go through, 10 jobs each" + now)
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
