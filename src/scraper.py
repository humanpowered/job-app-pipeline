"""
Pulls open postings from public Greenhouse and Lever job board APIs.
No auth required -- these endpoints are meant for public consumption
(they're what powers the "Careers" widget on a company's own site).

Also pulls from search-style aggregators (Jooble, Adzuna, Remotive) that
return postings across many companies for a keyword query, rather than one
company at a time. Those results still pass through the same
title_keywords/locations filter as everything else -- the aggregator's own
search is a coarse first net.
"""
import os
import re

import requests
import yaml
from pathlib import Path

CONFIG_PATH = Path(__file__).parent.parent / "config" / "boards.yaml"


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def fetch_greenhouse(token: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])
    return [
        {
            "source": "greenhouse",
            "company": token,
            "title": j["title"],
            "location": j.get("location", {}).get("name", ""),
            "url": j["absolute_url"],
            "description_html": j.get("content", ""),
            "posting_id": str(j["id"]),
        }
        for j in jobs
    ]


def fetch_lever(token: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    jobs = resp.json()
    return [
        {
            "source": "lever",
            "company": token,
            "title": j["text"],
            "location": j.get("categories", {}).get("location", ""),
            "url": j["hostedUrl"],
            "description_html": j.get("descriptionPlain", j.get("description", "")),
            "posting_id": j["id"],
        }
        for j in jobs
    ]


def fetch_workday(tenant: str, host: str, site: str, max_pages: int = 5) -> list[dict]:
    """
    Workday doesn't have one uniform public API, but most tenants expose an
    internal CXS endpoint used by their own careers page, which we can call
    the same way the page's JS does. Structure/behavior can vary by tenant --
    if this returns nothing, open the careers page devtools Network tab,
    filter for 'jobs', and adjust the payload shape to match.
    """
    base = f"https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    headers = {"Content-Type": "application/json"}
    results = []
    offset = 0
    page_size = 20

    for _ in range(max_pages):
        payload = {"limit": page_size, "offset": offset, "searchText": ""}
        resp = requests.post(base, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        postings = data.get("jobPostings", [])
        if not postings:
            break
        for p in postings:
            results.append({
                "source": "workday",
                "company": tenant,
                "title": p.get("title", ""),
                "location": p.get("locationsText", ""),
                "url": f"https://{tenant}.{host}.myworkdayjobs.com/{site}{p.get('externalPath', '')}",
                "description_html": "",  # Workday needs a per-posting fetch for full description
                "posting_id": p.get("bulletFields", [None])[0] or p.get("externalPath", ""),
            })
        offset += page_size
        if len(postings) < page_size:
            break

    return results


def fetch_ashby(board_name: str) -> list[dict]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{board_name}"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])
    return [
        {
            "source": "ashby",
            "company": board_name,
            "title": j["title"],
            "location": j.get("location", ""),
            "url": j["jobUrl"],
            "description_html": j.get("descriptionHtml", ""),
            "posting_id": j["id"],
        }
        for j in jobs
    ]


def fetch_smartrecruiters(company: str, keywords: list[str]) -> list[dict]:
    """
    The listing endpoint doesn't include the job description, so to avoid an
    expensive per-posting fetch for every listed job, only postings whose
    title already matches title_keywords get a detail fetch.
    """
    url = f"https://api.smartrecruiters.com/v1/companies/{company}/postings?limit=100"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    postings = resp.json().get("content", [])
    keywords_lower = [k.lower() for k in keywords]

    results = []
    for p in postings:
        title = p.get("name", "")
        if not _title_matches(title, keywords_lower):
            continue
        loc = p.get("location", {})
        location = loc.get("fullLocation", "") or ("Remote" if loc.get("remote") else "")

        detail_url = f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{p['id']}"
        detail_resp = requests.get(detail_url, timeout=15)
        detail_resp.raise_for_status()
        detail = detail_resp.json()
        sections = detail.get("jobAd", {}).get("sections", {})
        description = " ".join(s.get("text", "") for s in sections.values())

        results.append({
            "source": "smartrecruiters",
            "company": company,
            "title": title,
            "location": location,
            "url": detail.get("postingUrl", f"https://jobs.smartrecruiters.com/{company}/{p['id']}"),
            "description_html": description,
            "posting_id": p["id"],
        })
    return results


def fetch_workable(account: str) -> list[dict]:
    url = f"https://apply.workable.com/api/v1/widget/accounts/{account}?details=true"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])
    results = []
    for j in jobs:
        location = ", ".join(filter(None, [j.get("city", ""), j.get("state", ""), j.get("country", "")]))
        if j.get("telecommuting"):
            location = f"Remote ({location})" if location else "Remote"
        results.append({
            "source": "workable",
            "company": account,
            "title": j["title"],
            "location": location,
            "url": j.get("url", j.get("shortlink", "")),
            "description_html": j.get("description", ""),
            "posting_id": j["shortcode"],
        })
    return results


