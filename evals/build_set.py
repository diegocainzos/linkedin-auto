"""Collect public LinkedIn jobs (no login) for the Jev eval: evals/jev_set.jsonl, one job per line."""
import json, sys
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).parent.parent))
import jobs

OUT = Path(__file__).parent / "jev_set.jsonl"
SEARCHES = [(k, loc) for k in ("python developer", "ai engineer", "machine learning engineer", "data scientist",
                               "data engineer", "backend developer", "software engineer", "llm")
            for loc in ("Valencia", "Spain")]
PER_SEARCH = 10

seen = {json.loads(l)["url"] for l in OUT.read_text().splitlines()} if OUT.exists() else set()
with OUT.open("a") as f:
    for k, loc in SEARCHES:
        url = "https://www.linkedin.com/jobs/search/?" + urlencode({"keywords": k, "location": loc, "f_AL": "true", "f_TPR": "r2592000"})
        ids = jobs.parse_search(jobs.fetch(jobs.guest_search(url, 0)))[:PER_SEARCH]
        for i in ids:
            u = f"https://www.linkedin.com/jobs/view/{i}/"
            if u in seen:
                continue
            seen.add(u)
            try:
                j = jobs.parse_job(jobs.fetch(f"{jobs.GUEST}/jobPosting/{i}"))
            except Exception as e:
                print("skip", u, e)
                continue
            if j and len(j["jd"]) > 300:
                f.write(json.dumps({"url": u, "search": k, "company": j["company"], "title": j["title"], "jd": j["jd"]},
                                   ensure_ascii=False) + "\n")
                f.flush()
        print(k, loc, len(seen), flush=True)
