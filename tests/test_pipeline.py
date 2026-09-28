"""uv run pytest -q    (no network, no browser, no API keys: LinkedIn pages are saved fixtures, models are fakes)"""
import asyncio, sqlite3, sys, threading, time
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import jobs
from langchain.messages import AIMessage
from langchain.tools import tool
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

FIX = Path(__file__).parent / "fixtures"
SEARCH_URL = "https://www.linkedin.com/jobs/search/?keywords=python&location=Valencia&f_AL=true"


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """Every file jobs.py writes goes to tmp_path; no sleeping between requests."""
    for name, f in {"DB": "jobs.db", "EVENTS": "run.log", "ISSUES": "issues.log", "LOG": "applied.jsonl"}.items():
        monkeypatch.setattr(jobs, name, tmp_path / f)
    monkeypatch.setattr(jobs, "APPS", tmp_path / "applications")
    monkeypatch.setattr(jobs, "DELAY", 0)


def args(**kw):
    return SimpleNamespace(**{"max_applies": 0, "max_time": 0, "max_pages": 0, "model": "fake", **kw})


def row(url):
    r = jobs.db().execute("SELECT * FROM jobs WHERE url = ?", (jobs.job_url(url),)).fetchone()
    return dict(r) if r else None


def job(i, status="new", **cols):
    url = f"https://www.linkedin.com/jobs/view/{i}/"
    jobs.save(url, **{"ts": time.time(), "company": f"Co{i}", "title": f"Dev {i}", "jd": "jd", "status": status, **cols})
    return row(url)


# --- parsing the public (logged-out) LinkedIn pages -------------------------------------------------------------

def test_parse_search_gives_unique_ids_in_page_order():
    ids = jobs.parse_search((FIX / "search.html").read_text())
    assert len(ids) == 10 and len(set(ids)) == 10
    assert ids[:3] == ["4472284210", "4472283267", "4464754429"]


def test_parse_search_empty_page():
    assert jobs.parse_search("") == []


def test_parse_job():
    j = jobs.parse_job((FIX / "job.html").read_text())
    assert j["title"] == "Senior Backend Engineer, Integrations (Python)"
    assert j["company"] == "ruit"
    assert j["poster"] == ""
    assert len(j["jd"]) > 500 and "<" not in j["jd"] and "&amp;" not in j["jd"]


def test_parse_job_with_hiring_team():
    j = jobs.parse_job((FIX / "job_poster.html").read_text())
    assert j["poster"] == "Jane Doe"
    assert j["jd"].startswith("Buscamos Técnico/a de I+D con conocimientos de Python")
    assert "\n" in j["jd"]  # paragraphs survive as lines


def test_parse_job_rejects_a_page_without_a_job():
    assert jobs.parse_job("<html>rate limited</html>") is None


def test_guest_search_url():
    assert jobs.guest_search(SEARCH_URL, 20) == (
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
        "?keywords=python&location=Valencia&f_AL=true&start=20")


# --- fetch: retries on rate limits ------------------------------------------------------------------------------

def test_fetch_retries_429(monkeypatch):
    calls = []

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *e): pass
        def read(self): return b"ok"

    def urlopen(req, timeout):
        calls.append(req.full_url)
        if len(calls) < 3:
            raise HTTPError(req.full_url, 429, "Too Many Requests", {}, None)
        return Resp()

    monkeypatch.setattr(jobs, "urlopen", urlopen)
    assert jobs.fetch("https://x") == "ok" and len(calls) == 3


