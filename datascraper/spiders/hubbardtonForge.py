#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hubbardton Forge product spider (https://hubbardtonforge.com/products?page=1).

One item per *configured* SKU: every PDP is expanded into one row for each
selectable option combination (finish x accent x glass x ...).

The storefront is Magento PWA Studio (Venia): the server returns the same
empty shell for every URL and React fills it in. Two fetch modes are used:

- Listing pages (`/products?page=N`, 30 cards per page) are rendered with Zyte
  API browserHtml, because the grid is filled client-side. Page 1's pagination
  block ("1063 Results ... of 36") gives the page count and the expected
  product count, which is checked again when the spider closes.
- PDPs are read from the storefront's own public GraphQL endpoint (`/graphql`),
  the same query the page runs. No clicking through selectors is needed: the
  response carries every option and every variant that exists.

Everything below ports what the PDP's JavaScript does with that response:

- `sku` is "Configured Item #". The page finds the variant whose attributes
  equal the current selections and shows its sku. So the rows are the variants
  themselves, not the raw option cross product: ~280 PDPs have combinations
  the site disables because no such variant exists. If two variants carry the
  same combination only the first can ever be shown (`Array.find`), so later
  ones are skipped and logged.
- `images` is the carousel, left to right, for that configuration: the parent
  gallery, plus (unless the configuration is the page's default one) the
  variant's own images numbered above 59, sorted by the number in the file name
  (`<smart string>_<n>.png`; see `_image_sort_key`). Magento's resized
  `/cache/<hash>/` copy is mapped to the full-size original, which is also what
  the site does for its own drawing download.
- The DOWNLOADS drawer is not in the markup until it is opened. Its buttons, in
  order, and where each one's URL comes from:
    "Specifications Sheet"   no URL exists: the button prints a React component
                             in the browser (react-to-print). Column kept, blank.
    "Installation Sheet"     hf_installation_instructions, a JSON list of paths
                             on the Pimcore CDN. With more than one entry the
                             buttons are "Installation Sheet 1", "... 2", etc.
    "Dimensional Drawing"    the gallery image numbered 5.
  CAD / Revit / Images buttons only render for signed-in trade accounts and
  are not part of the public drawer, so they are not collected.
"""

import json
import logging
import re
from urllib.parse import urlsplit

import scrapy
from scrapy.downloadermiddlewares.retry import get_retry_request
from scrapy.http import JsonRequest, Request
from w3lib.url import safe_url_string

from datascraper.items import HUBBARDTONFORGE_FIELDS

logger = logging.getLogger(__name__)

BASE_URL = "https://hubbardtonforge.com"
GRAPHQL_URL = BASE_URL + "/graphql"
LISTING_URL = BASE_URL + "/products?page={page}"
PAGE_SIZE = 30

_CARD = 'div[class*="productItem-root"]'
# Grid is filled after the first paint; wait for a card's "Base Item" block.
_LISTING_META = {
    "zyte_api_automap": {
        "browserHtml": True,
        "actions": [
            {
                "action": "waitForSelector",
                "selector": {"type": "css", "value": '[class*="productItem-skuContent"]'},
                "timeout": 15,
            }
        ],
    }
}

PRODUCT_QUERY = """
query hubbardtonProduct($urlKey: String!) {
  storeConfig { pimcore_assets_host }
  products(filter: { url_key: { eq: $urlKey } }) {
    items {
      __typename
      sku
      name
      url_key
      hf_installation_instructions
      media_gallery { url }
      ... on ConfigurableProduct {
        configurable_options { attribute_code }
        variants {
          attributes { code value_index }
          product {
            sku
            hf_smart_string
            hf_installation_instructions
            media_gallery { url }
          }
        }
      }
    }
  }
}
"""

_CACHE_RE = re.compile(r"/cache/[^/]+/")
# First "<anything without _ or />_<digits>" path segment, as in the bundle.
_IMAGE_NUMBER_RE = re.compile(r"/[^/_]*_(\d+)")


def _clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


def _media_urls(product):
    return [m["url"] for m in product.get("media_gallery") or [] if m and m.get("url")]


def _image_number(url):
    match = _IMAGE_NUMBER_RE.search(url)
    return int(match.group(1)) if match else None


def _image_sort_key(url):
    """Port of the bundle's extractSortNumber: 49-58 first, then 59 and up,
    then 0-48, then files with no number."""
    n = _image_number(url)
    if n is None:
        return 1000
    if n < 49:
        return 200 + n
    if n < 59:
        return n
    return 100 + n


def _file_suffix(url):
    """'.../139051-LED-05_99_1.png' -> '99': what follows the first '_' of the
    file name, without extension and without a trailing '_<n>'."""
    name = url.rsplit("/", 1)[-1]
    name = re.sub(r"^.*?_", "", name, count=1)
    name = re.sub(r"\.[^/.]+$", "", name)
    return re.sub(r"_[0-9]+$", "", name)


def _is_variant_image(url):
    try:
        return float(_file_suffix(url)) > 59
    except ValueError:
        return False


def _original_image(url):
    """Full-size original, safe to ';'-join."""
    return safe_url_string(_CACHE_RE.sub("/", url, count=1)).replace(";", "%3B")


def _selection(variant, codes):
    """The variant's option values, in the PDP's option order."""
    values = {a["code"]: a["value_index"] for a in variant["attributes"] or []}
    return tuple(values.get(code) for code in codes)


def _default_variant(product):
    """The configuration a PDP opens on: the variant whose smart string names
    the gallery's "_1" image (else its first image), else the first variant."""
    variants = product["variants"]
    gallery = _media_urls(product)
    main = next((u for u in gallery if _file_suffix(u) == "1"), gallery[0] if gallery else None)
    smart = None
    if main is not None:
        name = main.rsplit("/", 1)[-1]
        smart = name[: name.index("_")] if "_" in name else ""
    return next((v for v in variants if v["product"].get("hf_smart_string") == smart), variants[0])


def _carousel(product, variant=None, is_default=True):
    urls = []
    if variant is not None and not is_default:
        urls += [u for u in _media_urls(variant) if _is_variant_image(u)]
    urls += _media_urls(product)
    urls.sort(key=_image_sort_key)
    return list(dict.fromkeys(_original_image(u) for u in urls))


def _downloads(product, configured, assets_host):
    """{button label: url} for the DOWNLOADS drawer of one configuration."""
    out = {"Specifications Sheet": ""}  # browser print job, has no URL

    raw = configured.get("hf_installation_instructions")
    try:
        paths = json.loads(raw) if raw else []
    except ValueError:
        logger.warning("[%s] unreadable hf_installation_instructions: %r", configured.get("sku"), raw)
        paths = []
    paths = [p for p in paths or [] if isinstance(p, str)]
    for i, path in enumerate(paths, 1):
        label = "Installation Sheet" if len(paths) == 1 else f"Installation Sheet {i}"
        out[label] = safe_url_string(assets_host + path)

    drawing = next((u for u in _media_urls(product) if _image_number(u) == 5), None)
    if drawing:
        out["Dimensional Drawing"] = _original_image(drawing)
    return out


class HubbardtonForgeSpider(scrapy.Spider):
    name = "hubbardtonForge"
    allowed_domains = ["hubbardtonforge.com"]

    custom_settings = {
        "CONCURRENT_REQUESTS": 8,
        "AUTOTHROTTLE_ENABLED": True,
        "RETRY_TIMES": 6,
        "RETRY_HTTP_CODES": [403, 429, 500, 502, 503, 504, 520, 521, 522, 524, 408],
        "FEED_EXPORT_FIELDS": HUBBARDTONFORGE_FIELDS,
    }

    def __init__(self, products=None, *args, **kwargs):
        """`-a products="shield-large-sconce-201067,..."` scrapes only those
        PDPs (URL slugs) and skips the listing, for testing."""
        super().__init__(*args, **kwargs)
        self.products = [p.strip().strip("/") for p in (products or "").split(",") if p.strip()]
        self._expected_products = None
        self._seen_keys = set()
        self._scraped_keys = set()
        self._seen_sku = {}
        self._unknown_labels = set()

    # Scrapy >= 2.13 drives spiders through `start()`; the Scrapy Cloud stack
    # (scrapy:2.12) only knows `start_requests()`. Define both.
    async def start(self):
        for request in self.start_requests():
            yield request

    def start_requests(self):
        if self.products:
            for key in self.products:
                yield self._product_request(key, LISTING_URL.format(page=1))
            return
        yield self._listing_request(1)

    # -------------------------------------------------------------- listing
    def _listing_request(self, page, last_page=None):
        return Request(
            LISTING_URL.format(page=page),
            callback=self.parse_listing,
            errback=self.on_error,
            meta=dict(_LISTING_META),
            cb_kwargs={"page": page, "last_page": last_page},
        )

    def parse_listing(self, response, page, last_page):
        keys = []
        for href in response.css(f"{_CARD} a::attr(href)").getall():
            key = urlsplit(response.urljoin(href)).path.strip("/")
            if key and key not in keys:
                keys.append(key)

        pager = _clean(" ".join(response.css('[class*="pagination-root"]')[:1].css("::text").getall()))
        total = re.search(r"(\d+) Results", pager)
        pages = re.search(r"\bof (\d+)", pager)
        total = int(total.group(1)) if total else None
        pages = int(pages.group(1)) if pages else last_page

        if total is None or not pages:
            expected = None
        elif page < pages:
            expected = PAGE_SIZE
        else:
            expected = total - PAGE_SIZE * (pages - 1)
        if not keys or expected is None or len(keys) < expected:
            # Rendered before the grid or the pager finished loading.
            retry = get_retry_request(
                response.request, spider=self, reason=f"incomplete listing ({len(keys)}/{expected})"
            )
            if retry:
                yield retry
                return
            logger.error("[%s] listing incomplete after retries: %d of %s products", response.url, len(keys), expected)

        if total is not None:
            self._expected_products = max(total, self._expected_products or 0)
        logger.info("[%s] %d products (page %d of %s, %s total)", response.url, len(keys), page, pages, total)

        for key in keys:
            self._seen_keys.add(key)
            yield self._product_request(key, response.url)

        if page == 1:
            for n in range(2, (pages or 1) + 1):
                yield self._listing_request(n, pages)

    # ------------------------------------------------------------------ PDP
    def _product_request(self, key, cate_url):
        return JsonRequest(
            GRAPHQL_URL,
            data={"query": PRODUCT_QUERY, "variables": {"urlKey": key}},
            callback=self.parse_product_detail,
            errback=self.on_error,
            cb_kwargs={"key": key, "cate_url": cate_url},
        )

    def parse_product_detail(self, response, key, cate_url):
        product = assets_host = problem = None
        try:
            payload = response.json()
            data = payload.get("data") or {}
            assets_host = ((data.get("storeConfig") or {}).get("pimcore_assets_host") or "").rstrip("/")
            found = (data.get("products") or {}).get("items") or []
            product = next((p for p in found if p and p.get("url_key") == key), None)
            if payload.get("errors"):
                problem = f"GraphQL errors: {str(payload['errors'])[:300]}"
            elif product is None:
                problem = "product not found"
            elif not assets_host:
                problem = "no pimcore_assets_host"
        except (ValueError, AttributeError) as exc:
            problem = f"unreadable response: {exc}"
        if problem:
            retry = get_retry_request(response.request, spider=self, reason=problem)
            if retry:
                yield retry
            else:
                logger.error("[%s] %s; giving up", key, problem)
            return

        self._scraped_keys.add(key)
        page = {"url": f"{BASE_URL}/{key}", "title": _clean(product.get("name")), "cate_url": cate_url}

        if product["__typename"] != "ConfigurableProduct":
            # No selectors and no "Configured Item #" on such a page; its only
            # item number is the base one.
            logger.warning("[%s] is a %s; emitting its base item number", key, product["__typename"])
            yield from self._emit(product, product, _carousel(product), assets_host, page)
            return

        variants = [v for v in product.get("variants") or [] if v and v.get("product")]
        if not variants:
            logger.error("[%s] configurable product with no variants", key)
            return
        product["variants"] = variants
        codes = [o["attribute_code"] for o in product["configurable_options"]]
        default = _selection(_default_variant(product), codes)

        shown = {}  # option combination -> sku the page displays for it
        for variant in variants:
            configured = variant["product"]
            combo = _selection(variant, codes)
            if combo in shown:
                logger.warning(
                    "[%s] %s has the same options as %s, which the page shows instead; skipping",
                    key, configured["sku"], shown[combo],
                )
                self.crawler.stats.inc_value("hubbardton/unreachable_variants")
                continue
            shown[combo] = configured["sku"]
            images = _carousel(product, configured, is_default=combo == default)
            yield from self._emit(product, configured, images, assets_host, page)

    def _emit(self, product, configured, images, assets_host, page):
        sku = _clean(configured.get("sku"))
        if not sku:
            logger.error("[%s] variant without a sku; skipping", page["url"])
            return
        first = self._seen_sku.setdefault(sku, page["url"])
        if first != page["url"]:
            logger.warning("[dup] %s on %s already emitted from %s", sku, page["url"], first)
            return
        downloads = _downloads(product, configured, assets_host)
        for label in downloads.keys() - set(HUBBARDTONFORGE_FIELDS) - self._unknown_labels:
            self._unknown_labels.add(label)
            logger.error("download column %r is not in HUBBARDTONFORGE_FIELDS; CSV export will drop it", label)
        yield {"sku": sku, "images": ";".join(images), **downloads, **page}

    # --------------------------------------------------------------- errors
    def on_error(self, failure):
        logger.error("Request failed: %s %s -- %r", failure.request.url, failure.request.cb_kwargs, failure.value)

    def closed(self, reason):
        if self.products:
            return
        logger.info(
            "listing says %s products; found %d, scraped %d, %d SKUs",
            self._expected_products, len(self._seen_keys), len(self._scraped_keys), len(self._seen_sku),
        )
        if self._expected_products != len(self._seen_keys):
            logger.error("listing is incomplete: expected %s, found %d", self._expected_products, len(self._seen_keys))
        missed = sorted(self._seen_keys - self._scraped_keys)
        if missed:
            logger.error("%d PDPs not scraped: %s", len(missed), ", ".join(missed[:50]))
