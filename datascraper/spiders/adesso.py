#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Adesso Home product spider (https://adessohome.com/).

Seed is the homepage SHOP mega-menu. Only these top-level nav entries are
followed, matched by their label so a changed pc_id / slug doesn't break it:

    FLOOR LAMPS, TABLE LAMPS, WALL LAMPS, CEILING LAMPS,
    Candle Warming Lamps, SMART TECH

The storefront is CIMcloud (classic ASP + Knockout). Pages are rendered
client-side from JSON the server embeds in the HTML, so no browserHtml is
needed -- the data is read straight out of those script blobs:

- Category page: `var productResults = {"products": [...], "count": N}`,
  24 per page, `&page=N` is honored server-side. Page 1 schedules the rest.
- PDP: `var product = {...}`. The "Item ID:" label is `sku`, and the image
  gallery (template `catalog.image_gallery`) is the main image
  (`largePic`, else `pic`) followed by the `multiPic` thumbs in order, each
  thumb linking to `link || image`. That is the carousel top-to-bottom.

Variants ("child" products, ~440 of the ~815 cards) link to their parent's
PDP with `&childskumatch=<SKU>`. That PDP's embedded `product` is the
*parent* (e.g. sku "10036312-P", children: []); the browser fetches the
children afterwards and shows the matched child's Item ID and images. The
child record in the category JSON is that same data (verified against a
headless-Chrome render: identical Item ID and image list), so for variants
the listing record is used, with the template's `$parent` fallbacks applied
from the PDP's parent record.

Image URLs follow `utils.buildImagePath`: relative paths are lower-cased and
put on the CloudFront CDN, the path is URL-encoded, and the `?v=` cache
buster is dropped.

