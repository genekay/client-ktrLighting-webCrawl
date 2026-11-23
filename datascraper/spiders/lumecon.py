#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lumecon product spider (https://lumecon.com/).

Seed is the /categories/ hub. The crawl drills down from there to whatever
depth the site has, following only links inside the page body (`#Content`),
so the header/footer menus and the PDP "Related products" strip never widen
it. On every non-product page reached this way:

- `/product/<slug>/` links are PDPs -> parse_product_detail
- any other on-site HTML link is another listing level -> parse_listing
  again. That covers WooCommerce product-category pages, their `/page/N/`
  pagination and sub-category tiles, and the hand-built "Specialty Lighting"
  page whose children (/amber/, /baa-compliant/, /dark-sky-friendly/) are
  plain WordPress pages listing products, not product categories.

Scrapy's dupefilter drops pages already seen (http/https are folded to
https first, since the hub links both), and PDPs are also deduped on their
final URL (old product slugs redirect), so a product listed under several
categories is emitted once, with the first category it was reached from.

The site is WooCommerce on the BeTheme theme and server-rendered, so no
browserHtml is needed:

- title: `h1.product_title`. There is no SKU field anywhere on the PDP; some
  titles lead with a catalog code ("LM-GRL The Grand Lantern"), some don't.
- images: the WooCommerce gallery, one `.woocommerce-product-gallery__image`
  per carousel slide, each linking to the full-size file.
- specsheet: links in the "DOWNLOADS" accordion labelled "Spec Sheet" (one
  product, LROF, has two). About 20 products label their only datasheet by
  product name instead ("Lumecon LMB-GB05", "Lumecon Steel Poles", ...); for
  those the first PDF that isn't an obvious secondary document (photometric
  files, ordering checklist, manual, ...) is used.
"""

import logging
import re
from urllib.parse import urlsplit, urlunsplit

import scrapy
from scrapy.http import Request, TextResponse

from datascraper.items import LUMECON_FIELDS

logger = logging.getLogger(__name__)

START_URL = "https://lumecon.com/categories/"
HOST = "lumecon.com"

_PRODUCT_PATH_RE = re.compile(r"^/product/[^/]+/?$")
# Non-page targets that can show up in body links.
_SKIP_PATH_RE = re.compile(r"^/(?:wp-content|wp-json|wp-admin|feed)/|/feed/?$|\.\w{2,4}$", re.I)
_SPEC_RE = re.compile(r"spec\s*sheet", re.I)
_NOT_SPEC_RE = re.compile(
    r"photometric|\bies\b|checklist|manual|application|configur|operation|statement|compliance",
    re.I,
)


def _clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


def _canonical(url):
    """https, lowercase host, no fragment -- so http/https dups collapse."""
    parts = urlsplit(url)
    return urlunsplit(("https", parts.netloc.lower(), parts.path or "/", parts.query, ""))


class LumeconSpider(scrapy.Spider):
    name = "web-lumecon"
    allowed_domains = [HOST]

    custom_settings = {
        "CONCURRENT_REQUESTS": 8,
        "RETRY_TIMES": 6,
        "RETRY_HTTP_CODES": [403, 429, 500, 502, 503, 504, 520, 521, 522, 524, 408],
        # Guard against a link loop between content pages; real depth is ~4.
        "DEPTH_LIMIT": 10,
        "FEED_EXPORT_FIELDS": LUMECON_FIELDS,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._seen_url = {}

    # Scrapy >= 2.13 drives spiders through `start()`; the Scrapy Cloud stack
    # (scrapy:2.12) only knows `start_requests()`. Define both.
    async def start(self):
        for request in self.start_requests():
            yield request

    def start_requests(self):
        yield Request(START_URL, callback=self.parse_listing, errback=self.on_error)

    # -------------------------------------------------------------- listing
    def parse_listing(self, response):
        # A mistyped body link (e.g. /prodproductuct/lws-qm/ on the BAA page)
        # can redirect straight to an image.
        if not isinstance(response, TextResponse):
            logger.warning("[%s] listing link is not an HTML page; skipping", response.url)
            return
        n_products = n_pages = 0
        for href in response.css("#Content a::attr(href)").getall():
            url = response.urljoin(href.strip())
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or parts.netloc.lower() != HOST:
                continue
            url = _canonical(url)
            if _PRODUCT_PATH_RE.match(parts.path):
                n_products += 1
                yield Request(
                    url,
                    callback=self.parse_product_detail,
                    errback=self.on_error,
                    meta={"cate_url": response.url},
                )
            elif not _SKIP_PATH_RE.search(parts.path) and url != _canonical(response.url):
                n_pages += 1
                yield Request(url, callback=self.parse_listing, errback=self.on_error)
        logger.info("[%s] %d product links, %d listing links", response.url, n_products, n_pages)

    # ------------------------------------------------------------------ PDP
    def parse_product_detail(self, response):
        # Old slugs (e.g. lmb-fc-full-cutoff-bollard on the BAA page) redirect
        # to the current one. Zyte API follows redirects server-side, so the
        # dupefilter only saw the old URL; dedupe on where we landed.
        first = self._seen_url.setdefault(_canonical(response.url), response.request.url)
        if first != response.request.url:
            logger.info("[dup] %s (via %s) already emitted via %s", response.url, response.request.url, first)
            return

        title =_clean(" ".join(response.css("h1.product_title ::text").getall()))
        if not title:
            logger.warning("[%s] no product title; skipping", response.url)
            return

        images = []
        for slide in response.css(".woocommerce-product-gallery__image"):
            src = (
                slide.css("a::attr(href)").get()
                or slide.css("img::attr(data-large_image)").get()
                or slide.css("img::attr(src)").get()
            )
            if src:
                src = response.urljoin(src.strip())
                if src not in images:
                    images.append(src)
        if not images:
            logger.warning("[%s] no gallery images for %r", response.url, title)

        yield {
            "title": title,
            "images": ";".join(images),
            "specsheet": ";".join(self._specsheets(response, title)),
            "url": response.url,
            "cate_url": response.meta.get("cate_url", ""),
        }

    def _specsheets(self, response, title):
        downloads = [
            q for q in response.css(".accordion .question")
            if _clean(" ".join(q.css(".title ::text").getall())).upper() == "DOWNLOADS"
        ]
        if not downloads:
            logger.warning("[%s] no DOWNLOADS section for %r", response.url, title)
            return []

        links = []
        for a in downloads[0].css(".answer a[href]"):
            href = response.urljoin(a.attrib["href"].strip())
            if urlsplit(href).scheme in ("http", "https"):
                links.append((_clean(" ".join(a.css("::text").getall())), href))

        specs = [href for label, href in links if _SPEC_RE.search(label)]
        if not specs:
            # Datasheet labelled by product name rather than "Spec Sheet".
            fallback = [
                href for label, href in links
                if urlsplit(href).path.lower().endswith(".pdf") and not _NOT_SPEC_RE.search(label)
            ]
            specs = fallback[:1]
            if specs:
                logger.info("[%s] no 'Spec Sheet' label; using first datasheet PDF %s", response.url, specs[0])
            else:
                logger.warning("[%s] no spec sheet among %d downloads for %r", response.url, len(links), title)

        return list(dict.fromkeys(specs))

    def on_error(self, failure):
        logger.warning("[%s] request failed: %s", failure.request.url, failure.value)
