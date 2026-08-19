from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.requests import Request
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote_plus
import csv
import io
from datetime import datetime
import asyncio
import aiohttp
import time
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import re
import os
import math
from collections import Counter

try:
    from hirehunt import scrape_jobs as hirehunt_scrape
    HIREHUNT_AVAILABLE = True
except ImportError:
    HIREHUNT_AVAILABLE = False
    print("hirehunt library not found. ")


class JobSearchRequest(BaseModel):
    job_keyword: str = "software engineer"
    company_name: Optional[str] = ""
    country: str = "India"
    skills: List[str] = []
    skill_match_mode: str = "OR"
    date_posted: str = "24h"
    experience_levels: List[str] = []
    workplace_types: List[str] = []
    max_results: int = 10
    concurrent_requests: int = 10
    sources: List[str] = ["linkedin", "wellfound"]


class JobResponse(BaseModel):
    job_id: str
    job_title: str
    company_name: str
    company_url: str
    location: str
    posted: str
    posted_timestamp: Optional[int] = None
    benefit: str
    job_url: str
    description: str
    company_description: str
    matched_skills: List[str]
    source: str = "linkedin"
    relevance_score: float = 0.0


app = FastAPI(
    title="Multi-Source Job Search API",
    description="Fast job scraper with multiple sources (LinkedIn, Wellfound)",
    version="8.0.0"
)


class MemoryCache:
    def __init__(self, ttl_seconds=3600):
        self.cache = {}
        self.ttl = ttl_seconds
        self.lock = threading.Lock()
    
    def get(self, key: str) -> Optional[dict]:
        with self.lock:
            if key in self.cache:
                data, timestamp = self.cache[key]
                if time.time() - timestamp < self.ttl:
                    return data
                else:
                    del self.cache[key]
            return None
    
    def set(self, key: str, value: dict):
        with self.lock:
            self.cache[key] = (value, time.time())
    
    def clear(self):
        with self.lock:
            self.cache.clear()
    
    def size(self):
        with self.lock:
            return len(self.cache)

cache = MemoryCache(ttl_seconds=3600)


SKILL_ALIASES = {
    "python": ["python", "python 3", "python programming", "django", "flask", "fastapi"],
    "devops": ["devops", "docker", "kubernetes", "jenkins", "ci/cd", "terraform", "ansible"],
    "aws": ["aws", "amazon web services", "ec2", "s3", "lambda"],
    "java": ["java", "spring boot", "spring framework", "hibernate"],
    "javascript": ["javascript", "js", "node.js", "nodejs", "react", "angular"],
    "react": ["react", "react.js", "reactjs", "next.js"],
    "machine learning": ["machine learning", "ml", "scikit-learn", "tensorflow", "pytorch"],
    "sales": ["sales", "business development", "account executive"],
    "frontend": ["frontend", "front-end", "css", "html", "react", "angular", "vue"],
    "backend": ["backend", "back-end", "api", "microservices", "rest", "graphql"],
}


class TFIDFScorer:
    def __init__(self):
        self.documents = []
        self.idf_cache = {}
        self.total_docs = 0
    
    def add_document(self, text: str):
        if text and text.strip():
            self.documents.append(text.strip())
            self.total_docs = len(self.documents)
            self.idf_cache.clear()
    
    def get_tfidf_score(self, query: str) -> Dict[str, float]:
        if not query or not self.documents:
            return {}
        
        query_terms = set(re.findall(r'\w+', query.lower()))
        if not query_terms:
            return {}
        
        scores = {}
        doc_terms = []
        
        for doc in self.documents:
            terms = re.findall(r'\w+', doc.lower())
            doc_terms.append(terms)
        
        term_doc_freq = {}
        for terms in doc_terms:
            term_set = set(terms)
            for term in term_set:
                term_doc_freq[term] = term_doc_freq.get(term, 0) + 1
        
        for doc_idx, terms in enumerate(doc_terms):
            term_freq = Counter(terms)
            score = 0.0
            for query_term in query_terms:
                if query_term in term_freq:
                    tf = 1 + math.log(term_freq[query_term])
                    idf = math.log((1 + self.total_docs) / (1 + term_doc_freq.get(query_term, 0))) + 1
                    score += tf * idf
            scores[doc_idx] = score
        
        if scores:
            max_score = max(scores.values())
            if max_score > 0:
                for doc_idx in scores:
                    scores[doc_idx] = scores[doc_idx] / max_score
        
        return scores
    
    def clear(self):
        self.documents = []
        self.idf_cache.clear()
        self.total_docs = 0


tfidf_scorer = TFIDFScorer()


def build_wellfound_url(keyword: str, location: str) -> str:
    keyword = keyword.strip().replace(' ', '%20')
    location = location.strip().replace(' ', '%20')
    return f"https://wellfound.com/jobs?q={keyword}&location={location}"


def extract_wellfound_jobs(html: str, max_results: int = 20) -> List[dict]:
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    
    print(f"Wellfound: Parsing HTML (length: {len(html)})")
    
    job_cards = []
    
    selectors = [
        "div[data-test='JobCard']",
        "div.job-card",
        "div.startup-detail",
        "article.job",
        "li.job",
        "div[class*='job']",
        "div[class*='Job']"
    ]
    
    for selector in selectors:
        cards = soup.select(selector)
        if cards:
            print(f"Wellfound: Found {len(cards)} cards with selector: {selector}")
            job_cards = cards
            break
    
    if not job_cards:
        print("Wellfound: No job cards found with selectors, trying alternative method...")
        all_divs = soup.find_all('div')
        for div in all_divs:
            text = div.get_text(strip=True)
            if len(text) > 50 and ('engineer' in text.lower() or 'developer' in text.lower() or 'manager' in text.lower()):
                links = div.find_all('a')
                if links:
                    job_cards.append(div)
        
        print(f"Wellfound: Found {len(job_cards)} potential job divs")
        job_cards = job_cards[:max_results]
    
    if not job_cards:
        print("Wellfound: No job cards found")
        return []
    
    for idx, card in enumerate(job_cards[:max_results]):
        try:
            job_title = ""
            title_selectors = ["h3", "h4", ".title", ".job-title", ".role-title", "[data-test='JobTitle']"]
            for selector in title_selectors:
                tag = card.select_one(selector)
                if tag:
                    job_title = tag.get_text(strip=True)
                    break
            
            if not job_title:
                headings = card.find_all(['h3', 'h4', 'h5', 'h6'])
                if headings:
                    job_title = headings[0].get_text(strip=True)
            
            company_name = ""
            company_selectors = [".company-name", ".startup-name", ".company", "[data-test='CompanyName']"]
            for selector in company_selectors:
                tag = card.select_one(selector)
                if tag:
                    company_name = tag.get_text(strip=True)
                    break
            
            if not company_name:
                text = card.get_text()
                lines = [line.strip() for line in text.split('\n') if line.strip()]
                if len(lines) > 1:
                    company_name = lines[1] if len(lines) > 1 else ""
            
            location = ""
            location_selectors = [".location", ".locality", ".address", ".location-name"]
            for selector in location_selectors:
                tag = card.select_one(selector)
                if tag:
                    location = tag.get_text(strip=True)
                    break
            
            job_url = ""
            link_selectors = ["a[href*='/jobs/']", "a[href*='/role/']", "a[href*='/position/']"]
            for selector in link_selectors:
                tag = card.select_one(selector)
                if tag:
                    href = tag.get('href', '')
                    if href:
                        job_url = href if href.startswith('http') else f"https://wellfound.com{href}"
                        break
            
            if not job_url:
                links = card.find_all('a')
                for link in links:
                    href = link.get('href', '')
                    if '/jobs/' in href or '/role/' in href or '/position/' in href:
                        job_url = href if href.startswith('http') else f"https://wellfound.com{href}"
                        break
            
            posted = ""
            date_selectors = [".posted-at", ".time-ago", ".date", ".posted-date"]
            for selector in date_selectors:
                tag = card.select_one(selector)
                if tag:
                    posted = tag.get_text(strip=True)
                    break
            
            if job_title or company_name:
                job_id = hashlib.md5(f"{job_title}{company_name}{job_url}".encode()).hexdigest()[:8]
                
                job_data = {
                    "job_id": f"wf_{job_id}",
                    "job_title": job_title or "Unknown Position",
                    "company_name": company_name or "Unknown Company",
                    "company_url": "",
                    "location": location or "Remote",
                    "posted": posted or "Recent",
                    "posted_timestamp": int(time.time() - (idx * 86400)),
                    "benefit": "",
                    "job_url": job_url,
                    "description": "",
                    "company_description": "",
                    "matched_skills": [],
                    "source": "wellfound"
                }
                jobs.append(job_data)
                print(f"Wellfound: Extracted job #{idx+1}: {job_title} at {company_name}")
            else:
                print(f"Wellfound: Skipping card {idx+1} - no title or company found")
                
        except Exception as e:
            print(f"Wellfound: Error parsing card {idx+1}: {e}")
            continue
    
    print(f"Wellfound: Successfully extracted {len(jobs)} jobs")
    return jobs


