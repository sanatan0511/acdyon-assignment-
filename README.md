# Decisions - Hey hi before you start take a look at my live project:- 
 note = it is same website but due to issue with uv and venv less time i have to merge main code with python code (also take ai help to be honest but 99% my work) and front end (full )
* front end -https://chimerical-brioche-00e5c0.netlify.app/
* Sarthi job - https://acdyon-assignment-2.onrender.com/

## 1. Why this ingestion strategy?

I chose a public job-board feed/API for the live demo instead of scraping a live LinkedIn account. The challenge explicitly asks for a low-risk source, so the goal was to demonstrate the ingestion architecture without creating an unnecessary account or IP-blocking risk.

The ingestion layer is separated from normalization and ranking. A source adapter fetches jobs, validates the response, converts them into a common schema, and passes them to the rest of the pipeline. This makes replacing the source possible without rewriting the application.

I rejected direct browser automation as the primary approach because it adds operational complexity and does not solve the underlying resilience problem. A controlled public feed gives predictable access while still allowing me to demonstrate caching, validation, retries, deduplication, and failure handling.

## 2. Trade-off

Under the time limit I kept the pipeline in-process rather than introducing Redis, a message queue, and persistent storage.

With a real week, I would add persistent job storage, source-health metrics, scheduled ingestion, structured retry/backoff policies, parser health checks, and monitoring. I would also add contract tests using saved source responses so markup/API changes are detected before production ingestion silently fails.

## 3. AI usage

I used AI assistance for code exploration, debugging ideas, and reviewing implementation choices. I personally verified the generated code, tested the API behavior, inspected responses, and changed parts that did not match the challenge requirements.

I did not treat generated code as authoritative. In particular, I reviewed the source restrictions, fallback behavior, error handling, concurrency, and response parsing before deciding what was appropriate for the final implementation.
