#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
Schonbek brand catalog spiders (Signature / Beyond / Forever).

Fix history:
- Site was 403'ing on the first request. Root cause: schonbek.com now sits
  behind bot-detection that fingerprints the plain Scrapy/Twisted downloader.
  Fixed by moving to scrapy-zyte-api transparent mode (same as the WAC
  invoice spider), which routes through Zyte's proxy/fingerprint layer
  instead of hand-rolled UA rotation.
- Site also got a front-end rewrite since this spider last worked. Collection
  and product listing grids are now injected client-side (confirmed: family
  pages serve a "No products found" placeholder until JS populates the grid).
  Fixed by requesting browserHtml from Zyte API for listing pages only;
  product detail pages are still server-rendered PHP and use the cheaper
  httpResponseBody default.
- Selectors rebuilt on stable href patterns (/families/?family=,
  /products/?product=) instead of the old class-name-based xpaths, which no
  longer exist in the current markup.

Assumes DOWNLOAD_HANDLERS / TWISTED_REACTOR / REQUEST_FINGERPRINTER_CLASS for
scrapy-zyte-api are already set in the project's settings.py (they must be,
since those are process-boot-time settings that custom_settings can't
reliably override). See README in this delivery for details.
"""

import datetime
import logging
import re

import scrapy
from scrapy.http import Request

logger = logging.getLogger(__name__)

# Download types whose asset URLs contain these path fragments (case-insensitive).
_DOWNLOAD_PATTERNS = {
    "specsheet": "/specsheet",
    "installation_guide": "/inst_sheet",
    "line_drawing": "/line_drawing",
    "ies_file": "/ies_files",
    "dimming_file": "/dim_report",
}

_LOWER = "translate(@href, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"

# "core" is the item/SKU code (e.g. "SW32520"), not a description. On the
# current template it sits as the text node right after <h1>. Validate
# against this shape before trusting it, since "following::text()" has no
# guardrails of its own.
_SKU_RE = re.compile(r"^[A-Za-z0-9]{3,15}$")


class SchonbekSignatureSpider(scrapy.Spider):
    name = "web-schonbek-signature"
    start_urls = ["https://schonbek.com/brands/signature/"]

    custom_settings = {
        "ZYTE_API_TRANSPARENT_MODE": True,
        "CONCURRENT_REQUESTS": 8,
        "AUTOTHROTTLE_ENABLED": True,
        "ROBOTSTXT_OBEY": False,
        "RETRY_TIMES": 6,
        "RETRY_HTTP_CODES": [429, 500, 502, 503, 504, 520, 521, 522, 524, 408],
    }

    # Listing pages (brand / family) need a rendered DOM; product pages don't.
    _BROWSER_META = {"zyte_api_automap": {"browserHtml": True}}

    def __init__(self, scraped_key=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.scraped_key = scraped_key or f"{self.name}_{datetime.datetime.now():%Y%m%d}"

    def start_requests(self):
        for url in self.start_urls:
            yield Request(url, callback=self.parse, meta=self._BROWSER_META, errback=self.on_error)

    def parse(self, response):
        # Collection links live directly on the brand page: /families/?family=X
        family_urls = response.xpath("//a[contains(@href, '/families/?family=')]/@href").getall()
        if not family_urls:
            logger.warning("No family links found on %s — page markup may have changed again", response.url)
        for url in family_urls:
            yield Request(
                response.urljoin(url),
                callback=self.parse_family,
                meta={**self._BROWSER_META, "cate_url": response.url},
                errback=self.on_error,
            )

    def parse_family(self, response):
        product_urls = response.xpath("//a[contains(@href, '/products/?product=')]/@href").getall()
        if not product_urls:
            logger.warning("No product links found on %s", response.url)
        for url in product_urls:
            yield Request(
                response.urljoin(url),
                callback=self.parse_product_detail,
                meta={"cate_url": response.meta.get("cate_url", response.url)},
                errback=self.on_error,
            )

    def parse_product_detail(self, response):
        item = {
            "cate_url": response.meta.get("cate_url", ""),
            "url": response.url,
            "title": response.xpath("//h1/text()").get(default="").strip(),
            "images": self._extract_images(response),
        }
        for field, fragment in _DOWNLOAD_PATTERNS.items():
            links = [
                response.urljoin(href)
                for href in response.xpath(f"//a[contains({_LOWER}, '{fragment}')]/@href").getall()
            ]
            item[field] = links[0] if links else None

        item["core"] = self._extract_core(response, item)

        yield item

    @staticmethod
    def _extract_images(response):
        srcs = response.xpath("//img[contains(@src, 'TOP_PROD_IMAGE')]/@src").getall()
        # Same image is often present in both the thumb strip and the main gallery.
        return list(dict.fromkeys(response.urljoin(src) for src in srcs))

    @staticmethod
    def _extract_core(response, item):
        # Primary: the SKU renders as the text node immediately after <h1>
        # (e.g. "<h1>Betony</h1> SW32520"). Validate the shape so an
        # unrelated stray text node doesn't get mistaken for a SKU.
        candidate = response.xpath("//h1/following::text()[normalize-space()][1]").get(default="").strip()
        if _SKU_RE.match(candidate):
            return candidate

        # Fallback: line_drawing/installation_guide filenames reliably embed
        # the SKU (SW32520_LINDR.jpg, SW32520_INSSHT.pdf) — use them if the
        # DOM lookup above didn't pan out.
        for field, marker in (("line_drawing", "_LINDR"), ("installation_guide", "_INSSHT")):
            href = item.get(field)
            if href:
                stem = href.rsplit("/", 1)[-1]
                if marker in stem:
                    return stem.split(marker)[0]

        # Last resort: product image filenames are "{SKU}-{finish}_IMRO_n.jpg".
        if item.get("images"):
            stem = item["images"][0].rsplit("/", 1)[-1]
            return stem.split("-")[0] if "-" in stem else stem.split("_IMRO")[0]

        return None

    def on_error(self, failure):
        logger.warning("Request failed: %s — %s", failure.request.url, failure.value)


class SchonbekBeyondSpider(SchonbekSignatureSpider):
    name = "web-schonbek-beyond"
    start_urls = ["https://schonbek.com/brands/beyond/"]


class SchonbekForeverSpider(SchonbekSignatureSpider):
    name = "web-schonbek-forever"
    start_urls = ["https://schonbek.com/brands/forever/"]
