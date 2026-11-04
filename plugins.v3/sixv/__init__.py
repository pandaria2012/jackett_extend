# -*- coding: utf-8 -*-
"""
MoviePilot v3 插件：6v520（6v电影）资源源。

把 https://www.6v520.cc 的搜索结果接入 MoviePilot 的「插件资源源」：
- 站内搜索（GBK 编码的表单 POST）
- 解析详情页里的磁力链接
- **重写磁力链接的 dn（显示名）为电影/电视剧/动漫的规范名字 + 画质**
  （原站 dn 是“爸爸.6v电影 地址发布页 www.6v123.net 收藏不迷路”这类广告名）

说明：该站详情页只有磁力链接 + 迅雷/夸克/百度网盘，没有 .torrent 文件；
所以“种子文件重命名”实际是重写 magnet 的 dn 显示名，qB 里就会显示规范名字，
下载完成后的文件由 MoviePilot 的整理入库流程按 TMDB 信息重命名。
"""
from __future__ import annotations

import html as html_lib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import quote, urljoin, urlparse, parse_qs, unquote

import requests
from fastapi.concurrency import run_in_threadpool

from app.core.config import settings
from app.domain.context import TorrentInfo
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import MediaSource

_FALLBACK_CONFIG = "/config/sixv.json"

# 站点栏目 -> 媒体类型
_MOVIE_PATH_RE = re.compile(r"/(?:dy|jddy|gq|3D|shoujidianyingmp4)/", re.I)
_TV_PATH_RE = re.compile(r"/(?:dlz|mj|rj|zy|zydy|gydy)/", re.I)


