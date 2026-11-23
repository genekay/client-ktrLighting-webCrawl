#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WAC Lighting Report System - Invoice Detail spider (Dealer logins).

Per-account flow:
    login.aspx  -> POST credentials
    r09.aspx    -> POST the Invoice Detail search (one <= 1-month window at a time)
    parse table -> for each invoice, follow r10.aspx?invoiceno=... and read
                   the "Sales Person" value
    emit        -> one record per line item, with sales_person appended.

Output is identical to the legacy rep spider plus the extra `sales_person`
column. The dealer page differs from the rep page only in that a dealer is a
single customer, so there is no customer-id dropdown to enumerate/chunk.

Auth/anti-block strategy (scales to ~1000 logins):
Each account has its own Scrapy cookie jar (meta['cookiejar']=user), forwarded
to Zyte API via experimental cookies, so the ASP.NET auth cookie persists across
login -> r09 -> r10 and accounts don't cross-contaminate. The login POST uses
followRedirect=False so the auth cookie set on the login 302 is captured. The
auth cookie is a self-contained encrypted token, so Zyte is left to rotate IPs
freely (proxy rotation + ban handling), which also spreads load across accounts.
All hops are plain HTTP (no browser), so each invoice lookup is cheap.
"""

import csv
import io
import os
import pkgutil
import logging
from datetime import datetime, date

from dateutil.relativedelta import relativedelta

import scrapy
from scrapy.http import Request, FormRequest

from datascraper.items import PortalInvoiceWaclighting

logger = logging.getLogger(__name__)

LOGIN_URL = "https://reportsystem.waclighting.com/login.aspx?ReturnUrl=%2f"
SEARCH_URL = "https://reportsystem.waclighting.com/reports/r09.aspx"
DATE_FMT = "%m/%d/%Y"


def _clean(value):
    return value.strip() if value else ""


def month_windows(start, end):
    """Yield (start, end) spans of <= 1 month covering [start, end] inclusive.

    The portal only allows a one-month range per search.
    """
    cur = start
    while cur <= end:
        stop = min(cur + relativedelta(months=1) - relativedelta(days=1), end)
        yield cur, stop
        cur = stop + relativedelta(days=1)


def _sales_person(response):
    """Read 'Sales Person' from the r10 invoice header table, position-independent."""
    hdr = response.xpath(
        "//table[@id='ctl00_CP_iheader']"
        "/tr[td[normalize-space()='Sales Person']][1]"
    )
    if not hdr:
        return ""
    idx = len(hdr.xpath("./td[normalize-space()='Sales Person'][1]/preceding-sibling::td")) + 1
    return _clean(hdr.xpath(f"./following-sibling::tr[1]/td[{idx}]//text()").get())


class PortalInvoiceWaclightingSpider(scrapy.Spider):
    name = "portal-invoice-waclighting"

    custom_settings = {
        # Portal refuses connections under load; keep the aggregate rate low.
        "CONCURRENT_REQUESTS": 8,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 8,
    }

    # Credentials file bundled inside the deploy package (read from the egg).
    ACCOUNTS_RESOURCE = "accounts.csv"

    def __init__(self, accounts=None, from_date=None, to_date=None, limit=None,
                 *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Optional filesystem override for local runs; None -> packaged file.
        self.accounts_path = accounts
        self.limit = int(limit) if limit else None

        today = date.today()
        self.to_date = self._parse_date(to_date, today)
        self.from_date = self._parse_date(
            from_date, self.to_date - relativedelta(months=1) + relativedelta(days=1)
        )
        if self.from_date > self.to_date:
            raise ValueError("from_date must be <= to_date")

        logger.info(
            "Range %s -> %s | accounts=%s",
            self.from_date.strftime(DATE_FMT),
            self.to_date.strftime(DATE_FMT),
            self.accounts_path or f"packaged:{self.ACCOUNTS_RESOURCE}",
        )

    @staticmethod
    def _parse_date(value, default):
        if not value:
            return default
        try:
            return datetime.strptime(value.strip(), DATE_FMT).date()
        except ValueError:
            raise ValueError(f"Invalid date {value!r}; expected mm/dd/yyyy")

    @staticmethod
    def _parse_accounts(fh):
        for row in csv.DictReader(fh):
            user = (row.get("user") or "").strip()
            if user:
                # Portal Customer IDs are 10-digit, zero-padded. Spreadsheets
                # strip leading zeros, so restore them. zfill is idempotent.
                yield user.zfill(10), (row.get("password") or "").strip()

    def _load_accounts(self):
        # Local override: a real file path passed via -a accounts=...
        if self.accounts_path and os.path.isfile(self.accounts_path):
            with open(self.accounts_path, newline="", encoding="utf-8-sig") as fh:
                yield from self._parse_accounts(fh)
            return

        # Default (incl. Scrapy Cloud): read the copy bundled in the deploy egg.
        data = pkgutil.get_data("datascraper", self.ACCOUNTS_RESOURCE)
        if not data:
            raise FileNotFoundError(
                f"{self.ACCOUNTS_RESOURCE} not found on disk or in the package. "
                f"Put it at datascraper/{self.ACCOUNTS_RESOURCE} and redeploy, "
                f"or pass -a accounts=<path> for local runs."
            )
        yield from self._parse_accounts(io.StringIO(data.decode("utf-8-sig")))

    # --- flow -------------------------------------------------------------

    def start_requests(self):
        for i, (user, password) in enumerate(self._load_accounts()):
            if self.limit is not None and i >= self.limit:
                break
            yield Request(
                LOGIN_URL,
                callback=self.do_login,
                dont_filter=True,
                meta={
                    # Isolated Scrapy cookie jar per account (forwarded to Zyte
                    # via experimental cookies). Zyte rotates IPs for antiban.
                    "cookiejar": user,
                    "user": user,
                    "password": password,
                },
            )

    def do_login(self, response):
        user = response.meta["user"]
        # The auth cookie is set on the login 302. Tell Zyte NOT to follow it
        # so the Set-Cookie is captured into the jar; Scrapy then follows the
        # redirect with the cookie in hand.
        return FormRequest.from_response(
            response,
            formdata={
                "userLogin$UserName": user,
                "userLogin$Password": response.meta["password"],
                "userLogin$LoginButton": "Log in",
            },
            callback=self.after_login,
            dont_filter=True,
            meta={**response.meta, "zyte_api_automap": {"followRedirect": False}},
        )

    def after_login(self, response):
        user = response.meta["user"]
        # Auth cookie is now in the jar; load the Invoice Detail search form.
        yield Request(
            SEARCH_URL,
            callback=self.submit_search,
            dont_filter=True,
            meta={"cookiejar": user, "user": user},
        )

    def submit_search(self, response):
        user = response.meta["user"]

        # Fail loud instead of silently yielding nothing.
        if not response.xpath("//input[@name='ctl00$CP$btnSubmit']"):
            if "login" in response.url.lower() or response.xpath("//input[@name='userLogin$UserName']"):
                logger.warning("[%s] not authenticated - login likely failed", user)
            else:
                logger.warning("[%s] r09 search form missing (url=%s, title=%r)",
                               user, response.url,
                               _clean(response.xpath("//title/text()").get()))
            return

        # A dealer sees no customer dropdown. If one is present, this is a Rep
        # login and needs the customer-enumeration flow, not this spider.
        if response.xpath("//select[contains(@id,'customerid')]"):
            logger.warning("[%s] looks like a Rep login (customer dropdown present); "
                           "skipping - use the rep spider for this account", user)
            return

        for start, stop in month_windows(self.from_date, self.to_date):
            yield FormRequest.from_response(
                response,
                formdata={
                    "ctl00$CP$searchType": "0",        # Standard (date range)
                    "ctl00$CP$datefrom": start.strftime(DATE_FMT),
                    "ctl00$CP$dateto": stop.strftime(DATE_FMT),
                    "ctl00$CP$material": "",            # all models
                    "ctl00$CP$listType": "1",          # Detail (line items)
                    "ctl00$CP$exportFile": "screen",
                    "ctl00$CP$btnSubmit": "Submit",
                },
                callback=self.parse_results,
                dont_filter=True,
                meta=response.meta,
            )

    def parse_results(self, response):
        user = response.meta["user"]

        orders = []          # [{"order": {...}, "items": [...], "url": str|None}]
        cur = None
        expect_order = False

        for row in response.xpath("//div[@id='main']//table//tr"):
            head = _clean(row.xpath("./th[1]/text()").get())

            if head == "Invoice Date":
                cur = {"order": None, "items": [], "url": None}
                orders.append(cur)
                expect_order = True
                continue

            if expect_order:
                expect_order = False
                inv_a = row.xpath("./td[2]/a")
                cur["order"] = {
                    "invoice_date": _clean(row.xpath("./td[1]/text()").get()),
                    "invoice": _clean(inv_a.xpath("./text()").get()),
                    "customer_no": _clean(row.xpath("./td[3]//text()").get()),
                    "company": _clean(row.xpath("./td[4]/text()").get()),
                    "city": _clean(row.xpath("./td[5]/text()").get()),
                    "purchase_order_number": _clean(row.xpath("./td[6]/text()").get()),
                    "order_no": _clean(row.xpath("./td[7]//text()").get()),
                }
                href = inv_a.xpath("./@href").get()
                cur["url"] = response.urljoin(href) if href else None
                continue

            if head == "Item":
                continue  # line-item header row

            tds = row.xpath("./td")
            first = _clean(tds[0].xpath("./text()").get()) if tds else ""
            if cur and cur["order"] and len(tds) >= 10 and first.isdigit():
                cur["items"].append({
                    "item": first,
                    "material_no": _clean(tds[1].xpath("./text()").get()),
                    "quantity": _clean(tds[2].xpath("./text()").get()),
                    "gross_value": _clean(tds[3].xpath("./text()").get()),
                    "promo_disc": _clean(tds[4].xpath("./text()").get()),
                    "comm_disc": _clean(tds[5].xpath("./text()").get()),
                    "restock_fee": _clean(tds[6].xpath("./text()").get()),
                    "gross_tariff_surcharge": _clean(tds[7].xpath("./text()").get()),
                    "freight": _clean(tds[8].xpath("./text()").get()),
                    "net_value": _clean(tds[9].xpath("./text()").get()),
                })

        # One r10 lookup per invoice; carry its line items along.
        invoices = [o for o in orders if o["url"] and o["items"]]
        logger.info("[%s] %s invoices with line items in %s..%s",
                    user, len(invoices),
                    self.from_date.strftime(DATE_FMT), self.to_date.strftime(DATE_FMT))
        for o in invoices:
            yield Request(
                o["url"],
                callback=self.parse_invoice,
                errback=self.invoice_failed,
                dont_filter=True,
                meta={
                    "cookiejar": user,
                    "user": user,
                    "order": o["order"],
                    "items": o["items"],
                },
            )

    def parse_invoice(self, response):
        base = {**response.meta["order"], "sales_person": _sales_person(response)}
        for it in response.meta["items"]:
            yield PortalInvoiceWaclighting(**{**base, **it})

    def invoice_failed(self, failure):
        # r10 unreachable after retries: keep the line items, drop sales_person,
        # rather than losing the invoice entirely.
        meta = failure.request.meta
        logger.warning("[%s] r10 failed (%s); emitting items without sales_person",
                       meta.get("user"), failure.value)
        base = {**meta["order"], "sales_person": ""}
        for it in meta["items"]:
            yield PortalInvoiceWaclighting(**{**base, **it})
