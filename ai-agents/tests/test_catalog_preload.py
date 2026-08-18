"""Preloading the live catalog into the specialist's prompt.

Why this exists (live call 78, 2026-08-17). Asked "what products do you offer
and what do they cost", the sales specialist answered:

    "we offer a range of products, including the smart home hub for two
     hundred dollars, wireless earbuds for one hundred fifty dollars, and a
     fitness tracker for seventy five dollars"

Two of those products have never existed and the third's price was wrong — and
it called NO catalog tool before saying it. It then repeated the invented $200
after find_product returned a different product and price, because once a
number is in the conversation the model defends it.

The lesson is that a typed tool return only helps if the model elects to call
the tool before committing to an answer. For the most common question on a
sales line, that election is too weak a link, so the catalog goes into the
prompt where there is no gap to invent into.

These tests pin the properties that keep it honest: the section must carry
every product with its exact price, must say the list is complete, and must
never take the call down with it when the gateway is unreachable.
"""

import os
import sys

import pytest

AGENTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if AGENTS_DIR not in sys.path:
    sys.path.insert(0, AGENTS_DIR)

main_agent = pytest.importorskip(
    'main_agent',
    reason='needs the signalwire SDK; runs in the agents image',
)


CATALOG = {
    'found': True,
    'most_popular': {'name': 'Wireless Over-Ear Headphones'},
    'products': [
        {'name': 'Wireless Over-Ear Headphones', 'price': '$149.99',
         'availability': 'in stock'},
        {'name': 'True Wireless Earbuds', 'price': '$79.99',
         'availability': 'out of stock'},
        {'name': 'USB-C Charging Cable, 2m', 'price': '$12.99',
         'availability': 'in stock'},
    ],
}

ENTRIES = [{'name': 'DemoShop', 'config': {
    'gateway_url': 'http://demo-mcp-gateway:8100',
    'auth_user': 'demo', 'auth_password': 'demo',
}}]


class FakeAgent:
    """Just the surface preload_mcp_context touches."""

    def __init__(self, **attrs):
        self.sections = []
        for key, value in attrs.items():
            setattr(self, key, value)

    def prompt_add_section(self, title, body=None, **_kwargs):
        self.sections.append((title, body))


@pytest.fixture(autouse=True)
def _clear_cache():
    main_agent._preload_cache.clear()
    yield
    main_agent._preload_cache.clear()


def test_the_catalog_lands_in_the_prompt_with_exact_prices(monkeypatch):
    monkeypatch.setattr(main_agent, '_call_mcp_tool',
                        lambda *a, **kw: CATALOG)
    agent = FakeAgent(_preload_mcp_tool='list_products',
                      _preload_section_title='Product Catalog')

    assert main_agent.preload_mcp_context(agent, ENTRIES) is True

    title, body = agent.sections[0]
    assert title == 'Product Catalog'
    # Every product, priced to the cent — the model quotes what it reads.
    for product in CATALOG['products']:
        assert product['name'] in body
        assert product['price'] in body
    # Closed set, stated as such.
    assert 'COMPLETE' in body
    assert 'Never name, describe, or price a product that is not on this list' in body
    # The best seller is marked, since "what's most popular" is the other
    # half of the question that started this.
    assert 'most popular' in body.lower()


def test_an_unreachable_gateway_costs_the_section_not_the_call(monkeypatch):
    """The specialist still has its tools; it just doesn't get the head start.
    A catalog preload must never be able to fail a call."""
    def _boom(*_args, **_kwargs):
        raise RuntimeError('connection refused')

    monkeypatch.setattr(main_agent, '_call_mcp_tool', _boom)
    agent = FakeAgent(_preload_mcp_tool='list_products')

    assert main_agent.preload_mcp_context(agent, ENTRIES) is False
    assert agent.sections == []


def test_agents_that_declare_nothing_are_untouched(monkeypatch):
    """Opt-in per agent: DemoShop is the bundled demo, and a cloner who
    points these agents at their own MCP server must not inherit a catalog
    section they never asked for."""
    called = []
    monkeypatch.setattr(main_agent, '_call_mcp_tool',
                        lambda *a, **kw: called.append(a) or CATALOG)

    agent = FakeAgent()  # no _preload_mcp_tool
    assert main_agent.preload_mcp_context(agent, ENTRIES) is False
    assert called == []
    assert agent.sections == []


def test_an_empty_catalog_adds_no_section(monkeypatch):
    """Better to say nothing than to publish 'we sell: (nothing)' as truth."""
    monkeypatch.setattr(main_agent, '_call_mcp_tool',
                        lambda *a, **kw: {'found': False, 'products': []})
    agent = FakeAgent(_preload_mcp_tool='list_products')

    assert main_agent.preload_mcp_context(agent, ENTRIES) is False
    assert agent.sections == []


def test_the_gateway_is_not_hit_once_per_call(monkeypatch):
    """This runs inside the SWML render path. The catalog changes rarely and
    every call would otherwise pay a round trip."""
    calls = []

    def _counted(*_args, **_kwargs):
        calls.append(1)
        return CATALOG

    monkeypatch.setattr(main_agent, '_call_mcp_tool', _counted)
    for _ in range(3):
        main_agent.preload_mcp_context(
            FakeAgent(_preload_mcp_tool='list_products'), ENTRIES,
        )

    assert len(calls) == 1


def test_sales_specialist_actually_opts_in():
    """The wiring itself. Without this, every test above passes against a
    FakeAgent while the live specialist declares nothing and gets nothing —
    which is precisely the failure being fixed."""
    from main_agent import SalesAISpecialist

    agent = SalesAISpecialist()

    assert agent._preload_mcp_tool == 'list_products'
    assert agent._preload_section_title == 'Product Catalog'
