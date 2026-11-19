"""Tests for the experimental web search and page reading tools. All offline:
the network calls are replaced with stand-ins."""

import io
import json
import os
import socket
import subprocess
import sys
import urllib.error
from email.message import Message
from pathlib import Path

import pytest

import web_tools

AGENT_DIR = Path(__file__).resolve().parents[1]

DDG_PAGE = """
<html><body>
<div class="result results_links results_links_deep web-result">
  <h2 class="result__title"><a rel="nofollow" class="result__a"
     href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fguide.cryosparc.com%2Fprocessing%2Ddata%2Fnu%2Drefine&amp;rut=abc">Non-uniform <b>Refinement</b> - CryoSPARC Guide</a></h2>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fguide.cryosparc.com">Non-uniform refinement <b>improves</b> maps of membrane proteins.</a>
</div>
<div class="result results_links web-result result--ad">
  <h2 class="result__title"><a class="result__a" href="https://duckduckgo.com/y.js?ad_provider=x">An advert</a></h2>
</div>
<div class="result web-result">
  <h2 class="result__title"><a class="result__a" href="https://www.nature.com/articles/s41592-020-00990-8">Punjani et al. 2020</a></h2>
  <a class="result__snippet">Adaptive regularization.</a>
</div>
</body></html>
"""


