"""User-scoped model settings; secrets stay in Windows Credential Manager."""

import ctypes
from ctypes import wintypes as wt
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit, urlunsplit

from .hunyuan import ALLOWED_MODELS, DEFAULT_ENDPOINT, DEFAULT_MODEL
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
    "hunyuan": {"base_url": DEFAULT_ENDPOINT, "model": DEFAULT_MODEL,
                "models": ALLOWED_MODELS},
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
    if provider is None:
        provider = ("seedance" if _ARK_ENDPOINT_ID.fullmatch(name) else
                    next((key for key, value in PROVIDERS.items() if name in value["models"]), None))
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise SettingsError("模型提供方须为 hunyuan 或 seedance，且须使用该提供方支持的模型 ID。")
    if name not in PROVIDERS[provider]["models"] and not (provider == "seedance" and _ARK_ENDPOINT_ID.fullmatch(name)):
        supported = " / ".join(sorted(PROVIDERS[provider]["models"]))
        raise SettingsError(f"{provider} 配置仅支持模型：{supported}。")
    return provider


def _nonsecret_configuration(data, *, legacy=False):
    if not isinstance(data, dict):
        raise SettingsError("模型配置格式无效。")
    model = data.get("model")
    # Old files omitted provider; supported model IDs still identify it without
    # forcing a newly saved Ark endpoint into the legacy Hunyuan branch.
    provider = _provider_for_model(model, data.get("provider"))
    endpoint = validated_endpoint(data.get("base_url"))
    hostname = urlsplit(endpoint).hostname
    for other, defaults in PROVIDERS.items():
        if other != provider and hostname == urlsplit(defaults["base_url"]).hostname:
            raise SettingsError("模型提供方与官方 API 地址不匹配，请使用对应平台的地址和密钥。")
    clean = {"provider": provider, "base_url": endpoint, "model": model.strip()}
    if provider == "seedance":
        family = seedance_family(clean["model"], data.get("model_family"))
        # Keep legacy direct-model profiles byte-shape compatible. Deployments
        # always bind an explicit family so capability limits cannot be guessed.
        if data.get("model_family") is not None or _ARK_ENDPOINT_ID.fullmatch(clean["model"]):
            clean["model_family"] = family
    elif data.get("model_family") is not None:
        raise SettingsError("混元配置不接受 Seedance 系列字段。")
    return clean


def settings():
    path = app_dir() / "settings.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return _nonsecret_configuration(data, legacy=True)
    except (OSError, ValueError, TypeError, KeyError):
        raise SettingsError("模型配置无法读取；请使用受支持的混元或 Seedance 模型重新运行 setup。") from None


def _profiles():
    path = app_dir() / "profiles.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError()
        profiles = {}
        for provider, profile in data.items():
            clean = _nonsecret_configuration(profile, legacy=True)
            if provider != clean["provider"]:
                raise ValueError()
            profiles[provider] = clean
        return profiles
    except (OSError, ValueError, TypeError, KeyError):
        raise SettingsError("已保存的模型配置档案无法读取；请先修复 profiles.json，以免覆盖原配置。") from None


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
    env = dict(os.environ, VL_DIALOG_API=initial.get("base_url", ""),
               VL_DIALOG_MODEL=initial.get("model", ""), VL_DIALOG_PROVIDER=initial.get("provider", "hunyuan"),
               VL_DIALOG_FAMILY=initial.get("model_family", ""))
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
                reply = {"ok": True, "message": "模型配置已保存到本机，尚未测试连接；保存不会自动生成视频。"}
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


def configure(gui=False, endpoint=None, model=None, provider=None, model_family=None):
    try:
        previous = settings() or {}
    except SettingsError:
        previous = {}
    selected = provider if provider is not None else (
        _provider_for_model(model) if model is not None else previous.get("provider", "hunyuan"))
    if not isinstance(selected, str) or selected not in PROVIDERS:
        raise SettingsError("模型提供方须为 hunyuan 或 seedance。")
    if model is not None:
        _provider_for_model(model, selected)
    saved = previous if previous.get("provider") == selected else (
        _profiles().get(selected, {}) if previous else {})
    default = {"provider": selected,
               "base_url": endpoint or saved.get("base_url", PROVIDERS[selected]["base_url"]),
               "model": model or saved.get("model", PROVIDERS[selected]["model"])}
    family = model_family
    if family is None and (model is None or model.strip() == saved.get("model")):
        family = saved.get("model_family")
    if family is not None:
        if selected != "seedance":
            raise SettingsError("混元配置不接受 Seedance 系列字段。")
        default["model_family"] = seedance_family(default["model"], family)
    if gui:
        return _dialog(default)
    if not sys.stdin.isatty():
        raise SettingsError("请使用 setup --gui 在本机遮挡输入密钥，或在交互式终端运行 setup。")
    url = endpoint or input(f"API 地址 [{default['base_url']}]: ").strip() or default["base_url"]
    name = model or input(f"模型 ID [{default['model']}]: ").strip() or default["model"]
    family = model_family if model_family is not None else (
        default.get("model_family") if name.strip() == default["model"].strip() else None)
    if selected == "seedance" and _ARK_ENDPOINT_ID.fullmatch(name.strip()) and family is None:
        family = input("Seedance 系列（seedance-2.0 / seedance-2.5）: ").strip()
    payload = {"provider": selected, "base_url": url, "model": name}
    if family is not None:
        payload["model_family"] = family
    _nonsecret_configuration(payload)
    payload["key"] = getpass.getpass("API Key（不回显，留空沿用该地址的凭据）: ")
    return _save_configuration(payload)


def _save_configuration(data):
    if not isinstance(data, dict) or any(not isinstance(data.get(k), str) for k in ("base_url", "model", "key")):
        raise SettingsError("请填写 API 地址、模型 ID 和密钥。")
    clean = _nonsecret_configuration(data)
    url = clean["base_url"]
    profiles = _profiles()
    try:
        previous = settings()
    except SettingsError:
        previous = None
    if previous:
        profiles[previous["provider"]] = previous
    for provider, profile in profiles.items():
        if provider != clean["provider"] and profile["base_url"] == url:
            raise SettingsError("该 API 地址已有另一提供方的凭据配置，请使用独立地址，避免覆盖原密钥。")
    key = data["key"].strip()
    if not key:
        try:
            read_key(url)
        except SettingsError:
            raise SettingsError("该接口尚无已保存的 API Key，请填写密钥后再保存。") from None
    # Keep a recoverable, allowlisted copy before replacing the active provider.
    # Endpoint-based vault targets remain unchanged for legacy Hy4 credentials.
    profiles[clean["provider"]] = clean
    write_json(app_dir() / "profiles.json", profiles)
    if key:
        _store_key(url, key)
    write_json(app_dir() / "settings.json", clean)
    result = {"configured": True, "provider": clean["provider"], "model": clean["model"],
              "connection_tested": False}
    if "model_family" in clean:
        result["model_family"] = clean["model_family"]
    return result
