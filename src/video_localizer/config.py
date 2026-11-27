"""User-scoped model settings; secrets stay in Windows Credential Manager."""

import ctypes
from ctypes import wintypes as wt
import getpass
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit, urlunsplit

from .persistence import atomic_replace


SEEDANCE_DEFAULT_ENDPOINT = "https://ark.cn-beijing.volces.com/api/v3"
SEEDANCE_20_MODEL = "doubao-seedance-2-0-260128"
SEEDANCE_25_MODEL = "doubao-seedance-2-5-260628"
SEEDANCE_20_FAMILY = "seedance-2.0"
SEEDANCE_25_FAMILY = "seedance-2.5"
SEEDANCE_DEFAULT_MODEL = SEEDANCE_20_MODEL
_SEEDANCE_FAMILIES = {SEEDANCE_20_MODEL: SEEDANCE_20_FAMILY,
                      SEEDANCE_25_MODEL: SEEDANCE_25_FAMILY}
_ARK_ENDPOINT_ID = re.compile(r"ep-[A-Za-z0-9][A-Za-z0-9_-]{0,123}")
PROVIDERS = {
    "seedance": {"base_url": SEEDANCE_DEFAULT_ENDPOINT, "model": SEEDANCE_DEFAULT_MODEL,
                 "models": frozenset({SEEDANCE_20_MODEL, SEEDANCE_25_MODEL})},
}


class SettingsError(ValueError):
    pass


def seedance_family(model, model_family=None):
    """Resolve supported capabilities separately from an Ark deployment ID."""
    if not isinstance(model, str):
        raise SettingsError("请填写受支持的 Seedance 模型 ID 或推理接入点 ID。")
    name = model.strip()
    if model_family is not None:
        if not isinstance(model_family, str) or model_family.strip() not in _SEEDANCE_FAMILIES.values():
            raise SettingsError("Seedance 系列须为 seedance-2.0 或 seedance-2.5。")
        model_family = model_family.strip()
    known = _SEEDANCE_FAMILIES.get(name)
    if known:
        if model_family is not None and model_family != known:
            raise SettingsError("Seedance 模型 ID 与所选系列不匹配。")
        return known
    if not _ARK_ENDPOINT_ID.fullmatch(name):
        raise SettingsError("请填写受支持的 Seedance 模型 ID 或有效的 ep- 推理接入点 ID。")
    if model_family is None:
        raise SettingsError("使用 ep- 推理接入点时必须明确选择 Seedance 系列，不能从接入点 ID 推断型号。")
    return model_family


def app_dir():
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        raise SettingsError("需要 Windows 用户环境（LOCALAPPDATA）。")
    return Path(root) / "YoungTiger" / "VideoLocalizer"


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".vl-", suffix=".tmp", delete=False) as out:
        temp = Path(out.name)
        try:
            json.dump(data, out, ensure_ascii=False, indent=2)
            out.write("\n")
        except BaseException:
            out.close()
            temp.unlink(missing_ok=True)
            raise
    try:
        atomic_replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def validated_endpoint(value):
    if not isinstance(value, str):
        raise SettingsError("请填写模型 API 地址。")
    raw = value.strip().rstrip("/")
    try:
        url = urlsplit(raw)
        port = url.port
        local = url.hostname in {"localhost", "127.0.0.1", "::1"}
        if (url.scheme != "https" and not (local and url.scheme == "http")) or not url.hostname:
            raise ValueError()
        if url.username is not None or url.password is not None or url.query or url.fragment or "\\" in raw:
            raise ValueError()
        if any(ord(c) < 33 for c in raw) or port == 0:
            raise ValueError()
    except ValueError:
        raise SettingsError("API 地址须为 HTTPS，或本机回环 HTTP 地址；请将密钥填入独立输入框。") from None
    return urlunsplit((url.scheme, url.netloc, url.path, "", ""))