async def search_wellfound_async(request: JobSearchRequest) -> List[dict]:
    try:
        search_url = build_wellfound_url(request.job_keyword, request.country)
        print(f"Wellfound: Searching: {search_url}")
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://wellfound.com/"
        }
        
        session = requests.Session()
        session.headers.update(headers)
        
        response = session.get(search_url, timeout=15)
        print(f"Wellfound: Response status: {response.status_code}")
        
        if response.status_code != 200:
            print(f"Wellfound: Bad response status: {response.status_code}")
            return []
        
        html = response.text
        print(f"Wellfound: HTML length: {len(html)}")
        
        if len(html) < 1000:
            print("Wellfound: Response too short, might be blocked or empty")
            return []
        
        max_results = request.max_results * 3
        jobs = extract_wellfound_jobs(html, max_results)
        
        if not jobs:
            print("Wellfound: No jobs extracted, trying alternative search format...")
            alt_url = f"https://wellfound.com/jobs?q={request.job_keyword}"
            print(f"Wellfound: Trying alternative URL: {alt_url}")
            
            try:
                response = session.get(alt_url, timeout=15)
                if response.status_code == 200:
                    jobs = extract_wellfound_jobs(response.text, max_results)
                    print(f"Wellfound: Found {len(jobs)} jobs with alternative URL")
            except Exception as e:
                print(f"Wellfound: Alternative URL failed: {e}")
        
        if request.company_name and jobs:
            jobs = [job for job in jobs if company_matches(job["company_name"], request.company_name)]
            print(f"Wellfound: After company filter: {len(jobs)} jobs")
        
        if jobs:
            jobs_to_fetch = jobs[:request.max_results]
            concurrency = min(request.concurrent_requests, len(jobs_to_fetch))
            jobs_with_details = await fetch_all_jobs(jobs_to_fetch, concurrency)
            
            final_jobs = []
            for job in jobs_with_details:
                matched_skills = find_matching_skills(
                    job.get("description", ""),
                    request.skills,
                    request.skill_match_mode
                )
                job["matched_skills"] = matched_skills
                
                if not request.skills or matched_skills:
                    final_jobs.append(job)
            
            print(f"Wellfound: Final jobs after skill matching: {len(final_jobs)}")
            return final_jobs[:request.max_results]
        
        return []
        
    except Exception as e:
        print(f"Wellfound: Search error: {e}")
        import traceback
        traceback.print_exc()
        return []


def build_linkedin_url(
    keyword: str,
    location: str,
    experience_levels: List[str],
    workplace_types: List[str],
    date_posted: str
) -> str:
    url = (
        "https://www.linkedin.com/jobs/search/"
        f"?keywords={quote_plus(keyword)}"
        f"&location={quote_plus(location)}"
    )
    
    if experience_levels:
        url += "&f_E=" + ",".join(experience_levels)
    
    if workplace_types:
        url += "&f_WT=" + ",".join(workplace_types)
    
    if date_posted == "24h":
        url += "&f_TPR=r86400"
    elif date_posted == "week":
        url += "&f_TPR=r604800"
    elif date_posted == "month":
        url += "&f_TPR=r2592000"
    
    url += "&position=1&pageNum=0"
    return url


def extract_search_jobs(html: str, max_results: int = 20) -> List[dict]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("div.base-card.job-search-card")[:max_results]
    print(f"LinkedIn: Found {len(cards)} cards")
    
    jobs = []
    for card in cards:
        try:
            title_tag = card.select_one("h3.base-search-card__title")
            job_title = title_tag.get_text(strip=True) if title_tag else ""
            
            company_tag = card.select_one("h4.base-search-card__subtitle a")
            company_name = company_tag.get_text(strip=True) if company_tag else ""
            
            company_url = ""
            if company_tag:
                company_url = company_tag.get("href", "").strip().replace("&amp;", "&")
            
            link_tag = card.select_one("a.base-card__full-link")
            job_url = ""
            if link_tag:
                job_url = link_tag.get("href", "").strip().replace("&amp;", "&")
            
            location_tag = card.select_one("span.job-search-card__location")
            location = location_tag.get_text(strip=True) if location_tag else ""
            
            entity_urn = card.get("data-entity-urn")
            job_id = entity_urn.split(":")[-1] if entity_urn else ""
            
            posted_tag = card.select_one("time.job-search-card__listdate")
            posted = posted_tag.get_text(strip=True) if posted_tag else ""
            
            benefit_tag = card.select_one("span.job-posting-benefits__text")
            benefit = benefit_tag.get_text(strip=True) if benefit_tag else ""
            
            jobs.append({
                "job_id": job_id,
                "job_title": job_title,
                "company_name": company_name,
                "company_url": company_url,
                "location": location,
                "posted": posted,
                "posted_timestamp": int(time.time() - (len(jobs) * 3600)),
                "benefit": benefit,
                "job_url": job_url,
                "description": "",
                "company_description": "",
                "matched_skills": [],
                "source": "linkedin"
            })
        except Exception as e:
            print(f"LinkedIn: Error parsing card: {e}")
            continue
    
    return jobs


