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
            "max_results": 50,
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
            values["max_results"] = max(1, min(int(values.get("max_results") or 50), 300))
        except (TypeError, ValueError):
            values["max_results"] = 50
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
            {
                "path": "/pan_links",
                "endpoint": self.api_pan_links,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "查询最近收集到的网盘链接",
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
    def _normalize_title(value: str) -> str:
        value = html_lib.unescape(value or "")
        value = re.sub(r"[《》]", "", value)
        value = re.sub(r"(?:19|20)\d{2}", "", value)
        value = re.sub(r"[\s：:、，,。.！!？?\-—_/（）()\[\]【】]+", "", value)
        return value.casefold()

    @classmethod
    def _title_matches(cls, keyword: str, result_title: str) -> bool:
        """严格片名匹配：只保留《片名》与关键词完全一致的结果。"""
        m = re.search(r"《([^》]+)》", result_title or "")
        name = m.group(1) if m else (result_title or "")
        norm_kw = cls._normalize_title(keyword)
        norm_name = cls._normalize_title(name)
        return bool(norm_kw and norm_name and norm_kw == norm_name)

    @staticmethod
    def _episode_number(label: str) -> int:
        """从“01-08 / 第07集 / S01E05-06 / 177-178”里取最大集数，用于最新一集优先。"""
        if not label:
            return -1
        numbers: List[int] = []
        for m in re.finditer(
            r"(?:第\s*|S\d+E|E)(\d{1,4})(?:\s*-\s*(?:E|第)?\s*(\d{1,4}))?",
            label,
            re.I,
        ):
            numbers.extend(int(g) for g in m.groups() if g and str(g).isdigit())
        if not numbers:
            for m in re.finditer(
                r"(?<![\dA-Za-z])(\d{1,4})(?:\s*-\s*(\d{1,4}))?(?![\dA-Za-z])",
                label,
            ):
                numbers.extend(int(g) for g in m.groups() if g and str(g).isdigit())
        return max(numbers) if numbers else -1

    @staticmethod
    def _resolution_rank(label: str) -> int:
        text = (label or "").lower()
        if "2160" in text or "4k" in text:
            return 3
        if "1080" in text:
            return 2
        if "720" in text:
            return 1
        return 0

    def _parse_pan_links(self, text: str, name: str, page_url: str) -> List[Dict[str, Any]]:
        """解析详情页里的迅雷/夸克/百度网盘链接，供插件页面展示。"""
        links: List[Dict[str, Any]] = []
        seen = set()
        pattern = re.compile(
            r'<a[^>]+href=["\'](https?://(?:pan\.xunlei\.com|pan\.quark\.cn|pan\.baidu\.com)/[^"\']+)["\'][^>]*>(.*?)</a>',
            re.S | re.I,
        )
        for m in pattern.finditer(text):
            url = html_lib.unescape(m.group(1)).strip()
            if not url or url in seen:
                continue
            seen.add(url)
            context = html_lib.unescape(
                re.sub(r"<[^>]+>", " ", text[max(0, m.start() - 800): m.start()])
            )
            qualities = re.findall(r"(2160p|4k|1080p|720p)", context, re.I)
            quality = (qualities[-1] if qualities else "").lower() or "未知"
            after = html_lib.unescape(
                re.sub(r"<[^>]+>", " ", text[m.end(): m.end() + 400])
            )
            code_match = re.search(r"提取码[：:]\s*([a-zA-Z0-9]{4})", after)
            if not code_match:
                code_match = re.search(r"提取码[：:]\s*([a-zA-Z0-9]{4})", context)
            pan_type = "迅雷" if "xunlei" in url else "夸克" if "quark" in url else "百度"
            code_value = code_match.group(1) if (pan_type == "百度" and code_match) else ""
            links.append(
                {
                    "title": name,
                    "quality": quality,
                    "pan_type": pan_type,
                    "url": url,
                    "code": code_value,
                    "page_url": page_url,
                }
            )
        return links

    def _record_pan_links(self, links: List[Dict[str, Any]]) -> None:
        if not links:
            return
        try:
            existing = self.get_data("pan_links") or []
        except Exception:  # noqa: BLE001
            existing = []
        existing = [item for item in existing if isinstance(item, dict) and item.get("url")]
        known = {item.get("url") for item in links}
        merged = list(links) + [item for item in existing if item.get("url") not in known]
        merged = merged[:200]
        try:
            self.save_data("pan_links", merged)
        except Exception as err:  # noqa: BLE001
            logger.warning(f"【{self.plugin_name}】保存网盘链接失败：{err}")

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

    def _detail(self, url: str) -> Dict[str, Any]:
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
            return {"torrents": [], "pan_links": []}

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
        # 最新一集优先；同集数时高分辨率优先
        entries.sort(
            key=lambda item: (
                -self._episode_number(item.get("quality", "")),
                -self._resolution_rank(item.get("quality", "") or item.get("display_name", "")),
                item.get("display_name", ""),
            )
        )
        return {
            "torrents": entries,
            "pan_links": self._parse_pan_links(text, name, url),
        }

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
        # 严格片名过滤：避免“爸爸”带出“假爸爸”“燃烧吧！爸爸”这类误匹配
        strict_items = [item for item in items if self._title_matches(keyword, item[1])]
        if strict_items:
            items = strict_items
        else:
            logger.info(f"【{self.plugin_name}】{keyword!r} 没有精确片名匹配，跳过")
            return []
        items = items[: max(self._max_results * 2, self._max_results)]

        details: List[Dict[str, Any]] = []
        all_pan: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(items)))) as pool:
            futures = {pool.submit(self._detail, url): url for url, _ in items}
            for future in as_completed(futures, timeout=self._timeout):
                try:
                    result = future.result()
                except Exception as err:  # noqa: BLE001
                    logger.warning(f"【{self.plugin_name}】详情页解析失败：{err}")
                    continue
                if isinstance(result, dict):
                    details.extend(result.get("torrents") or [])
                    all_pan.extend(result.get("pan_links") or [])
        # 最新一集优先，其次高分辨率优先
        details.sort(
            key=lambda item: (
                -self._episode_number(item.get("quality", "")),
                -self._resolution_rank(item.get("quality", "") or item.get("display_name", "")),
                item.get("display_name", ""),
            )
        )
        details = details[: self._max_results]
        self._record_pan_links(all_pan)
        if details:
            try:
                self.save_data(
                    "search_results",
                    [
                        {
                            "title": item.get("display_name", ""),
                            "label": item.get("quality", ""),
                            "size": item.get("size", 0),
                            "magnet": item.get("magnet", ""),
                            "page_url": item.get("page_url", ""),
                        }
                        for item in details
                    ],
                )
                self.save_data("search_keyword", keyword)
            except Exception as err:  # noqa: BLE001
                logger.warning(f"【{self.plugin_name}】保存最近搜索结果失败：{err}")
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
                                        "props": {"model": "max_results", "title": "每次最多返回条数（1-300，追番可调大）", "type": "number"},
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
        pan_links = self.get_data("pan_links") or []
        search_results = self.get_data("search_results") or []
        keyword = self.get_data("search_keyword") or ""
        if not pan_links and not search_results:
            return [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "prepend-icon": "mdi-information",
                        "text": "还没有数据。先在 MoviePilot 搜索一部 6V 上的片名/剧名，SixV 会记录最近一次搜索结果和 1080p/4K 网盘链接，再回到本页面查看。",
                    },
                }
            ]
        page: List[dict] = []

        def size_text(value: Any) -> str:
            try:
                size = int(value or 0)
            except (TypeError, ValueError):
                return "未知"
            return f"{size / 1024 / 1024 / 1024:.2f} GB" if size > 0 else "未知"

        if search_results:
            page.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "prepend-icon": "mdi-magnet",
                        "text": f"最近搜索：{keyword}，共 {len(search_results)} 条种子（展示前 100 条，最新一集在前，页面可滚动）。",
                    },
                }
            )
            rows = []
            for item in search_results[:100]:
                rows.append(
                    {
                        "component": "tr",
                        "content": [
                            {"component": "td", "text": str(item.get("title", ""))[:80]},
                            {"component": "td", "text": item.get("label", "") or "-"},
                            {"component": "td", "text": size_text(item.get("size"))},
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
                                        "text": "磁力",
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
                                        "text": "6V页面",
                                    }
                                ],
                            },
                        ],
                    }
                )
            page.append(
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
                                        for title in ("片名", "集数/画质", "大小", "种子", "来源")
                                    ],
                                }
                            ],
                        },
                        {"component": "tbody", "content": rows},
                    ],
                }
            )

        if pan_links:
            page.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "prepend-icon": "mdi-cloud-download",
                        "text": f"最近收集到 {len(pan_links)} 条网盘链接（最多展示 100 条），来自 6v520 详情页，仅用于手动转存。",
                    },
                }
            )
            rows = []
            for item in pan_links[:100]:
                rows.append(
                    {
                        "component": "tr",
                        "content": [
                            {"component": "td", "text": str(item.get("title", ""))[:60]},
                            {"component": "td", "text": item.get("quality", "") or "未知"},
                            {"component": "td", "text": item.get("pan_type", "")},
                            {
                                "component": "td",
                                "content": [
                                    {
                                        "component": "VBtn",
                                        "props": {
                                            "href": item.get("url", ""),
                                            "target": "_blank",
                                            "size": "x-small",
                                            "variant": "text",
                                            "color": "primary",
                                        },
                                        "text": "打开",
                                    }
                                ],
                            },
                            {"component": "td", "text": item.get("code", "") or "-"},
                        ],
                    }
                )
            page.append(
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
                                        for title in ("片名", "画质", "网盘", "链接", "提取码")
                                    ],
                                }
                            ],
                        },
                        {"component": "tbody", "content": rows},
                    ],
                }
            )
        return page

    def api_pan_links(self) -> Dict[str, Any]:
        return {"success": True, "data": self.get_data("pan_links") or []}