def _provider_for_model(model, provider=None):
    if not isinstance(model, str):
        raise SettingsError("请填写受支持的模型 ID。")
    name = model.strip()
    if provider not in (None, "seedance"):
        raise SettingsError("此发行版仅支持 Seedance，请使用 Seedance 模型配置。")
    if name not in PROVIDERS["seedance"]["models"] and not _ARK_ENDPOINT_ID.fullmatch(name):
        raise SettingsError("请填写受支持的 Seedance 模型 ID 或有效的 ep- 推理接入点 ID。")
    return "seedance"


def _nonsecret_configuration(data):
    if not isinstance(data, dict):
        raise SettingsError("模型配置格式无效。")
    model = data.get("model")
    provider = _provider_for_model(model, data.get("provider"))
    endpoint = validated_endpoint(data.get("base_url"))
    if urlsplit(endpoint).scheme != "https":
        raise SettingsError("Seedance API 地址须为 HTTPS 基础地址。")
    if urlsplit(endpoint).hostname == "tokenhub.tencentmaas.com":
        raise SettingsError("Seedance 模型不能使用旧提供方的 API 地址。")
    clean = {"provider": provider, "base_url": endpoint, "model": model.strip()}
    family = seedance_family(clean["model"], data.get("model_family"))
    if data.get("model_family") is not None or _ARK_ENDPOINT_ID.fullmatch(clean["model"]):
        clean["model_family"] = family
    return clean


def settings():
    path = app_dir() / "settings.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return _nonsecret_configuration(data)
    except (OSError, ValueError, TypeError, KeyError):
        raise SettingsError("模型配置无法读取或属于旧提供方；请重新运行 Seedance setup。") from None


def _read_configuration_file(name):
    path = app_dir() / name
    if not path.exists():
        return {}
    try:
        if path.stat().st_size > 65536:
            raise ValueError()
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (OSError, ValueError, TypeError, KeyError):
        raise SettingsError("已保存的配置档案无法读取，请先修复该配置文件，避免覆盖原记录。") from None


def _legacy_configuration(data):
    """Read-only allowlist for archiving; never enables a legacy provider."""
    if (not isinstance(data, dict) or data.get("provider", "hunyuan") != "hunyuan"
            or data.get("model") not in {"hy4", "hy4-flash"}):
        raise SettingsError("旧配置档案格式无效，未覆盖原记录。")
    return {"provider": "hunyuan", "base_url": validated_endpoint(data.get("base_url")),
            "model": data["model"]}


def _profile_records():
    active, legacy = {}, []
    for provider, profile in _read_configuration_file("profiles.json").items():
        if provider == "seedance":
            active[provider] = _nonsecret_configuration(profile)
        elif provider == "hunyuan":
            legacy.append(_legacy_configuration(profile))
        else:
            raise SettingsError("配置档案包含未知提供方，未覆盖原记录。")
    return active, legacy


def _profiles():
    # Old provider records must not prevent opening a usable Seedance profile.
    # Their validation and archival happen only when saving or migrating.
    value = _read_configuration_file("profiles.json").get("seedance")
    return {"seedance": _nonsecret_configuration(value)} if value is not None else {}


def _migration_plan():
    active, legacy = _profile_records()
    current = _read_configuration_file("settings.json")
    legacy_current = bool(current and (current.get("provider") == "hunyuan"
                                      or current.get("model") in {"hy4", "hy4-flash"}))
    if current:
        if legacy_current:
            legacy.append(_legacy_configuration(current))
        else:
            active["seedance"] = _nonsecret_configuration(current)
    archived = {}
    existing = _read_configuration_file("legacy-profiles.json")
    for entry in [*existing.values(), *legacy]:
        clean = _legacy_configuration(entry)
        name = clean["provider"]
        if name in archived and archived[name] != clean:
            name += "-" + hashlib.sha256(json.dumps(clean, sort_keys=True).encode()).hexdigest()[:12]
        archived[name] = clean
    return active, archived, legacy_current


