# Self-hosted SearXNG instance for web-crawler's live_url_search.py, replacing
# the free/unofficial DuckDuckGo scraper (ddgs) that proved unreliable under
# real load (see incident: identical query returned 5 results from a local
# machine and 0 from the Azure Function's outbound IP, moments apart).
#
# Settings path (/etc/searxng/settings.yml) and listening port (8080) are
# confirmed directly from the base image itself — not guessed:
#   docker inspect searxng/searxng:latest --format '{{range .Config.Env}}{{println .}}{{end}}'
# shows __SEARXNG_SETTINGS_PATH=/etc/searxng/settings.yml and GRANIAN_PORT=8080
# (this image serves via Granian, not the older uwsgi setup some docs describe).
FROM searxng/searxng:latest

COPY settings.yml /etc/searxng/settings.yml

EXPOSE 8080