Spec key/values (the `Brand: / UPC: / ... / Closeout:` block) are rendered
client-side by the Knockout template `catalog.optional_fields`, from 35
generic `pof_opt1..35` product fields. Which rows show, their order, their
labels and the combined rows ("Assembled Item Dims" = W x D x H, carton
dims, ...) all live in that template plus `oConfig.showOptN /
optNLabel` set in the same page. Nothing is hard-coded here: the template is
read from each PDP and evaluated the way Knockout would (`ko if` blocks via
the page's `shouldShowOptField` rule, `html:` bindings filled in), and the
resulting <dt>/<dd> pairs become `specs`. A binding or condition this
evaluator doesn't understand is logged, so a template change is visible.
"""

import ast
import html
import json
import logging
import math
import re
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import scrapy
from scrapy.http import Request
from scrapy.selector import Selector
from w3lib.url import add_or_replace_parameter

from datascraper.items import ADESSO_FIELDS

logger = logging.getLogger(__name__)

START_URL = "https://adessohome.com/"
NAV_CATEGORIES = (
    "FLOOR LAMPS",
    "TABLE LAMPS",
    "WALL LAMPS",
    "CEILING LAMPS",
    "CANDLE WARMING LAMPS",
    "SMART TECH",
)

# Fallback when the page's oConfig doesn't expose cdnUrl.
DEFAULT_CDN = "https://ds241vqkd0zfq.cloudfront.net"
_CDN_RE = re.compile(r'cdnUrl\s*:\s*"([^"]+)"')
_RPP_RE = re.compile(r"searchConfig\.rpp\s*=\s*(\d+)")

# Listing-record keys carried to the PDP callback (the full record is ~10 KB),
# plus every `pof_opt*` field the spec template may read.
_LISTED_KEYS = ("sku", "name", "largePic", "pic", "multiPic")


def _clean(value):
    return re.sub(r"\s+", " ", value).strip() if value else ""


def _js_object(text, marker):
    """Decode the JSON literal that follows `marker` in a <script>, or None."""
    start = text.find(marker)
    if start < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text, start + len(marker))
    except ValueError:
        return None
    return obj


def _image_url(path, cdn):
    """Port of the storefront's utils.buildImagePath, minus the cache buster."""
    path = (path or "").strip()
    if not path:
        return ""
    if "http" in path:
        url = re.sub(r"^http:", "https:", path, flags=re.I)
    elif "/" in path:
        url = cdn.rstrip("/") + "/" + path.lower().lstrip("/")
    else:
        url = cdn.rstrip("/") + "/images/" + path.lower().lstrip("/")
    parts = urlsplit(url)
    # Same character set encodeURIComponent leaves alone, '/' kept.
    encoded = quote(unquote(html.unescape(parts.path)), safe="/-_.!~*'()")
    return urlunsplit((parts.scheme, parts.netloc, encoded, "", ""))


def _carousel(shown, parent, cdn):
    """Gallery order from catalog.image_gallery: main image, then multiPic.
    `parent` supplies the template's `$parent.*` fallbacks on variant PDPs."""
    main = shown.get("largePic") or parent.get("largePic") or shown.get("pic") or parent.get("pic")
    thumbs = shown.get("multiPic") or parent.get("multiPic") or []
    paths = [main] + [t.get("link") or t.get("image") for t in thumbs if t.get("image")]
    # A few thumbs are a bare folder ("images/adessohome/products/images/"),
    # which renders as a broken image; there is no file to link to.
    urls = (_image_url(p, cdn) for p in paths if p and not p.rstrip().endswith("/"))
    return list(dict.fromkeys(u for u in urls if u))


# ---------------------------------------------------------------- specs
_SPEC_TEMPLATE = "//script[@type='text/html'][@id='catalog.optional_fields']/text()"
_SHOW_OPT_RE = re.compile(r"oConfig\.showOpt(\d+)\s*=\s*(true|false)\s*;")
_OPT_LABEL_RE = re.compile(r'oConfig\.opt(\d+)Label\s*=\s*("(?:[^"\\]|\\.)*")\s*;')
_KO_TOKEN_RE = re.compile(r"<!--\s*ko\s+if:\s*(.*?)\s*-->|<!--\s*/ko\s*-->|<!--.*?-->", re.S)
_SHOW_CALL_RE = re.compile(r"shouldShowOptField\(\s*(\d+)\s*,\s*selectedProduct\(\)\s*,\s*oConfig\s*\)")
# <tag data-bind="html: oConfig.optNLabel | selectedProduct().pof_optN"></tag>
_HTML_BIND_RE = re.compile(
    r'<(\w+)([^>]*?)\s+data-bind="\s*html:\s*'
    r'(?:oConfig\.opt(\d+)Label|selectedProduct\(\)\.pof_opt(\d+))\s*"([^>]*)>\s*</\1>'
)


def _spec_config(text):
    """`oConfig.showOptN` flags and `oConfig.optNLabel` labels set on the page."""
    show = {int(n): v == "true" for n, v in _SHOW_OPT_RE.findall(text)}
    labels = {int(n): json.loads(v) for n, v in _OPT_LABEL_RE.findall(text)}
    return show, labels


def _should_show(n, product, show):
    """Python port of the page's shouldShowOptField(optNum, product, oConfig)."""
    if not show.get(n):
        return False
    value = product.get(f"pof_opt{n}")
    return bool(value) and str(value) != "0"


def _eval_condition(expr, product, show):
    """Evaluate a `ko if:` expression built from shouldShowOptField calls,
    `!`, `&&`, `||` and parentheses. Returns None if it uses anything else."""
    py = _SHOW_CALL_RE.sub(r"S(\1)", expr).replace("&&", " and ").replace("||", " or ")
    py = re.sub(r"!(?!=)", " not ", py).strip()
    try:
        tree = ast.parse(py, mode="eval")
    except SyntaxError:
        return None

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.BoolOp):
            values = [walk(v) for v in node.values]
            if None in values:
                return None
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            inner = walk(node.operand)
            return None if inner is None else not inner
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "S"
                and len(node.args) == 1 and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, int)):
            return _should_show(node.args[0].value, product, show)
        return None

    return walk(tree)