def migrate_legacy_profiles():
    """Archive nonsecret legacy profiles; never read, delete or rewrite vault keys."""
    active, archived, legacy_current = _migration_plan()
    if archived:
        write_json(app_dir() / "legacy-profiles.json", archived)
    write_json(app_dir() / "profiles.json", active)
    if legacy_current:
        (app_dir() / "settings.json").unlink()
    return {"legacy_profiles_archived": len(archived), "active_providers": sorted(active)}


def validated_media_endpoint(value, ark_endpoint=None):
    """Check a public HTTPS staging origin without DNS or credential reads."""
    try:
        if not isinstance(value, str):
            raise ValueError()
        endpoint = value.strip().rstrip("/")
        parsed = urlsplit(endpoint)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (parsed.scheme != "https" or not host or parsed.port not in (None, 443)
                or parsed.username is not None or parsed.password is not None
                or "?" in endpoint or "#" in endpoint or parsed.path != "/codex-media/v1"
                or "\\" in endpoint or "%" in host
                or any(ord(c) < 33 or ord(c) > 126 for c in endpoint)
                or host == "localhost" or "." not in host
                or host.endswith((".localhost", ".local", ".internal"))
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host)):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and (not address.is_global or address.is_multicast):
            raise ValueError()
        if address is None and (all(part.isdigit() for part in host.split("."))
                                or any(not part or len(part) > 63 for part in host.split("."))):
            raise ValueError()
        ark_hosts = {urlsplit(SEEDANCE_DEFAULT_ENDPOINT).hostname}
        if ark_endpoint:
            ark_hosts.add(urlsplit(validated_endpoint(ark_endpoint)).hostname.lower().rstrip("."))
        if host in ark_hosts or (host.startswith("ark.") and host.endswith(".volces.com")):
            raise ValueError()
        return endpoint
    except (ValueError, TypeError):
        raise SettingsError("素材服务器须为独立公网 HTTPS 地址，路径为 /codex-media/v1，不能使用模型 API 的主机。") from None


def media_settings():
    value = _read_configuration_file("media-upload.json")
    if not value:
        return None
    if set(value) != {"provider", "endpoint"} or value.get("provider") != "self_hosted":
        raise SettingsError("自有素材服务器配置无效，请重新保存上传服务配置。")
    return {"provider": "self_hosted", "endpoint": validated_media_endpoint(value.get("endpoint"))}


def _checked_key(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 2560
            or any(ord(c) < 33 or ord(c) > 126 for c in value)):
        raise SettingsError("密钥为空、过长或包含不支持的字符。")
    return value


def _resolved_key(endpoint, value):
    if not isinstance(value, str):
        raise SettingsError("密钥须为文本，留空沿用对应地址的已保存凭据。")
    supplied = value.strip()
    try:
        return _checked_key(supplied or read_key(endpoint)), bool(supplied)
    except SettingsError:
        raise SettingsError("对应接口没有可用的已保存凭据，请填写该接口的独立密钥。") from None


def _prepare_media(endpoint, key, ark_endpoint=None, ark_key=None):
    endpoint = validated_media_endpoint(endpoint, ark_endpoint)
    secret, supplied = _resolved_key(endpoint, key)
    if secret.startswith("ark-") or (ark_key is not None and hmac.compare_digest(secret, ark_key)):
        raise SettingsError("上传服务须使用独立凭据，不能填写或复用 Ark API Key。")
    return {"provider": "self_hosted", "endpoint": endpoint}, secret, supplied


def save_media_configuration(endpoint, key="", *, ark_endpoint=None):
    """Save only staging settings; omitted key reuses that endpoint's vault item."""
    try:
        current = settings()
    except SettingsError:
        current = None
    ark_endpoint = ark_endpoint or (current["base_url"] if current else None)
    if ark_endpoint:
        ark_endpoint = validated_endpoint(ark_endpoint)
    endpoint = validated_media_endpoint(endpoint, ark_endpoint)
    ark_key = None
    if ark_endpoint:
        try:
            ark_key = _checked_key(read_key(ark_endpoint))
        except SettingsError:
            pass
    clean, secret, supplied = _prepare_media(endpoint, key, ark_endpoint, ark_key)
    if supplied:
        _store_key(clean["endpoint"], secret)
    write_json(app_dir() / "media-upload.json", clean)
    return {"configured": True, "provider": "self_hosted", "connection_tested": False}


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR),
        ("Comment", wt.LPWSTR), ("LastWritten", wt.FILETIME),
        ("CredentialBlobSize", wt.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wt.DWORD), ("AttributeCount", wt.DWORD), ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wt.LPWSTR), ("UserName", wt.LPWSTR),
    ]


