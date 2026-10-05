from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests

from protocol.base import ProtocolProvider


NHENTAI_CONFIG_KEY = "nhentai"
NHENTAI_PLUGIN_ID = "comic.nhentai"
NHENTAI_PLATFORM = "NHentai"
NHENTAI_HOST_ID_PREFIX = "NH"

DEFAULT_BASE_URL = "https://nhentai.net"
DEFAULT_IMAGE_CDN_BASE = "https://i.nhentai.net"
DEFAULT_THUMB_CDN_BASE = "https://t.nhentai.net"
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_USER_AGENT = "ULTIMATE_WEB/0.1 (https://github.com)"

# 标签 type -> 宿主通用分类
_TAG_TYPE_ARTIST = "artist"
_TAG_TYPE_GROUP = "group"
_TAG_TYPE_PARODY = "parody"
_TAG_TYPE_CHARACTER = "character"
_TAG_TYPE_LANGUAGE = "language"
_TAG_TYPE_CATEGORY = "category"

# 所有已知标签类型（对应 API 的 tag_type 路径参数）
_ALL_TAG_TYPES = ["tag", "artist", "parody", "character", "category", "language", "group"]

_TOKEN_MASK_VALUES = {"", "********", "******", "__KEEP__"}

SUPPORTED_CAPABILITIES = frozenset(
    {
        "catalog.search",
        "catalog.detail",
        "asset.bundle.fetch",
        "asset.cover.fetch",
        "asset.preview.resolve",
        "storage.comic_dir.resolve",
        "health.query.status",
        "taxonomy.tags",
        "taxonomy.tag_search",
    }
)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _as_int(value: Any, default: int, minimum: int = 1, maximum: int = 10000) -> int:
    try:
        parsed = int(float(value))
    except Exception:
        parsed = default
    if parsed < minimum:
        return minimum
    if parsed > maximum:
        return maximum
    return parsed


def _normalize_base_url(value: Any) -> str:
    text = str(value or "").strip().rstrip("/")
    return text or DEFAULT_BASE_URL


def _sanitize_path_segment(text: Any, fallback: str = "untitled") -> str:
    raw = str(text or "").strip()
    if not raw:
        return fallback
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.strip(".")
    return cleaned or fallback


def _join_cdn(cdn_base: str, path: str) -> str:
    """把 API 返回的相对 path 拼到 CDN base 上。
    如果 path 已经是完整 URL（含 http），则直接返回。
    """
    base = str(cdn_base or "").rstrip("/")
    relative = str(path or "").strip()
    if not relative:
        return ""
    if relative.startswith("http://") or relative.startswith("https://"):
        return relative
    return f"{base}/{relative.lstrip('/')}"


