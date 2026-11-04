# -*- coding: utf-8 -*-
"""
MoviePilot v3 插件：Mikan Project（蜜柑计划）动漫资源源。

把 https://mikan.tangbai.cc 的动漫 RSS 搜索接入 MoviePilot：
- 使用 /RSS/Search?searchstr=关键词 获取动漫种子
- 解析种子标题、torrent 链接、大小、发布时间、集数、画质
- 支持按最新集数/最早集数/画质/大小排序；可配置返回数量

Mikan 的种子本身命名规范（字幕组+番名+集数+画质），不需要像 6v 那样重命名。
"""
from __future__ import annotations

import json
import os
import re
import time
import html as html_lib
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urljoin

import requests
from fastapi.concurrency import run_in_threadpool

from app.core.config import settings
from app.domain.context import TorrentInfo
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import MediaType

_FALLBACK_CONFIG = "/config/mikananime.json"
_MIKAN_NS = "{https://mikan.tangbai.cc/0.1/}"


class MikanAnime(_PluginBase):
    plugin_name = "MikanAnime"
    plugin_desc = "蜜柑计划（mikan.tangbai.cc）动漫资源源：RSS 搜索、种子、集数/画质排序"
    plugin_icon = "Mikan.png"
    plugin_version = "1.0.0"
    plugin_author = "pandaria2012"
    author_url = "https://github.com/pandaria2012/jackett_extend"
    plugin_label = ""
    plugin_config_prefix = "mikananime_"
    plugin_order = 19
    auth_level = 0

    _enabled = False
    _base_url = "https://mikan.tangbai.cc"
    _max_results = 50
    _page_max_rows = 100
    _sort_by = "newest"
    _fetch_full = False
    _full_dedupe = True
    _bangumi_limit = 3
    _timeout = 30
    _session: Optional[requests.Session] = None

    @staticmethod
    def _defaults() -> Dict[str, Any]:
        return {
            "enabled": False,
            "base_url": "https://mikan.tangbai.cc",
            "max_results": 50,
            "page_max_rows": 100,
            "sort_by": "newest",
            "fetch_full": False,
            "full_dedupe": True,
            "bangumi_limit": 3,
            "search_timeout": 30,
        }

    def init_plugin(self, config: Optional[Mapping[str, Any]] = None) -> None:
        saved = self.get_config() or {}
        values = {**self._defaults(), **saved, **dict(config or {})}
        if not saved:
            try:
                if os.path.exists(_FALLBACK_CONFIG):
                    with open(_FALLBACK_CONFIG, "r", encoding="utf-8") as fp:
                        file_values = json.load(fp)
                    if isinstance(file_values, dict):
                        values = {**file_values, **values}
                        self.update_config(dict(values))
            except Exception as err:  # noqa: BLE001
                logger.warning(f"【{self.plugin_name}】读取本地配置失败：{err}")

        base_url = str(values.get("base_url") or "").strip().rstrip("/")
        if base_url and "://" not in base_url:
            base_url = "https://" + base_url
        values["base_url"] = base_url or "https://mikan.tangbai.cc"
        try:
            values["max_results"] = max(1, min(int(values.get("max_results") or 50), 300))
        except (TypeError, ValueError):
            values["max_results"] = 50
        try:
            values["page_max_rows"] = max(1, min(int(values.get("page_max_rows") or 100), 300))
        except (TypeError, ValueError):
            values["page_max_rows"] = 100
        try:
            values["search_timeout"] = max(5, min(int(values.get("search_timeout") or 30), 120))
        except (TypeError, ValueError):
            values["search_timeout"] = 30
        sort_by = str(values.get("sort_by") or "newest").strip()
        if sort_by not in {"newest", "oldest", "resolution", "size"}:
            sort_by = "newest"
        values["sort_by"] = sort_by
        values["fetch_full"] = bool(values.get("fetch_full"))
        values["full_dedupe"] = bool(values.get("full_dedupe", True))
        try:
            values["bangumi_limit"] = max(1, min(int(values.get("bangumi_limit") or 3), 10))
        except (TypeError, ValueError):
            values["bangumi_limit"] = 3
        values["enabled"] = bool(values.get("enabled"))

        self._enabled = values["enabled"]
        self._base_url = values["base_url"]
        self._max_results = values["max_results"]
        self._page_max_rows = values["page_max_rows"]
        self._sort_by = values["sort_by"]
        self._fetch_full = values["fetch_full"]
        self._full_dedupe = values["full_dedupe"]
        self._bangumi_limit = values["bangumi_limit"]
        self._timeout = values["search_timeout"]
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": getattr(settings, "USER_AGENT", "MoviePilot"),
                "Referer": self._base_url + "/",
            }
        )
        self._site_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
        logger.info(
            f"【{self.plugin_name}】初始化：enabled={self._enabled} "
            f"base_url={self._base_url} sort_by={self._sort_by}"
        )

    def stop_service(self) -> None:
        self._enabled = False
        self._session = None

    def get_state(self) -> bool:
        return bool(self._enabled and self._base_url)

    # ------------------------------------------------------------------ module
    def get_module(self) -> Dict[str, Any]:
        return {
            "search_torrents": self.search_torrents,
            "async_search_torrents": self.async_search_torrents,
        }

    def get_service(self) -> List[Dict[str, Any]]:
        return []

    def get_command(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/test",
                "endpoint": self.api_test,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "测试 Mikan 连接",
            },
            {
                "path": "/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "查询 MikanAnime 状态",
            },
            {
                "path": "/results",
                "endpoint": self.api_results,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "查询最近一次搜索结果",
            },
        ]

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _normalize(value: str) -> str:
        value = (value or "").casefold()
        value = re.sub(r"[\s：:、，,。.！!？?\-—_/（）()\[\]【】+]+", "", value)
        return value

    @staticmethod
    def _resolution_rank(value: str) -> int:
        text = (value or "").lower()
        if "2160" in text or "4k" in text:
            return 3
        if "1080" in text:
            return 2
        if "720" in text:
            return 1
        return 0

    @staticmethod
    def _episode_number(title: str) -> int:
        if not title:
            return -1
        numbers: List[int] = []
        for pattern in (
            r"第\s*(\d{1,4})\s*[话話集]",
            r"S\d+E(\d{1,4})(?:\s*-\s*E?(\d{1,4}))?",
            r"(?<![\dA-Za-z])E[Pp]?\s*(\d{1,4})(?![\dA-Za-z])",
            r"\[(\d{1,4})(?:\s*-\s*(\d{1,4}))?(?:\s*[+＋]SP\d*)?\]",
            r"\s-\s*(\d{1,4})(?:\s*-\s*(\d{1,4}))?(?:\s|$)",
        ):
            for m in re.finditer(pattern, title):
                numbers.extend(int(g) for g in m.groups() if g and str(g).isdigit())
        return max(numbers) if numbers else -1

    @staticmethod
    def _parse_pubdate(value: str) -> Optional[str]:
        if not value:
            return None
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
            return parsed.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            match = re.search(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", text)
            return f"{match.group(1)} {match.group(2)}" if match else None

    @staticmethod
    def _parse_size(description: str, length: int = 0) -> int:
        if length > 0:
            return length
        match = re.search(r"\[([\d.]+)\s*(GB|GiB|MB|MiB|TB|TiB)\]", description or "", re.I)
        if not match:
            return 0
        try:
            value = float(match.group(1))
        except ValueError:
            return 0
        unit = match.group(2).lower()
        factor = {
            "tb": 1024 ** 4,
            "tib": 1024 ** 4,
            "gb": 1024 ** 3,
            "gib": 1024 ** 3,
            "mb": 1024 ** 2,
            "mib": 1024 ** 2,
        }.get(unit, 1)
        return int(value * factor)

    # ------------------------------------------------------------------ search
    def _fetch_rss(self, keyword: str) -> str:
        resp = self._session.get(
            self._base_url + "/RSS/Search",
            params={"searchstr": keyword},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.text or ""

    def _entries(self, keyword: str) -> List[Dict[str, Any]]:
        cache = getattr(self, "_site_cache", None)
        if cache is None:
            cache = self._site_cache = {}
        now = time.monotonic()
        cached = cache.get(keyword)
        if cached and now - cached[0] < 300:
            return list(cached[1])

        variants = [keyword.strip()]
        cleaned = re.sub(r"[：:、，,。.！!？?\-—_/]+", " ", keyword)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if cleaned and cleaned not in variants:
            variants.append(cleaned)

        entries: List[Dict[str, Any]] = []
        for attempt in range(2):
            for variant in variants:
                try:
                    payload = self._fetch_rss(variant)
                except Exception as err:  # noqa: BLE001
                    logger.warning(f"【{self.plugin_name}】Mikan RSS 请求失败：{err}")
                    payload = ""
                entries = self._parse_rss(payload)
                if entries:
                    cache[keyword] = (now, list(entries))
                    return entries
            if attempt == 0:
                time.sleep(2)
        return []

    @staticmethod
    def _strip_tags(value: str) -> str:
        return html_lib.unescape(re.sub(r"<[^>]+>", " ", value or "")).strip()

    def _bangumi_titles(self, keyword: str) -> List[Tuple[str, str]]:
        """用 Mikan HTML 搜索找到番剧条目（Bangumi ID + 番名）。"""
        resp = self._session.get(
            self._base_url + "/Home/Search",
            params={"searchstr": keyword},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        text = resp.text or ""
        norm_kw = self._normalize(keyword)
        found: List[Tuple[str, str]] = []
        seen = set()
        for match in re.finditer(
            r'<a[^>]+href=["\']?/Home/Bangumi/(\d+)[^>]*>(.*?)</a>',
            text,
            re.S | re.I,
        ):
            bangumi_id = match.group(1)
            block = match.group(2)
            if bangumi_id in seen:
                continue
            seen.add(bangumi_id)
            title_match = re.search(r'title=["\']([^"\']+)["\']', block)
            title = (
                html_lib.unescape(title_match.group(1)).strip()
                if title_match
                else self._strip_tags(block)
            )
            if not title:
                continue
            norm_title = self._normalize(title)
            if norm_kw and (norm_kw in norm_title or norm_title in norm_kw):
                found.append((bangumi_id, title))
        found.sort(key=lambda item: (0 if self._normalize(item[1]) == norm_kw else 1, len(item[1])))
        return found[: self._bangumi_limit]

    def _full_entries(self, keyword: str) -> List[Dict[str, Any]]:
        """按番剧全集抓取：找到匹配的 Bangumi 后拉取该番剧的完整 RSS。"""
        cache = getattr(self, "_site_cache", None)
        if cache is None:
            cache = self._site_cache = {}
        cache_key = "full:" + keyword
        now = time.monotonic()
        cached = cache.get(cache_key)
        if cached and now - cached[0] < 300:
            return list(cached[1])

        bangumis = self._bangumi_titles(keyword)
        if not bangumis:
            return self._entries(keyword)
        entries: List[Dict[str, Any]] = []
        seen = set()
        for bangumi_id, bangumi_title in bangumis:
            try:
                resp = self._session.get(
                    self._base_url + "/RSS/Bangumi",
                    params={"bangumiId": bangumi_id},
                    timeout=self._timeout,
                )
                resp.raise_for_status()
                payload = resp.text or ""
            except Exception as err:  # noqa: BLE001
                logger.warning(
                    f"【{self.plugin_name}】抓取番剧 {bangumi_title}({bangumi_id}) RSS 失败：{err}"
                )
                continue
            for item in self._parse_rss(payload):
                url = item.get("magnet")
                if not url or url in seen:
                    continue
                seen.add(url)
                entries.append(item)
        if entries and self._full_dedupe:
            # 整部番剧的 RSS 会包含同集数多个字幕组版本，默认每个集数只保留最佳版本
            best_by_episode: Dict[int, Dict[str, Any]] = {}
            batches: List[Dict[str, Any]] = []
            for item in entries:
                episode = int(item.get("episode") or -1)
                if episode < 0:
                    batches.append(item)
                    continue
                candidate_key = (
                    int(item.get("resolution") or 0),
                    item.get("pubdate") or "",
                    int(item.get("size") or 0),
                )
                current = best_by_episode.get(episode)
                if current is None:
                    best_by_episode[episode] = item
                    continue
                current_key = (
                    int(current.get("resolution") or 0),
                    current.get("pubdate") or "",
                    int(current.get("size") or 0),
                )
                if candidate_key > current_key:
                    best_by_episode[episode] = item
            if best_by_episode:
                batches.sort(
                    key=lambda item: (
                        -int(item.get("resolution") or 0),
                        -int(item.get("size") or 0),
                        item.get("pubdate") or "",
                    )
                )
                entries = list(best_by_episode.values()) + batches[:10]
        if entries:
            cache[cache_key] = (now, list(entries))
            return entries
        return self._entries(keyword)

    def _parse_rss(self, payload: str) -> List[Dict[str, Any]]:
        if not payload.strip():
            return []
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as err:
            logger.warning(f"【{self.plugin_name}】Mikan RSS 解析失败：{err}")
            return []
        channel = root.find("channel")
        if channel is None:
            return []
        entries: List[Dict[str, Any]] = []
        for item in channel.findall("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            description = (item.findtext("description") or "").strip()
            enclosure = item.find("enclosure")
            torrent_url = ""
            length = 0
            if enclosure is not None:
                torrent_url = (enclosure.get("url") or "").strip()
                try:
                    length = int(enclosure.get("length") or 0)
                except (TypeError, ValueError):
                    length = 0
            torrent_node = item.find(_MIKAN_NS + "torrent")
            pubdate = ""
            if torrent_node is not None:
                pubdate = (torrent_node.findtext(_MIKAN_NS + "pubDate") or "").strip()
            if not torrent_url:
                continue
            entries.append(
                {
                    "title": title,
                    "magnet": torrent_url,
                    "size": self._parse_size(description, length),
                    "page_url": link,
                    "pubdate": self._parse_pubdate(pubdate),
                    "episode": self._episode_number(title),
                    "resolution": self._resolution_rank(title),
                    "description": description,
                }
            )
        return entries

    def _sort_entries(self, entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        mode = self._sort_by

        def key(item: Dict[str, Any]) -> Any:
            episode = int(item.get("episode") or -1)
            resolution = int(item.get("resolution") or 0)
            size = int(item.get("size") or 0)
            pubdate = item.get("pubdate") or ""
            if mode == "oldest":
                return (0 if episode >= 0 else 1, episode if episode >= 0 else 10 ** 9, -resolution, pubdate)
            if mode == "resolution":
                return (-resolution, -episode, pubdate)
            if mode == "size":
                return (-size, -episode, pubdate)
            return (-episode, -resolution, pubdate)

        return sorted(entries, key=key)

    def _search(self, keyword: str, mtype: Any = None, page: int = 0) -> List[TorrentInfo]:
        if not self.get_state():
            return []
        entries = self._full_entries(keyword) if self._fetch_full else self._entries(keyword)
        if not entries:
            return []
        if not self._fetch_full:
            normalized = self._normalize(keyword)
            matched = [e for e in entries if normalized and normalized in self._normalize(e["title"])]
            if matched:
                entries = matched
        entries = self._sort_entries(entries)[: self._max_results]
        if not entries:
            return []

        results: List[TorrentInfo] = []
        for item in entries:
            results.append(
                TorrentInfo(
                    site_name="Mikan",
                    site_cookie=None,
                    site_ua=getattr(settings, "USER_AGENT", None),
                    site_proxy=False,
                    site_order=0,
                    site_downloader=None,
                    title=str(item["title"])[:250],
                    enclosure=item["magnet"],
                    description=f"mikan: {item.get('description') or item['title']}",
                    size=item["size"],
                    seeders=0,
                    peers=0,
                    grabs=0,
                    pubdate=item.get("pubdate"),
                    page_url=item["page_url"],
                    media_source=None,
                    media_id=None,
                    labels=[],
                    category=MediaType.TV,
                    downloadvolumefactor=1.0,
                    uploadvolumefactor=1.0,
                )
            )
        try:
            self.save_data(
                "search_results",
                [
                    {
                        "title": item["title"],
                        "episode": item.get("episode", -1),
                        "size": item["size"],
                        "magnet": item["magnet"],
                        "page_url": item["page_url"],
                    }
                    for item in entries
                ],
            )
            self.save_data("search_keyword", keyword)
        except Exception as err:  # noqa: BLE001
            logger.warning(f"【{self.plugin_name}】保存最近搜索结果失败：{err}")
        logger.info(f"【{self.plugin_name}】搜索 {keyword!r} 完成，返回 {len(results)} 条")
        return results

    def search_torrents(
        self,
        site: dict,
        keyword: str,
        mtype: Any = None,
        page: Optional[int] = 0,
    ) -> List[TorrentInfo]:
        if site or not keyword or not self.get_state():
            return []
        started = time.monotonic()
        try:
            return self._search(keyword, mtype=mtype, page=int(page or 0))
        except Exception as err:  # noqa: BLE001
            logger.error(f"【{self.plugin_name}】搜索失败：{err}")
            return []
        finally:
            logger.debug(f"【{self.plugin_name}】耗时 {time.monotonic() - started:.2f}s")

    async def async_search_torrents(
        self,
        site: dict,
        keyword: str,
        mtype: Any = None,
        page: Optional[int] = 0,
    ) -> List[TorrentInfo]:
        return await run_in_threadpool(
            self.search_torrents, site=site, keyword=keyword, mtype=mtype, page=page
        )

    # ------------------------------------------------------------------ api/ui
    def _probe(self) -> Tuple[bool, str]:
        try:
            resp = self._session.get(self._base_url + "/", timeout=min(self._timeout, 10))
            if resp.status_code < 500:
                return True, f"连接成功（HTTP {resp.status_code}）"
            return False, f"站点返回异常：HTTP {resp.status_code}"
        except Exception as err:  # noqa: BLE001
            return False, f"连接失败：{err}"

    def api_test(self) -> Dict[str, Any]:
        ok, message = self._probe()
        return {"success": ok, "message": message, "base_url": self._base_url, "enabled": self._enabled}

    def api_status(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "base_url": self._base_url,
            "configured": bool(self._base_url),
            "max_results": self._max_results,
            "page_max_rows": self._page_max_rows,
            "sort_by": self._sort_by,
            "fetch_full": self._fetch_full,
            "full_dedupe": self._full_dedupe,
            "bangumi_limit": self._bangumi_limit,
            "timeout": self._timeout,
        }

    def api_results(self) -> Dict[str, Any]:
        return {"success": True, "data": self.get_data("search_results") or []}

    def get_page(self) -> Optional[List[dict]]:
        results = self.get_data("search_results") or []
        keyword = self.get_data("search_keyword") or ""
        if not results:
            return [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "prepend-icon": "mdi-information",
                        "text": "还没有数据。先在 MoviePilot 搜索一部动漫（中文/日文名），MikanAnime 会记录最近一次搜索结果，再回本页面查看。",
                    },
                }
            ]
        rows = []
        for item in results[: self._page_max_rows]:
            size = int(item.get("size") or 0)
            size_text = f"{size / 1024 / 1024 / 1024:.2f} GB" if size > 0 else "未知"
            episode = item.get("episode", -1)
            rows.append(
                {
                    "component": "tr",
                    "content": [
                        {"component": "td", "text": str(item.get("title", ""))[:120]},
                        {"component": "td", "text": f"{episode:02d}" if isinstance(episode, int) and episode > 0 else "-"},
                        {"component": "td", "text": size_text},
                        {
                            "component": "td",
                            "content": [
                                {
                                    "component": "VBtn",
                                    "props": {
                                        "href": item.get("magnet", ""),
                                        "size": "x-small",
                                        "variant": "text",
                                        "color": "primary",
                                    },
                                    "text": "种子",
                                }
                            ],
                        },
                        {
                            "component": "td",
                            "content": [
                                {
                                    "component": "VBtn",
                                    "props": {
                                        "href": item.get("page_url", ""),
                                        "target": "_blank",
                                        "size": "x-small",
                                        "variant": "text",
                                    },
                                    "text": "详情",
                                }
                            ],
                        },
                    ],
                }
            )
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "prepend-icon": "mdi-animation",
                    "text": f"最近搜索：{keyword}，共 {len(results)} 条种子（展示前 {min(len(results), self._page_max_rows)} 条）。",
                },
            },
            {
                "component": "VTable",
                "props": {"hover": True, "density": "compact"},
                "content": [
                    {
                        "component": "thead",
                        "content": [
                            {
                                "component": "tr",
                                "content": [
                                    {"component": "th", "text": title}
                                    for title in ("标题", "集数", "大小", "种子", "详情")
                                ],
                            }
                        ],
                    },
                    {"component": "tbody", "content": rows},
                ],
            },
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        form = [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "enabled", "title": "启用插件"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "max_results",
                                            "title": "每次最多返回条数（1-300）",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "page_max_rows",
                                            "title": "插件页面展示行数（1-300）",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "sort_by",
                                            "title": "搜索结果排序",
                                            "items": [
                                                {"title": "最新集数优先", "value": "newest"},
                                                {"title": "最早集数优先", "value": "oldest"},
                                                {"title": "画质优先（2160p/4K）", "value": "resolution"},
                                                {"title": "文件大小优先", "value": "size"},
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "base_url",
                                            "title": "站点地址（默认 https://mikan.tangbai.cc）",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "search_timeout",
                                            "title": "搜索超时（秒）",
                                            "type": "number",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ]
        form[0]["content"].append(
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "fetch_full",
                                    "title": "按番剧全集抓取（搜索时抓取整部番剧所有集的种子，较慢）",
                                },
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": "bangumi_limit",
                                    "title": "全集抓取番剧数量（1-10，默认 3）",
                                    "type": "number",
                                },
                            }
                        ],
                    },
                ],
            }
        )
        form[0]["content"].append(
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "full_dedupe",
                                    "title": "全集抓取时每个集数只保留一个最佳版本（推荐开启）",
                                },
                            }
                        ],
                    }
                ],
            }
        )
        return form, self._defaults()