def fetch_jooble(api_key: str, keywords: str, location: str = "", pages: int = 1) -> list[dict]:
    """
    Jooble is a search-style job aggregator, not a per-company board -- you
    query it with keywords/location instead of a company token. Requires a
    free key from https://jooble.org/api/about, passed via JOOBLE_API_KEY.
    """
    url = f"https://jooble.org/api/{api_key}"
    results = []
    for page in range(1, pages + 1):
        payload = {"keywords": keywords, "location": location, "page": str(page)}
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        jobs = resp.json().get("jobs", [])
        if not jobs:
            break
        for j in jobs:
            results.append({
                "source": "jooble",
                "company": j.get("company", ""),
                "title": j.get("title", ""),
                "location": j.get("location", ""),
                "url": j.get("link", ""),
                "description_html": j.get("snippet", ""),
                "posting_id": str(j.get("id", j.get("link", ""))),
            })
    return results


def fetch_adzuna(app_id: str, app_key: str, country: str, keywords: str,
                  results_per_page: int = 50, pages: int = 1) -> list[dict]:
    """
    Adzuna is another search-style aggregator, spanning many boards per
    country. Requires a free app_id/app_key from
    https://developer.adzuna.com, passed via ADZUNA_APP_ID / ADZUNA_APP_KEY.
    """
    results = []
    for page in range(1, pages + 1):
        url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
        params = {
            "app_id": app_id,
            "app_key": app_key,
            "what": keywords,
            "results_per_page": results_per_page,
            "content-type": "application/json",
        }
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        jobs = resp.json().get("results", [])
        if not jobs:
            break
        for j in jobs:
            results.append({
                "source": "adzuna",
                "company": j.get("company", {}).get("display_name", ""),
                "title": j.get("title", ""),
                "location": j.get("location", {}).get("display_name", ""),
                "url": j.get("redirect_url", ""),
                "description_html": j.get("description", ""),
                "posting_id": str(j.get("id", "")),
            })
    return results


def fetch_remotive() -> list[dict]:
    """
    Remotive lists remote-only postings and needs no API key. Since every
    listing is already remote, this is a good complement to the
    company-by-company boards for the "include remote roles" net.
    Remotive asks that you not poll this more than a few times a day --
    fine here since the pipeline is run manually, not on a schedule.
    """
    url = "https://remotive.com/api/remote-jobs"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])
    return [
        {
            "source": "remotive",
            "company": j.get("company_name", ""),
            "title": j.get("title", ""),
            "location": j.get("candidate_required_location", "") or "Remote",
            "url": j.get("url", ""),
            "description_html": j.get("description", ""),
            "posting_id": str(j.get("id", "")),
        }
        for j in jobs
    ]