class NHentaiProvider(ProtocolProvider):
    """nhentai.net API v2 的协议化适配器。

    API 返回的图片地址均为相对路径（如 galleries/{media_id}/1.webp），
    需要拼接 CDN base：全图用 i.nhentai.net，缩略图/封面用 t.nhentai.net。
    """

    def normalize_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw = dict(payload or {})
        normalized: Dict[str, Any] = {}
        normalized["enabled"] = _as_bool(raw.get("enabled"), False)
        normalized["base_url"] = _normalize_base_url(raw.get("base_url"))
        # 掩码/空值不写回，避免保存配置时把已存的 API Key 清空（宿主侧已无密文保护）
        raw_api_key = str(raw.get("api_key") or "").strip()
        if raw_api_key and raw_api_key not in _TOKEN_MASK_VALUES:
            normalized["api_key"] = raw_api_key
        normalized["user_agent"] = str(raw.get("user_agent") or "").strip() or DEFAULT_USER_AGENT

        # CDN base：如果等于 base_url（用户误填了主站地址），回退到默认 CDN
        base_url = normalized["base_url"]
        raw_image_cdn = str(raw.get("image_cdn_base") or "").strip().rstrip("/")
        if raw_image_cdn and raw_image_cdn != base_url:
            normalized["image_cdn_base"] = raw_image_cdn
        else:
            normalized["image_cdn_base"] = DEFAULT_IMAGE_CDN_BASE

        raw_thumb_cdn = str(raw.get("thumb_cdn_base") or "").strip().rstrip("/")
        if raw_thumb_cdn and raw_thumb_cdn != base_url:
            normalized["thumb_cdn_base"] = raw_thumb_cdn
        else:
            normalized["thumb_cdn_base"] = DEFAULT_THUMB_CDN_BASE

        normalized["timeout_seconds"] = _as_int(raw.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS, 1, 600)
        normalized["proxy"] = str(raw.get("proxy") or "").strip()
        return normalized

    def serialize_public_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self.normalize_config(config)
        public = dict(normalized)
        public.pop("api_key", None)
        public["api_key_configured"] = bool(str((config or {}).get("api_key") or "").strip())
        return public

    def get_query_status(self, config: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self.normalize_config(config)
        enabled = _as_bool(normalized.get("enabled"), False)
        base_url = str(normalized.get("base_url") or "").strip()
        configured = bool(enabled and base_url)
        return {
            "configured": configured,
            "message": "" if configured else "NHentai 未启用或站点地址未配置。",
            "missing_fields": [] if base_url else ["base_url"],
        }

    # ---------- 内部工具 ----------

    def _build_session(self, config: Dict[str, Any]) -> requests.Session:
        session = requests.Session()
        headers = {
            "User-Agent": str(config.get("user_agent") or DEFAULT_USER_AGENT),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.8",
            "Referer": str(config.get("base_url") or DEFAULT_BASE_URL).rstrip("/") + "/",
        }
        api_key = str(config.get("api_key") or "").strip()
        if api_key and api_key not in _TOKEN_MASK_VALUES:
            headers["Authorization"] = f"Key {api_key}"
        session.headers.update(headers)
        proxy = str(config.get("proxy") or "").strip()
        if proxy:
            session.proxies.update({"http": proxy, "https": proxy})
        return session

    def _api_get(
        self,
        session: requests.Session,
        config: Dict[str, Any],
        path: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        base_url = str(config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        url = f"{base_url}{path}"
        timeout = _as_int(config.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS, 1, 600)
        response = session.get(url, params=params, timeout=timeout)
        if response.status_code == 404:
            return None
        if response.status_code in (401, 403):
            raise RuntimeError(f"nhentai 鉴权失败 ({response.status_code})，请检查 API Key 配置。")
        if response.status_code >= 400:
            raise RuntimeError(f"nhentai 请求失败: {response.status_code} {response.reason}")
        try:
            return response.json()
        except Exception as exc:
            raise RuntimeError(f"nhentai 响应解析失败: {exc}") from exc

    # ---------- 字段提取（兼容 search 与 detail 两种结构） ----------

    def _extract_title(self, gallery: Dict[str, Any]) -> str:
        """search 用 english_title/japanese_title 扁平字段；detail 用 title 对象。"""
        title_obj = gallery.get("title")
        if isinstance(title_obj, dict):
            title = str(
                title_obj.get("english")
                or title_obj.get("pretty")
                or title_obj.get("japanese")
                or ""
            ).strip()
            if title:
                return title
        # search 形态
        return str(gallery.get("english_title") or "").strip()

    @staticmethod
    def _extract_tag_name(raw_name: Any) -> str:
        """从 nhentai tag 对象中提取英文标签名。
        
        nhentai API 中 tag 的 name 字段可能是：
          - 字符串（如 "blowjob"）
          - 对象（如 {"english": "blowjob", "japanese": "フェラチオ"}）
        """
        if isinstance(raw_name, dict):
            return str(
                raw_name.get("english")
                or raw_name.get("pretty")
                or raw_name.get("name")
                or ""
            ).strip()
        return str(raw_name or "").strip()

    def _extract_subtitle(self, gallery: Dict[str, Any]) -> str:
        title_obj = gallery.get("title")
        if isinstance(title_obj, dict):
            return str(title_obj.get("japanese") or "").strip()
        return str(gallery.get("japanese_title") or "").strip()

    def _extract_cover_url(self, config: Dict[str, Any], gallery: Dict[str, Any]) -> str:
        """优先用 cover.path，其次 thumbnail（可能是字符串或对象）。"""
        thumb_cdn = str(config.get("thumb_cdn_base") or DEFAULT_THUMB_CDN_BASE)
        image_cdn = str(config.get("image_cdn_base") or DEFAULT_IMAGE_CDN_BASE)

        cover = gallery.get("cover")
        if isinstance(cover, dict) and cover.get("path"):
            return _join_cdn(thumb_cdn, cover.get("path"))

        thumbnail = gallery.get("thumbnail")
        if isinstance(thumbnail, dict) and thumbnail.get("path"):
            return _join_cdn(thumb_cdn, thumbnail.get("path"))
        if isinstance(thumbnail, str) and thumbnail.strip():
            return _join_cdn(thumb_cdn, thumbnail.strip())

        # 兜底：media_id 构造（老格式，可能 404，仅作最后手段）
        media_id = str(gallery.get("media_id") or "").strip()
        if media_id:
            return _join_cdn(thumb_cdn, f"galleries/{media_id}/thumb.webp")
        return ""

    def _extract_preview_urls(
        self,
        config: Dict[str, Any],
        gallery: Dict[str, Any],
        max_count: Optional[int] = None,
    ) -> List[str]:
        """从 detail 的 pages 数组取缩略图 URL。"""
        thumb_cdn = str(config.get("thumb_cdn_base") or DEFAULT_THUMB_CDN_BASE)
        pages = gallery.get("pages")
        if not isinstance(pages, list) or not pages:
            return []
        urls: List[str] = []
        limit = len(pages) if max_count is None else min(max_count, len(pages))
        for index in range(limit):
            page_info = pages[index] if isinstance(pages[index], dict) else {}
            # 优先用 page_info.thumbnail（相对路径）
            thumb_path = page_info.get("thumbnail")
            if isinstance(thumb_path, str) and thumb_path.strip():
                urls.append(_join_cdn(thumb_cdn, thumb_path.strip()))
                continue
            # 兜底用 path
            page_path = page_info.get("path")
            if isinstance(page_path, str) and page_path.strip():
                urls.append(_join_cdn(thumb_cdn, page_path.strip()))
        return urls

    def _extract_full_image_urls(self, config: Dict[str, Any], gallery: Dict[str, Any]) -> List[str]:
        """从 detail 的 pages 数组取全图 URL（用于下载）。"""
        image_cdn = str(config.get("image_cdn_base") or DEFAULT_IMAGE_CDN_BASE)
        pages = gallery.get("pages")
        if not isinstance(pages, list):
            return []
        urls: List[str] = []
        for page_info in pages:
            if not isinstance(page_info, dict):
                continue
            page_path = page_info.get("path")
            if isinstance(page_path, str) and page_path.strip():
                urls.append(_join_cdn(image_cdn, page_path.strip()))
        return urls

    def _split_tags(self, gallery: Dict[str, Any]) -> Dict[str, List[str]]:
        result: Dict[str, List[str]] = {
            "artists": [],
            "groups": [],
            "parodies": [],
            "characters": [],
            "languages": [],
            "categories": [],
            "tags": [],
        }
        raw_tags = gallery.get("tags")
        # search 结果只有 tag_ids（数字数组），无法解析为名称，留空
        if not isinstance(raw_tags, list):
            return result
        for item in raw_tags:
            if not isinstance(item, dict):
                continue
            name = self._extract_tag_name(item.get("name"))
            tag_type = str(item.get("type") or "").strip().lower()
            if not name:
                continue
            if tag_type == _TAG_TYPE_ARTIST:
                result["artists"].append(name)
            elif tag_type == _TAG_TYPE_GROUP:
                result["groups"].append(name)
            elif tag_type == _TAG_TYPE_PARODY:
                result["parodies"].append(name)
            elif tag_type == _TAG_TYPE_CHARACTER:
                result["characters"].append(name)
            elif tag_type == _TAG_TYPE_LANGUAGE:
                result["languages"].append(name)
            elif tag_type == _TAG_TYPE_CATEGORY:
                result["categories"].append(name)
            else:
                result["tags"].append(name)
        return result

    def _to_album_summary(self, config: Dict[str, Any], gallery: Dict[str, Any]) -> Dict[str, Any]:
        gallery_id = str(gallery.get("id") or "").strip()
        title = self._extract_title(gallery)
        subtitle = self._extract_subtitle(gallery)
        tags = self._split_tags(gallery)
        cover_url = self._extract_cover_url(config, gallery)
        return {
            "album_id": gallery_id,
            "title": title,
            "subtitle": subtitle,
            "title_jp": subtitle,
            "author": ", ".join(tags["artists"]) or "Unknown",
            "cover_url": cover_url,
            "tags": tags["tags"],
            "artists": tags["artists"],
            "groups": tags["groups"],
            "parodies": tags["parodies"],
            "characters": tags["characters"],
            "languages": tags["languages"],
            "categories": tags["categories"],
            "page_count": _as_int(gallery.get("num_pages"), 0, 0, 100000),
            "pages": _as_int(gallery.get("num_pages"), 0, 0, 100000),  # 兼容旧格式
            "upload_date": _as_int(gallery.get("upload_date"), 0, 0, 10**12),
            "platform": NHENTAI_PLATFORM,
            "host_id": f"{NHENTAI_HOST_ID_PREFIX}{gallery_id}",
            "source_url": self._gallery_page_url(config, gallery_id),
        }

    def _to_album_detail(self, config: Dict[str, Any], gallery: Dict[str, Any]) -> Dict[str, Any]:
        summary = self._to_album_summary(config, gallery)
        preview_urls = self._extract_preview_urls(config, gallery, max_count=20)
        full_urls = self._extract_full_image_urls(config, gallery)
        summary.update({
            "media_id": str(gallery.get("media_id") or "").strip(),
            "preview_urls": preview_urls,
            "preview_pages": list(range(1, len(preview_urls) + 1)),
            "image_urls": full_urls,
            "scanlator": str(gallery.get("scanlator") or "").strip(),
            "raw": gallery,
        })
        return summary

    def _gallery_page_url(self, config: Dict[str, Any], gallery_id: str) -> str:
        base_url = str(config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        return f"{base_url}/g/{gallery_id}/"

    def _resolve_gallery_raw(
        self,
        session: requests.Session,
        config: Dict[str, Any],
        album_id: str,
    ) -> Optional[Dict[str, Any]]:
        gallery_id = str(album_id or "").strip()
        if not gallery_id:
            return None
        # 兼容 host_id 形态，例如 NH12345 -> 12345
        if gallery_id.upper().startswith(NHENTAI_HOST_ID_PREFIX):
            gallery_id = gallery_id[len(NHENTAI_HOST_ID_PREFIX):]
        payload = self._api_get(session, config, f"/api/v2/galleries/{gallery_id}")
        if payload is None:
            return None
        if isinstance(payload, dict) and "id" in payload:
            return dict(payload)
        if isinstance(payload, dict) and isinstance(payload.get("result"), list):
            results = payload["result"]
            return dict(results[0]) if results else None
        return None

    def _download_file(
        self,
        session: requests.Session,
        config: Dict[str, Any],
        url: str,
        save_path: str,
    ) -> bool:
        try:
            timeout = _as_int(config.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS, 1, 600)
            response = session.get(url, timeout=timeout, stream=True)
            if response.status_code >= 400:
                return False
            os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
            with open(save_path, "wb") as handle:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        handle.write(chunk)
            return True
        except Exception:
            return False

    # ---------- 协议入口 ----------

    def _declared_capabilities(self) -> set:
        """以清单声明为准；清单缺少 capabilities 时回退到代码内置能力集。"""
        raw = None
        if isinstance(self.manifest, dict):
            raw = self.manifest.get("capabilities")
        declared = {
            str((item or {}).get("key") or "").strip()
            for item in (raw or [])
            if isinstance(item, dict)
        }
        declared.discard("")
        return declared or set(SUPPORTED_CAPABILITIES)

    def execute(self, capability: str, params: Dict[str, Any], context: Dict[str, Any], config: Dict[str, Any]):
        normalized = self.normalize_config(config)
        if capability not in self._declared_capabilities():
            raise ValueError(f"unsupported capability: {capability}")

        if capability == "health.query.status":
            return self.get_query_status(config)

        enabled = _as_bool(normalized.get("enabled"), False)

        # taxonomy.* 不参与启用门禁：未启用时按宿主契约优雅降级为空结果
        if capability == "taxonomy.tags":
            if not enabled:
                return {"tags": [], "categories": {}}
            return self._handle_tags(self._build_session(normalized), normalized, params)
        if capability == "taxonomy.tag_search":
            if not enabled:
                return {
                    "page": _as_int(params.get("page"), 1, 1, 10000),
                    "has_next": False,
                    "albums": [],
                }
            return self._handle_tag_search(self._build_session(normalized), normalized, params)

        # storage.* 是纯路径解析，既不联网也不依赖启用状态
        if capability == "storage.comic_dir.resolve":
            return self._handle_comic_dir_resolve(params, normalized)

        if not enabled:
            raise RuntimeError("NHentai 插件未启用。")

        session = self._build_session(normalized)

        if capability == "catalog.search":
            return self._handle_search(session, normalized, params)
        if capability == "catalog.detail":
            return self._handle_detail(session, normalized, params)
        if capability == "asset.bundle.fetch":
            return self._handle_bundle_fetch(session, normalized, params)
        if capability == "asset.cover.fetch":
            return self._handle_cover_fetch(session, normalized, params)
        if capability == "asset.preview.resolve":
            return self._handle_preview_resolve(session, normalized, params)

        raise ValueError(f"unsupported capability: {capability}")

    # ---------- 能力实现 ----------

    def _handle_tags(
        self,
        session: requests.Session,
        config: Dict[str, Any],
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """处理 taxonomy.tags — 获取 nhentai 可用标签列表。

        使用 OpenAPI 规范中的正确端点：
          - POST /api/v2/tags/search  — 关键词前缀搜索（当提供了 keyword 时）
          - GET  /api/v2/tags/{tag_type} — 按类型分页获取全部标签（浏览模式）
        """
        keyword = str(params.get("keyword") or "").strip().lower()
        category = str(params.get("category") or "").strip().lower()
        now = time.time()

        # 关键词搜索模式 — 使用 POST /api/v2/tags/search
        if keyword:
            return self._search_tags_by_keyword(session, config, keyword, category)

        # 浏览模式 — 使用 GET /api/v2/tags/{tag_type} 按类型获取
        cache = getattr(self, "_tags_cache", None)
        if cache and now - cache["time"] < 600:
            all_tags_by_type = cache["data"]
        else:
            all_tags_by_type = self._fetch_tags_by_type(session, config)
            self._tags_cache = {"data": all_tags_by_type, "time": now}

        # 按分类过滤
        filtered = {}
        for tag_type_str, tags in all_tags_by_type.items():
            if category and tag_type_str != category:
                continue
            filtered[tag_type_str] = tags

        categories = [
            {"key": k, "name": k.capitalize(), "count": len(v)}
            for k, v in filtered.items()
        ]

        if category:
            all_tags = filtered.get(category, [])
        else:
            all_tags = []
            for tags in filtered.values():
                all_tags.extend(tags)

        return {"tags": all_tags, "categories": categories}

    def _search_tags_by_keyword(
        self,
        session: requests.Session,
        config: Dict[str, Any],
        keyword: str,
        category: str,
    ) -> Dict[str, Any]:
        """使用 POST /api/v2/tags/search 按名称前缀搜索标签。"""
        body: Dict[str, Any] = {"query": keyword, "limit": 50}
        if category:
            body["type"] = category

        timeout = _as_int(config.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS, 1, 600)
        base_url = str(config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        url = f"{base_url}/api/v2/tags/search"

        response = session.post(url, json=body, timeout=timeout)
        if response.status_code >= 400:
            raise RuntimeError(f"nhentai tags/search 失败: {response.status_code} {response.reason}")

        try:
            raw_tags = response.json()
        except Exception as exc:
            raise RuntimeError(f"nhentai tags/search 响应解析失败: {exc}") from exc

        if not isinstance(raw_tags, list):
            raw_tags = []

        # 归一化为统一格式
        tags: List[Dict[str, Any]] = []
        type_counts: Dict[str, int] = {}
        for t in raw_tags:
            if not isinstance(t, dict):
                continue
            tag_id = str(t.get("id") or "")
            tag_name = str(t.get("name") or "").strip()
            tag_type = str(t.get("type") or "tag").strip().lower()
            tag_count = _as_int(t.get("count"), 0, 0)
            if not tag_name:
                continue
            if category and tag_type != category:
                continue
            tags.append({
                "id": tag_id,
                "name": tag_name,
                "count": tag_count,
                "category": tag_type,
            })
            type_counts[tag_type] = type_counts.get(tag_type, 0) + 1

        categories = [
            {"key": k, "name": k.capitalize(), "count": v}
            for k, v in type_counts.items()
        ]

        return {"tags": tags, "categories": categories}

    def _fetch_tags_by_type(
        self,
        session: requests.Session,
        config: Dict[str, Any],
    ) -> Dict[str, List[Dict[str, Any]]]:
        """使用 GET /api/v2/tags/{tag_type} 获取所有已知类型的标签。"""
        result: Dict[str, List[Dict[str, Any]]] = {}
        timeout = _as_int(config.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS, 1, 600)
        base_url = str(config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")

        for tag_type_str in _ALL_TAG_TYPES:
            url = f"{base_url}/api/v2/tags/{tag_type_str}"
            params = {"page": 1, "per_page": 100, "sort": "popular"}
            try:
                response = session.get(url, params=params, timeout=timeout)
                if response.status_code != 200:
                    continue
                data = response.json()
                raw_tags = list(data.get("result") or [])
            except Exception:
                continue

            normalized: List[Dict[str, Any]] = []
            for t in raw_tags:
                if not isinstance(t, dict):
                    continue
                tag_id = str(t.get("id") or "")
                tag_name = str(t.get("name") or "").strip()
                tag_count = _as_int(t.get("count"), 0, 0)
                if not tag_name:
                    continue
                normalized.append({
                    "id": tag_id,
                    "name": tag_name,
                    "count": tag_count,
                    "category": tag_type_str,
                })
            result[tag_type_str] = normalized

        return result

    def _handle_tag_search(
        self,
        session: requests.Session,
        config: Dict[str, Any],
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """处理 taxonomy.tag_search — 通过标签名搜索 nhentai 作品。"""
        tag_ids = params.get("tag_ids") or []
        if not isinstance(tag_ids, list):
            if isinstance(tag_ids, str):
                tag_ids = [tag_ids]
            else:
                tag_ids = []
        tag_ids = [str(tid).strip() for tid in tag_ids if str(tid).strip()]

        page = _as_int(params.get("page"), 1, 1, 10000)

        # 用标签名构造查询："tag:"name1" tag:"name2""
        query_parts = [f'tag:"{tid}"' for tid in tag_ids if tid]
        query = " ".join(query_parts) if query_parts else ""

        api_params: Dict[str, Any] = {"page": page}
        if query:
            api_params["query"] = query

        payload = self._api_get(session, config, "/api/v2/search", params=api_params)
        if payload is None:
            return {"page": page, "has_next": False, "albums": [], "query": query, "effective_tag_ids": tag_ids}

        results = payload.get("result") if isinstance(payload, dict) else None
        if results is None and isinstance(payload, list):
            results = payload
        results = list(results or [])

        albums = [self._to_album_summary(config, dict(item)) for item in results if isinstance(item, dict)]
        num_pages = _as_int(payload.get("num_pages") if isinstance(payload, dict) else 0, 1, 1, 100000)
        has_next = page < num_pages

        return {
            "page": page,
            "has_next": has_next,
            "num_pages": num_pages,
            "albums": albums,
            "query": query,
            "requested_tag_ids": tag_ids,
            "effective_tag_ids": tag_ids,
            "invalid_tag_ids": [],
            "overridden_tag_ids": [],
        }

    def _handle_search(
        self,
        session: requests.Session,
        config: Dict[str, Any],
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        keyword = str(params.get("keyword") or params.get("query") or "").strip()
        page = _as_int(params.get("page"), 1, 1, 10000)
        sort = str(params.get("sort") or "date").strip().lower() or "date"
        if sort not in {"date", "popular", "popular-week", "popular-today"}:
            sort = "date"

        api_params: Dict[str, Any] = {"page": page}
        if keyword:
            api_params["query"] = keyword
            api_params["sort"] = sort
            path = "/api/v2/search"
        else:
            path = "/api/v2/galleries"

        payload = self._api_get(session, config, path, params=api_params)
        if payload is None:
            return {"page": page, "has_next": False, "albums": [], "keyword": keyword}

        results = payload.get("result") if isinstance(payload, dict) else None
        if results is None and isinstance(payload, list):
            results = payload
        results = list(results or [])

        albums = [self._to_album_summary(config, dict(item)) for item in results if isinstance(item, dict)]
        num_pages = _as_int(payload.get("num_pages") if isinstance(payload, dict) else 0, 1, 1, 100000)
        per_page = _as_int(payload.get("per_page") if isinstance(payload, dict) else 0, 25, 1, 1000)
        has_next = page < num_pages
        total = _as_int(
            payload.get("total") if isinstance(payload, dict) else 0,
            len(albums), 0, 10**9,
        )

        return {
            "page": page,
            "per_page": per_page,
            "num_pages": num_pages,
            "has_next": has_next,
            "total": total,
            "keyword": keyword,
            "sort": sort,
            "albums": albums,
        }

    def _handle_detail(
        self,
        session: requests.Session,
        config: Dict[str, Any],
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        album_id = str(params.get("album_id") or params.get("id") or "").strip()
        if not album_id:
            raise RuntimeError("catalog.detail 缺少 album_id 参数。")
        gallery = self._resolve_gallery_raw(session, config, album_id)
        if not gallery:
            return {"album_id": album_id, "found": False, "albums": []}
        detail = self._to_album_detail(config, gallery)
        return {"albums": [detail]}

    def _handle_preview_resolve(
        self,
        session: requests.Session,
        config: Dict[str, Any],
        params: Dict[str, Any],
    ) -> List[str]:
        album_id = str(params.get("album_id") or "").strip()
        preview_pages = params.get("preview_pages") or []
        gallery = self._resolve_gallery_raw(session, config, album_id)
        if not gallery:
            return []
        max_count = len(preview_pages) if isinstance(preview_pages, list) and preview_pages else None
        return self._extract_preview_urls(config, gallery, max_count=max_count)

    def _handle_cover_fetch(
        self,
        session: requests.Session,
        config: Dict[str, Any],
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        album_id = str(params.get("album_id") or "").strip()
        save_path = str(params.get("save_path") or "").strip()
        if not album_id or not save_path:
            raise RuntimeError("asset.cover.fetch 缺少 album_id 或 save_path。")
        gallery = self._resolve_gallery_raw(session, config, album_id)
        if not gallery:
            return {
                "detail": {"album_id": album_id, "found": False, "total_pages": 0, "local_pages": 0},
                "success": False,
            }
        cover_url = self._extract_cover_url(config, gallery)
        if not cover_url:
            detail = self._to_album_summary(config, gallery)
            detail["total_pages"] = _as_int(gallery.get("num_pages"), 0, 0, 100000)
            detail["local_pages"] = 0
            return {"detail": detail, "success": False}
        ok = self._download_file(session, config, cover_url, save_path)
        detail = self._to_album_summary(config, gallery)
        detail["cover_path"] = save_path if ok else ""
        detail["total_pages"] = _as_int(gallery.get("num_pages"), 0, 0, 100000)
        detail["local_pages"] = 1 if ok else 0
        return {"detail": detail, "success": ok}

    def _handle_bundle_fetch(
        self,
        session: requests.Session,
        config: Dict[str, Any],
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        album_id = str(params.get("album_id") or "").strip()
        download_dir = str(params.get("download_dir") or "").strip()
        if not album_id or not download_dir:
            raise RuntimeError("asset.bundle.fetch 缺少 album_id 或 download_dir。")
        show_progress = _as_bool(params.get("show_progress"), False)
        gallery = self._resolve_gallery_raw(session, config, album_id)
        if not gallery:
            return {
                "detail": {"album_id": album_id, "found": False, "total_pages": 0, "local_pages": 0},
                "success": False,
            }

        image_urls = self._extract_full_image_urls(config, gallery)
        if not image_urls:
            return {"detail": self._to_album_summary(config, gallery), "success": False}

        # 按 album 分组下载到子目录，与 storage.comic_dir 模板 {host_prefix}/{original_id} 对齐
        album_dir = os.path.join(download_dir, album_id)
        os.makedirs(album_dir, exist_ok=True)
        total = len(image_urls)
        succeeded = 0
        saved_paths: List[str] = []
        logger = _get_logger() if show_progress else None
        for index, url in enumerate(image_urls, start=1):
            ext = "jpg"
            path_part = url.rsplit(".", 1)[-1].split("?")[0].split("/")[-1]
            if path_part in {"webp", "jpg", "jpeg", "png", "gif"}:
                ext = path_part
            filename = f"{index:04d}.{ext}"
            save_path = os.path.join(album_dir, filename)
            ok = self._download_file(session, config, url, save_path)
            if ok:
                succeeded += 1
                saved_paths.append(save_path)
            if logger:
                logger.info(f"nhentai 下载 {album_id}: {index}/{total} ({'OK' if ok else 'FAIL'})")
            time.sleep(0.05)

        detail = self._to_album_detail(config, gallery)
        detail["download_dir"] = album_dir
        detail["saved_files"] = saved_paths
        detail["downloaded_count"] = succeeded
        detail["total_count"] = total
        detail["total_pages"] = total
        detail["local_pages"] = succeeded  # 兼容旧字段
        detail["pages_count"] = total
        return {
            "detail": detail,
            "success": succeeded == total,
            "partial": 0 < succeeded < total,
        }

    def _handle_comic_dir_resolve(
        self,
        params: Dict[str, Any],
        config: Dict[str, Any],
    ) -> str:
        album_id = str(params.get("album_id") or "").strip()
        base_dir = str(params.get("base_dir") or "").strip()
        if not base_dir:
            return album_id or "unknown"
        return os.path.join(base_dir, album_id) if album_id else base_dir


def _get_logger():
    """插件自带日志器，不再反向依赖宿主内部模块。"""
    return logging.getLogger(__name__)
