# -*- coding: utf-8 -*-
import os

BOT_NAME = "datascraper"
SPIDER_MODULES = ["datascraper.spiders"]
NEWSPIDER_MODULE = "datascraper.spiders"

# --- Zyte API (rotating proxies + ban handling + sessions) -------------------
# Smart Proxy Manager / Crawlera is legacy; Zyte API is the current product and
# is what "Zyte rotating proxy service" maps to today.
ADDONS = {"scrapy_zyte_api.Addon": 500}
ZYTE_API_KEY = os.environ.get("ZYTE_API_KEY", "")  # auto-provided on Scrapy Cloud
ZYTE_API_TRANSPARENT_MODE = True  # route all requests through Zyte API + automap
# Retry transient website-connection (421) and temporary-ban (520/521) errors.
ZYTE_API_RETRY_POLICY = "datascraper.retry.RETRY_POLICY"

# Each account keeps its own Scrapy cookie jar (meta['cookiejar']=user in the
# spider) so the ASP.NET auth cookie persists across login -> r09 -> r10, and
# accounts don't cross-contaminate. scrapy-zyte-api forwards that jar to Zyte
# API only when BOTH of these are on:
COOKIES_ENABLED = True
ZYTE_API_EXPERIMENTAL_COOKIES_ENABLED = True

# Throttling and ban handling are delegated to Zyte API; just cap local fan-out.
AUTOTHROTTLE_ENABLED = False
CONCURRENT_REQUESTS = 8
RETRY_ENABLED = True
DOWNLOAD_TIMEOUT = 180

ROBOTSTXT_OBEY = False
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
FEED_EXPORT_ENCODING = "utf-8"
LOG_LEVEL = "INFO"