def _render_template(template, product, show, labels, url):
    """Render a Knockout template the way the page would: drop `ko if` blocks
    whose condition is false, fill `html:` bindings. Returns HTML."""
    out = [[]]          # stack of output buffers, one per open `ko if`
    keep = [True]       # whether each open block renders
    pos = 0
    for m in _KO_TOKEN_RE.finditer(template):
        out[-1].append(template[pos:m.start()])
        pos = m.end()
        token = m.group(0)
        if m.group(1) is not None:              # <!-- ko if: ... -->
            cond = _eval_condition(m.group(1), product, show)
            if cond is None:
                logger.warning("[%s] spec template: unsupported condition %r; block dropped", url, m.group(1))
            out.append([])
            keep.append(bool(cond))
        elif re.match(r"<!--\s*/ko", token):    # <!-- /ko -->
            if len(out) == 1:
                logger.warning("[%s] spec template: unbalanced <!-- /ko -->", url)
                continue
            block, shown = "".join(out.pop()), keep.pop()
            if shown:
                out[-1].append(block)
        # plain comments are dropped
    out[-1].append(template[pos:])
    if len(out) != 1:
        logger.warning("[%s] spec template: %d unclosed ko blocks", url, len(out) - 1)
    rendered = "".join("".join(buf) for buf in out)

    def fill(m):
        tag, label_n, field_n = m.group(1), m.group(3), m.group(4)
        value = labels.get(int(label_n), "") if label_n else product.get(f"pof_opt{field_n}") or ""
        return f"<{tag}>{value}</{tag}>"

    rendered = _HTML_BIND_RE.sub(fill, rendered)
    if "data-bind" in rendered:
        logger.warning("[%s] spec template: unhandled data-bind left in output", url)
    return rendered


def _specs(response, product):
    """The spec block as [{'Brand': 'Adesso'}, {'UPC': '...'}, ...] in page order."""
    template = response.xpath(_SPEC_TEMPLATE).get()
    if not template:
        logger.warning("[%s] no catalog.optional_fields template on PDP", response.url)
        return []
    show, labels = _spec_config(response.text)
    if not show:
        logger.warning("[%s] no oConfig.showOptN flags on PDP; spec block will be empty", response.url)
    rendered = Selector(text=f"<dl>{_render_template(template, product, show, labels, response.url)}</dl>")
    specs = []
    for dt in rendered.xpath("//dl/dt"):
        # string(.) = textContent, so "<span>15.75</span>W" stays "15.75W".
        key = _clean(dt.xpath("string(.)").get()).rstrip(":").strip()
        dd = dt.xpath("following-sibling::*[1][self::dd]")
        value = _clean(dd.xpath("string(.)").get()) if dd else ""
        if key:
            specs.append({key: value})
    return specs


