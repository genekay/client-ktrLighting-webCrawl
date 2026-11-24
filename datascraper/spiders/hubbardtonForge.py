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
- The DOWNLOADS drawer is not in the markup until it is opened. Its buttons, and
  where each one's URL comes from:
    "Installation Sheet"     hf_installation_instructions, a JSON list of paths
                             on the Pimcore CDN. With more than one entry the
                             page numbers the buttons ("Installation Sheet 1",
                             "... 2"); here they share one column, ';'-joined
                             in button order.
    "Dimensional Drawing"    the gallery image numbered 5.
    "Specifications Sheet"   no URL exists: the button prints a React component
                             in the browser (react-to-print). What it prints is
                             the data below.
  CAD / Revit / Images buttons only render for signed-in trade accounts and
  are not part of the public drawer, so they are not collected.
- `spec-data` is the tabbed block at #specifications, as JSON keyed by tab (see
  `_spec_data`). Tabs and rows vary per product, and some rows (Overall Height,
  Product Weight, ...) per configuration. "Details" always shows; "Lamping" and
  "Features" only when they have content. "Shipping & Facts" is limited to
  Hubbardton's own customer-service logins and is not collected.
  The page reads a variant's values from its `custom_attributes`, which is up to
  17 MB per PDP for all variants. The same values are available as flat fields,
  with select attributes as option ids; those are resolved through one
  customAttributeMetadata request made before the crawl starts.
"""

import json
import logging
import re
from urllib.parse import urlsplit

import scrapy
import scrapy.exceptions
from parsel import Selector
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

# #specifications: variant attributes the Details and Lamping tabs read, with the
# label the page shows for those that are rendered under their own label.
_VARIANT_SPECS = {
    "hf_width": "Width",
    "hf_height": "Height",
    "hf_depth_length": "Depth",
    "hf_diameter": "Diameter",
    "hf_mounting_heighthorizontal": "Mounting Height - Horizontal",
    "hf_mounting_heightvertical": "Mounting Height - Vertical",
    "hf_mounting_direction": "Mounting Direction",
    "hf_chain_included": "Chain Included",
    "hf_directto_joist_caution": None,
    "hf_pipe_length_min": None,
    "hf_pipe_length_max": None,
    "hf_backplate_width": None,
    "hf_backplate_height": None,
    "hf_lamping": None,
    "hf_bulb_type1": None,
    "hf_bulb_watt1": None,
    "hf_bulbincluded": None,
    "hf_socket_qpa1": None,
    "hf_socket_qpa2": None,
    "hf_socket_type_fluorescent": None,
    "hf_bulb_type1_fluorescent": None,
    "hf_voltage_fluorescent": None,
    "hf_socket_qpa1_fluorescent": None,
    "hf_bulbincluded_fluorescent": None,
    "hf_sloped_ceiling": None,
    "hf_socket_type": "Socket Type",
    "hf_led": "LED",
    "hf_delivered_lumens": "Delivered Lumens",
    "hf_cct": "CCT",
    "hf_cri": "CRI",
    "hf_input": "Input",
    "hf_dimming": "Dimming",
    "hf_voltage": "Voltage",
    "hf_location_rating_ul": "Location Rating UL",
    "hf_safety_rating": "Safety Rating",
}

# Base-item values the page reads as flat fields (already display text).
_BULB1_FIELDS = (
    "hf_item_number", "hf_manufacturer", "hf_base", "hf_wattage", "hf_type_or_shape",
    "hf_color_temp", "hf_qty_per_product", "hf_included",
)
_BULB2_FIELDS = (
    "hf_bulb2item_number", "hf_bulb2manufacturer", "hf_bulb2base", "hf_bulb2wattage",
    "hf_bulb2type_or_shape", "hf_bulb2color_temp", "hf_bulb2qty_per_product", "hf_bulb2included",
)
_PARENT_SPECS = _BULB1_FIELDS + _BULB2_FIELDS + (
    "hf_canopy_width", "hf_canopy_height", "hf_canopy_depth", "hf_canopy_backplate_shape",
    "hf_shade_material", "hf_shade_shape", "hf_shade_width", "hf_shade_height", "hf_shade_depth",
    "hf_post_shape", "hf_post_width", "hf_post_depth",
    "hf_lamp_base_shape", "hf_lamp_base_width", "hf_lamp_base_height", "hf_lamp_base_depth",
    "hf_feature_bullet1", "hf_feature_bullet2", "hf_feature_bullet3", "hf_feature_bullet4",
    "hf_feature_bullet5", "hf_prop65",
)
# Base-item attributes listed as bullets on the Features tab.
_FEATURE_CODES = {
    "hf_option1", "hf_lamping", "hf_adacompliant", "hf_sloped_ceiling",
    "hf_dark_sky_friendly", "hf_patent", "hf_turtle_friendly",
}

METADATA_QUERY = "query hubbardtonMetadata { customAttributeMetadata(attributes: [%s]) { items { attribute_code attribute_options { label value } } } }" % ", ".join(
    '{ attribute_code: "%s", entity_type: "catalog_product" }' % code for code in _VARIANT_SPECS
)

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
      short_description { html }
      %s
      custom_attributes {
        attribute_metadata { code label }
        entered_attribute_value { value }
        selected_attribute_options { attribute_option { label } }
      }
      ... on ConfigurableProduct {
        configurable_options { attribute_code }
        variants {
          attributes { code value_index }
          product {
            sku
            weight
            hf_smart_string
            hf_installation_instructions
            media_gallery { url }
            %s
          }
        }
      }
    }
  }
}
""" % (" ".join(_PARENT_SPECS), " ".join(_VARIANT_SPECS))

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
    """{column: url(s)} for the DOWNLOADS drawer of one configuration."""
    raw = configured.get("hf_installation_instructions")
    try:
        paths = json.loads(raw) if raw else []
    except ValueError:
        logger.warning("[%s] unreadable hf_installation_instructions: %r", configured.get("sku"), raw)
        paths = []
    sheets = [safe_url_string(assets_host + p).replace(";", "%3B") for p in paths or [] if isinstance(p, str)]
    drawing = next((u for u in _media_urls(product) if _image_number(u) == 5), None)
    return {
        "Installation Sheet": ";".join(dict.fromkeys(sheets)),
        "Dimensional Drawing": _original_image(drawing) if drawing else "",
    }