def _vault():
    if sys.platform != "win32":
        raise SettingsError("首版使用 Windows 凭据管理器。")
    lib = ctypes.WinDLL("advapi32", use_last_error=True)
    lib.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wt.DWORD]
    lib.CredReadW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.POINTER(_Credential))]
    lib.CredFree.argtypes = [ctypes.c_void_p]
    lib.CredFree.restype = None
    return lib


def _target(endpoint):
    return "YoungTiger.VideoLocalizer/" + hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def read_key(endpoint):
    vault = _vault()
    entry = ctypes.POINTER(_Credential)()
    if not vault.CredReadW(_target(endpoint), 1, 0, ctypes.byref(entry)):
        raise SettingsError("未找到该接口的 Windows 凭据，请运行 setup 配置密钥。")
    try:
        row = entry.contents
        return ctypes.string_at(row.CredentialBlob, row.CredentialBlobSize).decode("utf-8")
    finally:
        vault.CredFree(entry)


def _store_key(endpoint, key):
    raw = key.encode("utf-8")
    if not raw or len(raw) > 2560 or any(ord(c) < 32 for c in key):
        raise SettingsError("密钥为空、过长或含控制字符。")
    buf = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    row = _Credential(Type=1, TargetName=_target(endpoint), CredentialBlobSize=len(raw),
                      CredentialBlob=buf, Persist=2, UserName="video-localizer")
    try:
        if not _vault().CredWriteW(ctypes.byref(row), 0):
            raise SettingsError("Windows 凭据管理器保存失败。")
    finally:
        ctypes.memset(buf, 0, len(buf))


def _dialog(initial):
    script = (Path(__file__).parent / "configuration-dialog.ps1").read_text(encoding="utf-8")
    media = media_settings() or {}
    env = dict(os.environ, VL_DIALOG_API=initial.get("base_url", ""),
               VL_DIALOG_MODEL=initial.get("model", ""),
               VL_DIALOG_FAMILY=initial.get("model_family", ""),
               VL_DIALOG_MEDIA_API=initial.get("media_endpoint", media.get("endpoint", "")))
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    saved = None
    with subprocess.Popen([str(shell), "-STA", "-NoProfile", "-Command", script],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=True, encoding="utf-8", env=env, creationflags=subprocess.CREATE_NO_WINDOW) as window:
        for line in window.stdout:
            try:
                saved = _save_configuration(json.loads(line))
            except SettingsError as error:
                reply = {"ok": False, "message": str(error)}
            except OSError:
                reply = {"ok": False, "message": "保存失败。请检查当前用户的配置目录和 Windows 凭据管理器后重试。"}
            else:
                message = "Seedance 和独立素材上传配置已保存，尚未测试连接；不会自动生成视频。"
                if not saved["media_configured"]:
                    message = "Seedance 配置已保存；自有素材上传服务尚未配置，暂不能运行视频流程。"
                reply = {"ok": True, "message": message}
            # PowerShell reads one reply per Save. ASCII framing also works in
            # Windows PowerShell installations that use a legacy input code page.
            window.stdin.write(json.dumps(reply, ensure_ascii=True) + "\n")
            window.stdin.flush()
        status = window.wait()
    if status == 2 and saved is None:
        raise SettingsError("已取消模型配置。")
    if status or saved is None:
        raise SettingsError("配置窗口未能完成操作，请在交互式终端运行 video-localizer setup。")
    return saved