def fetch_jobicy(industries: list[str], geo: str = "", count: int = 50) -> list[dict]:
    """
    Jobicy lists remote-only postings and needs no API key. Querying by
    industry is far more productive than the unfiltered feed -- testing
    showed the default feed returns almost no marketing-analytics roles,
    while industry=data-science returns them consistently. Also one of the
    few free sources that returns salary ranges.
    """
    results = []
    for industry in industries or [""]:
        params = {"count": count}
        if industry:
            params["industry"] = industry
        if geo:
            params["geo"] = geo
        resp = requests.get("https://jobicy.com/api/v2/remote-jobs", params=params,
                            headers={"User-Agent": "Mozilla/5.0 (job-search-script)"},
                            timeout=20)
        resp.raise_for_status()
        for j in resp.json().get("jobs", []):
            salary = ""
            if j.get("salaryMin") and j.get("salaryMax"):
                cur = j.get("salaryCurrency", "USD")
                per = j.get("salaryPeriod", "")
                salary = f" [{cur} {j['salaryMin']}-{j['salaryMax']} {per}]".rstrip() + "]"
                salary = salary.replace("]]", "]")
            # Every Jobicy listing is remote, but jobGeo reports the eligible
            # region ("USA") rather than saying so -- which the location
            # filter would otherwise reject. Mark it explicitly.
            geo_text = (j.get("jobGeo") or "").strip()
            location = f"Remote ({geo_text})" if geo_text else "Remote"
            results.append({
                "source": "jobicy",
                "company": j.get("companyName", ""),
                "title": j.get("jobTitle", ""),
                "location": location,
                "url": j.get("url", ""),
                "description_html": (j.get("jobDescription") or j.get("jobExcerpt") or "") + salary,
                "posting_id": str(j.get("id", "")),
            })
    return results


def fetch_careerjet(api_key: str, keywords: str, location: str = "usa",
                    pages: int = 1, page_size: int = 100) -> list[dict]:
    """
    Careerjet is a broad search aggregator. Free key from
    https://www.careerjet.com/partners/register/as-publisher, passed via
    CAREERJET_API_KEY. Auth is HTTP Basic with the key as username and an
    empty password. user_ip/user_agent are required by the API for
    attribution; they aren't used for filtering.
    """
    results = []
    ua = "Mozilla/5.0 (compatible; job-search-script)"
    for page in range(1, pages + 1):
        resp = requests.get(
            "https://search.api.careerjet.net/v4/query",
            params={
                "keywords": keywords,
                "location": location,
                "locale_code": "en_US",
                "page": page,
                "page_size": page_size,
                "sort": "date",
                "user_ip": "127.0.0.1",
                "user_agent": ua,
            },
            auth=(api_key, ""),
            headers={"User-Agent": ua},
            timeout=25,
        )
        resp.raise_for_status()
        data = resp.json()
        jobs = data.get("jobs", [])
        if not jobs:
            break
        for j in jobs:
            salary = f" [{j.get('salary')}]" if j.get("salary") else ""
            results.append({
                "source": "careerjet",
                "company": j.get("company", ""),
                "title": j.get("title", ""),
                "location": j.get("locations", ""),
                "url": j.get("url", ""),
                "description_html": (j.get("description") or "") + salary,
                "posting_id": str(abs(hash(j.get("url", ""))))[:12],
            })
        if len(jobs) < page_size:
            break
    return results


def fetch_company_watchlist(api_key: str, companies: list[str], location: str = "",
                            pages: int = 1) -> list[dict]:
    """
    Watch specific employers regardless of which ATS they run.

    Some companies sit on systems with no usable public feed -- Hershey is on
    SAP SuccessFactors, which doesn't expose one -- so a per-ATS integration
    isn't possible. Jooble indexes them anyway, so query it by company name
    and keep only the hits whose company field actually matches. Broad
    keyword queries return plenty of neighbours (Coca-Cola, Barry Callebaut)
    when you ask for "Hershey", hence the post-filter.
    """
    results, seen = [], set()
    for company in companies:
        short = re.sub(r"[^a-z0-9]", "", company.lower()
                       .replace("the ", "").replace(" company", "")
                       .replace(" inc", "").replace(" corporation", ""))
        # Bare company name alone surfaces mostly unrelated roles, so pair it
        # with the terms that matter. Keeps the call count small and still
        # catches things like "Mgr Commercial Insights Shopper Strategy".
        queries = [company, f"{company} analytics", f"{company} insights",
                   f"{company} data"]
        for q in queries:
            for page in range(1, pages + 1):
                payload = {"keywords": q, "location": location, "page": str(page)}
                resp = requests.post(f"https://jooble.org/api/{api_key}", json=payload, timeout=20)
                resp.raise_for_status()
                for j in resp.json().get("jobs", []):
                    found = re.sub(r"[^a-z0-9]", "", (j.get("company") or "").lower())
                    # Prefix match in either direction. Substring matching let
                    # "Milton Hershey School" through on a Hershey watch, which
                    # is a different organisation entirely.
                    if not found or not (found.startswith(short) or short.startswith(found)):
                        continue
                    key = str(j.get("id", j.get("link", "")))
                    if key in seen:
                        continue
                    seen.add(key)
                    results.append({
                    "source": "watchlist",
                    "company": j.get("company", company),
                    "title": j.get("title", ""),
                    "location": j.get("location", ""),
                    "url": j.get("link", ""),
                    "description_html": j.get("snippet", ""),
                    "posting_id": str(j.get("id", j.get("link", ""))),
                })
    return results


