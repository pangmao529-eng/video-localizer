"""Separate MediaKit profile; its credential never replaces the Ark credential."""
import getpass
import json
import sys

from . import config

ENDPOINT = "https://mediakit.cn-beijing.volces.com/api/v1"


def settings():
    path = config.app_dir() / "mediakit.json"
    if not path.exists():
        return None
    try:
        if path.stat().st_size > 4096:
            raise ValueError()
        value = json.loads(path.read_text(encoding="utf-8"))
        if (set(value) != {"provider", "base_url", "model_version"}
                or value["provider"] != "mediakit" or value["base_url"] != ENDPOINT
                or value["model_version"] not in {"v5", "v5.6"}):
            raise ValueError()
        return value
    except (ValueError, TypeError, OSError):
        raise config.SettingsError("MediaKit 配置无效，请重新保存独立配置。") from None


def save_configuration(key="", *, model_version="v5.6"):
    if model_version not in {"v5", "v5.6"}:
        raise config.SettingsError("MediaKit 精细擦除版本须为 v5 或 v5.6。")
    secret, supplied = config._resolved_key(ENDPOINT, key)
    if secret.startswith("ark-"):
        raise config.SettingsError("MediaKit 需要对应服务凭据，不能复用 Ark API Key。")
    if supplied:
        config._store_key(ENDPOINT, secret)
    value = {"provider": "mediakit", "base_url": ENDPOINT, "model_version": model_version}
    config.write_json(config.app_dir() / "mediakit.json", value)
    return {"configured": True, **value, "connection_tested": False}


def configure(*, model_version="v5.6"):
    if not sys.stdin.isatty():
        raise config.SettingsError("请在交互式终端配置 MediaKit 密钥；命令行参数不接受密钥。")
    return save_configuration(getpass.getpass("MediaKit API Key（不回显，留空沿用）: "),
                              model_version=model_version)


def check_setup():
    value = settings()
    key_available = False
    if value:
        try:
            config._checked_key(config.read_key(ENDPOINT))
            key_available = True
        except config.SettingsError:
            pass
    return {"provider": "mediakit", "configured": value is not None,
            "credential_available": key_available, "connection_tested": False,
            "model_version": value.get("model_version") if value else None}