# ------------------------------------------------------------ spec-data
_FLOAT_RE = re.compile(r"\s*[-+]?(?:\d+\.?\d*|\.\d+)")


def _number(value):
    """JS parseFloat, as display text ('58.100000' -> '58.1'); '' if not a number."""
    match = _FLOAT_RE.match(str(value if value is not None else ""))
    return f"{float(match.group(0)):.6f}".rstrip("0").rstrip(".") if match else ""


def _int(value):
    match = re.match(r"\s*[-+]?\d+", value or "")
    return int(match.group(0)) if match else 0


def _dims(product, *fields):
    """('hf_x_width', 'W'), ... -> '44.5”W x 1.7”H x 8.5”D'."""
    return " x ".join(f"{product[f]}”{letter}" for f, letter in fields if product.get(f))


def _html_text(html):
    return _clean(" ".join(Selector(text=html).xpath("//text()").getall())) if _clean(html) else ""


def _parent_attributes(product):
    """Base item custom_attributes in page order: code -> (label, entered, [option labels])."""
    out = {}
    for attr in product.get("custom_attributes") or []:
        meta = attr.get("attribute_metadata") or {}
        entered = (attr.get("entered_attribute_value") or {}).get("value")
        chosen = (attr.get("selected_attribute_options") or {}).get("attribute_option") or []
        out.setdefault(meta.get("code"), (_clean(meta.get("label")), entered, [_clean(o["label"]) for o in chosen]))
    return out


