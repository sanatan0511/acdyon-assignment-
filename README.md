                           ## SARTHI - JOB WEBSITE ##
# Note - Hey hi before you start take a look at my live project:- 
 note = it is same website but due to issue with uv and venv less time i have to merge main code with python code (also take ai help to be honest but 99% my work) and front end (full )
* front end -https://chimerical-brioche-00e5c0.netlify.app/
* Sarthi job - https://acdyon-assignment-2.onrender.com/
* 
  ![](Screen Recording 2026-08-19 172024.mp4)

🚀 Acdyon Technologies - Frontend Challenge Submission
"Build It Like You Mean It"

A production-ready job search platform that scrapes LinkedIn and Wellfound while staying under the radar, with Pydantic validation and HireHunt fallback.


# Clone & install
git clone https://github.com/yourusername/job-search-platform.git
cd job-search-platform
pip install -r requirements.txt

# Run
uvicorn Homepage1:app --reload

# Open
http://localhost:8000


Multi-source job scraper with anti-detection, Pydantic validation, and HireHunt fallback. Scrapes LinkedIn + Wellfound with TF-IDF ranking, dark mode UI, and 3 easter eggs. Built for Acdyon Technologies Frontend Challenge.

Live Demo: https://your-app.onrender.com

bash
pip install -r requirements.txt && uvicorn Homepage1:app --reload
Features: Async scraping | Skill matching | CSV export | Responsive (390px-1440px) | No fake data

Stack: FastAPI + Pydantic + BeautifulSoup4 + aiohttp + HireHunt (fallback)