def _title_matches(title: str, keywords_lower: list[str]) -> bool:
    return any(k in title.lower() for k in keywords_lower)


# Postings write states both ways ("Pennsylvania" and "Hershey, PA"), and a
# plain substring test never connects them -- which silently dropped local
# roles. Abbreviations are matched with word boundaries so "PA" doesn't hit
# "Tampa" or "Palo Alto".
STATE_ABBREV = {
    "pennsylvania": "pa", "new york": "ny", "california": "ca", "texas": "tx",
    "florida": "fl", "illinois": "il", "massachusetts": "ma", "washington": "wa",
    "colorado": "co", "georgia": "ga", "north carolina": "nc", "new jersey": "nj",
    "virginia": "va", "ohio": "oh", "michigan": "mi", "arizona": "az",
    "maryland": "md", "minnesota": "mn", "utah": "ut", "oregon": "or",
    "connecticut": "ct", "delaware": "de", "indiana": "in", "tennessee": "tn",
    "wisconsin": "wi", "missouri": "mo", "nevada": "nv",
}


def _location_matches(loc: str, locations_lower: list[str]) -> bool:
    if "remote" in loc:
        return True
    for want in locations_lower:
        if want in loc:
            return True
        abbrev = STATE_ABBREV.get(want)
        if abbrev and re.search(rf"\b{abbrev}\b", loc):
            return True
        # also handle the config itself being an abbreviation
        if len(want) <= 3 and re.search(rf"\b{re.escape(want)}\b", loc):
            return True
    return False


def keyword_filter(jobs: list[dict], keywords: list[str], locations: list[str]) -> list[dict]:
    keywords_lower = [k.lower() for k in keywords]
    locations_lower = [l.lower() for l in locations] if locations else []

    def matches(job):
        if not _title_matches(job["title"], keywords_lower):
            return False
        if not locations_lower:
            return True
        return _location_matches(job.get("location", "").lower(), locations_lower)
    return [j for j in jobs if matches(j)]


