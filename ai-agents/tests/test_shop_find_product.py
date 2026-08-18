"""DemoShop ``find_product`` — the by-name catalog lookup (AI-05).

Why this tool exists: a live fred_returning_caller run (2026-08-17) had the
sales specialist offer a "Bluetooth portable speaker" and a "fitness tracker",
neither of which this company has ever sold. The caller picked one, the agent
could not price it, and escalated — the call then parked in the queue at
status=waiting, never ended, so caller memory was never finalized and the
NEXT call failed to recognize him. One ungrounded sentence took out the whole
memory feature downstream.

The prompt already said "never from memory or guesswork" (main_agent.py, the
Approach section) and the model violated it anyway, which is the PGI point
exactly: a rule in a prompt is a proposal. So the fix is function design —
these tests pin the two properties that make fabrication recoverable rather
than terminal:

  1. a product we do not sell returns an authoritative NOT_IN_CATALOG that
     carries the real lineup, so the model can correct itself instead of
     transferring,
  2. a product we DO sell is answerable by the name a caller would actually
     say, not by SKU (check_inventory wants "HDPH-001"; nobody says that).
"""

import os
import sqlite3
import sys
import tempfile
import types

import pytest

DEMO_MCP = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'demo-mcp',
)
if DEMO_MCP not in sys.path:
    sys.path.insert(0, DEMO_MCP)

# Stub FastMCP rather than depend on the `mcp` package. These tests exercise
# catalog lookup, which is plain sqlite + string work; the decorator only
# registers the function with a server we never start. Keeping the dependency
# out means they run in this repo's normal local test pass — the demo-mcp
# image has `mcp` but no pytest, and ai-agents has neither.
if 'mcp.server.fastmcp' not in sys.modules:
    class _StubMCP:
        def __init__(self, *_args, **_kwargs):
            pass

        def tool(self, *_args, **_kwargs):
            return lambda fn: fn

    _mcp = types.ModuleType('mcp')
    _server = types.ModuleType('mcp.server')
    _fastmcp = types.ModuleType('mcp.server.fastmcp')
    _fastmcp.FastMCP = _StubMCP
    _server.fastmcp = _fastmcp
    _mcp.server = _server
    sys.modules.setdefault('mcp', _mcp)
    sys.modules.setdefault('mcp.server', _server)
    sys.modules['mcp.server.fastmcp'] = _fastmcp


CATALOG = [
    ('HDPH-001', 'Wireless Over-Ear Headphones', 14999, 12, 1284),
    ('HDPH-002', 'True Wireless Earbuds', 7999, 3, 946),
    ('CABL-USBC', 'USB-C Charging Cable, 2m', 1299, 0, 812),
    ('WBCM-4K', '4K Webcam, Auto-focus', 9900, 40, 512),
]


