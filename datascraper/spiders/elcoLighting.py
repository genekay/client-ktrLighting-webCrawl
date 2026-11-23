#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ELCO Lighting product-SKU spider (https://www.elcolighting.com/).

Seed is the "Product Systems" page. Every tile on it is a Drupal taxonomy
term; a term page holds either more tiles (sub-categories) or product cards
linking to /products/<slug>, which is the PDP. How many tiles deep a PDP
sits varies per branch (1 to 6+ observed), so this spider never assumes a
depth: every followed page is classified by what it *is*, not by how it was
reached:

    page-product-systems / page-taxonomy-term  -> follow its tiles + cards
    node-type-product with a #sku-table        -> scrape, one item per row
    anything else                              -> log and skip

Drupal exposes the same product from several category paths, but a PDP has
a single /products/<slug> URL, so the default dupefilter collapses those.
Catalog Number is the primary key: a catalog number seen on a second PDP, or
twice in one table, is dropped with a warning rather than emitted twice.
Tables with no Catalog Number column (Koto module+trim pages) are still
emitted, with a blank key, so those products are not lost.

PDP layout notes (confirmed on live markup via archive snapshots):
- Images: the thumbnail strip under PRODUCT IMAGE / FINISHES / LINE ART is
  the visual left-to-right, top-to-bottom order. Each thumb is a Drupal
  image-style derivative (`/styles/product_small/public/<file>?itok=..`);
  stripping the style segment gives the original upload, which is exactly
  what the gallery's lightbox anchor points at. Those originals are what
  `images` holds, ';'-joined, same for every row of that PDP.
- Product SKU table: `#sku-table`, headers in <thead>, column set varies per
  product. Spec column is an <a> to
  `/printpdf/products/<slug>?catalog-number=<CAT>` (a direct PDF download);
  the href is captured as an absolute URL.
- Product Number Builder (`section#product-number`): one div.column per
  builder segment. The finish column's options (`li > span.key/span.val`)
  become `available-finishes` as "CODE: Name" ';'-joined, blanket-mapped
  to every row of that PDP like `images`. Empty where the PDP has no
  builder or no finish column.

