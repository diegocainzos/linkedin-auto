"""Compare Jev question variants against hand labels.  uv run evals/jev_eval.py
jev_set.jsonl (jobs) + jev_labels.json ({url: 1 apply / 0 skip}) -> AUC and the best threshold per variant.
Answers are cached in jev_cache.json: re-running only asks Jev about new jobs or variants."""
import json, os, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).parent.parent))
import jobs

HERE = Path(__file__).parent
IGNORE = ("Ignore seniority, years of experience, degree requirements and the job title's level entirely: "
          "the candidate applies to senior roles too.")
VARIANTS = {
    "current": {"type": "noul",
                "instructions": "Judging only by the candidate's CV against the job's requirements, is the candidate a real match for this job?",
                "criteria": {"true": "The CV meets the job's must-have requirements: main tech stack, seniority or years "
                                     "of experience, required languages, location or work permit.",
                             "false": "The CV lacks at least one must-have requirement, or the job is in another field or seniority."}},
    "stack": {"type": "noul",
              "instructions": "Does this job fit the candidate's skills: the same kind of work and a main tech stack the CV shows? " + IGNORE,
              "criteria": {"true": "The job's core work is in the candidate's field and most of its core technologies appear in the CV.",
                           "false": "The job's core work is in another field or stack than the CV shows (another main programming "
                                    "language, embedded or hardware, ERP consulting, non-software work), or it requires a spoken "
                                    "language the CV doesn't list."}},
    "doable": {"type": "noul",
               "instructions": "Could the candidate credibly do this job's day-to-day work with the skills in the CV? " + IGNORE,
               "criteria": {"true": "The daily work (languages, frameworks, kind of problems) is what the CV shows the candidate doing.",
                            "false": "The daily work needs a main language, platform or domain the CV doesn't show, "
                                     "or a spoken language the CV doesn't list."}},
    "field": {"type": "noul",
              "instructions": "Is this job in the candidate's professional field? " + IGNORE,
              "criteria": {"true": "Same field as the CV (e.g. both AI/ML, both data, both Python backend), even if some tools differ.",
                           "false": "A different field: another core stack, hardware, industrial or non-software work, sales, "
                                    "or a spoken language the CV doesn't list."}},
    "field2": {"type": "noul",
               "instructions": "Is this job in the candidate's professional field, with a main programming language the CV shows? " + IGNORE,
               "criteria": {"true": "Same field as the CV (e.g. both AI/ML, both data, both Python backend), even if some tools, "
                                    "clouds or data platforms differ.",
                            "false": "The core work is in a main language the CV doesn't show (e.g. Java, C#/.NET, Go, Ruby, PHP, "
                                     "C++, JavaScript/TypeScript), even when the product uses AI; or it's platform administration, "
                                     "hardware, industrial or non-software work; or it needs a spoken language the CV doesn't list."}},
    "overlap": {"type": "score",
                "instructions": "How much of the job's core technical requirements does the CV cover? " + IGNORE,
                "criteria": ["none: different field or stack", "a little: a few shared tools, different core work",
                             "partial: same field, some core tools missing", "most: same field, most core tools present",
                             "all: the CV covers the core requirements"]},
}


def ask(job: dict, cv: str) -> dict:
    body = {"model": jobs.JEV, "state": {"candidate_cv": cv, "job_offer": f"{job['title']} at {job['company']}\n\n{job['jd']}"},
            "questions": VARIANTS}
    req = Request("https://openrouter.ai/api/alpha/decisions", json.dumps(body).encode(),
                  {"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}", "Content-Type": "application/json"})
    with urlopen(req, timeout=120) as r:
        a = json.load(r)["answers"]
    return {k: a[k]["noul"] if VARIANTS[k]["type"] == "noul" else a[k]["score"] / (len(VARIANTS[k]["criteria"]) - 1)
            for k in VARIANTS}


def auc(pos: list[float], neg: list[float]) -> float:
    return sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))


def at(t: float, pairs) -> tuple[float, float, float]:
    tp = sum(s >= t and y for s, y in pairs)
    fp = sum(s >= t and not y for s, y in pairs)
    fn = sum(s < t and y for s, y in pairs)
    p, r = tp / (tp + fp or 1), tp / (tp + fn or 1)
    return p, r, 5 * p * r / (4 * p + r or 1)  # F2: missing a good job costs more than trying a bad one


if __name__ == "__main__":
    js = [json.loads(l) for l in (HERE / "jev_set.jsonl").read_text().splitlines()]
    labels = json.loads((HERE / "jev_labels.json").read_text())
    js = [j for j in js if j["url"] in labels]
    cache_f = HERE / "jev_cache.json"
    cache = json.loads(cache_f.read_text()) if cache_f.exists() else {}
    cv = jobs.BASE_CV.read_text()
    todo = [j for j in js if set(VARIANTS) - set(cache.get(j["url"], {}))]
    with ThreadPoolExecutor(8) as ex:
        for j, a in zip(todo, ex.map(lambda j: ask(j, cv), todo), strict=True):
            cache[j["url"]] = cache.get(j["url"], {}) | a
    cache_f.write_text(json.dumps(cache, indent=1))
    print(f"{len(js)} jobs, {sum(labels[j['url']] for j in js)} labelled apply\n")
    print(f"{'variant':9} {'AUC':>5}  {'@0.5 P/R':>11}  best t   P     R     F2")
    for k in VARIANTS:
        pairs = [(cache[j["url"]][k], labels[j["url"]]) for j in js]
        best = max((at(t / 100, pairs) + (t / 100,) for t in range(1, 100)), key=lambda x: (x[2], x[3]))
        p5, r5, _ = at(0.5, pairs)
        print(f"{k:9} {auc([s for s, y in pairs if y], [s for s, y in pairs if not y]):5.2f}  {p5:5.2f}/{r5:4.2f}"
              f"   {best[3]:4.2f}  {best[0]:4.2f}  {best[1]:4.2f}  {best[2]:4.2f}")
    if "-v" in sys.argv:  # disagreements of the best variant, to read
        k = sys.argv[sys.argv.index("-v") + 1]
        for j in sorted(js, key=lambda j: cache[j["url"]][k]):
            print(f"{cache[j['url']][k]:.2f} {labels[j['url']]} {j['company'][:25]:25} {j['title'][:70]}")