@pytest.fixture()
def shop(monkeypatch):
    """A real sqlite catalog — the tool's whole job is reading one."""
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE products (
               sku TEXT PRIMARY KEY, name TEXT, price_cents INTEGER,
               in_stock INTEGER, units_sold INTEGER)"""
    )
    conn.executemany('INSERT INTO products VALUES (?,?,?,?,?)', CATALOG)
    conn.commit()
    conn.close()

    monkeypatch.setenv('SHOP_DB_PATH', path)
    import shop_mcp_server
    monkeypatch.setattr(shop_mcp_server, 'DB_PATH', path)
    yield shop_mcp_server
    os.unlink(path)


def _call(tool):
    """FastMCP wraps the function; reach the plain callable underneath."""
    return getattr(tool, 'fn', None) or getattr(tool, '__wrapped__', tool)


def test_a_product_we_do_not_sell_is_answered_not_deferred(shop):
    """The regression. The reply must be usable ON the call: an explicit no,
    plus the real lineup, plus an instruction NOT to go looking elsewhere."""
    result = _call(shop.find_product)('Smart Fitness Tracker')

    assert result['found'] is False
    assert result['reason'] == 'NOT_IN_CATALOG'
    # The real catalog rides along — this is what lets the model recover in
    # the same breath instead of transferring.
    assert 'Wireless Over-Ear Headphones' in result['catalog']
    guidance = result['answer_guidance']
    assert 'do NOT sell' in guidance
    assert 'Wireless Over-Ear Headphones' in guidance
    # ...and it must actively close off the escalation that lost the call.
    assert 'transfer' in guidance.lower()


def test_the_name_a_caller_would_actually_say_finds_the_product(shop):
    """check_inventory needs 'HDPH-002'. Callers say 'wireless earbuds'."""
    result = _call(shop.find_product)('wireless earbuds')

    assert result['found'] is True
    assert result['product']['name'] == 'True Wireless Earbuds'
    assert result['product']['price'] == '$79.99'
    # Price in the first sentence — callers interrupt.
    assert '$79.99' in result['answer_guidance']


def test_punctuated_names_still_match_spoken_words(shop):
    """'usb c cable' vs 'USB-C Charging Cable, 2m' — splitting on whitespace
    alone leaves 'usb-c' as one token and never matches."""
    result = _call(shop.find_product)('usb c cable')

    assert result['found'] is True
    assert result['product']['name'] == 'USB-C Charging Cable, 2m'
    assert result['product']['availability'] == 'out of stock'


@pytest.mark.parametrize('spoken', [
    'Smart Home Hub',   # 1 of 3
    'Smart Hub',        # 1 of 2 — exactly half, which a ratio rule allowed
    'Home Hub',         # 1 of 2
    'gaming headset',   # shares nothing meaningful
])
def test_one_shared_word_never_matches_at_any_query_length(shop, spoken):
    """A proportional threshold could not express this: at two tokens one
    overlap IS half, so 'Smart Hub' and 'Home Hub' resolved to a real hub at
    a real price while the three-word form was correctly rejected — the same
    false-product bug in a shorter sentence."""
    fixture_hub = ('HUB-USB', '7-Port USB Hub w/ Power', 3499, 9, 244)
    import sqlite3
    conn = sqlite3.connect(os.environ['SHOP_DB_PATH'])
    conn.execute('INSERT INTO products VALUES (?,?,?,?,?)', fixture_hub)
    conn.commit()
    conn.close()

    result = _call(shop.find_product)(spoken)

    assert result['found'] is False, spoken
    assert result['reason'] == 'NOT_IN_CATALOG'


def test_a_single_word_query_still_matches(shop):
    """One word is all the caller gave; matching it is the best available
    reading of what they said."""
    assert _call(shop.find_product)('earbuds')['product']['name'] == \
        'True Wireless Earbuds'


def test_one_shared_word_is_not_a_match(shop):
    """Caught on a live call: 'Smart Home Hub' matched '7-Port USB Hub w/
    Power' on the word 'hub' alone, so the tool answered an invented product
    with a real one at a real price. That is worse than saying we don't carry
    it — it destroys the not-in-catalog signal this tool exists to give."""
    result = _call(shop.find_product)('Smart Home Hub')

    assert result['found'] is False
    assert result['reason'] == 'NOT_IN_CATALOG'


@pytest.mark.parametrize('spoken,expected', [
    ('wireless earbuds', 'True Wireless Earbuds'),
    ('tell me about the usb c cable', 'USB-C Charging Cable, 2m'),
    ('what does the 4k webcam cost', '4K Webcam, Auto-focus'),
])
def test_filler_words_do_not_dilute_a_real_match(shop, spoken, expected):
    """The ratio threshold must not punish callers for speaking in sentences."""
    result = _call(shop.find_product)(spoken)

    assert result['found'] is True, spoken
    assert result.get('multiple_matches') is not True
    assert result['product']['name'] == expected


def test_an_ambiguous_name_asks_instead_of_picking(shop):
    """'headphones' could be either audio product; guessing one is the same
    failure in miniature."""
    result = _call(shop.find_product)('wireless')

    assert result['found'] is True
    assert result['multiple_matches'] is True
    names = {p['name'] for p in result['products']}
    assert names == {'Wireless Over-Ear Headphones', 'True Wireless Earbuds'}


def test_list_products_closes_the_set_it_invites_elaboration_on(shop):
    """The original guidance said 'elaborate after that sentence' and bounded
    nothing; the live call took the invitation."""
    result = _call(shop.list_products)()

    assert result['catalog_is_complete'] is True
    guidance = result['answer_guidance']
    assert 'ONLY ones we sell' in guidance
    # Every real product is named in the guidance the model reads.
    for _sku, name, _price, _stock, _sold in CATALOG:
        assert name in guidance
    # And the price still lands first.
    assert '$149.99' in guidance
