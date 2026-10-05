"""NHentai 插件协议契约测试（API_INTEGRATION_STANDARD §10）。

全部离线：不联网、不启动后端，只校验清单、Provider 契约与纯函数行为。
运行方式（在项目根目录）：python -m pytest comic_backend/third_party/NHentai/tests -q
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys

import pytest

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_ROOT = os.path.abspath(os.path.join(PLUGIN_DIR, "..", ".."))
PROJECT_ROOT = os.path.dirname(BACKEND_ROOT)
for _root in (BACKEND_ROOT, PROJECT_ROOT):
    if _root not in sys.path:
        sys.path.insert(0, _root)

protocol_base = pytest.importorskip("protocol.base")

MANIFEST_PATH = os.path.join(PLUGIN_DIR, "ultimate-plugin.json")
PROVIDER_PATH = os.path.join(PLUGIN_DIR, "ultimate_provider.py")

PLUGIN_ID = "comic.nhentai"
ENTRYPOINT = "./ultimate_provider.py:NHentaiProvider"
PROTOCOL_VERSIONS = {"1.0", "1.1", "2.0"}
FIELD_TYPES = {"boolean", "text", "password", "textarea", "number"}
SECRET_FIELDS = ("api_key",)
REQUIRED_FIELDS: tuple = ()
RUNTIME_DEPENDENCIES = ("requests",)

FAKE_GALLERY = {
    "id": 123,
    "media_id": "999",
    "title": {"english": "Title EN", "japanese": "タイトル"},
    "num_pages": 24,
    "upload_date": 1700000000,
    "tags": [{"name": {"english": "artist one"}, "type": "artist"}],
    "pages": [
        {"path": "galleries/999/1.jpg", "thumbnail": "galleries/999/1t.jpg"},
        {"path": "galleries/999/2.jpg", "thumbnail": "galleries/999/2t.jpg"},
    ],
    "cover": {"path": "galleries/999/cover.jpg"},
}


@pytest.fixture(scope="module")
def manifest() -> dict:
    with open(MANIFEST_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def plugin_module():
    spec = importlib.util.spec_from_file_location("_nhentai_plugin_under_test", PROVIDER_PATH)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


@pytest.fixture()
def provider(manifest, plugin_module):
    return plugin_module.NHentaiProvider(manifest=manifest, manifest_path=MANIFEST_PATH)


def _capability_keys(manifest: dict) -> set:
    return {str(item.get("key") or "").strip() for item in manifest["capabilities"]}


def _configuration_fields(manifest: dict) -> list:
    return [
        field
        for section in manifest["configuration"]["sections"]
        for field in section.get("fields") or []
    ]


# ---------- 清单契约 ----------

def test_manifest_minimum_contract(manifest):
    assert manifest["protocol_version"] in PROTOCOL_VERSIONS
    plugin = manifest["plugin"]
    assert plugin["id"] == PLUGIN_ID
    assert plugin["entrypoint"] == ENTRYPOINT
    assert plugin["config_key"] == "nhentai"
    assert plugin["version"]
    assert manifest["media_types"] == ["comic"]
    assert manifest["identity"]["host_id_prefix"] == "NH"
    assert manifest["identity"]["platform_label"] == "NHentai"


def test_capabilities_match_provider_constant(manifest, plugin_module):
    assert _capability_keys(manifest) == set(plugin_module.SUPPORTED_CAPABILITIES)


def test_capability_dispatch_covers_declared_set(plugin_module):
    with open(PROVIDER_PATH, "r", encoding="utf-8") as handle:
        source = handle.read()
    dispatched = set(re.findall(r'capability\s*==\s*"([^"]+)"', source))
    assert dispatched == set(plugin_module.SUPPORTED_CAPABILITIES)


def test_configuration_field_types_are_supported(manifest):
    for field in _configuration_fields(manifest):
        assert field["type"] in FIELD_TYPES, field


def test_configuration_credential_block_is_consistent(manifest):
    credential = manifest["configuration"]["credential"]
    fields = _configuration_fields(manifest)
    boolean_fields = {field["key"] for field in fields if field["type"] == "boolean"}
    assert credential["enabled_field"] in boolean_fields
    assert set(credential.get("required_fields") or []) <= {field["key"] for field in fields}
    assert credential["required_fields"] == list(REQUIRED_FIELDS)
    assert credential["disabled_message"]


def test_secret_fields_are_flagged(manifest):
    secrets = {field["key"] for field in _configuration_fields(manifest) if field.get("secret")}
    assert secrets == set(SECRET_FIELDS)


def test_packaging_declares_dependency_pool_entries(manifest):
    packaging = manifest["packaging"]
    assert packaging["android"]["enabled"] is True
    declared = set()
    for platform in ("android", "external", "pyinstaller"):
        requirements = packaging[platform]["pip_requirements"]
        assert requirements and all(str(item).strip() for item in requirements)
        declared |= {str(item).strip().lower().replace("_", "-") for item in requirements}
    for name in RUNTIME_DEPENDENCIES:
        assert name in declared


def test_presentation_matches_comic_card_contract(manifest):
    cover = manifest["presentation"]["media_card"]["cover"]
    assert cover["aspect_ratio"] == "2 / 3"
    assert cover["mobile_aspect_ratio"]
    assert cover["path_mode"] in {"local_static", "remote_first"}


def test_storage_keeps_legacy_comic_dir_binding(manifest):
    # 宿主 core/host_platform_fallback.py 仍按 storage.comic_dir 解析本地目录
    assert manifest["storage"]["comic_dir"]["template"]


def test_resource_policy_uses_host_asset_keys(manifest):
    assets = manifest["resource_policy"]["assets"]
    assert set(assets) <= {"image", "cover", "preview_video", "video", "asset"}


# ---------- Provider 契约 ----------

def test_provider_inherits_protocol_provider(provider):
    assert isinstance(provider, protocol_base.ProtocolProvider)
    for method in ("execute", "normalize_config", "serialize_public_config", "get_query_status", "build_client"):
        assert callable(getattr(provider, method))


def test_undeclared_capability_is_rejected(provider):
    with pytest.raises(ValueError):
        provider.execute("catalog.by_code", {}, {}, {"enabled": True})


def test_enabled_defaults_to_disabled(provider):
    assert provider.normalize_config({})["enabled"] is False
    assert provider.get_query_status({})["configured"] is False


def test_health_status_answers_while_disabled(provider):
    status = provider.execute("health.query.status", {}, {}, {})
    assert set(status) >= {"configured", "message", "missing_fields"}
    assert status["configured"] is False


def test_disabled_provider_refuses_catalog(provider):
    with pytest.raises(RuntimeError):
        provider.execute("catalog.search", {"keyword": "x"}, {}, {"enabled": False})


def test_taxonomy_degrades_gracefully_while_disabled(provider):
    tags = provider.execute("taxonomy.tags", {}, {}, {"enabled": False})
    assert tags == {"tags": [], "categories": {}}
    result = provider.execute("taxonomy.tag_search", {}, {}, {"enabled": False})
    assert result["albums"] == []
    assert result["has_next"] is False


def test_storage_resolution_works_while_disabled(provider):
    resolved = provider.execute(
        "storage.comic_dir.resolve", {"album_id": "123", "base_dir": "/data"}, {}, {}
    )
    assert resolved == os.path.join("/data", "123")


def test_detail_requires_album_id(provider):
    with pytest.raises(RuntimeError):
        provider.execute("catalog.detail", {}, {}, {"enabled": True})


def test_api_key_is_not_wiped_by_empty_or_masked_save(provider):
    assert "api_key" not in provider.normalize_config({"enabled": True})
    assert "api_key" not in provider.normalize_config({"enabled": True, "api_key": ""})
    assert "api_key" not in provider.normalize_config({"enabled": True, "api_key": "********"})
    assert provider.normalize_config({"enabled": True, "api_key": "real-key"})["api_key"] == "real-key"


def test_public_config_hides_api_key(provider):
    public = provider.serialize_public_config({"enabled": True, "api_key": "secret-value"})
    assert "api_key" not in public
    assert public["api_key_configured"] is True
    assert provider.serialize_public_config({"enabled": True})["api_key_configured"] is False


def test_album_summary_contract(provider, manifest):
    summary = provider._to_album_summary({}, FAKE_GALLERY)
    assert summary["album_id"] == "123"
    assert summary["title"] == "Title EN"
    assert summary["title_jp"] == "タイトル"
    assert summary["author"] == "artist one"
    assert summary["host_id"] == f'{manifest["identity"]["host_id_prefix"]}123'
    assert isinstance(summary["pages"], int)
    assert summary["cover_url"].startswith("https://")


def test_cover_fetch_detail_contract(provider, monkeypatch, tmp_path):
    monkeypatch.setattr(provider, "_resolve_gallery_raw", lambda *args, **kwargs: FAKE_GALLERY)
    monkeypatch.setattr(provider, "_download_file", lambda *args, **kwargs: True)
    result = provider.execute(
        "asset.cover.fetch",
        {"album_id": "123", "save_path": str(tmp_path / "cover.jpg")},
        {},
        {"enabled": True},
    )
    assert result["success"] is True
    detail = result["detail"]
    assert detail["album_id"] == "123"
    assert detail["total_pages"] == 24
    assert detail["local_pages"] == 1


def test_bundle_fetch_detail_contract(provider, monkeypatch, tmp_path):
    monkeypatch.setattr(provider, "_resolve_gallery_raw", lambda *args, **kwargs: FAKE_GALLERY)
    monkeypatch.setattr(provider, "_download_file", lambda *args, **kwargs: True)
    result = provider.execute(
        "asset.bundle.fetch",
        {"album_id": "123", "download_dir": str(tmp_path), "show_progress": False},
        {},
        {"enabled": True},
    )
    assert result["success"] is True
    detail = result["detail"]
    assert detail["album_id"] == "123"
    assert detail["total_pages"] == 2
    assert detail["local_pages"] == 2
    assert detail["saved_files"]
