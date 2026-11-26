"""Route connection checks by the explicitly selected provider; never fall back."""
from .config import SettingsError, settings


def check_model(*, image_path=None, timeout=180, cancelled=None):
    try:
        data = settings()
    except SettingsError as error:
        return {"status": "failed", "error_code": "configuration", "message": str(error)}
    if data and data.get("provider", "hunyuan") == "seedance":
        if image_path is not None:
            return {"status": "failed", "provider": "seedance", "error_code": "unsupported_probe",
                    "generation_submitted": False,
                    "message": "Seedance 连接检查只验证 API 鉴权，不接受图片或创建视频任务。"}
        from .seedance import check_api
        return check_api(timeout=timeout, cancelled=cancelled)
    if data and data.get("provider", "hunyuan") != "hunyuan":
        return {"status": "failed", "error_code": "configuration", "message": "不支持当前模型提供方；未执行回退调用。"}
    from .hunyuan import check_model as check_hunyuan
    return check_hunyuan(image_path=image_path, timeout=timeout, cancelled=cancelled)
