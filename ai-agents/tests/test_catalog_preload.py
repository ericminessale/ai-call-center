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
import time

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


def test_two_tenants_on_one_gateway_do_not_share_a_catalog(monkeypatch):
    """This process serves every workspace. Keying the cache on (url, tool)
    alone served whichever tenant rendered first to both — one tenant's
    product list inside another tenant's prompt."""
    tenant_a = [{'id': 1, 'name': 'Shop', 'config': {
        'gateway_url': 'https://gw.example', 'auth_user': 'a',
        'auth_password': 'secret-a'}}]
    tenant_b = [{'id': 2, 'name': 'Shop', 'config': {
        'gateway_url': 'https://gw.example', 'auth_user': 'b',
        'auth_password': 'secret-b'}}]

    catalogs = {
        'a': {'products': [{'name': 'Tenant A Widget', 'price': '$1.00'}]},
        'b': {'products': [{'name': 'Tenant B Gadget', 'price': '$2.00'}]},
    }
    monkeypatch.setattr(main_agent, '_call_mcp_tool',
                        lambda config, *a, **kw: catalogs[config['auth_user']])

    agent_a = FakeAgent(_preload_mcp_tool='list_products')
    agent_b = FakeAgent(_preload_mcp_tool='list_products')
    main_agent.preload_mcp_context(agent_a, tenant_a)
    main_agent.preload_mcp_context(agent_b, tenant_b)

    assert 'Tenant A Widget' in agent_a.sections[0][1]
    assert 'Tenant B Gadget' in agent_b.sections[0][1]
    assert 'Tenant A Widget' not in agent_b.sections[0][1]


def test_the_cache_key_does_not_store_the_password():
    """It is process-global and long-lived; a cache key is not a place to
    keep a credential."""
    key = main_agent._preload_cache_key(
        {'id': 1, 'name': 'Shop'},
        {'gateway_url': 'https://gw.example', 'auth_user': 'u',
         'auth_password': 'hunter2'},
        'list_products',
    )

    assert 'hunter2' not in repr(key)


def test_a_failing_gateway_is_not_retried_on_every_render(monkeypatch):
    """The skill's own health check already paid one timeout. Re-probing here
    on every sales call adds a second one to each render."""
    calls = []

    def _boom(*_args, **_kwargs):
        calls.append(1)
        raise RuntimeError('timeout')

    monkeypatch.setattr(main_agent, '_call_mcp_tool', _boom)
    for _ in range(4):
        main_agent.preload_mcp_context(
            FakeAgent(_preload_mcp_tool='list_products'), ENTRIES,
        )

    assert len(calls) == 1, 'failures must be cached like successes'


def test_only_successfully_attached_gateways_are_preloaded():
    """attach_mcp_gateways returns what it ATTACHED, not what was configured
    — otherwise preload re-probes exactly the gateways that just failed."""
    class Agent:
        _mcp_agent_id = 'sales-ai'

        def add_skill(self, _name, config):
            if 'dead' in config['gateway_url']:
                raise RuntimeError('connection refused')

    entries = [
        {'name': 'dead', 'config': {'gateway_url': 'http://dead:8100'}},
        {'name': 'live', 'config': {'gateway_url': 'http://live:8100'}},
    ]
    main_agent._mcp_setup_failures.clear()

    attached = main_agent.attach_mcp_gateways(
        Agent(), {'workspace_id': 1, 'mcp_gateways': [
            dict(e, bound_agent_ids=['sales-ai']) for e in entries
        ]},
    )

    assert [e['name'] for e in attached] == ['live']


@pytest.mark.parametrize('services,expected', [
    (['catalog'], 'catalog'),
    ([{'name': 'catalog', 'tools': ['list_products']}], 'catalog'),
])
def test_both_documented_service_shapes_resolve(services, expected):
    """services_filter stores bare strings OR {name, tools} objects. The
    object form was interpolated into the URL as a stringified dict, so
    filtered configs attached tools fine and silently lost preloading."""
    assert main_agent._resolve_service_for_tool(
        {'gateway_url': 'https://gw.example', 'services': services},
        'list_products',
    ) == expected


def test_the_service_is_discovered_when_none_is_configured(monkeypatch):
    """Assuming 'demoshop' silently disabled preloading for every cloner
    running their own MCP server under any other name."""
    import requests

    class Resp:
        ok = True

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self):
            pass

    def _get(url, **_kwargs):
        if url.endswith('/services'):
            return Resp({'inventory': {}, 'catalog': {}})
        if url.endswith('/catalog/tools'):
            return Resp({'tools': [{'name': 'list_products'}]})
        return Resp({'tools': [{'name': 'something_else'}]})

    monkeypatch.setattr(requests, 'get', _get)

    assert main_agent._resolve_service_for_tool(
        {'gateway_url': 'https://gw.example'}, 'list_products',
    ) == 'catalog'


def test_sales_specialist_actually_opts_in():
    """The wiring itself. Without this, every test above passes against a
    FakeAgent while the live specialist declares nothing and gets nothing —
    which is precisely the failure being fixed."""
    from main_agent import SalesAISpecialist

    agent = SalesAISpecialist()

    assert agent._preload_mcp_tool == 'list_products'
    assert agent._preload_section_title == 'Product Catalog'