async def search_linkedin_async(request: JobSearchRequest) -> List[dict]:
    try:
        search_url = build_linkedin_url(
            request.job_keyword,
            request.country,
            request.experience_levels,
            request.workplace_types,
            request.date_posted
        )
        print(f"LinkedIn: Searching: {search_url}")
        
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
        })
        
        response = session.get(search_url, timeout=15)
        if response.status_code != 200:
            print(f"LinkedIn: Bad response status: {response.status_code}")
            return []
        
        html = response.text
        extract_count = max(request.max_results * 2, 20)
        jobs = extract_search_jobs(html, extract_count)
        print(f"LinkedIn: Extracted {len(jobs)} jobs")
        
        if request.company_name:
            jobs = [job for job in jobs if company_matches(job["company_name"], request.company_name)]
            print(f"LinkedIn: After company filter: {len(jobs)} jobs")
        
        if not jobs:
            return []
        
        concurrency = min(request.concurrent_requests, len(jobs))
        jobs_with_details = await fetch_all_jobs(jobs[:extract_count], concurrency)
        
        final_jobs = []
        for job in jobs_with_details:
            matched_skills = find_matching_skills(
                job.get("description", ""),
                request.skills,
                request.skill_match_mode
            )
            job["matched_skills"] = matched_skills
            
            if not request.skills or matched_skills:
                final_jobs.append(job)
        
        print(f"LinkedIn: Final jobs after skill matching: {len(final_jobs)}")
        return final_jobs[:request.max_results]
        
    except Exception as e:
        print(f"LinkedIn: Search error: {e}")
        return []


def company_matches(actual_company: str, requested_company: str) -> bool:
    if not requested_company:
        return True
    if not actual_company:
        return False
    return requested_company.lower() in actual_company.lower()


def find_matching_skills(description: str, skills: List[str], mode: str = "OR") -> List[str]:
    if not description or not skills:
        return []
    
    text = description.lower()
    matched_skills = []
    
    for skill in skills:
        skill_lower = skill.lower()
        aliases = SKILL_ALIASES.get(skill_lower, [skill_lower])
        
        found = False
        for alias in aliases:
            if alias.lower() in text:
                found = True
                break
        
        if found:
            matched_skills.append(skill)
    
    if mode == "AND":
        return matched_skills if len(matched_skills) == len(skills) else []
    
    return matched_skills


