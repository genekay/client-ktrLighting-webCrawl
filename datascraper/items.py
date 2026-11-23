# -*- coding: utf-8 -*-
import scrapy


class PortalInvoiceWaclighting(scrapy.Item):
    # Order-level fields (from r09 results table)
    invoice_date = scrapy.Field()
    invoice = scrapy.Field()
    customer_no = scrapy.Field()
    company = scrapy.Field()
    city = scrapy.Field()
    purchase_order_number = scrapy.Field()
    order_no = scrapy.Field()
    # Line-item fields (from r09 results table)
    item = scrapy.Field()
    material_no = scrapy.Field()
    quantity = scrapy.Field()
    gross_value = scrapy.Field()
    promo_disc = scrapy.Field()
    comm_disc = scrapy.Field()
    restock_fee = scrapy.Field()
    gross_tariff_surcharge = scrapy.Field()
    freight = scrapy.Field()
    net_value = scrapy.Field()
    # Added: scraped from the singular invoice page (r10)
    sales_person = scrapy.Field()

class ModernFormsItem(scrapy.Item):
    url = scrapy.Field()
    cate_url = scrapy.Field()
    images = scrapy.Field()
    specsheet = scrapy.Field()
    installationsheet = scrapy.Field()
    sku = scrapy.Field()


# Declaration order is the CSV column order. Single-sourced for FEEDS.
INVOICE_FIELDS = list(PortalInvoiceWaclighting.fields)

MODERNFORMS_FIELDS = list(ModernFormsItem.fields)


# ELCO Lighting: one row per "Product SKU" table row. The table's column set
# differs per product (10 cols on a pendant, 4 on accessories, 2 on a j-box),
# so the row is keyed by the table's own headers, normalized to snake_case
# ("Catalog Number" -> catalog_number, "Lamp type" -> lamp_type, ...). The
# known headers get their own column; anything new lands in `extra` as JSON
# so the CSV shape stays fixed and a new column is still visible.
ELCO_FIELDS = [
    "catalog_number",  # primary key
    "description",
    "lamp_type",
    "wattage",
    "cct",
    "lumens",
    "voltage",
    "dimming",
    "finish",
    "beam_angle",
    "spec",            # absolute spec-sheet download URL from the "Spec" column
    "images",          # all PDP images, left-to-right/top-to-bottom, ';'-joined
    "available-finishes",  # Product Number Builder finish options, "CODE: Name", ';'-joined
    "url",
    "title",
    "cate_url",
    "extra",
]


# Adesso Home: one row per product card listed under the FLOOR / TABLE / WALL /
# CEILING LAMPS, Candle Warming Lamps and SMART TECH nav categories.
ADESSO_FIELDS = [
    "sku",     # PDP "Item ID:" value, stripped; primary key
    "images",  # PDP image carousel, main image then thumbs top-to-bottom, ';'-joined
    "specs",   # PDP spec block (Brand, UPC, ... Closeout) as a Python-literal list of {label: value} dicts, page order
    "title",
    "url",
    "cate_url",
]


# Lumecon: one row per PDP reached by drilling down from /categories/. The site
# shows no SKU field, so the PDP title is the key (some titles lead with a
# catalog code, e.g. "LM-GRL The Grand Lantern", some don't).
LUMECON_FIELDS = [
    "title",      # PDP <h1 class="product_title">; primary key
    "images",     # WooCommerce gallery full-size URLs in carousel order, ';'-joined
    "specsheet",  # DOWNLOADS accordion spec-sheet URL(s), ';'-joined
    "url",
    "cate_url",
]
