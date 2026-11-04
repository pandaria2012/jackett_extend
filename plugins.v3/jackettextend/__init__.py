# -*- coding: utf-8 -*-
"""
MoviePilot v3 插件：Jackett 索引器搜索源。

设计目标：把 Jackett 的公开/半公开/私有索引器（Torznab 聚合）作为
MoviePilot 的“插件资源源”接入，不写入 MoviePilot 的站点表，也不依赖
MoviePilot 的站点认证（认证门槛只作用于内置站点功能）。

兼容 MoviePilot v3.1.9+（在 v3.2.x 上按 3.1.9 插件接口编写）。
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urljoin

import requests
from fastapi.concurrency import run_in_threadpool

from app.core.config import settings
from app.domain.context import TorrentInfo
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import MediaSource

_FALLBACK_CONFIG = "/config/jackett_extend.json"
# 只传 Jackett/Torznab 的父分类，避免几百个 Category 参数把 URL 撑爆（HTTP 414）
_MOVIE_CATS = [2000]
_TV_CATS = [5000]


class JackettExtend(_PluginBase):
    """Jackett 聚合搜索插件（插件资源源架构）。"""

    plugin_name = "JackettExtend"
    plugin_desc = "通过 Jackett 的公开/半公开/私有索引器扩展 MoviePilot 资源检索"
    plugin_icon = "Jackett.png"
    plugin_version = "1.0.0"
    plugin_author = "pandaria2012"
    author_url = "https://github.com/pandaria2012/jackett_extend"
    plugin_label = ""
    plugin_config_prefix = "jackett_extend_"
    plugin_order = 17
    # 0 = 不要求站点认证等级即可加载；真正能否搜索见 README 的说明
    auth_level = 0

    _enabled = False
    _host = ""
    _api_key = ""
    _use_proxy = False
    _timeout = 30
    _indexers: List[str] = []
    _session: Optional[requests.Session] = None

    # ------------------------------------------------------------------ config
    @staticmethod
    def _defaults() -> Dict[str, Any]:
        return {
            "enabled": False,
            "host": "",
            "api_key": "",
            "proxy": False,
            "search_timeout": 30,
            # 只查这些索引器，避免 Jackett /all 被慢站拖死（逗号分隔）
            "indexers": "limetorrents,yts,thepiratebay",
        }

    def init_plugin(self, config: Optional[Mapping[str, Any]] = None) -> None:
        saved = self.get_config() or {}
        values = {**self._defaults(), **saved, **dict(config or {})}

        # 便于首次部署：如果传入配置为空，可读取 /config/jackett_extend.json
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

        host = str(values.get("host") or "").strip().rstrip("/")
        if host and "://" not in host:
            host = "http://" + host
        values["host"] = host
        values["api_key"] = str(values.get("api_key") or "").strip()
        try:
            values["search_timeout"] = max(5, min(int(values.get("search_timeout") or 30), 120))
        except (TypeError, ValueError):
            values["search_timeout"] = 30
        values["proxy"] = bool(values.get("proxy"))
        values["enabled"] = bool(values.get("enabled"))
        indexers_text = str(values.get("indexers") or "").strip()
        values["indexers"] = indexers_text
        self._indexers = [item.strip() for item in indexers_text.split(",") if item.strip()]

        self._enabled = values["enabled"]
        self._host = host
        self._api_key = values["api_key"]
        self._use_proxy = values["proxy"]
        self._timeout = values["search_timeout"]

        self._session = requests.Session()
        self._session.headers.update({"User-Agent": getattr(settings, "USER_AGENT", "MoviePilot")})
        if self._use_proxy:
            proxy = getattr(settings, "PROXY", None)
            if proxy:
                self._session.proxies.update(proxy)
        logger.info(
            f"【{self.plugin_name}】初始化：enabled={self._enabled} host={self._host} "
            f"proxy={self._use_proxy}"
        )

    def stop_service(self) -> None:
        """插件停用/卸载时清理会话。"""
        self._enabled = False
        self._session = None

    def get_state(self) -> bool:
        return bool(self._enabled and self._host and self._api_key)

    # ------------------------------------------------------------------ module
    def get_module(self) -> Dict[str, Any]:
        """向 MoviePilot 注册为插件资源源（搜索钩子）。"""
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
                "summary": "测试 Jackett 连接",
            },
            {
                "path": "/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "查询 JackettExtend 状态",
            },
        ]

    # ------------------------------------------------------------------ search
    @staticmethod
    def _parse_pubdate(value: Any) -> Optional[str]:
        """MoviePilot 响应模型要求 pubdate 是字符串，这里统一格式化。"""
        if not value or not isinstance(value, str):
            return None
        text = value.strip().replace("Z", "+00:00")
        parsed: Optional[datetime] = None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                try:
                    parsed = datetime.strptime(text[:19], fmt)
                    break
                except ValueError:
                    continue
        return parsed.strftime("%Y-%m-%d %H:%M:%S") if parsed else None

    @staticmethod
    def _infer_category(categories: Any) -> Any:
        if not isinstance(categories, list):
            return None
        nums: List[int] = []
        for cat in categories:
            try:
                nums.append(int(cat))
            except (TypeError, ValueError):
                continue
        if any(2000 <= n < 3000 for n in nums):
            return "电影"
        if any(5000 <= n < 6000 for n in nums):
            return "电视剧"
        return None

    def _request_json(self, path: str, params: Dict[str, Any], timeout: Optional[float] = None) -> Any:
        url = self._host + path
        query = {"apikey": self._api_key, **params}
        resp = self._session.get(url, params=query, timeout=timeout or self._timeout)
        resp.raise_for_status()
        return resp.json()

    def _fetch_indexer(self, indexer: str, keyword: str, categories: List[int]) -> List[dict]:
        """查询单个 Jackett 索引器（避免 /all 被慢站拖死）。"""
        params: Dict[str, Any] = {"Query": keyword}
        if categories:
            params["Category"] = categories
        data = self._request_json(
            f"/api/v2.0/indexers/{indexer}/results",
            params,
            timeout=min(self._timeout, 12),
        )
        rows = data.get("Results") if isinstance(data, dict) else None
        return rows if isinstance(rows, list) else []

    def _search(self, keyword: str, mtype: Any = None, page: int = 0) -> List[TorrentInfo]:
        if not self.get_state():
            return []
        categories: List[int] = []
        try:
            from app.schemas.types import MediaType
            if mtype == MediaType.MOVIE:
                categories = _MOVIE_CATS
            elif mtype == MediaType.TV:
                categories = _TV_CATS
        except Exception:  # noqa: BLE001
            categories = []

        rows: List[dict] = []
        indexers = self._indexers or ["limetorrents", "yts", "thepiratebay"]
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(indexers)))) as pool:
            futures = {
                pool.submit(self._fetch_indexer, indexer, keyword, categories): indexer
                for indexer in indexers
            }
            for future in as_completed(futures, timeout=self._timeout):
                indexer = futures[future]
                try:
                    rows.extend(future.result())
                except Exception as err:  # noqa: BLE001
                    logger.warning(f"【{self.plugin_name}】索引器 {indexer} 查询失败：{err}")

        results: List[TorrentInfo] = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            title = row.get("Title") or row.get("title")
            enclosure = row.get("MagnetUri") or row.get("Link")
            if not title or not enclosure:
                continue
            if str(enclosure).startswith("/"):
                enclosure = urljoin(self._host + "/", enclosure)
            tracker = str(row.get("Tracker") or "Jackett")
            key = (tracker, str(row.get("InfoHash") or enclosure))
            if key in seen:
                continue
            seen.add(key)
            row_categories = row.get("Category") or []
            try:
                size = int(row.get("Size") or 0)
            except (TypeError, ValueError):
                size = 0
            results.append(
                TorrentInfo(
                    site_name=tracker,
                    site_cookie=None,
                    site_ua=getattr(settings, "USER_AGENT", None),
                    site_proxy=False,
                    site_order=0,
                    site_downloader=None,
                    title=str(title),
                    enclosure=str(enclosure),
                    description=str(row.get("Description") or ""),
                    size=size,
                    seeders=row.get("Seeders"),
                    peers=row.get("Peers"),
                    grabs=row.get("Grabs"),
                    pubdate=self._parse_pubdate(row.get("PublishDate")),
                    page_url=row.get("Details"),
                    media_source=MediaSource.IMDb if row.get("Imdb") else None,
                    media_id=row.get("Imdb") or None,
                    labels=[],
                    category=self._infer_category(row_categories),
                    downloadvolumefactor=1.0,
                    uploadvolumefactor=1.0,
                )
            )
        logger.info(f"【{self.plugin_name}】搜索 {keyword!r} 完成，返回 {len(results)} 条")
        return results

    def search_torrents(
        self,
        site: dict,
        keyword: str,
        mtype: Any = None,
        page: Optional[int] = 0,
    ) -> List[TorrentInfo]:
        """插件资源源入口：MoviePilot 会以 site={} 调用。"""
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
        if not self._host or not self._api_key:
            return False, "未配置 Jackett 地址或 API Key"
        try:
            # 只探测首页，避免 /all 聚合搜索被慢索引器拖到超时
            resp = self._session.get(self._host + "/", timeout=min(self._timeout, 10))
            if resp.status_code < 500:
                return True, f"连接成功（HTTP {resp.status_code}）"
            return False, f"Jackett 返回异常状态：HTTP {resp.status_code}"
        except Exception as err:  # noqa: BLE001
            return False, f"连接失败：{err}"

    def api_test(self) -> Dict[str, Any]:
        ok, message = self._probe()
        return {"success": ok, "message": message, "host": self._host, "enabled": self._enabled}

    def api_status(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "host": self._host,
            "configured": bool(self._host and self._api_key),
            "timeout": self._timeout,
            "indexers": self._indexers,
        }

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """插件配置表单（Vuetify 结构）。"""
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
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "proxy",
                                            "label": "使用 MoviePilot 代理访问 Jackett",
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
                                            "model": "search_timeout",
                                            "label": "搜索超时（秒）",
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
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "host",
                                            "label": "Jackett 地址（如 http://192.168.50.103:9117）",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "api_key",
                                            "label": "Jackett API Key",
                                            "type": "password",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "indexers",
                                            "label": "要查询的索引器（逗号分隔，如 limetorrents,yts,thepiratebay）",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ]
        return form, self._defaults()

    def get_page(self) -> Optional[List[dict]]:
        return None