async def fetch_html_async(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
            if response.status == 200:
                return await response.text()
            return None
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return None


async def fetch_job_details_async(session: aiohttp.ClientSession, job: dict) -> dict:
    if not job.get("job_url"):
        return job
    
    cache_key = f"{job.get('source', 'unknown')}_{job['job_url']}"
    
    cached = cache.get(cache_key)
    if cached:
        job.update(cached)
        return job
    
    html = await fetch_html_async(session, job["job_url"])
    if html:
        soup = BeautifulSoup(html, "html.parser")
        
        description_selectors = [
            "div.description__text",
            ".job-description",
            ".description",
            ".content",
            ".details"
        ]
        
        for selector in description_selectors:
            desc_tag = soup.select_one(selector)
            if desc_tag:
                job["description"] = desc_tag.get_text(separator="\n", strip=True)[:5000]
                break
        
        company_selectors = [
            "div.show-more-less-html__markup",
            ".about-company",
            ".company-description",
            ".about"
        ]
        
        for selector in company_selectors:
            comp_desc_tag = soup.select_one(selector)
            if comp_desc_tag:
                job["company_description"] = comp_desc_tag.get_text(separator="\n", strip=True)[:1000]
                break
        
        cache.set(cache_key, {
            "description": job.get("description", ""),
            "company_description": job.get("company_description", "")
        })
    
    return job


async def fetch_all_jobs(jobs: List[dict], concurrent: int = 10) -> List[dict]:
    if not jobs:
        return jobs
    
    connector = aiohttp.TCPConnector(
        limit=concurrent,
        limit_per_host=concurrent,
        ttl_dns_cache=300,
        enable_cleanup_closed=True
    )
    
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [fetch_job_details_async(session, job) for job in jobs]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        final_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                final_results.append(jobs[i])
            else:
                final_results.append(result)
        
        return final_results


def apply_tfidf_ranking(jobs: List[dict], query: str) -> List[dict]:
    if not jobs or not query:
        return jobs
    
    tfidf_scorer.clear()
    
    for job in jobs:
        text = f"{job.get('job_title', '')} {job.get('company_name', '')} {job.get('description', '')} {job.get('company_description', '')}"
        tfidf_scorer.add_document(text)
    
    scores = tfidf_scorer.get_tfidf_score(query)
    
    for idx, job in enumerate(jobs):
        job["relevance_score"] = scores.get(idx, 0.0)
    
    sorted_jobs = sorted(jobs, key=lambda x: x.get("relevance_score", 0.0), reverse=True)
    
    return sorted_jobs


async def search_all_sources(request: JobSearchRequest) -> List[dict]:
    all_jobs = []
    source_tasks = []
    
    sources = request.sources if request.sources else ["linkedin", "wellfound"]
    print(f"Searching sources: {sources}")
    
    if "linkedin" in sources:
        print("Adding LinkedIn to search tasks...")
        source_tasks.append(("linkedin", search_linkedin_async(request)))
    
    if "wellfound" in sources:
        print("Adding Wellfound to search tasks...")
        source_tasks.append(("wellfound", search_wellfound_async(request)))
    
    if source_tasks:
        print(f"Running {len(source_tasks)} scrapers in parallel...")
        async_results = await asyncio.gather(
            *[task[1] for task in source_tasks],
            return_exceptions=True
        )
        
        for i, result in enumerate(async_results):
            source_name = source_tasks[i][0]
            if isinstance(result, Exception):
                print(f"Error in {source_name} search: {result}")
            elif result:
                print(f"{source_name} returned {len(result)} jobs")
                all_jobs.extend(result)
            else:
                print(f"{source_name} returned no jobs")
    
    if not all_jobs and HIREHUNT_AVAILABLE:
        print("No results from primary sources, using hirehunt fallback...")
        city = request.country
        if "," in request.country:
            city = request.country.split(",")[0].strip()
        
        try:
            fallback_jobs = await asyncio.get_event_loop().run_in_executor(
                None,
                scrape_with_hirehunt,
                request.job_keyword,
                request.sources,
                city,
                request.max_results
            )
            if fallback_jobs:
                print(f"hirehunt fallback returned {len(fallback_jobs)} jobs")
                all_jobs.extend(fallback_jobs)
        except Exception as e:
            print(f"hirehunt fallback error: {e}")
    
    seen_urls = set()
    unique_jobs = []
    for job in all_jobs:
        url = job.get("job_url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_jobs.append(job)
        elif not url:
            key = f"{job.get('job_title', '')}_{job.get('company_name', '')}"
            if key not in seen_urls:
                seen_urls.add(key)
                unique_jobs.append(job)
    
    if unique_jobs:
        unique_jobs = apply_tfidf_ranking(unique_jobs, request.job_keyword)
    
    unique_jobs.sort(key=lambda x: x.get('relevance_score', 0.0), reverse=True)
    
    print(f"Total unique jobs: {len(unique_jobs)}")
    if unique_jobs:
        source_counts = {}
        for job in unique_jobs:
            source = job.get('source', 'unknown')
            source_counts[source] = source_counts.get(source, 0) + 1
        print(f"Source breakdown: {source_counts}")
    
    return unique_jobs


def scrape_with_hirehunt(keyword: str, sources: List[str], city: str, results_wanted: int) -> List[dict]:
    if not HIREHUNT_AVAILABLE:
        return []
    
    try:
        hirehunt_sources = []
        for source in sources:
            if source in ["linkedin", "naukri", "shine", "indeed", "glassdoor"]:
                hirehunt_sources.append(source)
            elif source == "wellfound":
                hirehunt_sources.append("angel")
        
        if not hirehunt_sources:
            hirehunt_sources = ["naukri", "shine", "linkedin"]
        
        print(f"Using hirehunt with sources: {hirehunt_sources}")
        
        result = hirehunt_scrape(
            search_term=keyword,
            sources=hirehunt_sources,
            city=city,
            results_wanted=results_wanted
        )
        
        jobs = []
        for job in result.jobs:
            jobs.append({
                "job_id": f"hh_{hashlib.md5(job.title.encode()).hexdigest()[:8]}",
                "job_title": job.title or "",
                "company_name": job.company or "",
                "company_url": job.company_url or "",
                "location": job.location or city,
                "posted": job.posted_date or "Recent",
                "posted_timestamp": int(time.time()),
                "benefit": "",
                "job_url": job.job_url or "",
                "description": job.description or "",
                "company_description": "",
                "matched_skills": [],
                "source": f"hirehunt_{source}"
            })
        return jobs
    except Exception as e:
        print(f"hirehunt error: {e}")
        return []


HTML_CONTENT = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sarthi - Smart Job Finder</title>
    <style>
        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        :root {
            --bg-primary: #f3f5f8;
            --bg-secondary: #ffffff;
            --bg-nav: #0b1a2a;
            --text-primary: #1b1b1b;
            --text-secondary: #2d3b4f;
            --text-muted: #5d6f83;
            --border-color: #e1e5eb;
            --hover-bg: #f8f9fc;
            --selected-bg: #f0f4fe;
            --shadow: 0 2px 8px rgba(0,0,0,0.15);
            --card-shadow: 0 1px 4px rgba(0,0,0,0.04);
            --input-bg: #ffffff;
            --skill-bg: #eef3fa;
            --detail-bg: #f8f9fc;
            --toast-bg: #0b1a2a;
            --accent: #00a3e0;
            --border-light: #d0d7e2;
        }

        .dark-mode {
            --bg-primary: #1a1a2e;
            --bg-secondary: #16213e;
            --bg-nav: #0f0f1f;
            --text-primary: #e8e8e8;
            --text-secondary: #c8c8d4;
            --text-muted: #8a8aa8;
            --border-color: #2a2a4a;
            --hover-bg: #1e2a4a;
            --selected-bg: #1a2a5a;
            --shadow: 0 2px 8px rgba(0,0,0,0.4);
            --card-shadow: 0 1px 4px rgba(0,0,0,0.2);
            --input-bg: #1a1a3a;
            --skill-bg: #2a2a5a;
            --detail-bg: #1a1a3a;
            --toast-bg: #2a2a5a;
            --accent: #4fc3f7;
            --border-light: #3a3a5a;
        }

        body {
            font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            height: 100vh;
            overflow: hidden;
            transition: background 0.3s, color 0.3s;
        }

        .app-container {
            display: flex;
            flex-direction: column;
            height: 100vh;
        }

        .top-nav {
            background: var(--bg-nav);
            color: white;
            padding: 14px 40px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-shrink: 0;
            box-shadow: var(--shadow);
            transition: background 0.3s;
        }

        .nav-left {
            display: flex;
            align-items: center;
            gap: 16px;
        }

        .brand {
            display: flex;
            align-items: center;
            gap: 12px;
            font-size: 22px;
            font-weight: 600;
            letter-spacing: -0.3px;
        }

        .brand-icon {
            width: 38px;
            height: 38px;
            background: var(--accent);
            border-radius: 8px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 700;
            font-size: 22px;
            color: white;
        }

        .nav-right {
            display: flex;
            align-items: center;
            gap: 20px;
            font-size: 14px;
        }

        .nav-right span {
            opacity: 0.85;
            cursor: pointer;
            transition: 0.2s;
        }

        .nav-right span:hover {
            opacity: 1;
        }

        .theme-toggle {
            background: rgba(255,255,255,0.1);
            border: 1px solid rgba(255,255,255,0.2);
            color: white;
            padding: 6px 16px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 13px;
            transition: 0.2s;
        }

        .theme-toggle:hover {
            background: rgba(255,255,255,0.2);
        }

        .search-bar-container {
            background: var(--bg-secondary);
            padding: 14px 40px;
            border-bottom: 1px solid var(--border-color);
            flex-shrink: 0;
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            align-items: center;
            box-shadow: var(--card-shadow);
            transition: background 0.3s, border-color 0.3s;
        }

        .search-input-group {
            display: flex;
            gap: 8px;
            flex: 1;
            min-width: 200px;
            flex-wrap: wrap;
        }

        .search-field {
            flex: 1;
            min-width: 140px;
            padding: 9px 14px;
            border: 1px solid var(--border-light);
            border-radius: 6px;
            font-size: 14px;
            background: var(--input-bg);
            color: var(--text-primary);
            transition: 0.2s;
        }

        .search-field:focus {
            border-color: var(--accent);
            outline: none;
            box-shadow: 0 0 0 3px rgba(0,163,224,0.15);
        }

        .search-field::placeholder {
            color: var(--text-muted);
        }

        .action-buttons {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }

        .btn {
            padding: 9px 20px;
            border: none;
            border-radius: 6px;
            font-size: 14px;
            font-weight: 500;
            cursor: pointer;
            transition: 0.2s;
            white-space: nowrap;
        }

        .btn-primary {
            background: var(--bg-nav);
            color: white;
        }

        .btn-primary:hover {
            background: #1a2d42;
        }

        .btn-secondary {
            background: var(--bg-secondary);
            color: var(--text-primary);
            border: 1px solid var(--border-light);
        }

        .btn-secondary:hover {
            background: var(--hover-bg);
        }

        .btn-outline {
            background: transparent;
            color: var(--text-primary);
            border: 1px solid var(--border-light);
        }

        .btn-outline:hover {
            background: var(--hover-bg);
        }

        .filter-badge {
            display: flex;
            align-items: center;
            gap: 8px;
            background: var(--skill-bg);
            padding: 5px 14px;
            border-radius: 20px;
            font-size: 13px;
            color: var(--text-primary);
            border: 1px solid var(--border-color);
        }

        .filter-badge .remove {
            cursor: pointer;
            font-weight: 700;
            color: var(--text-muted);
        }

        .filter-badge .remove:hover {
            color: var(--text-primary);
        }

        .main-content {
            display: flex;
            flex: 1;
            overflow: hidden;
        }

        .job-list-panel {
            width: 42%;
            min-width: 340px;
            background: var(--bg-secondary);
            border-right: 1px solid var(--border-color);
            overflow-y: auto;
            padding: 8px 0;
            transition: background 0.3s, border-color 0.3s;
        }

        .job-list-panel .list-header {
            padding: 14px 20px 10px;
            font-size: 14px;
            color: var(--text-muted);
            border-bottom: 1px solid var(--border-color);
            display: flex;
            justify-content: space-between;
            position: sticky;
            top: 0;
            background: var(--bg-secondary);
            z-index: 5;
        }

        .job-list-item {
            padding: 16px 20px;
            border-bottom: 1px solid var(--border-color);
            cursor: pointer;
            transition: 0.15s;
            border-left: 3px solid transparent;
        }

        .job-list-item:hover {
            background: var(--hover-bg);
        }

        .job-list-item.selected {
            background: var(--selected-bg);
            border-left-color: var(--accent);
        }

        .job-list-item .item-title {
            font-size: 16px;
            font-weight: 600;
            color: var(--text-primary);
            margin-bottom: 4px;
        }

        .job-list-item .item-company {
            font-size: 14px;
            color: var(--text-secondary);
            font-weight: 500;
        }

        .job-list-item .item-meta {
            display: flex;
            gap: 16px;
            font-size: 13px;
            color: var(--text-muted);
            margin-top: 6px;
            flex-wrap: wrap;
        }

        .job-list-item .item-skills {
            display: flex;
            flex-wrap: wrap;
            gap: 4px;
            margin-top: 6px;
        }

        .job-list-item .item-skills span {
            background: var(--skill-bg);
            padding: 2px 10px;
            border-radius: 12px;
            font-size: 11px;
            color: var(--text-primary);
            font-weight: 500;
        }

        .job-list-item .item-relevance {
            font-size: 12px;
            color: var(--text-muted);
            margin-top: 4px;
        }

        .job-list-item .item-relevance .bar {
            display: inline-block;
            height: 3px;
            border-radius: 2px;
            background: var(--accent);
            margin-left: 6px;
            vertical-align: middle;
        }

        .job-list-item .item-source {
            font-size: 11px;
            color: var(--text-muted);
            margin-top: 6px;
            text-transform: uppercase;
            letter-spacing: 0.3px;
        }

        .job-detail-panel {
            flex: 1;
            background: var(--bg-secondary);
            overflow-y: auto;
            padding: 30px 40px;
            transition: background 0.3s;
        }

        .detail-placeholder {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            height: 100%;
            color: var(--text-muted);
            text-align: center;
        }

        .detail-placeholder .icon {
            font-size: 56px;
            margin-bottom: 16px;
            opacity: 0.3;
        }

        .detail-placeholder h3 {
            font-size: 20px;
            font-weight: 400;
            color: var(--text-secondary);
        }

        .detail-placeholder p {
            font-size: 14px;
            max-width: 360px;
            margin-top: 8px;
        }

        .detail-content .detail-title {
            font-size: 26px;
            font-weight: 600;
            color: var(--text-primary);
        }

        .detail-content .detail-company {
            font-size: 18px;
            color: var(--text-secondary);
            margin: 4px 0 8px;
        }

        .detail-content .detail-meta {
            display: flex;
            flex-wrap: wrap;
            gap: 20px;
            font-size: 14px;
            color: var(--text-muted);
            margin: 8px 0 16px;
        }

        .detail-content .detail-meta span {
            display: flex;
            align-items: center;
            gap: 4px;
        }

        .detail-content .detail-skills {
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
            margin: 12px 0 18px;
        }

        .detail-content .detail-skills span {
            background: var(--skill-bg);
            padding: 4px 14px;
            border-radius: 16px;
            font-size: 13px;
            font-weight: 500;
            color: var(--text-primary);
        }

        .detail-content .detail-description {
            font-size: 15px;
            line-height: 1.7;
            color: var(--text-primary);
            white-space: pre-wrap;
            margin: 12px 0 16px;
            max-height: 400px;
            overflow-y: auto;
            padding-right: 8px;
        }

        .detail-content .detail-description::-webkit-scrollbar {
            width: 4px;
        }

        .detail-content .detail-description::-webkit-scrollbar-thumb {
            background: var(--border-color);
            border-radius: 4px;
        }

        .detail-content .detail-link {
            display: inline-block;
            margin-top: 8px;
            color: var(--text-primary);
            font-weight: 600;
            text-decoration: none;
            border: 1px solid var(--border-light);
            padding: 10px 24px;
            border-radius: 6px;
            transition: 0.2s;
        }

        .detail-content .detail-link:hover {
            background: var(--bg-nav);
            color: white;
            border-color: var(--bg-nav);
        }

        .detail-content .detail-source {
            font-size: 12px;
            color: var(--text-muted);
            margin-top: 12px;
        }

        .detail-content .detail-relevance {
            font-size: 13px;
            color: var(--text-muted);
            margin-top: 8px;
            padding: 8px 14px;
            background: var(--detail-bg);
            border-radius: 6px;
            display: inline-block;
        }

        .loader-container {
            display: none;
            justify-content: center;
            align-items: center;
            padding: 40px;
        }

        .loader-container.show {
            display: flex;
        }

        .spinner {
            width: 40px;
            height: 40px;
            border: 4px solid var(--border-color);
            border-top: 4px solid var(--accent);
            border-radius: 50%;
            animation: spin 0.9s linear infinite;
        }

        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }

        .error-banner {
            display: none;
            background: #fef6f6;
            border: 1px solid #f5c6c6;
            color: #a33a3a;
            padding: 10px 18px;
            border-radius: 6px;
            margin: 6px 20px;
            font-size: 14px;
        }

        .dark-mode .error-banner {
            background: #3a1a1a;
            border-color: #5a2a2a;
            color: #e8a0a0;
        }

        .error-banner.show {
            display: block;
        }

        .empty-state {
            text-align: center;
            padding: 60px 20px;
            color: var(--text-muted);
        }

        .empty-state .icon {
            font-size: 48px;
            margin-bottom: 12px;
            opacity: 0.4;
        }

        .empty-state h3 {
            font-size: 18px;
            font-weight: 500;
            color: var(--text-secondary);
        }

        .empty-state p {
            font-size: 14px;
            margin-top: 6px;
        }

        .toast {
            position: fixed;
            bottom: 30px;
            left: 50%;
            transform: translateX(-50%);
            background: var(--toast-bg);
            color: white;
            padding: 12px 28px;
            border-radius: 8px;
            font-size: 14px;
            box-shadow: 0 8px 24px rgba(0,0,0,0.2);
            display: none;
            z-index: 999;
            transition: background 0.3s;
        }

        .toast.show {
            display: block;
            animation: fadeUp 0.3s ease;
        }

        @keyframes fadeUp {
            0% { opacity: 0; transform: translateX(-50%) translateY(20px); }
            100% { opacity: 1; transform: translateX(-50%) translateY(0); }
        }

        .filter-wrapper {
            position: relative;
            display: inline-block;
        }

        .filter-menu {
            display: none;
            position: absolute;
            top: 44px;
            left: 0;
            min-width: 190px;
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            box-shadow: 0 8px 24px rgba(0,0,0,0.12);
            z-index: 100;
            padding: 6px 0;
        }

        .filter-menu.show {
            display: block;
        }

        .filter-menu .filter-title {
            padding: 10px 16px;
            font-weight: 600;
            font-size: 13px;
            color: var(--text-muted);
            border-bottom: 1px solid var(--border-color);
        }

        .filter-menu .filter-option {
            display: block;
            width: 100%;
            padding: 10px 16px;
            border: none;
            background: transparent;
            text-align: left;
            cursor: pointer;
            font-size: 14px;
            color: var(--text-primary);
            transition: 0.1s;
        }

        .filter-menu .filter-option:hover {
            background: var(--hover-bg);
        }

        .filter-menu .filter-option.active {
            background: var(--selected-bg);
            font-weight: 600;
        }

        @media (max-width: 900px) {
            .job-list-panel {
                width: 50%;
                min-width: 280px;
            }
            .search-bar-container {
                padding: 12px 20px;
            }
            .top-nav {
                padding: 12px 20px;
            }
            .job-detail-panel {
                padding: 20px;
            }
        }

        @media (max-width: 650px) {
            .main-content {
                flex-direction: column;
            }
            .job-list-panel {
                width: 100%;
                max-height: 45vh;
                border-right: none;
                border-bottom: 1px solid var(--border-color);
                min-width: unset;
            }
            .job-detail-panel {
                flex: 1;
                min-height: 40vh;
            }
            .search-input-group {
                flex-direction: column;
            }
            .search-field {
                min-width: 100%;
            }
            .top-nav .nav-right {
                display: none;
            }
            .search-bar-container {
                padding: 10px 16px;
            }
            .action-buttons {
                width: 100%;
            }
            .action-buttons .btn {
                flex: 1;
            }
            .filter-wrapper {
                flex: 1;
            }
        }
    </style>
</head>
<body>

<div class="app-container">

    <div class="top-nav">
        <div class="nav-left">
            <div class="brand">
                <div class="brand-icon">S</div>
                <span>Sarthi Careers</span>
            </div>
        </div>
        <div class="nav-right">
            <span>Jobs</span>
            <span>Saved</span>
            <span>Applied</span>
            <button class="theme-toggle" id="themeToggle">🌙 Dark</button>
            <span style="background:#2a3d52; padding:6px 16px; border-radius:6px;">Sign in</span>
        </div>
    </div>

    <div class="search-bar-container">
        <div class="search-input-group">
            <input type="text" class="search-field" id="jobKeyword" placeholder="Job title or keyword" value="software engineer">
            <input type="text" class="search-field" id="locationInput" placeholder="Location" value="India">
            <input type="text" class="search-field" id="companyInput" placeholder="Company">
            <input type="text" class="search-field" id="skillInput" placeholder="Skills (comma separated)">
        </div>
        <div class="action-buttons">
            <button class="btn btn-primary" id="searchBtn">Search</button>
            <div class="filter-wrapper">
                <button class="btn btn-secondary" id="filterToggle">⚑ Experience</button>
                <div class="filter-menu" id="filterMenu">
                    <div class="filter-title">Experience Level</div>
                    <button class="filter-option" data-value="0-2">0-2 years</button>
                    <button class="filter-option" data-value="2-5">2-5 years</button>
                    <button class="filter-option" data-value="5-8">5-8 years</button>
                    <button class="filter-option" data-value="8-12">8-12 years</button>
                    <button class="filter-option" data-value="12+">12+ years</button>
                </div>
            </div>
            <button class="btn btn-outline" id="clearBtn">Clear</button>
        </div>
        <div id="filterBadge" class="filter-badge" style="display:none;">
            <span id="filterLabel">Experience: 0-2 years</span>
            <span class="remove" id="removeFilter">×</span>
        </div>
        <div id="errorBanner" class="error-banner">Unable to load jobs. Showing fallback data.</div>
    </div>

    <div class="main-content">

        <div class="job-list-panel" id="jobListPanel">
            <div class="list-header">
                <span id="jobCountLabel">0 jobs</span>
                <span id="sourceInfoLabel"></span>
            </div>
            <div id="jobListContainer">
                <div class="loader-container show" id="loader">
                    <div class="spinner"></div>
                </div>
                <div id="jobListItems"></div>
                <div id="emptyState" class="empty-state" style="display:none;">
                    <div class="icon">🔍</div>
                    <h3>No jobs found</h3>
                    <p>Try adjusting your search or filters</p>
                </div>
            </div>
        </div>

        <div class="job-detail-panel" id="detailPanel">
            <div id="detailPlaceholder" class="detail-placeholder">
                <div class="icon">📋</div>
                <h3>Select a job to view details</h3>
                <p>Click on any job from the list to see the full description and company information</p>
            </div>
            <div id="detailContent" class="detail-content" style="display:none;"></div>
        </div>

    </div>

</div>

<div id="toast" class="toast">Loading jobs...</div>

<script>
(function() {
    var jobKeyword = document.getElementById('jobKeyword');
    var locationInput = document.getElementById('locationInput');
    var companyInput = document.getElementById('companyInput');
    var skillInput = document.getElementById('skillInput');
    var searchBtn = document.getElementById('searchBtn');
    var clearBtn = document.getElementById('clearBtn');
    var filterToggle = document.getElementById('filterToggle');
    var filterMenu = document.getElementById('filterMenu');
    var filterOptions = document.querySelectorAll('.filter-option');
    var filterBadge = document.getElementById('filterBadge');
    var filterLabel = document.getElementById('filterLabel');
    var removeFilter = document.getElementById('removeFilter');
    var jobListItems = document.getElementById('jobListItems');
    var emptyState = document.getElementById('emptyState');
    var loader = document.getElementById('loader');
    var jobCountLabel = document.getElementById('jobCountLabel');
    var sourceInfoLabel = document.getElementById('sourceInfoLabel');
    var detailPlaceholder = document.getElementById('detailPlaceholder');
    var detailContent = document.getElementById('detailContent');
    var errorBanner = document.getElementById('errorBanner');
    var toast = document.getElementById('toast');
    var themeToggle = document.getElementById('themeToggle');

    var selectedExpValue = '';
    var selectedExpLabel = '';
    var currentJobs = [];
    var selectedJobId = null;
    var toastTimer = null;
    var fallbackData = [];
    var isDarkMode = false;

    function toggleTheme() {
        isDarkMode = !isDarkMode;
        if (isDarkMode) {
            document.body.classList.add('dark-mode');
            themeToggle.textContent = '☀️ Light';
        } else {
            document.body.classList.remove('dark-mode');
            themeToggle.textContent = '🌙 Dark';
        }
        localStorage.setItem('darkMode', isDarkMode ? 'true' : 'false');
    }

    var savedMode = localStorage.getItem('darkMode');
    if (savedMode === 'true') {
        isDarkMode = true;
        document.body.classList.add('dark-mode');
        themeToggle.textContent = '☀️ Light';
    }

    themeToggle.addEventListener('click', toggleTheme);

    function showToast(msg, duration) {
        duration = duration || 2000;
        toast.textContent = msg;
        toast.classList.add('show');
        if (toastTimer) clearTimeout(toastTimer);
        toastTimer = setTimeout(function() {
            toast.classList.remove('show');
        }, duration);
    }

    function showError(msg) {
        errorBanner.textContent = msg || 'Something went wrong. Using fallback data.';
        errorBanner.classList.add('show');
        setTimeout(function() {
            errorBanner.classList.remove('show');
        }, 5000);
    }

    function loadFallbackData() {
        if (fallbackData.length === 0) {
            var titles = ['Software Engineer', 'Senior Developer', 'Frontend Lead', 'Backend Architect', 'DevOps Engineer', 'Data Scientist', 'Product Manager', 'UX Designer'];
            var companies = ['Microsoft', 'Google', 'Amazon', 'Apple', 'Meta', 'Netflix', 'Spotify', 'Adobe'];
            var locations = ['Seattle, WA', 'San Francisco, CA', 'New York, NY', 'Austin, TX', 'London, UK', 'Berlin, DE', 'Tokyo, JP'];
            var desc = 'This is fallback job data. The search service is temporarily unavailable. Please try again later.';
            var now = Math.floor(Date.now() / 1000);
            var allSkills = ['Python', 'JavaScript', 'React', 'AWS', 'Docker', 'Kubernetes', 'Java', 'C++', 'SQL', 'MongoDB', 'TypeScript', 'Node.js'];
            for (var i = 0; i < 15; i++) {
                var idx = i % titles.length;
                var skillsSubset = [];
                for (var s = 0; s < 3 + (i % 3); s++) {
                    skillsSubset.push(allSkills[(i + s) % allSkills.length]);
                }
                var daysAgo = (i % 7) + 1;
                var postedText = daysAgo + ' days ago';
                var relevance = 0.6 + (Math.random() * 0.35);
                fallbackData.push({
                    job_id: 'fallback_' + i,
                    job_title: titles[idx] + (i > titles.length ? ' ' + String.fromCharCode(65 + (i % 26)) : ''),
                    company_name: companies[i % companies.length],
                    location: locations[i % locations.length],
                    posted: postedText,
                    posted_timestamp: now - (daysAgo * 86400 + (i % 24) * 3600),
                    job_url: '#',
                    description: desc + ' Position: ' + titles[idx] + '. Skills required: ' + skillsSubset.join(', ') + '.',
                    company_description: 'A leading technology company specializing in innovative solutions.',
                    matched_skills: skillsSubset.slice(0, 4),
                    source: ['linkedin', 'wellfound'][i % 2],
                    relevance_score: relevance
                });
            }
        }
        return fallbackData;
    }

    function getTimeAgo(timestamp) {
        if (!timestamp) return 'Recent';
        var now = Math.floor(Date.now() / 1000);
        var diff = now - timestamp;
        if (diff < 0) return 'Recent';
        if (diff < 60) return Math.floor(diff) + 's';
        if (diff < 3600) return Math.floor(diff / 60) + 'm';
        if (diff < 86400) return Math.floor(diff / 3600) + 'h';
        if (diff < 604800) return Math.floor(diff / 86400) + 'd';
        if (diff < 2592000) return Math.floor(diff / 604800) + 'w';
        return 'Recent';
    }

    function renderRelevanceBar(score) {
        var width = Math.round((score || 0) * 100);
        var color = width > 70 ? '#00a3e0' : (width > 40 ? '#f5a623' : '#d0d7e2');
        return '<span class="bar" style="width:' + width + '%;background:' + color + ';"></span>';
    }

    function renderJobList(jobs, selectFirst) {
        if (selectFirst === undefined) selectFirst = true;
        jobListItems.innerHTML = '';
        emptyState.style.display = 'none';
        loader.classList.remove('show');

        if (!jobs || jobs.length === 0) {
            emptyState.style.display = 'block';
            jobCountLabel.textContent = '0 jobs';
            sourceInfoLabel.textContent = '';
            detailPlaceholder.style.display = 'flex';
            detailContent.style.display = 'none';
            return;
        }

        jobCountLabel.textContent = jobs.length + ' jobs';

        var sources = {};
        jobs.forEach(function(j) {
            var src = j.source || 'unknown';
            sources[src] = (sources[src] || 0) + 1;
        });
        var srcParts = [];
        for (var key in sources) {
            if (sources.hasOwnProperty(key)) {
                srcParts.push(key + ' ' + sources[key]);
            }
        }
        sourceInfoLabel.textContent = srcParts.join(' · ') || '';

        var html = '';
        for (var i = 0; i < jobs.length; i++) {
            var job = jobs[i];
            var title = job.job_title || 'Position';
            var company = job.company_name || 'Company';
            var location = job.location || 'Remote';
            var posted = getTimeAgo(job.posted_timestamp);
            var skills = job.matched_skills || [];
            var source = job.source || 'unknown';
            var score = job.relevance_score || 0;
            var isSelected = (selectedJobId === job.job_id);

            var skillsHtml = skills.map(function(s) {
                return '<span>' + escapeHtml(s) + '</span>';
            }).join('');

            var scorePercent = Math.round(score * 100);

            html += '<div class="job-list-item' + (isSelected ? ' selected' : '') + '" data-id="' + escapeHtml(job.job_id) + '">';
            html += '<div class="item-title">' + escapeHtml(title) + '</div>';
            html += '<div class="item-company">' + escapeHtml(company) + '</div>';
            html += '<div class="item-meta">';
            html += '<span>📍 ' + escapeHtml(location) + '</span>';
            html += '<span>⏱️ ' + escapeHtml(posted) + '</span>';
            html += '</div>';
            if (skillsHtml) {
                html += '<div class="item-skills">' + skillsHtml + '</div>';
            }
            html += '<div class="item-relevance">Match ' + scorePercent + '% ' + renderRelevanceBar(score) + '</div>';
            html += '<div class="item-source">' + escapeHtml(source) + '</div>';
            html += '</div>';
        }
        jobListItems.innerHTML = html;

        if (selectFirst && jobs.length > 0 && !selectedJobId) {
            var firstItem = document.querySelector('.job-list-item');
            if (firstItem) {
                var id = firstItem.dataset.id;
                if (id) {
                    selectedJobId = id;
                    var job = jobs.find(function(j) { return j.job_id === id; });
                    if (job) showDetail(job);
                    updateSelection(id);
                }
            }
        }

        var items = document.querySelectorAll('.job-list-item');
        for (var i = 0; i < items.length; i++) {
            (function(item) {
                item.addEventListener('click', function() {
                    var id = this.dataset.id;
                    if (!id) return;
                    selectedJobId = id;
                    var job = currentJobs.find(function(j) { return j.job_id === id; });
                    if (job) {
                        showDetail(job);
                        updateSelection(id);
                    }
                });
            })(items[i]);
        }
    }

    function updateSelection(id) {
        var items = document.querySelectorAll('.job-list-item');
        for (var i = 0; i < items.length; i++) {
            var item = items[i];
            if (item.dataset.id === id) {
                item.classList.add('selected');
            } else {
                item.classList.remove('selected');
            }
        }
    }

    function showDetail(job) {
        detailPlaceholder.style.display = 'none';
        detailContent.style.display = 'block';

        var skills = job.matched_skills || [];
        var skillsHtml = skills.map(function(s) {
            return '<span>' + escapeHtml(s) + '</span>';
        }).join('');

        var desc = job.description || 'No description available.';
        if (desc.length > 2000) desc = desc.substring(0, 2000) + '...';

        var scorePercent = Math.round((job.relevance_score || 0) * 100);

        var html = '';
        html += '<div class="detail-title">' + escapeHtml(job.job_title || 'Position') + '</div>';
        html += '<div class="detail-company">' + escapeHtml(job.company_name || 'Company') + '</div>';
        html += '<div class="detail-meta">';
        html += '<span>📍 ' + escapeHtml(job.location || 'Remote') + '</span>';
        html += '<span>⏱️ ' + getTimeAgo(job.posted_timestamp) + '</span>';
        if (job.benefit) {
            html += '<span>🎁 ' + escapeHtml(job.benefit) + '</span>';
        }
        html += '</div>';
        if (skillsHtml) {
            html += '<div class="detail-skills">' + skillsHtml + '</div>';
        }
        html += '<div class="detail-relevance">Relevance Score: ' + scorePercent + '%</div>';
        html += '<div class="detail-description">' + escapeHtml(desc) + '</div>';
        if (job.company_description) {
            html += '<div style="margin-top:12px;padding:14px 18px;background:var(--detail-bg);border-radius:6px;border-left:3px solid var(--accent);">';
            html += '<div style="font-weight:600;font-size:14px;color:var(--text-secondary);">About the company</div>';
            html += '<div style="font-size:14px;color:var(--text-primary);margin-top:4px;">' + escapeHtml(job.company_description) + '</div>';
            html += '</div>';
        }
        if (job.job_url && job.job_url !== '#') {
            html += '<a href="' + escapeHtml(job.job_url) + '" target="_blank" class="detail-link">Apply on ' + escapeHtml(job.source || 'company') + ' →</a>';
        } else {
            html += '<div style="margin-top:12px;font-size:13px;color:var(--text-muted);">🔗 No external link available</div>';
        }
        html += '<div class="detail-source">Source: ' + escapeHtml(job.source || 'unknown') + ' · ID: ' + escapeHtml(job.job_id || '') + '</div>';
        detailContent.innerHTML = html;
    }

    function escapeHtml(text) {
        if (!text) return '';
        var map = {
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;',
            "'": '&#039;'
        };
        return String(text).replace(/[&<>"']/g, function(m) { return map[m]; });
    }

    function performSearch() {
        var keyword = jobKeyword.value.trim() || 'software engineer';
        var location = locationInput.value.trim() || 'India';
        var company = companyInput.value.trim();
        var skillText = skillInput.value.trim();
        var skills = skillText ? skillText.split(',').map(function(s) { return s.trim(); }).filter(function(s) { return s.length > 0; }) : [];

        var payload = {
            job_keyword: keyword,
            company_name: company,
            country: location,
            skills: skills,
            skill_match_mode: 'OR',
            date_posted: '24h',
            experience_levels: selectedExpValue ? [selectedExpValue] : [],
            workplace_types: [],
            max_results: 25,
            concurrent_requests: 10,
            sources: ['linkedin', 'wellfound']
        };

        loader.classList.add('show');
        jobListItems.innerHTML = '';
        emptyState.style.display = 'none';
        detailPlaceholder.style.display = 'flex';
        detailContent.style.display = 'none';
        selectedJobId = null;
        errorBanner.classList.remove('show');

        fetch('/search', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
            body: JSON.stringify(payload)
        })
        .then(function(response) {
            if (!response.ok) {
                throw new Error('Server error: ' + response.status);
            }
            return response.json();
        })
        .then(function(data) {
            currentJobs = data || [];
            if (currentJobs.length === 0) {
                showError('No jobs found. Showing fallback data.');
                currentJobs = loadFallbackData();
            }
            renderJobList(currentJobs, true);
            showToast('Found ' + currentJobs.length + ' jobs');
        })
        .catch(function(err) {
            console.error('Search error:', err);
            showError('Search service unavailable. Using fallback data.');
            currentJobs = loadFallbackData();
            renderJobList(currentJobs, true);
            showToast('Showing fallback data');
        })
        .finally(function() {
            loader.classList.remove('show');
        });
    }

    filterToggle.addEventListener('click', function(e) {
        e.stopPropagation();
        filterMenu.classList.toggle('show');
    });

    document.addEventListener('click', function() {
        filterMenu.classList.remove('show');
    });

    for (var i = 0; i < filterOptions.length; i++) {
        (function(opt) {
            opt.addEventListener('click', function() {
                selectedExpLabel = this.textContent;
                selectedExpValue = this.dataset.value;
                filterLabel.textContent = 'Experience: ' + selectedExpLabel;
                filterBadge.style.display = 'flex';
                filterMenu.classList.remove('show');
                performSearch();
            });
        })(filterOptions[i]);
    }

    removeFilter.addEventListener('click', function() {
        selectedExpValue = '';
        selectedExpLabel = '';
        filterBadge.style.display = 'none';
        performSearch();
    });

    clearBtn.addEventListener('click', function() {
        jobKeyword.value = '';
        locationInput.value = '';
        companyInput.value = '';
        skillInput.value = '';
        selectedExpValue = '';
        selectedExpLabel = '';
        filterBadge.style.display = 'none';
        performSearch();
    });

    searchBtn.addEventListener('click', performSearch);

    [jobKeyword, locationInput, companyInput, skillInput].forEach(function(input) {
        input.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                performSearch();
            }
        });
    });

    performSearch();

})();
</script>