def test_fetch_gives_up_after_retries(monkeypatch):
    def urlopen(req, timeout):
        raise HTTPError(req.full_url, 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(jobs, "urlopen", urlopen)
    with pytest.raises(HTTPError):
        jobs.fetch("https://x")


def test_fetch_does_not_retry_404(monkeypatch):
    calls = []

    def urlopen(req, timeout):
        calls.append(1)
        raise HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(jobs, "urlopen", urlopen)
    with pytest.raises(HTTPError):
        jobs.fetch("https://x")
    assert len(calls) == 1


# --- scrape: sequential pages -> new jobs in jobs.db ------------------------------------------------------------

def fake_site(monkeypatch, pages: list[list[str]], broken=()):
    """A LinkedIn with these search result pages (start= is an offset, like the real one); every job page is the fixture."""
    fetched = []
    card = '<div data-entity-urn="urn:li:jobPosting:{}"></div>'
    page = (FIX / "job.html").read_text()
    offsets, n = {}, 0
    for p in pages:
        offsets[n], n = p, n + len(p)

    def fetch(url):
        fetched.append(url)
        if "seeMoreJobPostings" in url:
            return "".join(card.format(i) for i in offsets.get(int(url.rsplit("start=", 1)[1]), []))
        i = url.rsplit("/", 1)[1]
        if i in broken:
            raise HTTPError(url, 404, "Not Found", {}, None)
        return page.replace("Senior Backend Engineer", f"Job {i}")

    monkeypatch.setattr(jobs, "fetch", fetch)
    return fetched


def test_scrape_saves_new_jobs_and_pages_until_empty(monkeypatch):
    fake_site(monkeypatch, [["1", "2"], ["3"]])
    got = list(jobs.scrape(SEARCH_URL, 0))
    assert [j["url"] for j in got] == [f"https://www.linkedin.com/jobs/view/{i}/" for i in "123"]
    r = row(got[0]["url"])
    assert r["status"] == "new" and r["company"] == "ruit" and r["title"].startswith("Job 1") and len(r["jd"]) > 500


def test_scrape_respects_max_pages(monkeypatch):
    fake_site(monkeypatch, [["1"], ["2"], ["3"]])
    assert len(list(jobs.scrape(SEARCH_URL, 2))) == 2


def test_scrape_skips_known_jobs_and_repeats(monkeypatch):
    job(1, "applied")
    job(2, "skipped")
    job(3, "ready")
    job(4, None)  # opened by an old run that crashed: scrape it again
    fetched = fake_site(monkeypatch, [["1", "2", "3", "4", "5"], ["5"]])
    assert [j["url"][-2] for j in jobs.scrape(SEARCH_URL, 0)] == ["4", "5"]
    assert sum("/jobPosting/" in u for u in fetched) == 2  # known jobs cost no request


def test_scrape_skips_a_broken_job_page(monkeypatch):
    fake_site(monkeypatch, [["1", "2", "3"]], broken={"2"})
    assert [j["url"][-2] for j in jobs.scrape(SEARCH_URL, 0)] == ["1", "3"]


def test_scrape_stops_when_the_search_fails(monkeypatch):
    def fetch(url):
        raise HTTPError(url, 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(jobs, "fetch", fetch)
    assert list(jobs.scrape(SEARCH_URL, 0)) == []
    assert "search page failed" in jobs.EVENTS.read_text()


# --- prepare: Jev fit + tailored CV (runs in parallel threads) --------------------------------------------------

def test_prepare_skips_a_poor_fit(monkeypatch):
    monkeypatch.setattr(jobs, "jev", lambda *a: 0.2)
    monkeypatch.setattr(jobs, "tailor", lambda *a: pytest.fail("no CV for a skipped job"))
    j = job(1)
    assert jobs.prepare(j, "m") is None
    r = row(j["url"])
    assert r["status"] == "skipped" and r["fit"] == 0.2 and "0.20" in r["note"]


def test_prepare_tailors_a_good_fit(monkeypatch):
    monkeypatch.setattr(jobs, "jev", lambda *a: 0.8)
    monkeypatch.setattr(jobs, "tailor", lambda *a: (True, "/cv/Co1.pdf"))
    j = jobs.prepare(job(1), "m")
    assert j["status"] == "ready" and j["pdf"] == "/cv/Co1.pdf" and j["fit"] == 0.8
    assert row(j["url"])["pdf"] == "/cv/Co1.pdf"


def test_prepare_same_title_twice_gets_two_folders(monkeypatch):
    """Two jobs with the same title at the same company are tailored at the same time: they can't share a folder."""
    folders = []
    monkeypatch.setattr(jobs, "jev", lambda *a: 0.8)
    monkeypatch.setattr(jobs, "tailor", lambda company, title, jd, model, folder: folders.append(folder) or (True, "/x.pdf"))
    jobs.prepare(job(1) | {"company": "Acme", "title": "Dev"}, "m")
    jobs.prepare(job(2) | {"company": "Acme", "title": "Dev"}, "m")
    assert len(set(folders)) == 2


def test_prepare_applies_when_jev_is_down(monkeypatch):
    def down(*a):
        raise OSError("alpha API down")

    monkeypatch.setattr(jobs, "jev", down)
    monkeypatch.setattr(jobs, "tailor", lambda *a: (True, "/cv.pdf"))
    assert jobs.prepare(job(1), "m")["status"] == "ready"


def test_prepare_falls_back_to_the_base_cv(monkeypatch):
    monkeypatch.setattr(jobs, "jev", lambda *a: 0.9)
    monkeypatch.setattr(jobs, "tailor", lambda *a: (False, "CV tailoring failed, upload the base CV instead:\n/base_cv.pdf"))
    assert jobs.prepare(job(1), "m")["pdf"] == "/base_cv.pdf"


# --- jobs.db ----------------------------------------------------------------------------------------------------

def test_old_db_gets_the_pdf_column():
    c = sqlite3.connect(jobs.DB)
    c.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY, ts REAL, company TEXT, title TEXT, status TEXT, note TEXT, fit REAL, "
              "poster TEXT, jd TEXT, cv_yaml TEXT, contact_email TEXT, email_subject TEXT, email_body TEXT, email_sent REAL)")
    c.execute("INSERT INTO jobs (url, status) VALUES ('https://www.linkedin.com/jobs/view/1/', 'applied')")
    c.commit()
    c.close()
    jobs.save("https://www.linkedin.com/jobs/view/1/", pdf="/x.pdf")
    r = row("https://www.linkedin.com/jobs/view/1/")
    assert (r["status"], r["pdf"]) == ("applied", "/x.pdf")


def test_queued_oldest_first():
    job(2, "ready", ts=2)
    job(1, "ready", ts=1)
    job(3, "new", ts=0)
    assert [j["url"][-2] for j in jobs.queued("ready")] == ["1", "2"]


# --- pipeline: parallel prepare, one browser job at a time, hard limits ------------------------------------------

class Tracker:
    """Counts how many calls run at the same time."""
    def __init__(self):
        self.now = self.max = self.calls = 0
        self.lock = threading.Lock()

    def __enter__(self):
        with self.lock:
            self.now += 1
            self.calls += 1
            self.max = max(self.max, self.now)

    def __exit__(self, *e):
        with self.lock:
            self.now -= 1


def fake_pipeline(monkeypatch, n_jobs=40, prep_s=0.05, apply_s=0.01, result="applied", skip=()):
    scraped, prep, applying, applied = [], Tracker(), Tracker(), []

    def scrape(url, max_pages):
        for i in range(n_jobs):
            scraped.append(i)
            yield job(1000 + i)

    def prepare(j, model):
        with prep:
            time.sleep(prep_s)
        if int(j["url"].split("/")[-2]) - 1000 in skip:
            jobs.save(j["url"], status="skipped")
            return None
        jobs.save(j["url"], status="ready", pdf="/cv.pdf")
        return j | {"status": "ready", "pdf": "/cv.pdf"}

    async def apply(j):
        with applying:
            await asyncio.sleep(apply_s)
            applied.append(j["url"])
            jobs.save(j["url"], ts=time.time(), status=result)

    monkeypatch.setattr(jobs, "scrape", scrape)
    monkeypatch.setattr(jobs, "prepare", prepare)
    return SimpleNamespace(scraped=scraped, prep=prep, applying=applying, applied=applied, apply=apply)


def run_pipeline(a, apply):
    asyncio.run(jobs.pipeline(a, SEARCH_URL, time.time(), apply))


def test_pipeline_hard_stops_at_max_applies(monkeypatch):
    f = fake_pipeline(monkeypatch)
    run_pipeline(args(max_applies=5), f.apply)
    assert len(f.applied) == 5
    assert jobs.db().execute("SELECT COUNT(*) FROM jobs WHERE status = 'applied'").fetchone()[0] == 5


def test_pipeline_one_browser_job_at_a_time_but_parallel_prepare(monkeypatch):
    f = fake_pipeline(monkeypatch, n_jobs=12, prep_s=0.1)
    run_pipeline(args(), f.apply)
    assert len(f.applied) == 12
    assert f.applying.max == 1
    assert 1 < f.prep.max <= jobs.WORKERS


def test_pipeline_does_not_scrape_or_tailor_far_ahead(monkeypatch):
    f = fake_pipeline(monkeypatch, n_jobs=500, prep_s=0.001, apply_s=0.05)
    run_pipeline(args(max_applies=3), f.apply)
    time.sleep(0.2)  # let the scrape thread notice
    ahead = 3 * jobs.WORKERS + 2  # ready queue + workers + todo queue + the job in the scraper's hand
    assert len(f.scraped) <= 3 + ahead and f.prep.calls <= 3 + ahead


def test_pipeline_skipped_jobs_do_not_count(monkeypatch):
    f = fake_pipeline(monkeypatch, n_jobs=10, skip={0, 1, 2})
    run_pipeline(args(max_applies=4), f.apply)
    assert len(f.applied) == 4 and all(int(u.split("/")[-2]) - 1000 > 2 for u in f.applied)


def test_pipeline_failed_applications_do_not_count(monkeypatch):
    f = fake_pipeline(monkeypatch, n_jobs=6, result="failed")
    run_pipeline(args(max_applies=2), f.apply)
    assert len(f.applied) == 6  # it kept trying, none succeeded


def test_pipeline_hard_stops_at_max_time(monkeypatch):
    f = fake_pipeline(monkeypatch, n_jobs=50, prep_s=0, apply_s=0.1)
    t = time.time()
    run_pipeline(args(max_time=0.35 / 3600), f.apply)
    assert 2 <= len(f.applied) <= 5 and time.time() - t < 1.5


def test_pipeline_resumes_ready_jobs_first(monkeypatch):
    job(1, "ready", pdf="/old.pdf", ts=1)
    f = fake_pipeline(monkeypatch, n_jobs=2)
    run_pipeline(args(), f.apply)
    assert f.applied[0] == "https://www.linkedin.com/jobs/view/1/" and len(f.applied) == 3


def test_pipeline_resumes_new_jobs_before_scraping(monkeypatch):
    job(2, "new", ts=1)
    seen = []
    f = fake_pipeline(monkeypatch, n_jobs=2)
    prepare = jobs.prepare
    monkeypatch.setattr(jobs, "prepare", lambda j, m: seen.append(j["url"]) or prepare(j, m))
    run_pipeline(args(), f.apply)
    assert seen[0] == "https://www.linkedin.com/jobs/view/2/" and len(f.applied) == 3


def test_pipeline_times_out_a_stuck_job_and_goes_on(monkeypatch):
    monkeypatch.setattr(jobs, "JOB_TIMEOUT", 0.2)
    f = fake_pipeline(monkeypatch, n_jobs=3)
    stuck = []

    async def apply(j):
        if not stuck:
            stuck.append(j["url"])
            await asyncio.sleep(10)
        await f.apply(j)

    t = time.time()
    run_pipeline(args(), apply)
    assert row(stuck[0])["status"] == "failed" and "timed out" in row(stuck[0])["note"]
    assert len(f.applied) == 2 and time.time() - t < 3


def test_pipeline_survives_a_crashing_job(monkeypatch):
    f = fake_pipeline(monkeypatch, n_jobs=3)

    async def apply(j):
        if j["url"].endswith("/1000/"):
            raise RuntimeError("browser died")
        await f.apply(j)

    run_pipeline(args(), apply)
    assert row("https://www.linkedin.com/jobs/view/1000/")["status"] == "failed"
    assert len(f.applied) == 2


def test_pipeline_stops_when_the_browser_is_logged_out(monkeypatch):
    fake_pipeline(monkeypatch, n_jobs=5)

    async def apply(j):
        raise jobs.Stop("LinkedIn asks to log in")

    run_pipeline(args(), apply)
    assert "LinkedIn asks to log in" in jobs.EVENTS.read_text()
    assert jobs.db().execute("SELECT COUNT(*) FROM jobs WHERE status = 'failed'").fetchone()[0] == 0


def test_pipeline_survives_a_crashing_prepare(monkeypatch):
    f = fake_pipeline(monkeypatch, n_jobs=4)
    prepare = jobs.prepare

    def flaky(j, m):
        if j["url"].endswith("/1001/"):
            raise RuntimeError("rendercv exploded")
        return prepare(j, m)

    monkeypatch.setattr(jobs, "prepare", flaky)
    run_pipeline(args(), f.apply)
    assert len(f.applied) == 3
    assert row("https://www.linkedin.com/jobs/view/1001/")["status"] == "failed"


# --- apply_one: a fresh agent per job, with a hard cap on model calls -------------------------------------------

class FakeModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kw):
        return self