def collect_all_postings() -> list[dict]:
    cfg = load_config()
    all_jobs = []
    for token in cfg.get("greenhouse") or []:
        try:
            all_jobs.extend(fetch_greenhouse(token))
        except Exception as e:
            print(f"[warn] greenhouse/{token} failed: {e}")
    for token in cfg.get("lever") or []:
        try:
            all_jobs.extend(fetch_lever(token))
        except Exception as e:
            print(f"[warn] lever/{token} failed: {e}")
    for wd in cfg.get("workday") or []:
        try:
            all_jobs.extend(fetch_workday(wd["tenant"], wd["host"], wd["site"]))
        except Exception as e:
            print(f"[warn] workday/{wd.get('tenant')} failed: {e}")
    for board in cfg.get("ashby") or []:
        try:
            all_jobs.extend(fetch_ashby(board))
        except Exception as e:
            print(f"[warn] ashby/{board} failed: {e}")
    for company in cfg.get("smartrecruiters") or []:
        try:
            all_jobs.extend(fetch_smartrecruiters(company, cfg.get("title_keywords", [])))
        except Exception as e:
            print(f"[warn] smartrecruiters/{company} failed: {e}")
    for account in cfg.get("workable") or []:
        try:
            all_jobs.extend(fetch_workable(account))
        except Exception as e:
            print(f"[warn] workable/{account} failed: {e}")

    jooble_cfg = cfg.get("jooble") or {}
    if jooble_cfg.get("enabled"):
        api_key = os.environ.get("JOOBLE_API_KEY")
        if not api_key:
            print("[warn] jooble enabled but JOOBLE_API_KEY not set -- skipping")
        else:
            location = jooble_cfg.get("location", "")
            pages = jooble_cfg.get("pages_per_query", 1)
            for query in jooble_cfg.get("queries") or []:
                try:
                    all_jobs.extend(fetch_jooble(api_key, query, location, pages))
                except Exception as e:
                    print(f"[warn] jooble query '{query}' failed: {e}")

    adzuna_cfg = cfg.get("adzuna") or {}
    if adzuna_cfg.get("enabled"):
        app_id = os.environ.get("ADZUNA_APP_ID")
        app_key = os.environ.get("ADZUNA_APP_KEY")
        if not (app_id and app_key):
            print("[warn] adzuna enabled but ADZUNA_APP_ID/ADZUNA_APP_KEY not set -- skipping")
        else:
            country = adzuna_cfg.get("country", "us")
            results_per_page = adzuna_cfg.get("results_per_page", 50)
            pages = adzuna_cfg.get("pages_per_query", 1)
            for query in adzuna_cfg.get("queries") or []:
                try:
                    all_jobs.extend(fetch_adzuna(app_id, app_key, country, query, results_per_page, pages))
                except Exception as e:
                    print(f"[warn] adzuna query '{query}' failed: {e}")

    remotive_cfg = cfg.get("remotive") or {}
    if remotive_cfg.get("enabled", True):
        try:
            all_jobs.extend(fetch_remotive())
        except Exception as e:
            print(f"[warn] remotive failed: {e}")

    careerjet_cfg = cfg.get("careerjet") or {}
    if careerjet_cfg.get("enabled"):
        api_key = os.environ.get("CAREERJET_API_KEY")
        if not api_key:
            print("[warn] careerjet enabled but CAREERJET_API_KEY not set -- skipping")
        else:
            location = careerjet_cfg.get("location", "usa")
            pages = careerjet_cfg.get("pages_per_query", 1)
            page_size = careerjet_cfg.get("page_size", 100)
            for query in careerjet_cfg.get("queries") or []:
                try:
                    all_jobs.extend(fetch_careerjet(api_key, query, location, pages, page_size))
                except Exception as e:
                    print(f"[warn] careerjet query '{query}' failed: {e}")

    jobicy_cfg = cfg.get("jobicy") or {}
    if jobicy_cfg.get("enabled", True):
        try:
            all_jobs.extend(fetch_jobicy(
                jobicy_cfg.get("industries") or ["data-science"],
                jobicy_cfg.get("geo", ""),
                jobicy_cfg.get("count", 50),
            ))
        except Exception as e:
            print(f"[warn] jobicy failed: {e}")

    watch_cfg = cfg.get("company_watchlist") or {}
    if watch_cfg.get("enabled") and watch_cfg.get("companies"):
        api_key = os.environ.get("JOOBLE_API_KEY")
        if not api_key:
            print("[warn] company_watchlist needs JOOBLE_API_KEY -- skipping")
        else:
            try:
                hits = fetch_company_watchlist(
                    api_key, watch_cfg["companies"],
                    watch_cfg.get("location", ""), watch_cfg.get("pages_per_company", 1))
                print(f"  watchlist: {len(hits)} posting(s) across "
                      f"{len(watch_cfg['companies'])} watched compan(ies)")
                all_jobs.extend(hits)
            except Exception as e:
                print(f"[warn] company_watchlist failed: {e}")

    excluded = [re.sub(r"[^a-z0-9]", "", c.lower())
                for c in (cfg.get("exclude_companies") or [])]
    if excluded:
        before = len(all_jobs)
        all_jobs = [j for j in all_jobs
                    if not any(x in re.sub(r"[^a-z0-9]", "", (j.get("company") or "").lower())
                               for x in excluded)]
        dropped = before - len(all_jobs)
        if dropped:
            print(f"  excluded {dropped} posting(s) from {len(excluded)} blocked compan(ies)")

    deduped = []
    seen = set()
    for job in all_jobs:
        key = (job["source"], job["company"], job["posting_id"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(job)

    filtered = keyword_filter(deduped, cfg.get("title_keywords", []), cfg.get("locations", []))
    return filtered


if __name__ == "__main__":
    postings = collect_all_postings()
    print(f"Found {len(postings)} matching postings:")
    for p in postings:
        print(f" - [{p['company']}] {p['title']} ({p['location']}) -> {p['url']}")
