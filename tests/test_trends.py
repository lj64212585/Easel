"""热榜响应解析、访客 Cookie、来源回退和 API 缓存契约；不访问外部站点。"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web"))

import app as web
from easel import trend_sources as sources


@pytest.mark.parametrize("platform,payload,expected", [
    ("douyin", {"data": {"word_list": [
        None, {"word": "缺少话题 ID"},
        {"sentence_id": "123", "word": " 抖音话题 ", "hot_value": 12345},
    ]}}, {"title": "抖音话题", "url": "https://www.douyin.com/hot/123", "hot": "12345"}),
    ("zhihu", {"data": [None, {"target": None}, {"target": {
        "title_area": {"text": "知乎问题"}, "metrics_area": {"text": "123 万热度"},
        "link": {"url": "https://www.zhihu.com/question/456"},
    }}]}, {"title": "知乎问题", "url": "https://www.zhihu.com/question/456", "hot": "123 万热度"}),
    ("bilibili", {"list": [
        {"keyword": "A&B 视频", "show_name": "B站话题", "heat_score": 456},
    ]}, {"title": "B站话题", "url": "https://search.bilibili.com/all?keyword=A%26B%20%E8%A7%86%E9%A2%91", "hot": "456"}),
    ("toutiao", {"data": [
        {"ClusterIdStr": "7890123456789012345", "Title": "头条话题", "HotValue": "789"},
    ]}, {"title": "头条话题", "url": "https://www.toutiao.com/trending/7890123456789012345/", "hot": "789"}),
])
def test_json_platforms_keep_titles_heat_and_urls(platform, payload, expected):
    assert sources.parse_direct_trends(platform, json.dumps(payload)) == [expected]


def test_weibo_scopes_table_skips_pinned_ads_and_reads_nested_titles():
    outside = '<table><tr><td class="td-01">1</td><td class="td-02"><a href="/weibo?q=bad">无关</a></td></tr></table>'
    html = outside + '''<div id="pl_top_realtimehot"><div><table><tbody>
      <tr><td class="td-01">置顶</td><td class="td-02"><a href="/weibo?q=pinned">置顶新闻</a></td></tr>
      <tr><td class="td-01 ranktop">1</td><td class="td-02">
        <a href="javascript:void(0);">广告</a>
        <a href="/weibo?q=topic&amp;Refer=top"><em>微博</em> &amp; 话题</a><span> 98765 </span>
      </td><td class="td-03">热</td></tr>
      <tr><td class="td-01">广告</td><td class="td-02"><a href="/weibo?q=ad">广告</a></td></tr>
      <tr><td class="td-01">2</td><td class="td-02"><a href="javascript:alert(1)">无效</a></td></tr>
    </tbody></table></div></div>''' + outside
    assert sources.parse_direct_trends("weibo", html) == [{
        "title": "微博 & 话题", "url": "https://s.weibo.com/weibo?q=topic&Refer=top", "hot": "98765",
    }]


def test_baidu_embedded_json_skips_pinned_and_retains_heat():
    data = {"data": {"cards": [{"content": [
        {"isTop": True, "word": "置顶", "rawUrl": "https://example.com/pinned"},
        {"word": "百度话题", "rawUrl": "https://example.com/news", "hotScore": "45678"},
    ]}]}}
    html = '<html><!--s-data:\n' + json.dumps(data) + '\n--></html>'
    assert sources.parse_direct_trends("baidu", html) == [{
        "title": "百度话题", "url": "https://example.com/news", "hot": "45678",
    }]


@pytest.mark.parametrize("platform", sources.DIRECT_URLS)
def test_unrecognized_responses_produce_no_fake_news(platform):
    text = '<html>请完成验证</html>' if platform in ("weibo", "baidu") else '{"data": null, "code": -1}'
    assert sources.parse_direct_trends(platform, text) == []


def test_malformed_json_is_a_fetch_failure():
    with pytest.raises(ValueError):
        sources.parse_direct_trends("douyin", '<html>登录</html>')


def test_duplicate_missing_titles_unsafe_urls_and_missing_heat():
    rows = [{"target": {"title_area": {"text": title}, "link": {"url": url}}}
            for title, url in [(None, "https://example.com"), ("  ", ""),
                               ("话题", "javascript:alert(1)"), ("话题", "https://example.com")]]
    assert sources.parse_direct_trends("zhihu", json.dumps({"data": rows})) == [
        {"title": "话题", "url": "", "hot": ""}]


def test_douyin_uses_bootstrap_cookie_in_same_request_session(monkeypatch):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.path, self.headers.get("Cookie")))
            self.send_response(200)
            if self.path == "/bootstrap":
                self.send_header("Set-Cookie", "guest=fixture; Path=/")
            self.end_headers()
            if self.path == "/hot":
                self.wfile.write(b'{"data":{"word_list":[{"sentence_id":"123","word":"topic"}]}}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.delenv("EASEL_PROXY", raising=False)
    monkeypatch.setattr(sources, "DOUYIN_BOOTSTRAP_URL", base + "/bootstrap")
    monkeypatch.setitem(sources.DIRECT_URLS, "douyin", base + "/hot")
    try:
        assert sources.fetch_direct_trends("douyin")[0]["title"] == "topic"
        assert received == [("/bootstrap", None), ("/hot", "guest=fixture")]
        assert sources.fetch_direct_trends("douyin")[0]["title"] == "topic"
        assert received[2:] == [("/bootstrap", None), ("/hot", "guest=fixture")]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_explicit_proxy_overrides_environment(monkeypatch):
    monkeypatch.setenv("EASEL_PROXY", "http://127.0.0.1:18888")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:19999")
    opener = sources.make_opener()
    handler = next(h for h in opener.handlers if isinstance(h, sources.urllib.request.ProxyHandler))
    assert handler.proxies == {"http": "http://127.0.0.1:18888", "https": "http://127.0.0.1:18888"}


@pytest.fixture
def trend_env(monkeypatch):
    monkeypatch.setattr(web, "_TREND_CACHE", {})
    aggregate = Mock()
    direct = Mock(return_value=[{"title": "直抓", "url": "https://example.com", "hot": "10"}])
    monkeypatch.setattr(web, "_http_get_json", aggregate)
    monkeypatch.setattr(web, "fetch_direct_trends", direct)
    return aggregate, direct


@pytest.mark.parametrize("backup", [False, True])
def test_aggregate_success_does_not_request_direct_source(trend_env, backup):
    aggregate, direct = trend_env
    success = {"data": [{"title": "聚合", "url": "https://example.com"}]}
    aggregate.side_effect = [TimeoutError(), success] if backup else [success]
    assert web._fetch_platform("weibo")[0]["title"] == "聚合"
    assert aggregate.call_count == (2 if backup else 1)
    direct.assert_not_called()


@pytest.mark.parametrize("responses", [
    [TimeoutError(), {"data": []}],
    [{"data": []}, {"data": []}],
    [{"data": "unexpected"}, ValueError("not json")],
])
def test_failed_or_empty_aggregates_fall_back_to_direct(trend_env, responses):
    aggregate, direct = trend_env
    aggregate.side_effect = responses
    assert web._fetch_platform("bilibili")[0]["title"] == "直抓"
    assert [call.args[0] for call in aggregate.call_args_list] == list(web.TREND_SOURCES["bilibili"])
    direct.assert_called_once_with("bilibili")


@pytest.mark.parametrize("cached", [False, True])
def test_failed_sources_preserve_old_cache_without_refreshing_timestamp(trend_env, cached):
    aggregate, direct = trend_env
    aggregate.side_effect = TimeoutError()
    direct.side_effect = ValueError("upstream changed")
    old = [{"title": "历史", "url": "https://example.com", "hot": "1"}]
    if cached:
        web._TREND_CACHE["weibo"] = (1, old)
    response = TestClient(web.app).get("/api/trends?platforms=weibo")
    assert response.status_code == 200
    assert response.json()["trends"][0]["items"] == (old if cached else [])
    assert web._TREND_CACHE == ({"weibo": (1, old)} if cached else {})


def test_direct_fallback_is_cached_and_api_contract_is_unchanged(trend_env):
    aggregate, direct = trend_env
    aggregate.return_value = {"data": []}
    client = TestClient(web.app)
    first = client.get("/api/trends?platforms=bilibili").json()
    second = client.get("/api/trends?platforms=bilibili").json()
    assert set(first) == {"trends", "updated"}
    assert isinstance(first["updated"], int)
    assert first["trends"] == second["trends"] == [
        {"platform": "bilibili", "label": "B站", "items": direct.return_value}]
    direct.assert_called_once_with("bilibili")
    assert aggregate.call_count == 2


def test_expired_cache_is_replaced_on_success(trend_env):
    aggregate, direct = trend_env
    aggregate.return_value = {"data": []}
    web._TREND_CACHE["zhihu"] = (1, [])
    data = TestClient(web.app).get("/api/trends?platforms=zhihu").json()
    assert data["trends"][0]["items"] == direct.return_value
    assert web._TREND_CACHE["zhihu"][0] > 1


def test_sources_load_concurrently_preserving_selection_order(monkeypatch, trend_env):
    barrier = threading.Barrier(3, timeout=3)

    def fetch(platform):
        barrier.wait()  # 顺序执行会超时，所有平台必须已进入采集。
        return [{"title": platform, "url": "", "hot": ""}] * 40

    monkeypatch.setattr(web, "_fetch_platform", fetch)
    data = TestClient(web.app).get(
        "/api/trends?platforms=toutiao,zhihu,bilibili,zhihu,unknown&limit=99").json()
    assert [group["platform"] for group in data["trends"]] == ["toutiao", "zhihu", "bilibili"]
    assert all(len(group["items"]) == 30 for group in data["trends"])


def test_unknown_platforms_do_not_trigger_requests(trend_env):
    aggregate, direct = trend_env
    assert web._fetch_platform("unknown") == []
    assert TestClient(web.app).get("/api/trends?platforms=unknown").json()["trends"] == []
    aggregate.assert_not_called()
    direct.assert_not_called()
