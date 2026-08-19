# DECISIONS.md

## 1. Why this ingestion strategy?

I chose a public, low-risk job source for the live demo instead of directly scraping a live LinkedIn account. The challenge specifically asks the live demo to use a public job-board RSS/API or a sandbox, so I wanted to demonstrate the ingestion system without creating unnecessary account, IP, or Terms-of-Service risk.

The application separates ingestion from the rest of the search pipeline. A source adapter is responsible for fetching jobs, validating the response, and converting the source-specific fields into a common job schema. The normalized jobs then go through deduplication, filtering, and relevance ranking before being returned by the FastAPI API.

I rejected browser automation as the primary approach because it would add another layer of complexity without being necessary for the permitted live demo. For a source that provides structured public data, an HTTP client is simpler, cheaper, easier to test, and less fragile than controlling a browser.

The design also keeps the source adapter replaceable. If the current source becomes unavailable, another permitted source can be added without changing the frontend or job-ranking logic.

## 2. Resilience and failure handling

The ingestion layer treats an unsuccessful fetch differently from an empty result. HTTP failures, timeouts, malformed responses, and unexpected source structures are detected before data reaches the search layer.

Responses are validated for both HTTP success and expected job data. This prevents a source markup/API change from silently appearing to the user as "0 jobs."

Requests are bounded and cached where appropriate to avoid repeatedly fetching the same job details. When a source returns a temporary failure or rate-limit response, the correct behavior is to back off and retry within a bounded limit rather than continuously retrying.

If the primary source becomes unavailable, the application can return the results already collected from other permitted sources or expose a clear source-unavailable state. It does not fabricate job listings to make the interface appear successful.

For a production version, I would add persistent source-health state, structured metrics, scheduled ingestion, exponential backoff with `Retry-After` support, and parser contract tests using previously captured source responses.

## 3. Trade-off

The main trade-off was keeping the system lightweight enough to build and deploy within the challenge time limit.

I kept the API, caching, normalization, filtering, and ranking logic in the application instead of introducing Redis, a message queue, a database, and a separate worker service.

With a real week, I would move ingestion into scheduled background workers, persist normalized jobs, add Redis for shared caching/rate limiting, add source-health monitoring, and store ingestion metrics so failures could be detected without manually inspecting logs.

## 4. Where I used AI

I used AI tools during development for code exploration, debugging suggestions, implementation alternatives, and reviewing parts of the ingestion and frontend logic.

I personally verified the generated code by running the application, inspecting HTTP responses, checking parsed job fields, testing filters and failure cases, and changing implementation details where the generated solution did not match the challenge requirements.

One important verification step was reviewing the challenge's scope guardrail rather than assuming that technically working scraping code was automatically appropriate for the submission. The final live demo therefore uses a permitted low-risk source rather than relying on a live LinkedIn account.

## 5. Where I would stop

I would not attempt to bypass CAPTCHA, defeat bot detection, use stolen or authenticated sessions, or continuously rotate identities/IPs to evade a platform's explicit blocking controls.

The goal of the project is to demonstrate reliable ingestion engineering, not to defeat a platform's security or access controls. If a source blocks the application or changes its access policy, the system should recognize the source as unavailable and switch to an allowed fallback rather than escalating the evasion techniques.



###  Note - Search time as comapre to other - 10s max and also tf-idf and microsoft layout i do as much as possible .as i have to face alot of challanges  