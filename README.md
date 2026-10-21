# Industry News Bot

Pulls new articles from ~36 SEO, paid media, and AI sources every 30 minutes, has Claude score and summarize them, and posts them to Slack.

## How posting works

Each new article gets a relevance score from 1 to 5 and is routed to one channel:

| Score | What happens |
|---|---|
| 5 (must-know: core updates, major platform or policy changes, flagship model launches) | Posted right away with 🚨, even if the daily cap is reached |
| 4 | Posted right away, up to **8 per channel per day** |
| 3, or a 4 after the cap is hit | Held for one **"Also worth a look"** roundup per channel at 4pm Central |
| 1–2 | Dropped |

If the same story shows up from several outlets, the extra coverage goes into a **thread reply** under the first post instead of a new message. So each channel gets at most about 8–9 top-level messages a day, plus breaking news.

You can change all of these limits in `settings` at the top of `sources.yaml`.

## Setup

### 1. Slack channels and app
1. Create the channels, for example `#news-seo`, `#news-paid-media`, and `#news-ai`.
2. Go to https://api.slack.com/apps → **Create New App** → **From a manifest**, pick the workspace, and paste:
   ```yaml
   display_information:
     name: Industry News
     description: SEO, paid media, and AI news, summarized.
   features:
     bot_user:
       display_name: Industry News
       always_online: true
   oauth_config:
     scopes:
       bot:
         - chat:write
         - chat:write.public
   settings:
     org_deploy_enabled: false
     socket_mode_enabled: false
   ```
3. Click **Install to Workspace** and copy the **Bot User OAuth Token** (`xoxb-...`).
4. Get each channel's ID: open the channel → click its name → the ID is at the bottom of the About tab. Paste the IDs into `channels:` in `sources.yaml`.
   - The bot can post to public channels without being invited. For a private channel, run `/invite @Industry News` in it first.

### 2. GitHub
1. Create a **private** repo and push this folder to it.
2. Go to repo **Settings → Secrets and variables → Actions** and add two secrets:
   - `ANTHROPIC_API_KEY`: from https://console.anthropic.com
   - `SLACK_BOT_TOKEN`: the `xoxb-...` token
3. Go to the **Actions** tab → **Industry news bot** → **Run workflow** to start the first run by hand.

**First run:** every source's existing articles are marked as already seen and nothing is posted. Posts start with the next article published after that. The same thing happens automatically whenever you add a source later, so a new source never floods a channel.

## Running locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python bot.py --check-feeds                     # check that every feed loads
ANTHROPIC_API_KEY=... python bot.py --dry-run --test-limit 2   # preview real summaries in the terminal, no Slack
```

## Maintenance
- **Add or remove a source:** edit `sources.yaml`.
- **A feed broke:** the run log in the Actions tab shows `FEED ERROR <name>`. The other sources keep working.
- **State:** `state/state.json` tracks seen and posted articles and is committed back to the repo after each run. Delete it to start fresh (the next run re-seeds every source).
- **Model and cost:** the bot uses `claude-opus-5-5` at low effort. Each run does one cheap scoring pass over headlines; only articles that will be posted get a full read and summary. To cut API cost by about 95%, change `model:` to `claude-haiku-5-5`.

## Sources with workarounds
- **Search Engine Land** blocks scripted feed requests. The bot tries the real feed first and falls back to Google News, which only gives headlines, so those summaries are thinner.
- **Anthropic** has no RSS feed, so the bot scrapes the links on anthropic.com/news.
- **Not included (no usable feed):** LinkedIn Marketing Blog, TikTok newsroom, Amazon Ads blog, Perplexity, Meta AI, Profound. Trade press covers their big announcements.
