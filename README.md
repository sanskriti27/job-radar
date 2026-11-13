# Job Radar

Checks the careers systems behind ~120 companies **every 5 minutes** and pings your phone the moment a matching role goes live, so you can apply while you're still among the first applicants.

- **Sources:** Greenhouse, Lever, Ashby and SmartRecruiters boards (found automatically from company names), Workday (Salesforce, NVIDIA, Adobe, Walmart, PayPal, Mastercard, Target), Amazon, plus experimental Microsoft and Google.
- **Filter:** SDE-2 / SWE II / backend / full-stack titles; no staff, lead, manager, intern, frontend-only, QA or hardware roles. India only, or remote roles open to India.
- **Scoring:** Gemini rates each new match 1–10 against `config/profile.md` and drops anything under 6. If Gemini is down you still get the alert, unscored, because speed comes first.
- **Alert:** an ntfy push notification with **Apply** and **Find hiring mgr** buttons (Telegram also supported).
- **No flood:** the first time a company is read, its current openings are recorded silently. You only hear about roles posted after that.
- **Cost:** ₹0 (public GitHub repo + Gemini free tier + ntfy).

---

## Setup (~10 minutes)

### 1. Put the code on GitHub (public repo)
Public repos get unlimited free Actions minutes. A private repo's 2,000 free minutes/month only covers about one run every 20–25 minutes. Nothing secret lives in the code: keys go in GitHub Secrets.

```bash
unzip job-radar.zip && cd job-radar
git init -b main && git add . && git commit -m "job radar"
gh repo create job-radar --public --source=. --push      # or create the repo on github.com and `git push`
```
> On a Mac, don't drag the folder into the GitHub web uploader: Finder hides the `.github` folder, and the workflow would be left out.

### 2. Phone notifications (ntfy, no account)
1. Install **ntfy** from the Play Store or App Store.
2. Tap **+** and subscribe to a long, random topic name, e.g. `radar-sa-7f3k9q2m`. Anyone who knows the name can read it, so don't use something guessable.
3. For the fastest pings on Android, set the subscription to **instant delivery**.

### 3. Gemini API key (free)
Your Google AI Pro subscription doesn't include API access, but the free API tier is plenty: the radar only calls Gemini when there are new matches.
Get a key at **https://aistudio.google.com/apikey**.

### 4. Add the secrets
```bash
gh secret set NTFY_TOPIC      --body "radar-sa-7f3k9q2m"
gh secret set GEMINI_API_KEY  --body "AIza..."
```
(Or on github.com: repo → Settings → Secrets and variables → Actions → New repository secret.)

Optional:
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` to get alerts on Telegram too.
- Repository **variable** `GEMINI_MODEL` to pin a model (default `gemini-flash-latest`).

### 5. Turn it on
On the repo's **Actions** tab: click *Enable workflows* if asked → **Job Radar** → **Run workflow**, then run these one at a time:
1. `test-notify`: a sample alert should reach your phone within seconds.
2. `validate`: open the run's summary to see a table of every board, how many roles it has, and which company names weren't found.
3. `run`: the first full run records everything silently. From then on it runs by itself every 5 minutes.

---

## Daily use

- **Add a company:** add its name under `auto:` in `config/watchlist.yaml` and commit. It gets found and picked up on the next run.
- **Company not found?** Open their careers page. If the URL contains `greenhouse.io/<x>`, `lever.co/<x>`, `ashbyhq.com/<x>` or `myworkdayjobs.com`, pin it under `pinned:` (examples are in the file).
- **Tune what counts as relevant:** edit `config/filters.yaml` (title patterns, `max_age_hours`, `min_score`).
- **Change the profile Gemini scores against:** edit `config/profile.md`, or set a `CANDIDATE_PROFILE` secret to keep it private.
- **Each run's summary** (Actions → a run) lists boards read, new roles, alerts, failures and companies not found.

## Making it run exactly every 5 minutes (optional)
GitHub often runs scheduled jobs 5–15 minutes late at busy times. For punctual runs, trigger the workflow from **cron-job.org** (free):
1. Create a fine-grained GitHub token with access to just this repo and the **Actions: Read and write** permission.
2. On cron-job.org, add a job that runs every 5 minutes:
   - URL: `https://api.github.com/repos/<you>/job-radar/actions/workflows/radar.yml/dispatches`
   - Method: POST
   - Headers: `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`
   - Body: `{"ref":"main"}`
3. Then delete the `schedule:` block from `.github/workflows/radar.yml`, or leave both: overlapping runs wait in line and never double-alert.

## Notes
- GitHub pauses scheduled workflows on repos with 60 days of no activity. The radar's own state commits normally count as activity, but if alerts stop, check the Actions tab.
- The Microsoft and Google sources read their public search pages, which can change without notice. `validate` shows if they break; the other sources use stable public data feeds.
- Scraping LinkedIn is deliberately left out (it breaks LinkedIn's terms and gets accounts flagged). Use LinkedIn's own instant job alerts for that.

## Run locally
```bash
pip install -r requirements.txt
NTFY_TOPIC=... GEMINI_API_KEY=... python -m radar run
python -m radar discover "Razorpay" "Postman"     # which board does a company use?
python -m pytest tests                            # offline tests
```