class SixV(_PluginBase):
    """6v520 资源源插件（插件资源源架构）。"""

    plugin_name = "SixV"
    plugin_desc = "6V电影（6v520.cc）资源源：搜索、磁力、按影视名重命名种子显示名"
    plugin_icon = "6v.png"
    plugin_version = "1.0.0"
    plugin_author = "pandaria2012"
    author_url = "https://github.com/pandaria2012/jackett_extend"
    plugin_label = ""
    plugin_config_prefix = "sixv_"
    plugin_order = 18
    auth_level = 0

    _enabled = False
    _base_url = "https://www.6v520.cc"
    _max_results = 10
    _rename = True
    _timeout = 30
    _session: Optional[requests.Session] = None

    # ------------------------------------------------------------------ config
    @staticmethod
    def _defaults() -> Dict[str, Any]:
        return {
            "enabled": False,
            "base_url": "https://www.6v520.cc",
            "max_results": 10,
            "rename": True,
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
        values["base_url"] = base_url or "https://www.6v520.cc"
        try:
            values["max_results"] = max(1, min(int(values.get("max_results") or 10), 30))
        except (TypeError, ValueError):
            values["max_results"] = 10
        try:
            values["search_timeout"] = max(5, min(int(values.get("search_timeout") or 30), 120))
        except (TypeError, ValueError):
            values["search_timeout"] = 30
        values["rename"] = bool(values.get("rename", True))
        values["enabled"] = bool(values.get("enabled"))

        self._enabled = values["enabled"]
        self._base_url = values["base_url"]
        self._max_results = values["max_results"]
        self._rename = values["rename"]
        self._timeout = values["search_timeout"]
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": getattr(settings, "USER_AGENT", "MoviePilot"),
                "Referer": self._base_url + "/",
            }
        )
        logger.info(
            f"【{self.plugin_name}】初始化：enabled={self._enabled} "
            f"base_url={self._base_url} rename={self._rename}"
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
                "summary": "测试 6v520 连接",
            },
            {
                "path": "/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "查询 SixV 状态",
            },
        ]

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _strip_tags(value: str) -> str:
        return html_lib.unescape(re.sub(r"<[^>]+>", "", value or "")).strip()

    @staticmethod
    def _clean_filename(value: str) -> str:
        value = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", value or "")
        return re.sub(r"\s+", " ", value).strip(" .")

    @staticmethod
    def _parse_pubdate(value: Any) -> Optional[str]:
        if not value:
            return None
        text = str(value).strip()
        m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", text)
        return m.group(1) if m else None

    @staticmethod
    def _magnet_query(magnet: str) -> Dict[str, str]:
        try:
            return {k: v[0] for k, v in parse_qs(urlparse(magnet).query).items() if v}
        except Exception:  # noqa: BLE001
            return {}

    def _rename_magnet(self, magnet: str, display_name: str) -> str:
        """把 magnet 的 dn 参数替换成规范影视名（UTF-8 百分号编码）。"""
        if not self._rename or not display_name:
            return magnet
        encoded = quote(display_name, safe="")
        if re.search(r"[?&]dn=", magnet):
            return re.sub(r"([?&]dn=)[^&]*", lambda m: m.group(1) + encoded, magnet, count=1)
        sep = "&" if "?" in magnet else "?"
        return f"{magnet}{sep}dn={encoded}"

    # ------------------------------------------------------------------ search
    @staticmethod
    def _keyword_variants(keyword: str) -> List[str]:
        """站点搜索对中文标点较敏感，生成几个降级关键词。"""
        variants = [keyword.strip()]
        cleaned = re.sub(r"[：:、，,。.！!？?\-—_/]+", " ", keyword)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if cleaned and cleaned not in variants:
            variants.append(cleaned)
        parts = [p.strip() for p in re.split(r"[：:]", keyword) if p.strip()]
        for part in sorted(parts, key=len, reverse=True):
            if part not in variants:
                variants.append(part)
        return [v for v in variants if v]

    def _search_site_once(self, keyword: str) -> List[Tuple[str, str]]:
        """单次站内搜索，返回 [(详情URL, 搜索结果标题)]。"""
        body = (
            "show=title%2Csmalltext&tempid=1&tbname=article&keyboard="
            + quote(keyword.encode("gb18030", errors="ignore"))
        )
        resp = self._session.post(
            self._base_url + "/e/search/index.php",
            data=body.encode("ascii"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": self._base_url + "/",
            },
            timeout=self._timeout,
            allow_redirects=True,
        )
        resp.encoding = "gb18030"
        text = resp.text or ""
        if "sousuo.html" in resp.url or "信息提示" in text:
            return []
        items: List[Tuple[str, str]] = []
        seen = set()
        for m in re.finditer(
            r'<span\s+class="blue14">\s*<a\s+href=["\']?([^"\'\s>]+)["\']?[^>]*>(.*?)</a>',
            text,
            re.S | re.I,
        ):
            href = html_lib.unescape(m.group(1)).strip()
            title = self._strip_tags(m.group(2))
            url = urljoin(self._base_url + "/", href)
            if url in seen:
                continue
            if not re.search(
                r"/(?:dy|dlz|gq|jddy|mj|rj|zy|zydy|gydy|3D|shoujidianyingmp4)/\d",
                url,
                re.I,
            ):
                continue
            seen.add(url)
            items.append((url, title))
        return items

    def _search_site(self, keyword: str) -> List[Tuple[str, str]]:
        cache = getattr(self, "_site_cache", None)
        if cache is None:
            cache = self._site_cache = {}
        now = time.monotonic()
        cached = cache.get(keyword)
        if cached and now - cached[0] < 300:
            return list(cached[1])
        variants = self._keyword_variants(keyword)
        for attempt in range(2):
            for variant in variants:
                try:
                    items = self._search_site_once(variant)
                except Exception as err:  # noqa: BLE001
                    logger.warning(f"【{self.plugin_name}】站内搜索失败：{err}")
                    items = []
                if items:
                    cache[keyword] = (now, list(items))
                    return items
            if attempt == 0:
                # 站点有搜索频率限制，稍等后重试一次
                time.sleep(3)
        return []

    def _detail(self, url: str) -> List[Dict[str, Any]]:
        resp = self._session.get(url, timeout=self._timeout)
        resp.encoding = "gb18030"
        text = resp.text or ""
        h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", text, re.S | re.I)
        h1 = self._strip_tags(h1_match.group(1)) if h1_match else ""
        title_match = re.search(r"<title>(.*?)</title>", text, re.S | re.I)
        page_title = self._strip_tags(title_match.group(1)) if title_match else h1

        meta: Dict[str, str] = {}
        for field in ("片 名", "标 题", "年 代", "类 别", "集 数", "上映日期"):
            m = re.search(re.escape(field) + r"[：:]\s*(.*?)(?:<br|</td|</div)", text, re.S)
            if m:
                meta[field] = self._strip_tags(m.group(1))[:120]

        source_name = h1 or page_title
        suffix = ""
        m = re.search(r"《([^》]+)》", source_name)
        if m:
            name = m.group(1).strip()
            suffix = source_name[m.end():].strip()
            quality = re.split(r"[，,]", suffix)[0].strip() if suffix else ""
        else:
            name = (meta.get("片 名") or meta.get("标 题") or source_name).strip()
            quality = ""
        name = self._clean_filename(name) or self._clean_filename(page_title)
        quality = self._clean_filename(quality)
        if not name:
            return []

        is_tv = bool(_TV_PATH_RE.search(url))
        year = ""
        year_match = re.search(r"((?:19|20)\d{2})", meta.get("年 代") or "")
        if year_match:
            year = year_match.group(1)
        episode = ""
        if is_tv:
            ep_match = re.search(r"更新\s*(\d{1,3})(?:\s*-\s*(\d{1,3}))?", suffix)
            if ep_match:
                begin = int(ep_match.group(1))
                end = int(ep_match.group(2) or begin)
                episode = f"S01E{begin:02d}" + (f"-E{end:02d}" if end != begin else "")
            elif "全集" in suffix:
                episode = "Complete"

        # 详情页可能有多条磁力（分集/分版本），按 <a href="magnet:..."> 文本标签逐条解析
        def clean_label(value: str) -> str:
            value = self._clean_filename(self._strip_tags(value))
            value = re.sub(r"\.(mkv|mp4|avi|ts|m2ts|rmvb|wmv)$", "", value, flags=re.I)
            value = re.sub(
                r"(6v电影|6v123|66影视|地址发布页|收藏不迷路).*$", "", value, flags=re.I
            )
            return value.strip(" .-_")

        entries: List[Dict[str, Any]] = []
        seen = set()
        for anchor in re.finditer(
            r'<a[^>]+href=["\'](magnet:[^"\']+)["\'][^>]*>(.*?)</a>',
            text,
            re.S | re.I,
        ):
            magnet = html_lib.unescape(anchor.group(1)).strip()
            query = self._magnet_query(magnet)
            btih = query.get("xt") or ""
            if not btih or btih in seen:
                continue
            seen.add(btih)
            label = clean_label(anchor.group(2))
            if label.startswith(name):
                label = label[len(name):].lstrip(" .-_")
            if not label:
                label = episode or quality or suffix
            parts = [name]
            if year:
                parts.append(year)
            if label:
                parts.append(label)
            display_name = ".".join(piece for piece in parts if piece)
            if not self._rename:
                display_name = ".".join(piece for piece in (name, year, episode) if piece)
            size = 0
            try:
                size = int(query.get("xl") or 0)
            except (TypeError, ValueError):
                size = 0
            entries.append(
                {
                    "name": name,
                    "quality": label,
                    "display_name": display_name,
                    "title": display_name,
                    "original_title": source_name or display_name,
                    "magnet": self._rename_magnet(magnet, display_name),
                    "size": size,
                    "page_url": url,
                    "year": year,
                    "is_tv": is_tv,
                }
            )
        return entries

    @staticmethod
    def _match_path(url: str, mtype: Any) -> bool:
        try:
            from app.schemas.types import MediaType

            if mtype == MediaType.MOVIE:
                return bool(_MOVIE_PATH_RE.search(url))
            if mtype == MediaType.TV:
                return bool(_TV_PATH_RE.search(url))
        except Exception:  # noqa: BLE001
            pass
        return True

    def _search(self, keyword: str, mtype: Any = None, page: int = 0) -> List[TorrentInfo]:
        if not self.get_state():
            return []
        items = self._search_site(keyword)
        if not items:
            # 站点搜索对“片名 + 年份”这类关键字可能没有结果，降级再搜一次
            cleaned = re.sub(r"\b(19|20)\d{2}\b", "", keyword).strip()
            if cleaned and cleaned != keyword:
                items = self._search_site(cleaned)
        if not items:
            return []
        items = [item for item in items if self._match_path(item[0], mtype)]
        # 先按标题关键字过滤，避免搜索把无关内容带进来
        normalized = keyword.strip().lower()
        filtered = [item for item in items if normalized and normalized in item[1].lower()]
        if filtered:
            items = filtered
        items = items[: max(self._max_results * 2, self._max_results)]

        details: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(items)))) as pool:
            futures = {pool.submit(self._detail, url): url for url, _ in items}
            for future in as_completed(futures, timeout=self._timeout):
                try:
                    item = future.result()
                except Exception as err:  # noqa: BLE001
                    logger.warning(f"【{self.plugin_name}】详情页解析失败：{err}")
                    continue
                if item:
                    details.extend(item[: self._max_results])
        details = details[: self._max_results]
        if not details:
            return []

        try:
            from app.schemas.types import MediaType
        except Exception:  # noqa: BLE001
            MediaType = None  # type: ignore

        results: List[TorrentInfo] = []
        for item in details:
            category = None
            if MediaType is not None and item.get("is_tv"):
                category = MediaType.TV
            elif MediaType is not None:
                category = MediaType.MOVIE
            results.append(
                TorrentInfo(
                    site_name="6V电影",
                    site_cookie=None,
                    site_ua=getattr(settings, "USER_AGENT", None),
                    site_proxy=False,
                    site_order=0,
                    site_downloader=None,
                    title=str(item["title"])[:200],
                    enclosure=item["magnet"],
                    description=f"6v520: {item.get('original_title') or item['name']}".strip(),
                    size=item["size"],
                    seeders=0,
                    peers=0,
                    grabs=0,
                    pubdate=None,
                    page_url=item["page_url"],
                    media_source=None,
                    media_id=None,
                    labels=[],
                    category=category,
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
            "rename": self._rename,
            "max_results": self._max_results,
            "timeout": self._timeout,
        }

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
                                    {"component": "VSwitch", "props": {"model": "rename", "title": "重命名种子显示名"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "max_results", "title": "每次最多返回条数", "type": "number"},
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
                                        "props": {"model": "base_url", "title": "站点地址，如 https://www.6v520.cc"},
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
                                        "props": {"model": "search_timeout", "title": "搜索超时（秒）", "type": "number"},
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