# ---------------------------------------------------------------------------
# Second audit round.
# ---------------------------------------------------------------------------

def test_bare_string_services_reach_the_sdk_as_objects():
    """The SDK's register_tools calls service_config.get("name") on every
    entry, so a bare string — a shape our own config explicitly accepts —
    raises, the gateway is recorded as failed, and the agent loses its MCP
    tools AND this preload. Normalization has to happen before add_skill, not
    only inside the preload resolver."""
    seen = {}

    class Agent:
        _mcp_agent_id = 'sales-ai'

        def add_skill(self, _name, config):
            seen['services'] = config.get('services')

    main_agent._mcp_setup_failures.clear()
    main_agent.attach_mcp_gateways(Agent(), {'workspace_id': 1, 'mcp_gateways': [{
        'name': 'Shop', 'bound_agent_ids': ['sales-ai'],
        'config': {'gateway_url': 'http://gw:8100', 'services': ['catalog']},
    }]})

    assert seen['services'] == [{'name': 'catalog', 'tools': '*'}]


def test_the_operator_tool_filter_is_not_bypassed():
    """A gateway exposing a tool is not permission to call it. If the filter
    says this agent may only use lookup_order, preloading list_products walks
    straight past that decision."""
    excluded = {'gateway_url': 'https://gw.example',
                'services': [{'name': 'catalog', 'tools': ['lookup_order']}]}
    permitted = {'gateway_url': 'https://gw.example',
                 'services': [{'name': 'catalog', 'tools': ['list_products']}]}

    assert main_agent._resolve_service_for_tool(excluded, 'list_products') is None
    assert main_agent._resolve_service_for_tool(permitted, 'list_products') == 'catalog'


def test_the_cache_key_tracks_the_tool_filter():
    """Two configs differing only in what they permit are not the same answer."""
    base = {'gateway_url': 'https://gw.example'}
    a = main_agent._preload_cache_key(
        {'id': 1}, dict(base, services=[{'name': 'catalog', 'tools': ['list_products']}]),
        'list_products')
    b = main_agent._preload_cache_key(
        {'id': 1}, dict(base, services=[{'name': 'catalog', 'tools': ['lookup_order']}]),
        'list_products')

    assert a != b


def test_concurrent_misses_make_one_gateway_call(monkeypatch):
    """The lock covered lookup and storage but not the request between them,
    so every simultaneous miss made its own call. Under call-center
    concurrency that is N timeouts, not one."""
    import threading
    import time as _time

    calls = []
    barrier = threading.Barrier(4)

    def slow(*_args, **_kwargs):
        calls.append(1)
        _time.sleep(0.3)
        return CATALOG

    monkeypatch.setattr(main_agent, '_call_mcp_tool', slow)
    main_agent._preload_inflight.clear()

    def worker():
        barrier.wait()
        main_agent.preload_mcp_context(
            FakeAgent(_preload_mcp_tool='list_products'), ENTRIES)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1


def test_an_unexpected_tool_shape_does_not_wedge_the_key(monkeypatch):
    """The formatter ran outside the handler, so a custom preload tool
    answering with an unexpected shape raised after ownership was claimed —
    leaving the event unset and the key in _preload_inflight forever. Every
    later render then waited the full timeout and repeated the failure."""
    monkeypatch.setattr(main_agent, '_call_mcp_tool', lambda *a, **kw: [1, 2, 3])
    main_agent._preload_inflight.clear()

    assert main_agent.preload_mcp_context(
        FakeAgent(_preload_mcp_tool='list_products'), ENTRIES) is False

    assert main_agent._preload_inflight == {}, 'in-flight key must be released'
    # ...and the failure is cached, so the next render doesn't repeat it.
    calls = []
    monkeypatch.setattr(main_agent, '_call_mcp_tool',
                        lambda *a, **kw: calls.append(1) or CATALOG)
    main_agent.preload_mcp_context(
        FakeAgent(_preload_mcp_tool='list_products'), ENTRIES)
    assert calls == []


def test_a_timed_out_follower_goes_without_rather_than_refetching(monkeypatch):
    """Service discovery can issue several sequential requests, so a valid
    leader may outlive one request_timeout. A follower that then starts its
    own fetch becomes a second owner and rebuilds the pile-up single-flight
    exists to prevent. Preloading is optional; going without is correct."""
    import threading

    calls = []
    release = threading.Event()

    def _slow(*_args, **_kwargs):
        calls.append(1)
        release.wait(timeout=5)
        return CATALOG

    monkeypatch.setattr(main_agent, '_call_mcp_tool', _slow)
    main_agent._preload_cache.clear()
    main_agent._preload_inflight.clear()

    # Leader takes the key and stalls past the follower's patience.
    entries = [{'id': 1, 'name': 'Shop', 'config': {
        'gateway_url': 'http://gw:8100', 'request_timeout': 0}}]
    leader = threading.Thread(
        target=main_agent.preload_mcp_context,
        args=(FakeAgent(_preload_mcp_tool='list_products'), entries))
    leader.start()
    time.sleep(0.2)

    follower = FakeAgent(_preload_mcp_tool='list_products')
    assert main_agent.preload_mcp_context(follower, entries) is False
    assert follower.sections == []
    assert len(calls) == 1, 'the follower must not start a second fetch'

    release.set()
    leader.join(timeout=5)
