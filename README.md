# News digest

A personal news dashboard that runs itself. It pulls newsletters, YouTube channels and websites into one searchable page, writes a short learning briefing every morning, and emails both before 6:00 AM.

**Live:** https://syb-code.github.io/news-digest/

This is a template repository. Click **Use this template** to get your own copy, then follow [Run your own](#run-your-own). A copy runs nothing until you turn it on.

![The dashboard in dark mode: a headline, four metrics, today's briefing, search and filters, and the newest items](screenshots/dashboard-dark.jpg)

| Learning briefing | Phone | Morning email |
|---|---|---|
| ![A slide from the daily learning briefing, in the site colors](screenshots/slides.jpg) | ![Dashboard on a phone](screenshots/dashboard-mobile.jpg) | ![The morning email, with the briefing first](screenshots/email.jpg) |

## What it does

- **One list for everything.** Newsletters (RSS), YouTube channels and websites, newest first. The page opens on the 10 newest items; search and filters reach the rest.
- **A learning briefing every morning.** Three to five topics on what is new and important in AI, as a slideshow in the dashboard and a PowerPoint file. Each topic says what happened, why it matters, what to read first and one open question.
- **Ranked by importance, not by you.** Topics are picked by code from four signals: how many sources cover the story, whether a primary source published it, whether it is new since last week, and whether it fits a lane you watch. Nothing tracks clicks or reading time. One topic always comes from outside your lanes.
- **AI summaries on demand.** Tick items and click Summarize. A Cloudflare Worker reads each article or video and returns the key points.
- **Edit the X accounts from the dashboard.** With your key set, the On X panel gets an Add account form and a Remove button on each account. Visitors do not get them, and the Worker refuses an edit without the key.
- **A morning email.** The briefing first, then everything published since the last email.

## How a day runs

| Time (America/Los_Angeles) | What happens |
|---|---|
| 5:45 AM | The morning run: refresh every feed, pick the topics, write the briefing with Claude, build the slides, publish the dashboard, send the email. |
| 12:00 PM | The noon run: refresh the feeds and publish the dashboard, so the morning's articles are there by lunch. |

A Cloudflare cron trigger starts both runs (`worker/scheduler.js`). GitHub's own scheduler can start a job hours late, which a 6:00 AM email cannot afford.

**Daylight saving.** Cloudflare cron runs in UTC, and Los Angeles is 7 hours behind UTC in summer and 8 in winter. So each local time has a cron expression that fires at both UTC hours, and the Worker starts a run only when the local clock reads exactly 5:45 or 12:00. That gives one run per time, every day, including the days the clocks change.

## How it is built

| Part | What it is |
|---|---|
| `config.yml` | The one file a copier edits: feeds, themes, time zone, and the briefing's model and limits. |
| `learn/lanes.md` | The areas the briefing watches, with keywords and sources. `learn/seen.json` logs past topics so one is not repeated without a new development. |
| `scripts/fetch_and_build.py` | Fetches every feed with timeouts and a time budget per source, and writes `docs/data/items.json`. A feed that fails keeps its last good items. |
| `scripts/learn/` | `candidates.py` groups and ranks the day's stories. `brief.py` asks Claude to write the topics. `render.py` builds the PowerPoint file and the dated folder. |
| `scripts/send_email.py` | The morning email, built with the Python standard library only. |
| `docs/` | The dashboard and the slideshow viewer: plain HTML, CSS and JavaScript on GitHub Pages, with self-hosted fonts. |
| `.github/workflows/daily.yml` | The morning and noon runs. |
| `worker/` | Blocks to paste into your Cloudflare Worker: `auth-guard.js` (the lock), `scheduler.js` (the clock) and `sources.js` (add and remove X accounts from the dashboard). `worker.js` is all three in one file, for a Worker with no summaries code of its own. |
| `.github/workflows/sources.yml` | Adds or removes one X account in `data/x_accounts.json`, then starts a noon run to publish it. Started by the Worker when the owner edits the list. |
| `data/x_accounts.json` | The X accounts on the dashboard. Without it, `feeds.twitter_embeds` in `config.yml` is used. |
| `tests/` | Offline tests: `python3 -m unittest discover -s tests`. |

## The learning briefing

`candidates.py` takes the last 36 hours of items plus the sources in `learn/lanes.md`, groups stories that cover the same thing, and scores each group from the four signals. The weights are one table at the top of that file. It then picks the top three to five, always keeping the best story from outside your lanes.

`brief.py` sends only those picked stories to Claude and asks for a fixed JSON shape. Every link in the answer must be one of the links it was given, so the model cannot invent a source. If the key is missing or the call fails, the briefing is built from the headlines and the run still succeeds.

**What a day costs.** One Claude call per day, on `claude-opus-5-5` at medium effort:

| | Tokens | Cost |
|---|---|---|
| A typical day | about 6,000 in, 5,500 out | about $0.13 |
| The cap (`max_input_chars`, `max_output_tokens` in `config.yml`) | about 12,000 in, 12,000 out | about $0.30 |

That is about $4 a month. In the rare case that the model declines and a fallback model answers, the run can cost up to twice the cap. Every run logs its token counts and cost, and the last slide of each briefing shows the model and the cost. To spend less, set `model` to a smaller Claude model in `config.yml`.

## Security design

The repository and the page are public, so everything is built on the assumption that anyone can read the code. No key, token, email address or private address is in any file.

| Secret | Where it lives | Who can use it |
|---|---|---|
| `ANTHROPIC_API_KEY` | GitHub environment `digest` (main only) | Only the step that calls Claude |
| Gmail client id, secret, refresh token, sender, recipient | GitHub environment `digest` (main only) | Only the step that sends the email |
| AI keys for summaries, `ACCESS_KEY`, `GH_DISPATCH_TOKEN` | Cloudflare Worker secrets | Only the Worker |
| The Worker's address | GitHub repository variable `WORKER_URL` | Written into the published site only |

- **Only the owner can change it.** Others can read and copy the code. They cannot push to it, and a fork or a copy gets none of the secrets or variables.
- **No workflow runs on pull requests**, and every job is off unless the variable `DIGEST_ENABLED` is `true`.
- **No third-party code runs next to a secret or a write token.** Feeds are fetched in a job with no secret. The Claude step installs only the Anthropic SDK. The commit job installs nothing and copies only checked files. The email runs on the standard library. Every package is pinned by hash.
- **Only the key holder edits the X accounts.** The dashboard sends the edit to the Worker, which checks the access key and then starts a workflow with a token that can start workflows and nothing else. The workflow checks the handle again, runs no third-party code, and is the only thing that writes the list.
- **The Worker is locked.** `worker/auth-guard.js` refuses any request without the access key, so visitors can browse the dashboard but only the owner can spend the AI budget.
- **The pages load only their own files.** A strict Content-Security-Policy allows no inline script or style. Feed text and model text are always rendered as text, and only `https` links are kept.

## Run your own

About 30 minutes. You need a GitHub account, a Cloudflare account (free), a Claude API key and a Gmail account to send from.

1. **Copy it.** Click **Use this template**, then **Create a new repository**. Public is required for free GitHub Pages.
2. **Edit `config.yml`.** Put in your own feeds, themes and time zone. Edit `learn/lanes.md` for the areas you want to watch.
3. **Turn on Pages.** Settings > Pages > Source: **GitHub Actions**. Do not pick "Deploy from a branch".
4. **Create the secrets.** Settings > Environments > New environment, named `digest`. Under Deployment branches choose **Selected branches** and add `main`. Add these environment secrets:
   - `ANTHROPIC_API_KEY`, from the Claude Console. Set a monthly spend limit there.
   - `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`, `SENDER_EMAIL`, `RECIPIENT_EMAIL`. The steps for a send-only Gmail token are in `scripts/oauth_local_example.py`.
5. **Set up the Worker.** In Cloudflare, create a Worker for summaries, or skip summaries and use the Worker only as the clock.
   - No summaries code of your own: paste `worker/worker.js` as the whole Worker. It is the lock, the clock and the X account editor in one file. With your own summaries code: paste `worker/auth-guard.js`, `worker/scheduler.js` and `worker/sources.js` at the top of it and follow the steps at the top of each file.
   - Add the Worker secrets `ACCESS_KEY` and `GH_DISPATCH_TOKEN`, and the variables `GH_REPO` (`your-name/your-repo`) and `ALLOWED_ORIGINS` (`https://your-name.github.io`).
   - Add the two cron triggers listed in `scheduler.js`.
6. **Set the variables.** Settings > Secrets and variables > Actions > Variables:
   - `WORKER_URL`: your Worker's address, such as `https://name.your-subdomain.workers.dev`. Leave it out to turn summaries off.
   - `DIGEST_ENABLED`: `true`. This is the on switch, so set it last.
7. **Test it.** Actions > Daily digest > Run workflow > `morning`. When it finishes, open your site, click **Today's briefing**, and check your inbox. Then click **Set key** on the dashboard and paste your `ACCESS_KEY`. With the key set, the On X panel shows Add account and Remove. A change appears on the site a couple of minutes later.

`.env.example` lists every setting by name, with empty values. Real values never go in a file. `.gitignore` only applies to `git add`, and uploading a folder on github.com commits everything in it, so keep credential files outside the project folder.

## Local development

```sh
pip install --require-hashes -r requirements-build.lock
pip install --require-hashes --no-build-isolation -r requirements.lock
python3 scripts/fetch_and_build.py
python3 scripts/learn/candidates.py --out /tmp/candidates.json
python3 scripts/learn/brief.py --candidates /tmp/candidates.json --out /tmp/briefing.json
python3 -m unittest discover -s tests
python3 -m http.server 8000 --bind 127.0.0.1 -d docs
```

Then open http://localhost:8000/. Without `ANTHROPIC_API_KEY` set, `brief.py` writes a briefing from the headlines, which is enough to try the rest. To call Claude locally, also run `pip install --require-hashes -r requirements-brief.lock`. To change dependencies, edit the `.in` files and regenerate the locks as described at the top of `requirements.in`.