def _details_tab(product, parent, spec, weight):
    rows = []
    if spec("hf_directto_joist_caution") == "Yes":
        rows.append((
            "Caution!",
            "The weight of this fixture requires direct-to-joist, beam or overlay attachment "
            "independent of the electrical joist box.",
        ))

    def dimension(code, unit='"'):
        number = _number(spec(code))
        rows.append((_VARIANT_SPECS[code], number + unit if number else ""))

    if spec("hf_diameter"):
        dimension("hf_height")
        dimension("hf_diameter")
    else:
        dimension("hf_width")
        dimension("hf_height")
        dimension("hf_depth_length")
    if weight:
        rows.append(("Product Weight", f"{_number(weight)} lbs"))
    low, high = spec("hf_pipe_length_min"), spec("hf_pipe_length_max")
    if low and high:
        rows.append(("Overall Height", f'{low}" to {high}"'))
    tall, wide = spec("hf_backplate_height"), spec("hf_backplate_width")
    if tall and wide:
        rows.append(("Backplate", f'{tall}" x {wide}"'))
    dimension("hf_mounting_heighthorizontal")
    dimension("hf_chain_included", "'")
    for code in ("hf_mounting_heightvertical", "hf_mounting_direction"):
        rows.append((_VARIANT_SPECS[code], spec(code)))

    def base(code):
        return next(iter(parent.get(code, ("", None, []))[2]), "")

    rows += [
        ("Canopy", _dims(product, ("hf_canopy_width", "W"), ("hf_canopy_height", "H"), ("hf_canopy_depth", "D"))),
        ("Canopy / Backplate Shape", product.get("hf_canopy_backplate_shape")),
        ("Glass/Shade QPA", base("hf_glass_shade_qpa")),
        ("Shade Material", product.get("hf_shade_material")),
        ("Shade Shape", product.get("hf_shade_shape")),
        ("Shade", _dims(product, ("hf_shade_width", "W"), ("hf_shade_height", "H"), ("hf_shade_depth", "D"))),
        ("Post Shape", product.get("hf_post_shape")),
        ("Post", _dims(product, ("hf_post_width", "W"), ("hf_post_depth", "D"))),
        ("Lamp Base Shape", product.get("hf_lamp_base_shape")),
        ("Lamp Base", _dims(product, ("hf_lamp_base_width", "W"), ("hf_lamp_base_height", "H"), ("hf_lamp_base_depth", "D"))),
        ("Fixture Material", base("hf_fixture_material")),
        ("Number of Arms", base("hf_numberof_arms")),
        ("Number of Tiers", base("hf_numberof_tiers")),
    ]
    return rows


def _lamping_tab(spec):
    rows = []

    def attribute(code, suffix=""):
        value = spec(code)
        rows.append((_VARIANT_SPECS[code], value + suffix if value else ""))

    lamping = spec("hf_lamping")
    if "FLU" in lamping:
        rows.append(("Socket", spec("hf_socket_type_fluorescent")))
        rows.append(("Bulb", spec("hf_bulb_type1_fluorescent")))
        rows.append(("Voltage", spec("hf_voltage_fluorescent")))
        bulbs = spec("hf_socket_qpa1_fluorescent")
        if bulbs:
            # The page tests that the attribute has a label at all, so "No"
            # also reads as included.
            included = "included" if spec("hf_bulbincluded_fluorescent") else "not included"
            rows.append(("Number of Bulbs", f"{bulbs} ({included})"))
    else:
        bulb, watt = spec("hf_bulb_type1"), spec("hf_bulb_watt1")
        if lamping == "LED" and "GU24" in bulb:
            rows.append(("Lamping", "Retrofit"))
        elif spec("hf_led"):
            rows.append(("Lamping", "Dedicated"))
        attribute("hf_socket_type")
        rows.append(("Bulb", ", ".join(x for x in (bulb, f"{watt} Max" if watt else "") if x)))
        count = _int(spec("hf_socket_qpa1")) + _int(spec("hf_socket_qpa2"))
        if count > 0:
            included = "included" if spec("hf_bulbincluded") == "Yes" else "not included"
            rows.append(("Number of Bulbs", f"{count} ({included})"))
        for code in ("hf_led", "hf_delivered_lumens", "hf_cct", "hf_cri", "hf_input", "hf_dimming"):
            attribute(code)
        attribute("hf_voltage", "V")
    attribute("hf_location_rating_ul")
    attribute("hf_safety_rating", " listed")
    return rows


def _recommended_bulb(product, parent, fields):
    """Rows follow the base item's attribute order, labels lose their 'Bulb 2'."""
    return [
        (_clean(re.sub(r"Bulb\s*\d+", "", label)), product.get(code))
        for code, (label, _, _) in parent.items()
        if code in fields
    ]