def call(name, **a):
    return AIMessage("", tool_calls=[{"name": name, "args": a, "id": f"{name}-{time.time_ns()}", "type": "tool_call"}])


@tool
def browser_click(ref: str) -> str:
    """Click an element."""
    return "clicked"


def apply_with(monkeypatch, tmp_path, messages):
    monkeypatch.setattr(jobs, "llm", lambda model: FakeModel(messages=iter(messages)))
    monkeypatch.setattr(jobs, "contact", lambda *a: None)
    j = job(1, "ready", pdf="/cv/Co1.pdf")
    asyncio.run(jobs.apply_one(j, "fake", [browser_click], tmp_path))
    return row(j["url"])


def test_apply_one_logs_what_the_agent_reports(monkeypatch, tmp_path):
    r = apply_with(monkeypatch, tmp_path, [call("browser_click", ref="e1"), call("log_job", status="applied", note="ok"),
                                           AIMessage("done")])
    assert r["status"] == "applied" and r["note"] == "ok"
    assert "browser_click" in (tmp_path / "1.txt").read_text()  # per-job transcript


def test_apply_one_prompt_has_the_job_and_the_pdf(monkeypatch, tmp_path):
    seen = []

    class Spy(FakeModel):
        def _generate(self, messages, *a, **kw):
            seen.append(messages)
            return super()._generate(messages, *a, **kw)

    monkeypatch.setattr(jobs, "llm", lambda model: Spy(messages=iter([call("log_job", status="applied"), AIMessage("done")])))
    monkeypatch.setattr(jobs, "contact", lambda *a: None)
    asyncio.run(jobs.apply_one(job(1, "ready", pdf="/cv/Co1.pdf"), "fake", [browser_click], tmp_path))
    text = "\n".join(str(m.content) for m in seen[0])
    assert "https://www.linkedin.com/jobs/view/1/" in text and "/cv/Co1.pdf" in text and "<candidate_answers>" in text