def configure(gui=False, endpoint=None, model=None, provider=None, model_family=None, media_endpoint=None):
    try:
        previous = settings() or {}
    except SettingsError:
        previous = {}
    if provider not in (None, "seedance"):
        raise SettingsError("此发行版仅支持 Seedance，请使用 Seedance 模型配置。")
    if model is not None:
        _provider_for_model(model, provider)
    saved = previous or _profiles().get("seedance", {})
    default = {"provider": "seedance",
               "base_url": endpoint or saved.get("base_url", SEEDANCE_DEFAULT_ENDPOINT),
               "model": model or saved.get("model", SEEDANCE_DEFAULT_MODEL)}
    family = model_family
    if family is None and (model is None or model.strip() == saved.get("model")):
        family = saved.get("model_family")
    if family is not None:
        default["model_family"] = seedance_family(default["model"], family)
    if media_endpoint is not None:
        default["media_endpoint"] = validated_media_endpoint(media_endpoint, default["base_url"])
    if gui:
        return _dialog(default)
    if not sys.stdin.isatty():
        raise SettingsError("请使用 setup --gui 在本机遮挡输入密钥，或在交互式终端运行 setup。")
    url = endpoint or input(f"API 地址 [{default['base_url']}]: ").strip() or default["base_url"]
    name = model or input(f"模型 ID [{default['model']}]: ").strip() or default["model"]
    family = model_family if model_family is not None else (
        default.get("model_family") if name.strip() == default["model"].strip() else None)
    if _ARK_ENDPOINT_ID.fullmatch(name.strip()) and family is None:
        family = input("Seedance 系列（seedance-2.0 / seedance-2.5）: ").strip()
    payload = {"provider": "seedance", "base_url": url, "model": name}
    if family is not None:
        payload["model_family"] = family
    _nonsecret_configuration(payload)
    payload["key"] = getpass.getpass("API Key（不回显，留空沿用该地址的凭据）: ")
    if media_endpoint is not None:
        payload["media_endpoint"] = media_endpoint
        payload["media_key"] = getpass.getpass("素材上传 Key（必须独立于 Ark，留空沿用该地址的凭据）: ")
    return _save_configuration(payload)


def _save_configuration(data):
    if not isinstance(data, dict) or any(not isinstance(data.get(k), str) for k in ("base_url", "model", "key")):
        raise SettingsError("请填写 API 地址、模型 ID 和密钥。")
    clean = _nonsecret_configuration(data)
    url = clean["base_url"]
    profiles, archived, _ = _migration_plan()
    for profile in archived.values():
        if _target(profile["base_url"]) == _target(url):
            raise SettingsError("该 API 地址已有另一提供方的凭据配置，请使用独立地址，避免覆盖原密钥。")
    explicit_media = "media_endpoint" in data
    if "media_key" in data and not explicit_media:
        raise SettingsError("保存上传密钥时必须同时指定独立素材服务器地址。")
    # Validate fields, both independent credentials, and migration inputs before
    # writing any configuration or replacing a vault entry.
    media = ({"endpoint": validated_media_endpoint(data["media_endpoint"], url)}
             if explicit_media else media_settings())
    if media:
        validated_media_endpoint(media["endpoint"], url)
    key, supplied = _resolved_key(url, data["key"])
    media_key, media_supplied = None, False
    if media:
        media, media_key, media_supplied = _prepare_media(
            media["endpoint"], data.get("media_key", ""), url, key)
    # Archive only allowlisted nonsecret legacy fields; old vault targets stay.
    if archived:
        write_json(app_dir() / "legacy-profiles.json", archived)
    if supplied:
        _store_key(url, key)
    if media_supplied:
        _store_key(media["endpoint"], media_key)
    profiles[clean["provider"]] = clean
    write_json(app_dir() / "profiles.json", profiles)
    write_json(app_dir() / "settings.json", clean)
    if explicit_media:
        write_json(app_dir() / "media-upload.json", media)
    result = {"configured": True, "provider": clean["provider"], "model": clean["model"],
              "connection_tested": False, "media_configured": media is not None}
    if "model_family" in clean:
        result["model_family"] = clean["model_family"]
    return result