class AdessoSpider(scrapy.Spider):
    name = "web-addeso"
    allowed_domains = ["adessohome.com"]

    custom_settings = {
        "CONCURRENT_REQUESTS": 8,
        "AUTOTHROTTLE_ENABLED": True,
        "RETRY_TIMES": 6,
        "RETRY_HTTP_CODES": [403, 429, 500, 502, 503, 504, 520, 521, 522, 524, 408],
        "FEED_EXPORT_FIELDS": ADESSO_FIELDS,
    }

    def __init__(self, categories=None, *args, **kwargs):
        """`-a categories="WALL LAMPS,SMART TECH"` narrows the crawl for
        testing; default is all six nav categories."""
        super().__init__(*args, **kwargs)
        wanted = categories.split(",") if categories else NAV_CATEGORIES
        self.categories = {_clean(c).upper() for c in wanted}
        self._seen_sku = {}

    # Scrapy >= 2.13 drives spiders through `start()`; the Scrapy Cloud stack
    # (scrapy:2.12) only knows `start_requests()`. Define both.
    async def start(self):
        for request in self.start_requests():
            yield request

    def start_requests(self):
        yield Request(START_URL, callback=self.parse_nav, errback=self.on_error)

    # ------------------------------------------------------------------ nav
    def parse_nav(self, response):
        found = set()
        for a in response.css("a.retail-main-nav__a-2"):
            label = _clean(" ".join(a.css("span.cat-nm ::text").getall())).upper()
            href = a.attrib.get("href")
            if label not in self.categories or not href or label in found:
                continue
            found.add(label)
            yield Request(
                response.urljoin(href),
                callback=self.parse_listing,
                errback=self.on_error,
                meta={"category": label},
            )
        for label in sorted(self.categories - found):
            logger.error("[nav] category %r not found in the SHOP menu on %s", label, response.url)

    # -------------------------------------------------------------- listing
    def parse_listing(self, response):
        results = _js_object(response.text, "var productResults = ")
        if results is None:
            logger.error("[%s] no productResults JSON on listing page", response.url)
            return
        products = [p for p in results.get("products") or [] if p]

        if response.meta.get("page", 1) == 1:
            count = int(results.get("count") or 0)
            rpp_match = _RPP_RE.search(response.text)
            rpp = int(rpp_match.group(1)) if rpp_match else len(products)
            pages = math.ceil(count / rpp) if rpp else 1
            logger.info("[%s] %s: %d products over %d pages", response.url, response.meta["category"], count, pages)
            for page in range(2, pages + 1):
                yield Request(
                    add_or_replace_parameter(response.url, "page", str(page)),
                    callback=self.parse_listing,
                    errback=self.on_error,
                    meta={"category": response.meta["category"], "page": page},
                )

        if not products:
            logger.warning("[%s] listing page has no products", response.url)
        for product in products:
            link = product.get("link")
            if not link:
                logger.warning("[%s] product %s has no link; skipping", response.url, product.get("sku"))
                continue
            yield Request(
                response.urljoin(link),
                callback=self.parse_product_detail,
                errback=self.on_error,
                meta={
                    "cate_url": response.url,
                    "listed": {k: v for k, v in product.items() if k in _LISTED_KEYS or k.startswith("pof_opt")},
                },
            )

    # ------------------------------------------------------------------ PDP
    def parse_product_detail(self, response):
        listed = response.meta["listed"]
        page = _js_object(response.text, "var product = ")
        cdn_match = _CDN_RE.search(response.text)
        cdn = cdn_match.group(1) if cdn_match else DEFAULT_CDN

        if page is None:
            logger.warning("[%s] no product JSON on PDP; using listing record for %s", response.url, listed.get("sku"))
            shown, parent = listed, {}
        elif _clean(page.get("sku")) == _clean(listed.get("sku")):
            shown, parent = page, {}
        else:
            # Variant: PDP embeds the parent, the browser swaps in the child.
            shown, parent = listed, page

        sku = _clean(shown.get("sku"))
        if not sku:
            logger.warning("[%s] empty Item ID; skipping", response.url)
            return
        first = self._seen_sku.setdefault(sku, response.url)
        if first != response.url:
            logger.warning("[dup] sku %s on %s already emitted from %s", sku, response.url, first)
            return

        images = _carousel(shown, parent, cdn)
        if not images:
            logger.warning("[%s] no images for %s", response.url, sku)

        yield {
            "sku": sku,
            "images": ";".join(images),
            # Python-literal string, e.g. "[{'Brand': 'Adesso'}, {'UPC': '798919156313'}]";
            # ast.literal_eval() turns it back into the list of dicts.
            "specs": repr(_specs(response, shown)),
            "title": _clean(html.unescape(shown.get("name") or "")),
            "url": response.url,
            "cate_url": response.meta.get("cate_url", ""),
        }

    def on_error(self, failure):
        logger.warning("[%s] request failed: %s", failure.request.url, failure.value)