class FakeResponse(io.BytesIO):
    def __init__(self, body, content_type="text/html; charset=utf-8", url="https://example.org/", status=200):
        super().__init__(body if isinstance(body, bytes) else body.encode())
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self._url = url
        self.status = status

    def geturl(self):
        return self._url


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Every host resolves to a public address unless a test says otherwise."""

    web_tools._pages.clear()
    monkeypatch.setattr(web_tools, "SEARCH_SPACING", 0)
    monkeypatch.setattr(
        web_tools.socket, "getaddrinfo", lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
    )


def test_duckduckgo_results_unwrap_links_and_skip_ads():
    results = web_tools.parse_duckduckgo(DDG_PAGE)
    assert results == [
        {
            "title": "Non-uniform Refinement - CryoSPARC Guide",
            "url": "https://guide.cryosparc.com/processing-data/nu-refine",
            "snippet": "Non-uniform refinement improves maps of membrane proteins.",
        },
        {"title": "Punjani et al. 2020", "url": "https://www.nature.com/articles/s41592-020-00990-8", "snippet": "Adaptive regularization."},
    ]


def test_web_search_sends_the_query_and_marks_results_untrusted(monkeypatch):
    sent = []

    def fake_open(request):
        sent.append(request)
        return FakeResponse(DDG_PAGE)

    monkeypatch.setattr(web_tools, "_open", fake_open)
    result = web_tools.call("web_search", {"query": "  non-uniform   refinement ", "max_results": 1})
    assert result["ok"] and result["provider"] == "DuckDuckGo" and len(result["results"]) == 1
    assert "untrusted" in result["note"]
    assert sent[0].full_url == web_tools.SEARCH_URL and b"q=non-uniform+refinement" in sent[0].data
    assert sent[0].get_header("User-agent").startswith("CryoPilot")


def test_a_blocked_search_explains_what_to_do(monkeypatch):
    monkeypatch.setattr(web_tools, "_open", lambda request: FakeResponse("<html>anomaly detected</html>", status=202))
    result = web_tools.web_search("cryosparc")
    assert not result["ok"] and "source='papers'" in result["error"]


def test_paper_search_reads_europe_pmc(monkeypatch):
    payload = {
        "resultList": {
            "result": [
                {"id": "123", "source": "MED", "pmid": "123", "pmcid": "PMC9", "doi": "10.1/x", "title": "A  map", "authorString": "A B, C D.",
                 "journalTitle": "Nat Methods", "pubYear": "2020", "isOpenAccess": "Y"},
                {"id": "PPR1", "source": "PPR", "title": "A preprint", "isOpenAccess": "N"},
            ]
        }
    }
    seen = []

    def fake_open(request):
        seen.append(request.full_url)
        return FakeResponse(json.dumps(payload), content_type="application/json")

    monkeypatch.setattr(web_tools, "_open", fake_open)
    result = web_tools.web_search("non-uniform refinement", source="papers")
    assert result["ok"] and result["provider"] == "Europe PMC"
    first, second = result["results"]
    assert first["url"] == "https://doi.org/10.1/x" and first["title"] == "A map" and first["year"] == "2020"
    assert first["full_text_url"].endswith("/PMC9/fullTextXML")
    assert second["url"] == "https://europepmc.org/article/PPR/PPR1" and "full_text_url" not in second
    assert "query=non-uniform+refinement" in seen[0]


def test_bad_queries_and_sources_are_refused():
    assert not web_tools.web_search("   ")["ok"]
    assert not web_tools.web_search("x", source="images")["ok"]


def test_fetch_turns_html_into_text_and_pages_through_it(monkeypatch):
    markup = (
        "<html><head><title>NU Refinement</title><style>.x{}</style><script>alert(1)</script></head>"
        "<body><nav><a href='/home'>Home</a></nav><h2>Parameters</h2><p>Set the  <b>dynamic mask</b>.</p>"
        "<ul><li>One</li><li>Two</li></ul><pre>a  b\n c</pre>" + "<p>" + "word " * 3000 + "</p></body></html>"
    )
    calls = []

    def fake_open(request):
        calls.append(request.full_url)
        return FakeResponse(markup, url="https://guide.cryosparc.com/nu")

    monkeypatch.setattr(web_tools, "_open", fake_open)
    first = web_tools.web_fetch("https://guide.cryosparc.com/nu", max_chars=1000, links=True)
    assert first["ok"] and first["title"] == "NU Refinement" and "untrusted" in first["note"]
    assert first["text"].startswith("Home\n\n## Parameters\n\nSet the dynamic mask.")
    assert "- One" in first["text"] and "a  b\n c" in first["text"]
    assert "alert" not in first["text"] and ".x{}" not in first["text"]
    assert first["links"] == [{"text": "Home", "url": "https://guide.cryosparc.com/home"}]
    assert first["end"] == 1000 and first["next_start"] == 1000
    second = web_tools.web_fetch("https://guide.cryosparc.com/nu", start=first["next_start"], max_chars=50_000)
    assert second["start"] == 1000 and second["end"] == second["total_chars"] and "next_start" not in second
    # The second part comes from the page already read.
    assert len(calls) == 1


def test_europe_pmc_section_titles_become_headings():
    page = web_tools.html_to_text("<article><front><article-title>Paper</article-title></front><sec><title>Methods</title><p>We did it.</p></sec></article>", xml=True)
    assert page["title"] == "Paper"
    assert "## Methods" in page["text"] and "We did it." in page["text"]


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.5", "192.168.1.20", "172.18.132.6", "169.254.169.254", "::1", "fd00::1"]
)
def test_fetch_refuses_this_computer_and_the_local_network(monkeypatch, address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    monkeypatch.setattr(web_tools.socket, "getaddrinfo", lambda host, port, **kw: [(family, socket.SOCK_STREAM, 6, "", (address, port))])
    monkeypatch.setattr(web_tools, "_open", lambda request: pytest.fail("must not connect"))
    result = web_tools.web_fetch("http://cryosparc.lab:61000/api")
    assert not result["ok"] and "local network" in result["error"]


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.org/x", "https://user:pw@example.org/", "not a url"])
def test_fetch_refuses_other_kinds_of_address(url):
    assert not web_tools.web_fetch(url)["ok"]


def test_redirects_to_the_local_network_are_refused(monkeypatch):
    def resolve(host, port, **kw):
        address = "127.0.0.1" if host == "localhost" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(web_tools.socket, "getaddrinfo", resolve)
    handler = web_tools._CheckedRedirects()
    request = web_tools.urllib.request.Request("https://example.org/start")
    with pytest.raises(web_tools.WebToolError):
        handler.redirect_request(request, None, 302, "Found", {}, "http://localhost:39000/")
    assert handler.redirect_request(request, None, 302, "Found", {}, "/next") is not None


def test_pdfs_and_errors_come_back_as_messages(monkeypatch):
    monkeypatch.setattr(web_tools, "_open", lambda request: FakeResponse(b"%PDF-1.7 ...", content_type="application/pdf"))
    pdf = web_tools.web_fetch("https://example.org/paper.pdf")
    assert not pdf["ok"] and "full_text_url" in pdf["error"]

    def not_found(request):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", Message(), None)

    monkeypatch.setattr(web_tools, "_open", not_found)
    missing = web_tools.web_fetch("https://example.org/missing")
    assert not missing["ok"] and "404" in missing["error"]


def test_plain_text_and_json_are_returned_as_they_are(monkeypatch):
    monkeypatch.setattr(web_tools, "_open", lambda request: FakeResponse('{"a": 1}', content_type="application/json"))
    assert web_tools.web_fetch("https://example.org/data.json")["text"] == '{"a": 1}'


def _harness_tools(flag):
    env = {key: value for key, value in os.environ.items() if key != web_tools.ENV_FLAG}
    if flag is not None:
        env[web_tools.ENV_FLAG] = flag
    code = "import json, harness; print(json.dumps([t['name'] for t in harness.TOOLS]))"
    output = subprocess.run([sys.executable, "-c", code], cwd=AGENT_DIR, env=env, capture_output=True, text=True, check=True).stdout
    return json.loads(output.strip().splitlines()[-1])


def test_the_harness_offers_the_tools_only_when_turned_on():
    assert _harness_tools(None) == ["run_code", "run_and_wait", "view_image"]
    assert _harness_tools("0") == ["run_code", "run_and_wait", "view_image"]
    assert _harness_tools("1") == ["run_code", "run_and_wait", "view_image", "web_search", "web_fetch"]


def test_the_harness_refuses_web_tools_when_they_are_off(monkeypatch):
    import harness

    monkeypatch.setattr(harness, "WEB_TOOLS_ENABLED", False)
    output, _ = harness.call_tool({"name": "web_fetch", "call_id": "c1", "arguments": json.dumps({"url": "https://example.org"})})
    assert json.loads(output["output"]) == {"ok": False, "error": "Unknown tool: web_fetch"}


def test_menus_stay_out_when_the_page_has_a_main_part():
    menu = "<nav>" + "".join(f"<a href='/p{i}'>Menu item {i}</a>" for i in range(50)) + "</nav>"
    page = web_tools.html_to_text(f"<body>{menu}<main><h1>Job</h1><p>{'Real content. ' * 200}</p></main></body>", "https://x.org/")
    assert page["text"].startswith("# Job") and "Menu item" not in page["text"]
    assert len(page["links"]) == 50
    # A small <main> wrapper does not hide the rest of the page.
    small = web_tools.html_to_text(f"<body><main><p>Cookie banner</p></main><div>{'Body text. ' * 300}</div></body>")
    assert "Body text." in small["text"]


def test_article_xml_keeps_the_abstract_and_body_only():
    xml = (
        "<?xml version='1.0'?><article><front><journal-meta><journal-title>Journal</journal-title></journal-meta>"
        "<article-meta><article-id>PMC9</article-id><title-group><article-title>A map</article-title></title-group>"
        "<abstract><p>We solved it.</p></abstract></article-meta></front>"
        "<body><sec><title>Methods</title><p>Particles were picked.</p></sec></body></article>"
    )
    page = web_tools.html_to_text(xml, xml=True)
    assert page["title"] == "A map"
    assert page["text"] == "## Abstract\n\nWe solved it.\n\n## Methods\n\nParticles were picked."
