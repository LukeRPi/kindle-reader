# KindleReader

A self-hosted RSS reader and article fetcher designed specifically for the **Kindle experimental browser** (tested on Kindle 2013, firmware 5.12).

The interface is intentionally minimal: no flexbox, no animations, high-contrast black and white, large tap targets. It runs as a Docker container and serves a plain HTML interface your Kindle can actually use.

![Kindle 2013](https://img.shields.io/badge/Kindle-2013%20%2F%20SW%205.12-black) ![Docker](https://img.shields.io/badge/Docker-compose-blue) ![Python](https://img.shields.io/badge/Python-3.12-green)

---

## Features

- **RSS aggregator** — organise feeds into categories, paginated article lists
- **Article extractor** — full-text reading with RSS summary fallback if the site blocks scraping
- **Reddit support** — dedicated JSON API integration: reads post body + top comments with nested replies
- **Read later** — save articles from any feed with one tap; also accepts links via integrated API
- **Read tracking** — articles marked as read are dimmed in the list; toggle to hide them entirely
- **Reading time estimate** — calculated on the full extracted article text
- **Font size control** — S / M / L buttons in the toolbar
- **In-memory cache** — feeds cached for 30 minutes, manual refresh available
- **Fully configurable** — add/remove feeds by editing a YAML file, no rebuild required

---

## Requirements

- Docker
- docker compose

---

## Usage

### Quick start - use the pre-built image (recommended)
 
Create a `docker-compose.yml` with the following content:
 
```yaml
services:
  kindle-reader:
    image: ghcr.io/lukerpi/kindle-reader:latest
    container_name: kindle-reader
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      - ./config:/config:ro      # RSS feeds config (read-only)
      - ./data:/data             # saved articles (bind mount)
    environment:
      - CACHE_TTL=1800
      - MAX_ITEMS=25
      - REQUEST_TIMEOUT=15
      - FEEDS_FILE=/config/feeds.yaml
      - DATA_FILE=/data/saved.json
      - API_KEY=change-this-to-a-secret-key
```
 
Then create the required directories, drop in your `feeds.yaml` and start:
 
```bash
mkdir -p config data
cp feeds.sample.yaml config/feeds.yaml   # edit as needed
docker compose pull
docker compose up -d
```

### Build from source

```bash
git clone https://github.com/LukeRPi/kindle-reader
cd kindle-reader

# Set your API key in docker-compose.yml before starting
# API_KEY=your-secret-key-here

docker compose up -d --build
```

Open `http://<your-server-ip>:5000` in the Kindle experimental browser.

---

## Project structure

```
kindle-reader/
├── Dockerfile
├── docker-compose.yml
├── config/
│   └── feeds.yaml           # Edit this to add/remove feeds — no rebuild needed
├── data/                    # Saved articles (bind-mounted, persists across rebuilds)
└── app/
    ├── app.py
    ├── requirements.txt
    ├── static/
    │   └── style.css
    └── templates/
        ├── base.html
        ├── index.html
        ├── category.html
        ├── feed.html
        ├── article.html
        ├── saved_list.html
        └── saved_article.html
```

---

## Configuring feeds

Edit `config/feeds.yaml`. Changes are picked up immediately — no restart needed.

```yaml
categories:
  - name: "Section1"
    icon: "[S1]"
    feeds:
      - name: "Source 1"
        url: "https://..."

  # Multiple URLs in one feed = merged and sorted by date
  - name: "Reddit"
    icon: "[R]"
    feeds:
      - name: "Reddit Mix"
        urls:
          - "https://www.reddit.com/r/subreddit1/hot.rss"
          - "https://www.reddit.com/r/subreddit2/hot.rss"
```

---

## Save articles for later

Tap **[+] Salva** next to any article in the Kindle interface to save it directly.

You can also push articles from any external tool via the REST API:

```bash
curl -X POST http://localhost:5000/api/save \
  -H "X-API-Key: your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/article"}'
```

The server fetches and stores the full extracted text server-side. Useful for sending links from your phone via any automation tool or script you already use.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `API_KEY` | — | Required for the `/api/save` endpoint |
| `CACHE_TTL` | `1800` | Feed cache duration in seconds |
| `MAX_ITEMS` | `25` | Max articles fetched per feed |
| `REQUEST_TIMEOUT` | `15` | HTTP timeout for article fetching |
| `FEEDS_FILE` | `/config/feeds.yaml` | Path to feeds config |
| `DATA_FILE` | `/data/saved.json` | Path to saved articles JSON |

---

## License

MIT
