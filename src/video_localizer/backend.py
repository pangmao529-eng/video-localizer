"""Seedance-only authentication check; never select another provider."""
from .config import SettingsError, settings


def check_model(*, image_path=None, timeout=180, cancelled=None):
    try:
        data = settings()
    except SettingsError as error:
        return {"status": "failed", "error_code": "configuration", "message": str(error)}
    if data and data.get("provider") == "seedance":
        if image_path is not None:
            return {"status": "failed", "provider": "seedance", "error_code": "unsupported_probe",
                    "generation_submitted": False,
                    "message": "Seedance 连接检查只验证 API 鉴权，不接受图片或创建视频任务。"}
        from .seedance import check_api
        return check_api(timeout=timeout, cancelled=cancelled)
    return {"status": "failed", "provider": "seedance", "error_code": "configuration",
            "generation_submitted": False, "message": "请先保存 Seedance 配置；本版不调用旧模型提供方。"}