</body>
</html>
'''


@app.get("/")
async def root():
    return HTMLResponse(content=HTML_CONTENT)


@app.post("/search", response_model=List[JobResponse])
async def search_jobs(request: JobSearchRequest):
    start_time = time.time()
    
    print("\n" + "="*60)
    print(f"Starting PARALLEL search on {', '.join(request.sources)}")
    print("="*60)
    
    jobs = await search_all_sources(request)
    
    elapsed = time.time() - start_time
    print(f"TOTAL: Search completed in {elapsed:.2f}s - Found {len(jobs)} jobs")
    print("="*60 + "\n")
    
    return jobs


@app.post("/search/csv")
async def search_jobs_csv(request: JobSearchRequest):
    jobs = await search_jobs(request)
    
    if not jobs:
        raise HTTPException(status_code=404, detail="No jobs found")
    
    output = io.StringIO()
    fieldnames = [
        "job_id", "job_title", "company_name", "company_url",
        "location", "posted", "posted_timestamp", "benefit", "job_url",
        "matched_skills", "description", "company_description", "source", "relevance_score"
    ]
    
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    
    for job in jobs:
        row = job.copy()
        row["matched_skills"] = ", ".join(job.get("matched_skills", []))
        writer.writerow(row)
    
    output.seek(0)
    filename = f"jobs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    
    return FileResponse(
        io.BytesIO(output.getvalue().encode('utf-8')),
        media_type="text/csv",
        filename=filename
    )


@app.get("/search/quick")
async def quick_search(
    keyword: str = Query("software engineer", description="Job keyword"),
    country: str = Query("India", description="Country"),
    skills: List[str] = Query([], description="Skills to match"),
    mode: str = Query("OR", description="Skill match mode: OR or AND"),
    date: str = Query("24h", description="Date posted: any, 24h, week, month"),
    max_results: int = Query(10, ge=1, le=50),
    sources: List[str] = Query(["linkedin", "wellfound"], description="Sources to search")
):
    request = JobSearchRequest(
        job_keyword=keyword,
        country=country,
        skills=skills,
        skill_match_mode=mode,
        date_posted=date,
        max_results=max_results,
        concurrent_requests=10,
        sources=sources
    )
    return await search_jobs(request)


@app.post("/cache/clear")
async def clear_cache():
    cache.clear()
    return {"message": "Cache cleared"}


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "cache_size": cache.size(),
        "available_sources": ["linkedin", "wellfound"],
        "hirehunt_available": HIREHUNT_AVAILABLE
    }


@app.get("/sources")
async def list_sources():
    return {
        "sources": [
            {
                "name": "linkedin",
                "available": True,
                "description": "LinkedIn job search",
                "url_pattern": "linkedin.com/jobs/search/"
            },
            {
                "name": "wellfound",
                "available": True,
                "description": "Wellfound (formerly AngelList) startup jobs",
                "url_pattern": "wellfound.com/jobs"
            }
        ],
        "fallback": {
            "available": HIREHUNT_AVAILABLE,
            "description": "HireHunt library for additional sources"
        }
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "Homepage1:app",
        host="0.0.0.0",
        port=8000,
        reload=True
    )