Site is behind a Cloudflare JS challenge (plain fetch -> 403 "Just a
moment..."), so all requests go through scrapy-zyte-api transparent mode
(project default in settings.py). Pages are server-rendered Drupal 7; no
browserHtml needed.
"""

import json
import logging
import re
from urllib.parse import urlsplit, urlunsplit

import scrapy
from scrapy.http import Request

from datascraper.items import ELCO_FIELDS

logger = logging.getLogger(__name__)

START_URL = "https://www.elcolighting.com/product-systems"

# Page-content container. The header megamenu reuses an `item-wrap` class,
# so tile/card xpaths are rooted here to keep the crawl inside the content.
_CONTENT = "//div[contains(@class,'main-container')]"
_TILE_LINKS = _CONTENT + "//div[contains(@class,'item-wrap')]/a/@href"
_CARD_LINKS = _CONTENT + "//div[contains(@class,'single-product')]//a[contains(@href,'/products/')]/@href"

# Drupal image-style derivative -> original upload.
_IMAGE_STYLE_RE = re.compile(r"/styles/[^/]+/public/")

_PAGE_FIELDS = {"images", "available-finishes", "url", "title", "cate_url", "extra"}
_TABLE_FIELDS = set(ELCO_FIELDS) - _PAGE_FIELDS

# Product Number Builder: one div.column per builder segment, h5 = label,
# li > span.key / span.val = option code / option name. Only the column(s)
# whose label mentions "finish" (e.g. "Color Finish") are scraped.
_BUILDER_FINISH_COLUMNS = (
    "//section[@id='product-number']//div[contains(@class,'column')]"
    "[h5[contains(translate(normalize-space(.),'FINSH','finsh'),'finish')]]"
)


def _clean(value):
    return re.sub(r"\s+", " ", value).strip() if value else ""


def _norm_header(label):
    """'Catalog Number' -> 'catalog_number', 'Lamp type' -> 'lamp_type'."""
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def _original_image_url(src):
    """Strip the image-style segment and the itok cache-buster so the URL is
    the original file, e.g.
    .../files/styles/product_small/public/EPD4W_3D.png?itok=x
      -> .../files/EPD4W_3D.png"""
    parts = urlsplit(src)
    path = _IMAGE_STYLE_RE.sub("/", parts.path, count=1)
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


class ElcoLightingSpider(scrapy.Spider):
    name = "web-elcoLighting"
    allowed_domains = ["elcolighting.com"]

    custom_settings = {
        "CONCURRENT_REQUESTS": 8,
        "AUTOTHROTTLE_ENABLED": True,
        "RETRY_TIMES": 6,
        "RETRY_HTTP_CODES": [403, 429, 500, 502, 503, 504, 520, 521, 522, 524, 408],
        # Fixed CSV shape; unknown table headers are folded into `extra`.
        "FEED_EXPORT_FIELDS": ELCO_FIELDS,
    }

    def __init__(self, start_url=None, *args, **kwargs):
        """`-a start_url=` narrows the crawl to one branch (or one PDP) for
        testing; default is the full Product Systems tree."""
        super().__init__(*args, **kwargs)
        self.start_url = start_url or START_URL
        self._seen_catalog = {}
        self._unknown_headers = set()

    # Scrapy >= 2.13 drives spiders through `start()`; 2.19's default no
    # longer falls back to `start_requests()` (confirmed: the crawl closed
    # with zero requests). The Scrapy Cloud stack is scrapy:2.12, which only
    # knows `start_requests()`. Define both so the spider runs on either.
    async def start(self):
        for request in self.start_requests():
            yield request

    def start_requests(self):
        yield Request(self.start_url, callback=self.parse_page, errback=self.on_error)

    # ------------------------------------------------------------------ routing
    def parse_page(self, response):
        body_class = response.xpath("//body/@class").get(default="")
        is_pdp = "node-type-product" in body_class and response.xpath("//table[@id='sku-table']")
        if is_pdp:
            yield from self.parse_product_detail(response)
            return

        if "page-taxonomy-term" in body_class or "page-product-systems" in body_class:
            yield from self._follow_children(response)
            return

        logger.info("[skip] not a category or PDP: %s (body=%s)", response.url, body_class[:60])

    def _follow_children(self, response):
        hrefs = response.xpath(_TILE_LINKS).getall() + response.xpath(_CARD_LINKS).getall()
        urls = []
        for href in hrefs:
            url = response.urljoin(href)
            if url.startswith("javascript:") or "/printpdf/" in url:
                continue
            urls.append(url.split("#", 1)[0])
        urls = list(dict.fromkeys(urls))

        if not urls:
            logger.warning("[empty] no tiles or product cards on %s", response.url)

        for url in urls:
            yield Request(
                url,
                callback=self.parse_page,
                errback=self.on_error,
                meta={"cate_url": response.url},
            )

    # -------------------------------------------------------------------- PDP
    def parse_product_detail(self, response):
        title = _clean(response.xpath("//h1//text()").get())
        images = self._extract_images(response)
        if not images:
            logger.warning("[%s] no images found", response.url)

        table = response.xpath("//table[@id='sku-table']")
        headers = [_norm_header(_clean(" ".join(th.xpath(".//text()").getall())))
                   for th in table.xpath("./thead//th")]
        # Some PDPs (Koto module + trim combos, ~69 on the site) have a SKU
        # table with no Catalog Number column at all. Emit those rows with a
        # blank key rather than drop the product; they are excluded from the
        # catalog-number dedup since there is nothing to key on.
        keyed = "catalog_number" in headers
        if not keyed:
            logger.warning("[%s] #sku-table has no Catalog Number column (%s); emitting rows with blank catalog_number",
                           response.url, headers)

        for header in headers:
            if header not in _TABLE_FIELDS and header not in self._unknown_headers:
                self._unknown_headers.add(header)
                logger.warning("[%s] new SKU table column %r -> stored in `extra`", response.url, header)

        base = {
            "images": ";".join(images),
            "available-finishes": ";".join(self._extract_finishes(response)),
            "url": response.url,
            "title": title,
            "cate_url": response.meta.get("cate_url", ""),
        }

        rows = table.xpath("./tbody/tr")
        if not rows:
            logger.warning("[%s] #sku-table has no rows", response.url)

        seen_on_page = set()
        for tr in rows:
            cells = tr.xpath("./td")
            if len(cells) != len(headers):
                logger.warning("[%s] row has %d cells for %d headers; skipping", response.url, len(cells), len(headers))
                continue

            item = {field: "" for field in ELCO_FIELDS}
            item.update(base)
            extra = {}
            for header, td in zip(headers, cells):
                if header == "spec":
                    href = td.xpath(".//a/@href").get()
                    value = response.urljoin(href) if href else ""
                else:
                    value = _clean(" ".join(td.xpath(".//text()").getall()))
                if header in _TABLE_FIELDS:
                    item[header] = value
                else:
                    extra[header] = value
            item["extra"] = json.dumps(extra) if extra else ""

            cat = item["catalog_number"]
            if keyed:
                if not cat:
                    logger.warning("[%s] row with empty Catalog Number skipped", response.url)
                    continue
                first = self._seen_catalog.setdefault(cat, response.url)
                if first != response.url:
                    logger.warning("[dup] catalog number %s on %s already emitted from %s", cat, response.url, first)
                    continue
                if cat in seen_on_page:
                    logger.warning("[dup] catalog number %s listed twice on %s", cat, response.url)
                    continue
                seen_on_page.add(cat)
            yield item

    @staticmethod
    def _extract_images(response):
        srcs = response.xpath(
            "//section[@id='overview']//div[contains(@class,'thumbs')]"
            "//div[contains(@class,'thumbs-box')]//img/@src"
        ).getall()
        urls = (_original_image_url(response.urljoin(src)) for src in srcs)
        # Same original can back two thumbs (e.g. hero image repeated under
        # FINISHES); keep first occurrence so order is preserved.
        return list(dict.fromkeys(urls))

    @staticmethod
    def _extract_finishes(response):
        """Finish options from the Product Number Builder as "CODE: Name",
        in on-page order. Empty when the PDP has no builder or no finish
        column (accessories, junction boxes). Footnote markers (<sup>) in
        the name are dropped."""
        columns = response.xpath(_BUILDER_FINISH_COLUMNS)
        if len(columns) > 1:
            labels = [_clean(" ".join(c.xpath("./h5//text()").getall())) for c in columns]
            logger.info("[%s] %d finish columns in builder, merging: %s", response.url, len(columns), labels)
        finishes = []
        for li in columns.xpath(".//ul/li"):
            code = _clean(" ".join(li.xpath("./span[contains(@class,'key')]//text()").getall()))
            name = _clean(" ".join(li.xpath("./span[contains(@class,'val')]//text()[not(ancestor::sup)]").getall()))
            if code or name:
                finishes.append(f"{code}: {name}" if code and name else code or name)
        return list(dict.fromkeys(finishes))

    def on_error(self, failure):
        logger.warning("[%s] request failed: %s", failure.request.url, failure.value)
