"""User-scoped model settings. Only the endpoint and model name are stored in JSON."""

import ctypes
from ctypes import wintypes as wt
import getpass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit, urlunsplit


class SettingsError(ValueError):
    pass


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
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def validated_endpoint(value):
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


def settings():
    path = app_dir() / "settings.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        endpoint = validated_endpoint(data["base_url"])
        model = data["model"]
        if not isinstance(model, str) or not model.strip():
            raise ValueError()
        return {"base_url": endpoint, "model": model.strip()}
    except (OSError, ValueError, TypeError, KeyError):
        raise SettingsError("模型配置无法读取，请重新运行 setup。") from None


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
    env = dict(os.environ, VL_DIALOG_API=initial.get("base_url", ""), VL_DIALOG_MODEL=initial.get("model", ""))
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
                reply = {"ok": True, "message": "模型配置已保存到本机。"}
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


def configure(gui=False, endpoint=None, model=None):
    try:
        previous = settings() or {}
    except SettingsError:
        previous = {}
    default = {"base_url": endpoint or previous.get("base_url", ""), "model": model or previous.get("model", "")}
    if gui:
        return _dialog(default)
    if not sys.stdin.isatty():
        raise SettingsError("请使用 setup --gui 在本机遮挡输入密钥，或在交互式终端运行 setup。")
    url = endpoint or input(f"API 地址 [{default['base_url']}]: ").strip() or default["base_url"]
    name = model or input(f"视觉模型名 [{default['model']}]: ").strip() or default["model"]
    return _save_configuration({"base_url": url, "model": name,
                                "key": getpass.getpass("API Key（不回显，留空沿用该地址的凭据）: ")})


def _save_configuration(data):
    url = validated_endpoint(data["base_url"])
    name = data["model"].strip()
    if not name or len(name) > 256 or any(ord(c) < 32 for c in name):
        raise SettingsError("请填写有效的模型 ID。")
    if data["key"].strip():
        _store_key(url, data["key"].strip())
    else:
        try:
            read_key(url)
        except SettingsError:
            raise SettingsError("该接口尚无已保存的 API Key，请填写密钥后再保存。") from None
    write_json(app_dir() / "settings.json", {"base_url": url, "model": name})
    return {"configured": True, "model": name}
