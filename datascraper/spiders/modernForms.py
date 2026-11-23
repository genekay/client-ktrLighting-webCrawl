#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Modern Forms product-detail spider (https://modernforms.com/).

Two seed sources, both landing on the same parse_product_detail:
    PRODUCT_URLS   -> products not reachable from any category listing
    CATEGORY_URLS  -> product-category pages, each paginated past its first
                       12 items via the site's `lazyUnfiltered` AJAX action

Modern Forms is a sibling WAC Group brand site (same platform as
schonbek.com: identical PPID/FILTERED_INDEX image layout, the same
Spec Sheet/Instructions/IES File/Dimming Report download set, same
"Cat ID" debug markup). Category and product pages are server-rendered
(no browserHtml needed here, unlike Schonbek's listing grids), but the
site sits behind the same bot-detection that fingerprints a plain
Scrapy/Twisted downloader.

Fix history:
- Previous version hand-spoofed a single browser session (hardcoded
  Chrome UA + copy-pasted sec-ch-ua/newrelic/traceparent headers) only
  on the `lazyUnfiltered` AJAX request, with no errback anywhere and a
  bare `except Exception: log and drop` around the JSON parse. When
  that hand-rolled fingerprint got blocked -- which happens
  intermittently, not on every request -- the category's remaining
  pages were silently dropped with nothing but a log line, and no
  retry ever ran (default RETRY_TIMES=2, and 403 isn't even in
  Scrapy's default RETRY_HTTP_CODES). That is what produced a
  different item count on every run: which requests got blocked was
  non-deterministic, not the scraping logic itself.
  Fixed by dropping the manual header spoofing entirely and using
  scrapy-zyte-api transparent mode (already the project default, see
  settings.py) for every request including the AJAX call, plus an
  errback on every request so failures are logged instead of silently
  swallowed. ZYTE_API_RETRY_POLICY (datascraper/retry.py) already
  retries the transient statuses (421/429/500/503/520/521) for free.
- `//idunfiltered/...` and `//catid/...` selected literal `<idunfiltered>`
  and `<catid>` *tag names*, which don't exist; the real markup exposes
  these as element `id` attributes (confirmed live: the category page
  renders a "Cat ID: 7500" block). Fixed to `//*[@id='idunfiltered']`
  and `//*[@id='catid']`, so pagination past the first 12 items now
  actually fires instead of silently no-opping.
- Spec Sheet / Instructions links were matched with an exact
  `text()='...'` xpath, including a magic leading space, so any
  whitespace change in the markup broke it. Matching on the href
  instead (the fix web_schonbek_spd.py uses for its download links)
  turned out not to work here either: fan pages link "Spec Sheet"
  straight to a static PDF under `SPECSHEET_PDF/`, while luminaire
  pages route the same label through `/dynamic-specsheet/?...` --
  two different href shapes for the one download. The one thing
  that's stable across both templates is the visible link text, so
  both fields now match on `normalize-space()` of the anchor's text
  instead of its href.
- Removed everything left over from the pre-refactor repo that no
  longer exists here: `datascraper.middlewares`, `datascraper.pipelines`,
  `datascraper.etuannv_utils` (see README: these were all removed).
  Also dropped the ItemLoader indirection, the dead `ISLOCAL`
  hostname check (permanently False either way), and the stray
  `from curses import meta` import, none of which did anything.
"""

import json
import logging
import math
import re

import scrapy
from scrapy.http import Request

from datascraper.items import ModernFormsItem

logger = logging.getLogger(__name__)

AJAX_URL = "https://www.modernforms.com/wp-admin/admin-ajax.php?action=lazyUnfiltered"
PAGE_SIZE = 12


def _clean(value):
    return value.strip() if value else ""


def _to_int(value):
    try:
        return int(_clean(value))
    except (TypeError, ValueError):
        return None


def _download_href(response, label):
    """Downloads-section link URL, matched by visible text (not href shape --
    fan and luminaire product templates put the same label on two different
    href patterns for e.g. Spec Sheet)."""
    href = response.xpath(f"//a[contains(normalize-space(.), '{label}')]/@href").get()
    return response.urljoin(href) if href else None


class ModernFormsSpider(scrapy.Spider):
    name = "web-modernforms"

    custom_settings = {
        # Same posture as web-schonbek-*: be gentle with a bot-sensitive
        # WAC Group site rather than hammering it at the project default.
        "CONCURRENT_REQUESTS": 8,
        "AUTOTHROTTLE_ENABLED": True,
        "RETRY_TIMES": 6,
        "RETRY_HTTP_CODES": [403, 429, 500, 502, 503, 504, 520, 521, 522, 524, 408],
    }

    # Products that don't hang off any product-category listing.
    PRODUCT_URLS = [
        'https://modernforms.com/product/wynd-60/',
        'https://modernforms.com/product/renegade-66/',
        'https://modernforms.com/product/renegade-52/',
        'https://modernforms.com/product/slim/',
        'https://modernforms.com/product/zephyr-52/',
        'https://modernforms.com/product/zephyr-62/',
        'https://modernforms.com/product/woody-60/',
        'https://modernforms.com/product/woody-72/',
        'https://modernforms.com/product/wynd-52/',
        'https://modernforms.com/product/slim-flush/',
        'https://modernforms.com/product/tip-top-44/',
        'https://modernforms.com/product/tip-top-52/',
        'https://modernforms.com/product/cervantes/',
        'https://modernforms.com/product/lotus/',
        'https://modernforms.com/product/aviator-70/',
        'https://modernforms.com/product/morpheus-iii/',
        'https://modernforms.com/product/windflower-60/',
        'https://modernforms.com/product/windflower-80/',
        'https://modernforms.com/product/osprey/',
        'https://modernforms.com/product/mykonos/',
        'https://modernforms.com/product/axis-52/',
        'https://modernforms.com/product/wynd-42/',
        'https://modernforms.com/product/axis-52-flush/',
        'https://modernforms.com/product/vox-26/',
        'https://modernforms.com/product/vox-38/',
        'https://modernforms.com/product/hydra-120/',
        'https://modernforms.com/product/hydra-80/',
        'https://modernforms.com/product/hydra-96/',
        'https://modernforms.com/product/vortex/',
        'https://modernforms.com/product/nautilus/',
        'https://modernforms.com/product/nirvana/',
        'https://modernforms.com/product/aviator-5-luminaire/',
        'https://modernforms.com/product/bluetooth-remote-control/',
        'https://modernforms.com/product/bluetooth-wall-control/',
        'https://modernforms.com/product/downrods/',
        'https://modernforms.com/product/downrod-couplers/',
        'https://modernforms.com/product/sloped-ceiling-kits/',
        'https://modernforms.com/product/radio-frequency-remote-control/',
        'https://modernforms.com/product/aviator-luminaire/',
        'https://modernforms.com/product/aviator-5-flush/',
        'https://modernforms.com/product/beam/',
        'https://modernforms.com/product/metropolis/',
        'https://modernforms.com/product/matrix/',
        'https://modernforms.com/product/pi/',
        'https://modernforms.com/product/mystic/',
        'https://modernforms.com/product/metropolis-2/',
        'https://modernforms.com/product/veloce/',
        'https://modernforms.com/product/cosmic/',
        'https://modernforms.com/product/cosmic-3/',
        'https://modernforms.com/product/cinema/',
        'https://modernforms.com/product/cinema-2/',
        'https://modernforms.com/product/cascade/',
        'https://modernforms.com/product/cascade-2/',
        'https://modernforms.com/product/cascade-3/',
        'https://modernforms.com/product/cascade-4/',
        'https://modernforms.com/product/cascade-5/',
        'https://modernforms.com/product/cascade-6/',
        'https://modernforms.com/product/marimba/',
        'https://modernforms.com/product/marimba-2/',
        'https://modernforms.com/product/racetrack/',
        'https://modernforms.com/product/kinetic/',
        'https://modernforms.com/product/kinetic-2/',
        'https://modernforms.com/product/chaos/',
        'https://modernforms.com/product/chaos-2/',
        'https://modernforms.com/product/chaos-3/',
        'https://modernforms.com/product/chaos-4/',
        'https://modernforms.com/product/chaos-5/',
        'https://modernforms.com/product/minx/',
        'https://modernforms.com/product/avant-garde/',
        'https://modernforms.com/product/bauer/',
        'https://modernforms.com/product/magic/',
        'https://modernforms.com/product/magic-2/',
        'https://modernforms.com/product/magic-3/',
        'https://modernforms.com/product/sofia/',
        'https://modernforms.com/product/polar/',
        'https://modernforms.com/product/tusk/',
        'https://modernforms.com/product/elessar/',
        'https://modernforms.com/product/asgard/',
        'https://modernforms.com/product/lithium/',
        'https://modernforms.com/product/suspense/',
        'https://modernforms.com/product/framed/',
        'https://modernforms.com/product/rain/',
        'https://modernforms.com/product/balthus/',
        'https://modernforms.com/product/pandora/',
        'https://modernforms.com/product/vitrine/',
        'https://modernforms.com/product/helix/',
        'https://modernforms.com/product/omni/',
        'https://modernforms.com/product/twilight/',
        'https://modernforms.com/product/circle/',
        'https://modernforms.com/product/mega/',
        'https://modernforms.com/product/framed-2/',
        'https://modernforms.com/product/tusk-2/',
        'https://modernforms.com/product/soleil/',
        'https://modernforms.com/product/acid/',
        'https://modernforms.com/product/acid-4/',
        'https://modernforms.com/product/acid-5/',
        'https://modernforms.com/product/acid-2/',
        'https://modernforms.com/product/acid-3/',
        'https://modernforms.com/product/acid-6/',
        'https://modernforms.com/product/isotope/',
        'https://modernforms.com/product/gilt/',
        'https://modernforms.com/product/yolo/',
        'https://modernforms.com/product/tango/',
        'https://modernforms.com/product/catalyst/',
        'https://modernforms.com/product/zelda/',
        'https://modernforms.com/product/kryptonite/',
        'https://modernforms.com/product/magic-4/',
        'https://modernforms.com/product/magic-5/',
        'https://modernforms.com/product/magic-6/',
        'https://modernforms.com/product/swing/',
        'https://modernforms.com/product/bdsm/',
        'https://modernforms.com/product/drift/',
        'https://modernforms.com/product/tablet/',
        'https://modernforms.com/product/koolhaus/',
        'https://modernforms.com/product/padron/',
        'https://modernforms.com/product/memory/',
        'https://modernforms.com/product/memory-2/',
        'https://modernforms.com/product/marin/',
        'https://modernforms.com/product/gotham/',
        'https://modernforms.com/product/nimbus/',
        'https://modernforms.com/product/nimbus-2/',
        'https://modernforms.com/product/rings/',
        'https://modernforms.com/product/rings-2/',
        'https://modernforms.com/product/rings-3/',
        'https://modernforms.com/product/rings-4/',
        'https://modernforms.com/product/dream/',
        'https://modernforms.com/product/scissors/',
        'https://modernforms.com/product/stacked/',
        'https://modernforms.com/product/metric/',
        'https://modernforms.com/product/firefly/',
        'https://modernforms.com/product/riddle/',
        'https://modernforms.com/product/riddle-2/',
        'https://modernforms.com/product/chronos/',
        'https://modernforms.com/product/mies/',
        'https://modernforms.com/product/portal/',
        'https://modernforms.com/product/labyrinth/',
        'https://modernforms.com/product/flute/',
        'https://modernforms.com/product/harmonix/',
        'https://modernforms.com/product/harmonix-2/',
        'https://modernforms.com/product/harmonix-3/',
        'https://modernforms.com/product/stormy/',
        'https://modernforms.com/product/fluffy/',
        'https://modernforms.com/product/suspense-2/',
        'https://modernforms.com/product/groovy/',
        'https://modernforms.com/product/punk/',
        'https://modernforms.com/product/the-ring/',
        'https://modernforms.com/product/bloom/',
        'https://modernforms.com/product/lucid/',
        'https://modernforms.com/product/double-bubble/',
        'https://modernforms.com/product/jupiter/',
        'https://modernforms.com/product/atlantis/',
        'https://modernforms.com/product/atlantis-2/',
        'https://modernforms.com/product/jackal/',
        'https://modernforms.com/product/amari/',
        'https://modernforms.com/product/minx-2/',
        'https://modernforms.com/product/minx-3/',
        'https://modernforms.com/product/forever/',
        'https://modernforms.com/product/arcs/',
        'https://modernforms.com/product/wexler/',
        'https://modernforms.com/product/fury/',
        'https://modernforms.com/product/fury-2/',
        'https://modernforms.com/product/aretha/',
        'https://modernforms.com/product/harmony/',
        'https://modernforms.com/product/harmony-2/',
        'https://modernforms.com/product/puck/',
        'https://modernforms.com/product/arc/',
        'https://modernforms.com/product/quantum/',
        'https://modernforms.com/product/circa/',
        'https://modernforms.com/product/argo/',
        'https://modernforms.com/product/logo/',
        'https://modernforms.com/product/sol/',
        'https://modernforms.com/product/box/',
        'https://modernforms.com/product/segment/',
        'https://modernforms.com/product/blaze/',
        'https://modernforms.com/product/gilt-2/',
        'https://modernforms.com/product/issa/',
        'https://modernforms.com/product/luma/',
        'https://modernforms.com/product/neo/',
        'https://modernforms.com/product/vodka/',
        'https://modernforms.com/product/fluffy-2/',
        'https://modernforms.com/product/mini-loft/',
        'https://modernforms.com/product/loft-3/',
        'https://modernforms.com/product/aegean/',
        'https://modernforms.com/product/neo-2/',
        'https://modernforms.com/product/fusion/',
        'https://modernforms.com/product/vetri/',
        'https://modernforms.com/product/glacier/',
        'https://modernforms.com/product/glacier-2/',
        'https://modernforms.com/product/seismic/',
        'https://modernforms.com/product/bahn/',
        'https://modernforms.com/product/margin/',
        'https://modernforms.com/product/vogue/',
        'https://modernforms.com/product/mini-cloud/',
        'https://modernforms.com/product/vogue-2/',
        'https://modernforms.com/product/spectre/',
        'https://modernforms.com/product/sabre/',
        'https://modernforms.com/product/cloud/',
        'https://modernforms.com/product/vigo-2/',
        'https://modernforms.com/product/vigo/',
        'https://modernforms.com/product/mini-vogue/',
        'https://modernforms.com/product/lightstick/',
        'https://modernforms.com/product/nightlife/',
        'https://modernforms.com/product/vodka-2/',
        'https://modernforms.com/product/vodka-3/',
        'https://modernforms.com/product/ice/',
        'https://modernforms.com/product/shimmer/',
        'https://modernforms.com/product/shock-waves/',
        'https://modernforms.com/product/memory-3/',
        'https://modernforms.com/product/garbo/',
        'https://modernforms.com/product/arch/',
        'https://modernforms.com/product/lumnos/',
        'https://modernforms.com/product/nia/',
        'https://modernforms.com/product/i-beam/',
        'https://modernforms.com/product/slide/',
        'https://modernforms.com/product/escher/',
        'https://modernforms.com/product/fold/',
        'https://modernforms.com/product/glacier-3/',
        'https://modernforms.com/product/blade/',
        'https://modernforms.com/product/blade-2/',
        'https://modernforms.com/product/blade-3/',
        'https://modernforms.com/product/stem/',
        'https://modernforms.com/product/elysia/',
        'https://modernforms.com/product/muse/',
        'https://modernforms.com/product/cosmic-5/',
        'https://modernforms.com/product/chill/',
        'https://modernforms.com/product/lance/',
        'https://modernforms.com/product/palladian/',
        'https://modernforms.com/product/vermeil/',
        'https://modernforms.com/product/dink/',
        'https://modernforms.com/product/mulholland/',
        'https://modernforms.com/product/tribeca/',
        'https://modernforms.com/product/scepter/',
        'https://modernforms.com/product/ceres/',
        'https://modernforms.com/product/cinema-3/',
        'https://modernforms.com/product/cinema-4/',
        'https://modernforms.com/product/tower/',
        'https://modernforms.com/product/stormy-2/',
        'https://modernforms.com/product/gatsby/',
        'https://modernforms.com/product/cinderella/',
        'https://modernforms.com/product/wasp/',
        'https://modernforms.com/product/argo-2/',
        'https://modernforms.com/product/mies-2/',
        'https://modernforms.com/product/quarry/',
        'https://modernforms.com/product/minx-4/',
        'https://modernforms.com/product/forbes/',
        'https://modernforms.com/product/minx-5/',
        'https://modernforms.com/product/burning-man/',
        'https://modernforms.com/product/ember/',
        'https://modernforms.com/product/diplomat/',
        'https://modernforms.com/product/fury-3/',
        'https://modernforms.com/product/chaos-6/',
        'https://modernforms.com/product/harmony-3/',
        'https://modernforms.com/product/oracle/',
        'https://modernforms.com/product/razor/',
        'https://modernforms.com/product/vessel/',
        'https://modernforms.com/product/bloc/',
        'https://modernforms.com/product/burlington/',
        'https://modernforms.com/product/bloc-2/',
        'https://modernforms.com/product/bloc-3/',
        'https://modernforms.com/product/vessel-2/',
        'https://modernforms.com/product/vessel-3/',
        'https://modernforms.com/product/skyscraper/',
        'https://modernforms.com/product/phantom/',
        'https://modernforms.com/product/jedi/',
        'https://modernforms.com/product/hiline/',
        'https://modernforms.com/product/urban/',
        'https://modernforms.com/product/elevation/',
        'https://modernforms.com/product/kyoto/',
        'https://modernforms.com/product/twist/',
        'https://modernforms.com/product/torch/',
        'https://modernforms.com/product/forq/',
        'https://modernforms.com/product/big-v/',
        'https://modernforms.com/product/staff/',
        'https://modernforms.com/product/dawn/',
        'https://modernforms.com/product/blade-4/',
        'https://modernforms.com/product/tuxedo/',
        'https://modernforms.com/product/double-down/',
        'https://modernforms.com/product/ledge-3/',
        'https://modernforms.com/product/ledge-4/',
        'https://modernforms.com/product/oath/',
        'https://modernforms.com/product/square/',
        'https://modernforms.com/product/blinc-2/',
        'https://modernforms.com/product/oslo/',
        'https://modernforms.com/product/structure/',
        'https://modernforms.com/product/lucid-2/',
        'https://modernforms.com/product/logic/',
        'https://modernforms.com/product/avant-garde-2/',
        'https://modernforms.com/product/shadow/',
        'https://modernforms.com/product/frost/',
        'https://modernforms.com/product/two-if-by-sea/',
        'https://modernforms.com/product/fathom/',
        'https://modernforms.com/product/wexler-2/',
        'https://modernforms.com/product/lanterna/',
        'https://modernforms.com/product/elyse/',
        'https://modernforms.com/product/dorne/',
        'https://modernforms.com/product/mist/',
        'https://modernforms.com/product/hangman/',
        'https://modernforms.com/product/linear/',
        'https://modernforms.com/product/suspension/',
        'https://modernforms.com/product/stylus/',
        'https://modernforms.com/product/reflex/',
        'https://modernforms.com/product/avant-garde-3/',
        'https://modernforms.com/product/magic-7/',
        'https://modernforms.com/product/framed-4/',
        'https://modernforms.com/product/axis-44-flush/',
        'https://modernforms.com/product/axis-44/',
        'https://modernforms.com/product/mykonos-5/',
        'https://modernforms.com/product/polar-2/',
        'https://modernforms.com/product/kind/',
        'https://modernforms.com/product/prisma/',
        'https://modernforms.com/product/kind-2/',
        'https://modernforms.com/product/plum/',
        'https://modernforms.com/product/basalt/',
        'https://modernforms.com/product/tandem/',
        'https://modernforms.com/product/juliet/',
        'https://modernforms.com/product/cymbal/',
        'https://modernforms.com/product/harper/',
        'https://modernforms.com/product/ingot/',
        'https://modernforms.com/product/rhiannon/',
        'https://modernforms.com/product/coliseo/',
        'https://modernforms.com/product/starlight-starbright/',
        'https://modernforms.com/product/step-light/',
        'https://modernforms.com/product/downton/',
        'https://modernforms.com/product/kinsman/',
        'https://modernforms.com/product/emblem/',
        'https://modernforms.com/product/cinch/',
        'https://modernforms.com/product/poet/',
        'https://modernforms.com/product/bantam/',
        'https://modernforms.com/product/opus/',
        'https://modernforms.com/product/valor/',
        'https://modernforms.com/product/regal/',
        'https://modernforms.com/product/open-bar/',
        'https://modernforms.com/product/cadence/',
        'https://modernforms.com/product/0-to-60/',
        'https://modernforms.com/product/juliet-2/',
        'https://modernforms.com/product/curvana/',
        'https://modernforms.com/product/mako/',
        'https://modernforms.com/product/reflect/',
        'https://modernforms.com/product/revere/',
        'https://modernforms.com/product/maglev/',
        'https://modernforms.com/product/chaos-7/',
        'https://modernforms.com/product/zenith-2/',
        'https://modernforms.com/product/juliet-3/',
        'https://modernforms.com/product/wynd-xl/',
        'https://modernforms.com/product/twirl/',
        'https://modernforms.com/product/step-light-2/',
        'https://modernforms.com/product/catalyst-2/',
        'https://modernforms.com/product/kryptonite-2/',
        'https://modernforms.com/product/isotope-2/',
        'https://modernforms.com/product/acropolis/',
        'https://modernforms.com/product/cosmic-2/',
        'https://modernforms.com/product/aloft/',
        'https://modernforms.com/product/stargazer/',
        'https://modernforms.com/product/morpheus-ii/',
        'https://modernforms.com/product/orb-62/',
        'https://modernforms.com/product/orb-82/',
        'https://modernforms.com/product/venus/',
        'https://modernforms.com/product/paradox/',
        'https://modernforms.com/product/interlace/',
        'https://modernforms.com/product/luzerne/',
        'https://modernforms.com/product/ultra/',
        'https://modernforms.com/product/veloce-2/',
        'https://modernforms.com/product/grommet/',
        'https://modernforms.com/product/cascade-7/',
        'https://modernforms.com/product/catalonia/',
        'https://modernforms.com/product/i-spy/',
        'https://modernforms.com/product/squat/',
        'https://modernforms.com/product/kube/',
        'https://modernforms.com/product/metropolis-3/',
        'https://modernforms.com/product/imperial/',
        'https://modernforms.com/product/battlestar/',
        'https://modernforms.com/product/serenity/',
        'https://modernforms.com/product/bough/',
        'https://modernforms.com/product/tempest/',
        'https://modernforms.com/product/charlize/',
        'https://modernforms.com/product/equilibrium/',
        'https://modernforms.com/product/orion/',
        'https://modernforms.com/product/tidal/',
        'https://modernforms.com/product/sydney/',
        'https://modernforms.com/product/burning-man-2/',
        'https://modernforms.com/product/rhiannon-2/',
        'https://modernforms.com/product/zenya/',
        'https://modernforms.com/product/amari-2/',
        'https://modernforms.com/product/double-bubble-2/',
        'https://modernforms.com/product/double-bubble-3/',
        'https://modernforms.com/product/double-bubble-4/',
        'https://modernforms.com/product/double-bubble-5/',
        'https://modernforms.com/product/cambridge/',
        'https://modernforms.com/product/balthus-2/',
        'https://modernforms.com/product/swerve/',
        'https://modernforms.com/product/vander/',
        'https://modernforms.com/product/vander-2/',
        'https://modernforms.com/product/serenity-2/',
        'https://modernforms.com/product/tandem-2/',
        'https://modernforms.com/product/fleur/',
        'https://modernforms.com/product/acropolis-2/',
        'https://modernforms.com/product/barre/',
        'https://modernforms.com/product/ophelia/',
        'https://modernforms.com/product/double-bubble-6/',
        'https://modernforms.com/product/double-bubble-7/',
        'https://modernforms.com/product/cupid/',
        'https://modernforms.com/product/dusk/',
        'https://modernforms.com/product/enigma/',
        'https://modernforms.com/product/monarch/',
        'https://modernforms.com/product/cambridge-2/',
        'https://modernforms.com/product/midnight/',
        'https://modernforms.com/product/carillion/',
        'https://modernforms.com/product/carillion-2/',
        'https://modernforms.com/product/firenze/',
        'https://modernforms.com/product/skylark-flush/',
        'https://modernforms.com/product/corona-44/',
        'https://modernforms.com/product/corona-52/',
        'https://modernforms.com/product/wyndmill/',
        'https://modernforms.com/product/ultra-uv-c-replacement-module/',
        'https://modernforms.com/product/roboto-52/',
        'https://modernforms.com/product/roboto-62/',
        'https://modernforms.com/product/aegis/',
        'https://modernforms.com/product/beacon/',
        'https://modernforms.com/product/haze/',
        'https://modernforms.com/product/myla/',
        'https://modernforms.com/product/aspire/',
        'https://modernforms.com/product/nexus/',
        'https://modernforms.com/product/stylus-2/',
        'https://modernforms.com/product/terra/',
        'https://modernforms.com/product/lior/',
        'https://modernforms.com/product/toulouse/',
        'https://modernforms.com/product/rowlings/',
        'https://modernforms.com/product/cymbal-2/',
        'https://modernforms.com/product/ezra/',
        'https://modernforms.com/product/amari-3/',
        'https://modernforms.com/product/harmonix-4/',
        'https://modernforms.com/product/sartre/',
        'https://modernforms.com/product/mercer/',
        'https://modernforms.com/product/zurich/',
        'https://modernforms.com/product/echelon/',
        'https://modernforms.com/product/amari-4/',
        'https://modernforms.com/product/hugo/',
        'https://modernforms.com/product/helios/',
        'https://modernforms.com/product/rollins/',
        'https://modernforms.com/product/ezra-2/',
        'https://modernforms.com/product/alfa/',
        'https://modernforms.com/product/illusion/',
        'https://modernforms.com/product/echelon-2/',
        'https://modernforms.com/product/echelon-3/',
        'https://modernforms.com/product/minx-6/',
        'https://modernforms.com/product/luzerne-2/',
        'https://modernforms.com/product/black-jack/',
        'https://modernforms.com/product/intrasection/',
        'https://modernforms.com/product/hustler/',
        'https://modernforms.com/product/bentley/',
        'https://modernforms.com/product/bentley-2/',
        'https://modernforms.com/product/vesta/',
        'https://modernforms.com/product/ilios/',
        'https://modernforms.com/product/lucid-3/',
        'https://modernforms.com/product/skylark-62/',
        'https://modernforms.com/product/torque-70/',
        'https://modernforms.com/product/elf/',
        'https://modernforms.com/product/vox-26-2/',
        'https://modernforms.com/product/loft/',
        'https://modernforms.com/product/roboto-xl-with-luminaire/',
        'https://modernforms.com/product/roboto-xl/',
        'https://modernforms.com/product/veloce-smart-fan/',
        'https://modernforms.com/product/aura-60/',
        'https://modernforms.com/product/zephyr-5/',
        'https://modernforms.com/product/size-matters-65/',
        'https://modernforms.com/product/size-matters-84-with-luminaire/',
        'https://modernforms.com/product/size-matters-84/',
        'https://modernforms.com/product/spinster/',
        'https://modernforms.com/product/size-matters-65-with-luminaire/',
        'https://modernforms.com/product/boxie/',
        'https://modernforms.com/product/draped/',
        'https://modernforms.com/product/landed/',
        'https://modernforms.com/product/luzerne-3/',
        'https://modernforms.com/product/cascade-8/',
        'https://modernforms.com/product/cascade-9/',
        'https://modernforms.com/product/cambia/',
        'https://modernforms.com/product/torque/',
        'https://modernforms.com/product/skylark-54/',
        'https://modernforms.com/product/aura/',
        'https://modernforms.com/product/berkeley/',
        'https://modernforms.com/product/aviator-flush/',
        'https://modernforms.com/product/aviator/',
        'https://modernforms.com/product/flamme/',
        'https://modernforms.com/product/heliograph/',
        'https://modernforms.com/product/flue/',
        'https://modernforms.com/product/posh/',
        'https://modernforms.com/product/ascot/',
        'https://modernforms.com/product/swoop/',
        'https://modernforms.com/product/lanza/',
        'https://modernforms.com/product/lyrikal/',
        'https://modernforms.com/product/sensu/',
        'https://modernforms.com/product/brody/',
        'https://modernforms.com/product/azure/',
        'https://modernforms.com/product/montage/',
        'https://modernforms.com/product/saber/',
        'https://modernforms.com/product/taper/',
        'https://modernforms.com/product/clique/',
        'https://modernforms.com/product/parel/',
        'https://modernforms.com/product/parel-2/',
        'https://modernforms.com/product/synopsis/',
        'https://modernforms.com/product/minx-7/',
        'https://modernforms.com/product/minx-8/',
        'https://modernforms.com/product/magic-8/',
        'https://modernforms.com/product/double-bubble-8/',
        'https://modernforms.com/product/double-bubble-9/',
        'https://modernforms.com/product/double-bubble-10/',
        'https://modernforms.com/product/woven/',
        'https://modernforms.com/product/entice/',
        'https://modernforms.com/product/javelin/',
        'https://modernforms.com/product/firenze-2/',
        'https://modernforms.com/product/line-up/',
        'https://modernforms.com/product/wizard/',
        'https://modernforms.com/product/ivery/',
        'https://modernforms.com/product/pavilion/',
        'https://modernforms.com/product/gavel/',
        'https://modernforms.com/product/out-of-this-world/',
        'https://modernforms.com/product/preston/',
        'https://modernforms.com/product/tinzel/',
        'https://modernforms.com/product/pearla/',
        'https://modernforms.com/product/emblem-2/',
        'https://modernforms.com/product/tableau/',
        'https://modernforms.com/product/cinema-5/',
        'https://modernforms.com/product/copa/',
        'https://modernforms.com/product/illusion-2/',
        'https://modernforms.com/product/cadiz/',
        'https://modernforms.com/product/tendril/',
        'https://modernforms.com/product/cinema-6/',
        'https://modernforms.com/product/chyzel/',
        'https://modernforms.com/product/swirly/',
        'https://modernforms.com/product/siren/',
        'https://modernforms.com/product/pompeii/',
        'https://modernforms.com/product/dashe/',
        'https://modernforms.com/product/quincy/',
        'https://modernforms.com/product/copa-2/',
        'https://modernforms.com/product/chauncey/',
        'https://modernforms.com/product/clique-2/',
        'https://modernforms.com/product/kiefer/',
        'https://modernforms.com/product/sybil/',
        'https://modernforms.com/product/strait/',
        'https://modernforms.com/product/caviar/',
        'https://modernforms.com/product/catania/',
        'https://modernforms.com/product/dolman/',
        'https://modernforms.com/product/storm-chaser/',
        'https://modernforms.com/product/lute/',
        'https://modernforms.com/product/delaney/',
        'https://modernforms.com/product/fleur-de-lis/',
        'https://modernforms.com/product/ronan/',
        'https://modernforms.com/product/ansel/',
        'https://modernforms.com/product/diver/',
        'https://modernforms.com/product/inlet/',
        'https://modernforms.com/product/ledge/',
        'https://modernforms.com/product/cambridge-3/',
        'https://modernforms.com/product/balthus-3/',
        'https://modernforms.com/product/two-if-by-sea-2/',
        'https://modernforms.com/product/mist-2/',
        'https://modernforms.com/product/framed-3/',
        'https://modernforms.com/product/swale/',
        'https://modernforms.com/product/ice-bar/',
        'https://modernforms.com/product/skein/',
        'https://modernforms.com/product/tableau-2/',
        'https://modernforms.com/product/ribeye/',
        'https://modernforms.com/product/clasp/',
        'https://modernforms.com/product/montage-2/',
        'https://modernforms.com/product/cinema-7/',
        'https://modernforms.com/product/elixir/',
        'https://modernforms.com/product/fresco/',
        'https://modernforms.com/product/stanchion/',
        'https://modernforms.com/product/carlisle/',
        'https://modernforms.com/product/strait-2/',
        'https://modernforms.com/product/deco/',
        'https://modernforms.com/product/clare/',
        'https://modernforms.com/product/taffeta/',
        'https://modernforms.com/product/pirouette/',
        'https://modernforms.com/product/royce/',
        'https://modernforms.com/product/cinema-8/',
        'https://modernforms.com/product/clog/',
        'https://modernforms.com/product/ice-bar-2/',
        'https://modernforms.com/product/gander/',
        'https://modernforms.com/product/pillar/',
        'https://modernforms.com/product/krimp/',
        'https://modernforms.com/product/scepter-2/',
        'https://modernforms.com/product/keyhole/',
        'https://modernforms.com/product/kane/',
        'https://modernforms.com/product/smooth-flush/',
        'https://modernforms.com/product/zephyr-72/',
        'https://modernforms.com/product/bolo/',
        'https://modernforms.com/product/smooth/',
        'https://modernforms.com/product/carve-60/',
        'https://modernforms.com/product/carve-60-with-luminaire/',
        'https://modernforms.com/product/carve-72/',
        'https://modernforms.com/product/carve-72-with-luminaire/',
        'https://modernforms.com/product/kilt/',
        'https://modernforms.com/product/kilt-4/',
        'https://modernforms.com/product/kilt-2/',
        'https://modernforms.com/product/kilt-5/',
        'https://modernforms.com/product/kilt-3/',
        'https://modernforms.com/product/chaos-10/',
        'https://modernforms.com/product/pisces/',
        'https://modernforms.com/product/pisces-4/',
        'https://modernforms.com/product/pisces-5/',
        'https://modernforms.com/product/pisces-3/',
        'https://modernforms.com/product/pisces-2/',
        'https://modernforms.com/product/chaos-9/',
        'https://modernforms.com/product/chaos-8/',
        'https://modernforms.com/product/uv-bluetooth-remote-control/',
        'https://modernforms.com/product/radio-frequency-wall-control/',
        'https://modernforms.com/product/strut/',
        'https://modernforms.com/product/mocha/',
        'https://modernforms.com/product/abicus-dmx-ip-gateway/',
        'https://modernforms.com/product/konstrukt/',
        'https://modernforms.com/product/ribeye-2/',
        'https://modernforms.com/product/carlisle-2/',
        'https://modernforms.com/product/quantum-2/',
        'https://modernforms.com/product/invisiled-palette/',
        'https://modernforms.com/product/aviator-54-5/',
    ]

    # Category listing pages; each is paginated past its first 12 items.
    CATEGORY_URLS = [
        'https://modernforms.com/product-category/accessories',
        'https://modernforms.com/product-category/accessories-and-integration/',
        'https://modernforms.com/product-category/bathroom-sconces/',
        'https://modernforms.com/product-category/chandeliers/',
        'https://modernforms.com/product-category/downrod-fans/',
        'https://modernforms.com/product-category/flush-mount-fans/',
        'https://modernforms.com/product-category/indoor-flush-mounts/',
        'https://modernforms.com/product-category/indoor-outdoor-flush-mounts/',
        'https://modernforms.com/product-category/indoor-outdoor-pendants/',
        'https://modernforms.com/product-category/indoor-outdoor-sconces/',
        'https://modernforms.com/product-category/indoor-pendants-2/',
        'https://modernforms.com/product-category/indoor-pendants/',
        'https://modernforms.com/product-category/indoor-sconces/',
        'https://modernforms.com/product-category/linear-pendants/',
        'https://modernforms.com/product-category/mini-pendants/',
        'https://modernforms.com/product-category/multi-lights/',
        'https://modernforms.com/product-category/outdoor-flush-mounts/',
        'https://modernforms.com/product-category/outdoor-pendants/',
        'https://modernforms.com/product-category/outdoor-sconces/',
        'https://modernforms.com/product-category/pendant-chandelier/',
        'https://modernforms.com/product-category/post-lights/',
        'https://modernforms.com/product-category/step-lights/',
        'https://modernforms.com/product-category/step-lights2/',
        'https://modernforms.com/product-category/swing-arm---picture-lights/',
        'https://modernforms.com/product-category/swing-arm-picture-lights/',
        'https://modernforms.com/product-category/uncategorized/',
        'https://modernforms.com/product-category/vanity-lights/',
    ]

    def start_requests(self):
        for url in self.PRODUCT_URLS:
            yield Request(url, callback=self.parse_product_detail, errback=self.on_error)

        for url in self.CATEGORY_URLS:
            yield Request(url, callback=self.parse_category_html, errback=self.on_error)

    def parse_category_html(self, response):
        cate_title = _clean(
            response.xpath("//header[contains(@class,'category-page-header')]/section/text()").get()
        )
        cateid = _clean(response.xpath("//*[@id='catid']/text()").get())
        product_urls = response.xpath("//aside/a[contains(@href,'product')]/@href").getall()

        if not product_urls:
            logger.warning("[%s] no product links found (url=%s)", cate_title or cateid, response.url)

        meta = {"cate_title": cate_title, "cate_url": response.url}
        for url in product_urls:
            yield Request(
                response.urljoin(url),
                callback=self.parse_product_detail,
                errback=self.on_error,
                meta=meta,
            )

        # First page is already scraped above; page past it if there's more.
        total_item = _to_int(response.xpath("//*[@id='idunfiltered']/@data-total").get())
        data_key = response.xpath("//*[@id='idunfiltered']/text()").get()
        if not (total_item and data_key and total_item > PAGE_SIZE):
            return

        total_pages = math.ceil(total_item / PAGE_SIZE)
        for i in range(1, total_pages):
            yield scrapy.FormRequest(
                AJAX_URL,
                formdata={"data": data_key, "page": str(PAGE_SIZE * i), "catID": cateid},
                callback=self.parse_cate_json,
                errback=self.on_error,
                dont_filter=True,
                meta=meta,
            )

    def parse_cate_json(self, response):
        try:
            jdata = json.loads(response.text)
        except json.JSONDecodeError:
            logger.error("[%s] lazyUnfiltered returned non-JSON (url=%s)",
                        response.meta.get("cate_title"), response.url)
            return

        for entry in jdata or []:
            yield Request(
                entry["link"],
                callback=self.parse_product_detail,
                errback=self.on_error,
                meta=response.meta,
            )

    def parse_product_detail(self, response):
        cate_url = (
            _clean(response.xpath("//aside[@id='breadcrumb']/a[2]/@href").get())
            or response.meta.get("cate_url", "")
        )
        base = {
            "url": response.url,
            "cate_url": cate_url,
            "images": list(dict.fromkeys(
                response.urljoin(src)
                for src in response.xpath("//section[@class='product-thumbs']//img/@src").getall()
            )),
            "specsheet": _download_href(response, "Spec Sheet"),
            "installationsheet": _download_href(response, "Instructions"),
        }

        jtext = re.findall(r"var all_models\s*=\s*(.[^;]*)", response.text)
        try:
            jdata = json.loads(jtext[0]) if jtext else []
        except json.JSONDecodeError:
            jdata = []

        if not jdata:
            default_sku = _clean(response.xpath("//section[@id='order-number']/strong/text()").get())
            if not default_sku:
                logger.warning("[%s] no SKU found (no all_models JSON, no HTML fallback)", response.url)
            yield ModernFormsItem(**base, sku=default_sku)
            return

        for model in jdata:
            for sku in model.keys():
                yield ModernFormsItem(**base, sku=sku)

    def on_error(self, failure):
        logger.warning("[%s] request failed: %s", failure.request.url, failure.value)