def test_apply_one_without_log_is_failed(monkeypatch, tmp_path):
    r = apply_with(monkeypatch, tmp_path, [AIMessage("I think I'm done")])
    assert r["status"] == "failed" and "without logging" in r["note"]


def test_apply_one_caps_model_calls(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "MAX_MODEL_CALLS", 3)
    r = apply_with(monkeypatch, tmp_path, [call("browser_click", ref=f"e{i}") for i in range(50)])
    assert r["status"] == "failed"
    assert (tmp_path / "1.txt").read_text().count("Tool Calls:") <= 3


def test_apply_one_skip_is_kept(monkeypatch, tmp_path):
    r = apply_with(monkeypatch, tmp_path, [call("log_job", status="skipped", note="asks for a driving licence"),
                                           AIMessage("done")])
    assert r["status"] == "skipped"


def test_apply_one_stop_raises(monkeypatch, tmp_path):
    with pytest.raises(jobs.Stop, match="captcha"):
        apply_with(monkeypatch, tmp_path, [call("stop_run", reason="captcha"), AIMessage("stopped")])


def test_log_job_emails_only_after_applying(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(jobs, "llm", lambda model: FakeModel(messages=iter([call("log_job", status="applied"), AIMessage("ok")])))
    monkeypatch.setattr(jobs, "contact", lambda url, pdf, model: sent.append(pdf))
    asyncio.run(jobs.apply_one(job(1, "ready", pdf="/cv/Co1.pdf"), "fake", [browser_click], tmp_path))
    monkeypatch.setattr(jobs, "llm", lambda model: FakeModel(messages=iter([call("log_job", status="skipped"), AIMessage("ok")])))
    asyncio.run(jobs.apply_one(job(2, "ready", pdf="/cv/Co2.pdf"), "fake", [browser_click], tmp_path))
    assert sent == [Path("/cv/Co1.pdf")]