def _features_tab(product, parent):
    configurable = {o["attribute_code"] for o in product.get("configurable_options") or []}
    rows = []
    for code, (label, entered, options) in parent.items():
        if code not in _FEATURE_CODES or code in configurable:
            continue
        option = options[0] if options else ""
        if (
            (code == "hf_lamping" and "None" in option)
            or (code in ("hf_adacompliant", "hf_dark_sky_friendly") and option.lower() == "no")
            or (code == "hf_sloped_ceiling" and option.lower() in ("no", "no (0)"))
            or (code == "hf_patent" and (entered or "").lower() == "no")
        ):
            continue
        # Patent has no options: the page prints only the bare label, but its
        # data carries the patent number (or "Pending"), so keep that.
        rows.append((label, option or _clean(entered) or "Yes"))

    description = _html_text((product.get("short_description") or {}).get("html"))
    if not rows and not description:
        return []  # the page leaves the tab out
    rows.insert(0, ("Description", description))
    rows.append(("Notes", [n for n in (_clean(product.get(f"hf_feature_bullet{i}")) for i in range(1, 6)) if n]))
    if product.get("hf_prop65"):
        rows.append(("California Residents", "Prop 65 regulations"))
    return rows


def _spec_data(product, configured, options):
    """{tab: {label: value}} as the #specifications tabs show it, in page order.

    `options` is {attribute code: {option id: label}}. Rows the page renders
    without a value are left out. The Lamping tab's bulb blocks get their own
    key because they repeat labels ("Wattage", "Base", ...).
    """
    parent = _parent_attributes(product)

    def spec(code):
        raw = configured.get(code)
        if raw is None or raw == "":
            return ""
        labels = options.get(code)
        if not labels:
            return _clean(str(raw))
        ids = str(raw).split(",")
        for option_id in ids:
            if option_id not in labels:
                logger.error("[%s] %s: unknown option id %s", configured.get("sku"), code, option_id)
        # Multi-selects are listed in the attribute's option order.
        return ", ".join(label for option_id, label in labels.items() if option_id in ids)

    lamping = _lamping_tab(spec)
    # A row that is never displayed (Sloped Ceiling) still makes the tab show.
    has_lamping = any(v for _, v in lamping) or spec("hf_sloped_ceiling")
    tabs = [("Details", _details_tab(product, parent, spec, configured.get("weight")))]
    if has_lamping:
        tabs += [
            ("Lamping", lamping),
            ("Recommended Bulb 1", _recommended_bulb(product, parent, _BULB1_FIELDS)),
            ("Recommended Bulb 2", _recommended_bulb(product, parent, _BULB2_FIELDS)),
        ]
    tabs.append(("Features", _features_tab(product, parent)))

    out = {}
    for tab, rows in tabs:
        rows = {
            label: value if isinstance(value, list) else _clean(str(value)) for label, value in rows if value
        }
        if rows:
            out[tab] = rows
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
        self._options = {}

    # Scrapy >= 2.13 drives spiders through `start()`; the Scrapy Cloud stack
    # (scrapy:2.12) only knows `start_requests()`. Define both.
    async def start(self):
        for request in self.start_requests():
            yield request

    def start_requests(self):
        yield JsonRequest(
            GRAPHQL_URL, data={"query": METADATA_QUERY}, callback=self.parse_metadata, errback=self.on_error
        )

    def parse_metadata(self, response):
        """Option labels for `spec-data`; the crawl starts once they are in."""
        try:
            items = response.json()["data"]["customAttributeMetadata"]["items"]
            self._options = {
                item["attribute_code"]: {o["value"]: _clean(o["label"]) for o in item["attribute_options"]}
                for item in items
                if item and item.get("attribute_options")
            }
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("attribute metadata unreadable: %r", exc)
        if not self._options:
            retry = get_retry_request(response.request, spider=self, reason="no attribute metadata")
            if retry:
                yield retry
            else:
                raise scrapy.exceptions.CloseSpider("no attribute metadata")
            return

        if self.products:
            for key in self.products:
                yield self._product_request(key, LISTING_URL.format(page=1))
        else:
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
        yield {
            "sku": sku,
            "images": ";".join(images),
            "spec-data": json.dumps(_spec_data(product, configured, self._options), ensure_ascii=False),
            **_downloads(product, configured, assets_host),
            **page,
        }

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
