"""平台热榜直抓，作为聚合接口的备用来源。

仅使用 Python 标准库，不保存站点 Cookie。
"""
from __future__ import annotations

from html.parser import HTMLParser
from http.cookiejar import CookieJar
import json
import os
import re
import urllib.parse
import urllib.request


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
DOUYIN_BOOTSTRAP_URL = "https://login.douyin.com/"
DIRECT_URLS = {
    "weibo": "https://s.weibo.com/top/summary?cate=realtimehot",
    "douyin": (
        "https://www.douyin.com/aweme/v1/web/hot/search/list/"
        "?device_platform=webapp&aid=6383&channel=channel_pc_web&detail_list=1"
    ),
    "zhihu": "https://www.zhihu.com/api/v3/feed/topstory/hot-list-web?limit=20&desktop=true",
    "bilibili": "https://s.search.bilibili.com/main/hotword?limit=30",
    "baidu": "https://top.baidu.com/board?tab=realtime",
    "toutiao": "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
}


def make_opener():
    """每次采集独立 CookieJar；尊重 EASEL_PROXY 或系统代理环境变量。"""
    proxy = os.environ.get("EASEL_PROXY", "").strip()
    proxy_handler = (urllib.request.ProxyHandler({"http": proxy, "https": proxy})
                     if proxy else urllib.request.ProxyHandler())
    return urllib.request.build_opener(
        proxy_handler, urllib.request.HTTPCookieProcessor(CookieJar()))


def _get_text(opener, url: str, timeout: int, headers: dict | None = None) -> str:
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Referer": url, **(headers or {}),
    })
    with opener.open(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def _rows(value) -> list[dict]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _get(value, *keys):
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _item(title, url, hot=None) -> dict:
    return {"title": title, "url": url, "hot": hot}


class _WeiboParser(HTMLParser):
    """只读取热榜容器内有数字排名的行，跳过置顶、广告与无效链接。"""

    def __init__(self):
        super().__init__()
        self.items: list[dict] = []
        self._depth = 0
        self._row: dict | None = None
        self._cell = ""
        self._anchor = False
        self._heat = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div":
            if self._depth:
                self._depth += 1
            elif attrs.get("id") == "pl_top_realtimehot":
                self._depth = 1
        if not self._depth:
            return
        if tag == "tr":
            self._row = {"rank": "", "title": "", "url": "", "hot": ""}
        elif self._row is not None:
            if tag == "td":
                classes = (attrs.get("class") or "").split()
                self._cell = next((c for c in classes if c in ("td-01", "td-02")), "")
            elif tag == "a" and self._cell == "td-02" and not self._row["url"]:
                href = attrs.get("href") or ""
                # 一行可能先有 javascript 广告链接，再有真实话题链接。
                if href.startswith(("/weibo?", "https://s.weibo.com/weibo?")):
                    self._row["url"] = urllib.parse.urljoin("https://s.weibo.com", href)
                    self._anchor = True
            elif tag == "span" and self._cell == "td-02" and not self._anchor:
                self._heat = True

    def handle_endtag(self, tag):
        if not self._depth:
            return
        if tag == "tr" and self._row is not None:
            if self._row.pop("rank").strip().isdigit() and self._row["url"]:
                self.items.append(self._row)
            self._row = None
            self._cell = ""
            self._anchor = self._heat = False
        elif tag == "td":
            self._cell = ""
            self._anchor = self._heat = False
        elif tag == "a":
            self._anchor = False
        elif tag == "span":
            self._heat = False
        elif tag == "div":
            self._depth -= 1

    def handle_data(self, data):
        if self._row is None:
            return
        if self._cell == "td-01":
            self._row["rank"] += data
        elif self._anchor:
            self._row["title"] += data
        elif self._heat:
            self._row["hot"] += data


def parse_direct_trends(platform: str, text: str) -> list[dict]:
    """解析站点原始响应为现有 TrendItem；空响应/校验页由调用方回退。"""
    items = []
    if platform == "weibo":
        parser = _WeiboParser()
        parser.feed(text)
        items = parser.items
    elif platform == "baidu":
        match = re.search(r"<!--s-data:(.*?)-->", text, re.DOTALL)
        if not match:
            return []
        cards = _rows(_get(json.loads(match.group(1)), "data", "cards"))
        for row in _rows(cards[0].get("content")) if cards else []:
            if not row.get("isTop"):
                items.append(_item(row.get("word"), row.get("rawUrl"), row.get("hotScore")))
    else:
        data = json.loads(text)
        if platform == "douyin":
            for row in _rows(_get(data, "data", "word_list")):
                if row.get("sentence_id"):
                    items.append(_item(row.get("word"),
                                       f"https://www.douyin.com/hot/{row['sentence_id']}",
                                       row.get("hot_value")))
        elif platform == "zhihu":
            for row in _rows(_get(data, "data")):
                target = row.get("target")
                items.append(_item(_get(target, "title_area", "text"),
                                   _get(target, "link", "url"),
                                   _get(target, "metrics_area", "text")))
        elif platform == "bilibili":
            for row in _rows(_get(data, "list")):
                if row.get("keyword"):
                    items.append(_item(row.get("show_name") or row["keyword"],
                                       "https://search.bilibili.com/all?keyword="
                                       + urllib.parse.quote(str(row["keyword"]), safe=""),
                                       row.get("heat_score")))
        elif platform == "toutiao":
            for row in _rows(_get(data, "data")):
                if row.get("ClusterIdStr"):
                    items.append(_item(row.get("Title"),
                                       f"https://www.toutiao.com/trending/{row['ClusterIdStr']}/",
                                       row.get("HotValue")))
        else:
            raise ValueError(f"Unsupported trend platform: {platform}")

    result = []
    seen = set()
    for item in items:
        title = item["title"]
        if not isinstance(title, str) or not title.strip():
            continue
        title = title.strip()
        if title in seen:
            continue
        url = item["url"] if isinstance(item["url"], str) else ""
        if not url.startswith(("https://", "http://")):
            url = ""
        hot = item["hot"]
        result.append({"title": title, "url": url,
                       "hot": str(hot).strip() if isinstance(hot, (str, int, float))
                       and not isinstance(hot, bool) else ""})
        seen.add(title)
    return result


def fetch_direct_trends(platform: str, timeout: int = 8) -> list[dict]:
    """执行单平台采集；网络异常交给上层统一记录并回退缓存。"""
    url = DIRECT_URLS[platform]
    opener = make_opener()
    headers = {}
    if platform == "douyin":
        # 使用本次访客请求收到的 Cookie，而非持久化或硬编码会话。
        _get_text(opener, DOUYIN_BOOTSTRAP_URL, timeout)
    elif platform == "weibo":
        # 仅接受用户自配，不复制第三方会话。
        cookie = os.environ.get("EASEL_WEIBO_COOKIE", "").strip()
        if cookie:
            headers["Cookie"] = cookie
    return parse_direct_trends(platform, _get_text(opener, url, timeout, headers))
