"""Resumable cloud OCR/erase plus server-only deterministic Korean rendering."""
import hashlib
import json
import math
import re
from pathlib import Path
import secrets
import subprocess
import time

from . import config, mediakit_config
from .batch import batch_lock, runtime_paths
from .mediakit_client import MediaKitClient
from .mediakit_subtitles import SubtitleBindingError
from .seedance_client import SeedanceError
from .selfhost_media import MediaClient, configuration


def _digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _probe(path):
    executable = runtime_paths()['ffprobe']
    if not executable:
        raise ValueError('缺少 ffprobe，无法核验视频。')
    result = subprocess.run([str(executable), '-v', 'error', '-count_frames',
        '-show_entries', 'stream=codec_type,codec_name,width,height,avg_frame_rate,start_time,duration,nb_read_frames:format=duration,size',
        '-of', 'json', str(path)], capture_output=True, timeout=120,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError('视频元数据读取失败。')
    value = json.loads(result.stdout)
    video = next(row for row in value['streams'] if row['codec_type'] == 'video')
    numerator, denominator = map(int, video['avg_frame_rate'].split('/'))
    fps = numerator / denominator
    duration = float(video.get('duration', value['format']['duration']))
    if not 0 < duration <= 300 or not 24 <= fps <= 60 or max(video['width'], video['height']) > 2560:
        raise ValueError('MediaKit 当前流程支持最长300秒、24–60fps、最高2K的输入。')
    return {'duration': duration, 'width': video['width'], 'height': video['height'],
            'fps': fps, 'fps_ratio': video['avg_frame_rate'], 'frame_count': int(video['nb_read_frames']),
            'has_audio': any(row['codec_type'] == 'audio' for row in value['streams']), 'probe': value}


def _write(folder, report):
    config.write_json(folder / 'mediakit-batch.json', report)


def _public(report):
    return {name: report.get(name) for name in ('provider', 'status', 'phase', 'batch_dir',
        'source', 'candidate', 'cloud_tasks', 'error', 'technical_review', 'binding_summary',
        'visual_acceptance', 'final_ocr') if name in report}


def convert(input_path, translations, output_dir, *, max_polls=60, progress=None, cancelled=None,
            stop_after=None):
    source, refs, output = (Path(value).resolve() for value in (input_path, translations, output_dir))
    if not source.is_file() or source.suffix.lower() != '.mp4' or source.stat().st_size > 200*1024**2:
        raise ValueError('请提供不超过200MiB的本机MP4文件。')
    if not refs.is_file() or refs.stat().st_size > 1024**2:
        raise ValueError('请提供已定稿中韩译文JSON表。')
    mappings = json.loads(refs.read_text(encoding='utf-8-sig'))
    if (not isinstance(mappings, list) or not mappings or any(not isinstance(row, dict)
            or set(row) != {'source', 'target'} or any(not isinstance(row[key], str) or not row[key]
                for key in ('source', 'target')) for row in mappings)):
        raise ValueError('译文表须为包含source和target的非空对象数组。')
    from .mediakit_subtitles import normalize_source
    normalized = [normalize_source(row['source']) for row in mappings]
    if len(set(normalized)) != len(normalized) or any(not value for value in normalized):
        raise ValueError('参考原文不能重复或为空。')
    if any(not row['target'].strip() or any(c in row['target'] for c in '\\{}\r\n\t') for row in mappings):
        raise ValueError('译文不能包含字幕控制符或只有空白。')
    profile = mediakit_config.settings()
    media_config = configuration()
    if not profile or not media_config:
        raise ValueError('请先配置独立MediaKit凭据及素材服务器。')
    with batch_lock():
        metadata = _probe(source)
        folder = output / ('mediakit-' + time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(4))
        folder.mkdir(parents=True)
        report = {'schema_version': 1, 'provider': 'mediakit', 'status': 'processing', 'phase': 'prepared',
            'batch_dir': str(folder), 'source': str(source), 'source_sha256': _digest(source),
            'source_bytes': source.stat().st_size, 'source_media': metadata,
            'translations': mappings, 'translations_path': str(refs), 'translations_sha256': _digest(refs),
            'profile': profile, 'media_backend': media_config, 'cloud_tasks': {}, 'server_jobs': {},
            'visual_acceptance': 'not_reviewed', 'created_at': time.time()}
        _write(folder, report)
        return _execute(folder, report, max_polls, progress, cancelled, stop_after)


def resume(batch_dir, *, max_polls=60, progress=None, cancelled=None, stop_after=None):
    folder = Path(batch_dir).resolve()
    with batch_lock():
        path = folder / 'mediakit-batch.json'
        if path.stat().st_size > 4*1024**2:
            raise ValueError('MediaKit断点文件过大。')
        report = json.loads(path.read_text(encoding='utf-8'))
        if report.get('provider') != 'mediakit' or report.get('schema_version') != 1:
            raise ValueError('不是受支持的MediaKit断点。')
        if (report['profile'] != mediakit_config.settings() or report['media_backend'] != configuration()
                or Path(report['batch_dir']).resolve() != folder
                or _digest(report['source']) != report['source_sha256']
                or _digest(report['translations_path']) != report['translations_sha256']
                or report['translations'] != json.loads(Path(report['translations_path']).read_text(encoding='utf-8-sig'))):
            raise ValueError('原片、译文或服务配置已改变，不能沿用此断点。')
        return _execute(folder, report, max_polls, progress, cancelled, stop_after)


def _execute(folder, report, max_polls, progress, cancelled, stop_after):
    if type(max_polls) is not int or not 1 <= max_polls <= 200:
        raise ValueError('max_polls须为1–200。')
    client = MediaKitClient(config.read_key(mediakit_config.ENDPOINT), cancelled=cancelled)
    media = MediaClient(report['media_backend']['endpoint'], cancelled=cancelled)

    def update(phase):
        report.update(status='processing', phase=phase)
        report.pop('error', None)
        _write(folder, report)
        if progress:
            progress({'phase': phase, 'batch_dir': str(folder)})

    def pause(status):
        report['status'] = status
        _write(folder, report)
        return _public(report)

    def wait_interval():
        for _ in range(10):
            if cancelled and cancelled():
                raise SeedanceError('cancelled', '已停止本地等待，远程任务与断点保留。')
            time.sleep(1)

    def cloud(name, submit):
        state = report['cloud_tasks'].get(name)
        if not state:
            state = {'client_token': 'vl-' + secrets.token_hex(20), 'submission_state': 'submitting',
                     'started_at': time.time()}
            report['cloud_tasks'][name] = state
            _write(folder, report)
            try:
                response = submit(state['client_token'])
            except SeedanceError as error:
                state['submission_state'] = 'unknown' if error.submission_uncertain else 'rejected'
                _write(folder, report)
                raise
            state.update(task_id=response['task_id'], submission_state='submitted', request_id=response.get('request_id'))
            _write(folder, report)
        if not state.get('task_id'):
            raise SeedanceError('submission_unresolved', '上次创建结果未确认或已被拒绝；保留断点，未重发付费请求。')
        for index in range(max_polls):
            value = client.get_task(state['task_id'])
            state['status'] = value['status']
            state['last_checked_at'] = time.time()
            state['request_id'] = value.get('request_id')
            # Result video URLs must remain in memory only.
            _write(folder, report)
            if value['status'] == 'failed':
                state['provider_error'] = value.get('error')
                raise SeedanceError('cloud_task_failed', 'MediaKit任务失败，原任务ID已保留。',
                                    provider_code=value.get('error', {}).get('code'))
            if value['status'] == 'completed':
                return value['result']
            if index + 1 < max_polls:
                wait_interval()
        return None

    def server(name, payload):
        state = report['server_jobs'].get(name)
        if not state:
            state = media.submit_job(payload)
            report['server_jobs'][name] = state
            _write(folder, report)
        for index in range(max_polls):
            state = media.get_job(state['id'])
            report['server_jobs'][name] = state
            _write(folder, report)
            if state['status'] == 'succeeded':
                return state['result']
            if state['status'] in {'failed', 'expired'}:
                raise SeedanceError('server_processing', '服务器处理失败，检查点已保存。',
                                    provider_code=state.get('error', {}).get('code') if isinstance(state.get('error'), dict) else None)
            if index + 1 < max_polls:
                wait_interval()
        return None

    try:
        if not report.get('uploaded'):
            update('uploading_source')
            report['uploaded'] = media.ensure_uploaded(report['source'], report['source_sha256'])
            _write(folder, report)
        if not report.get('ocr_result'):
            update('recognizing_source_subtitles')
            result = cloud('source_ocr', lambda token: client.submit_video_ocr(
                media.reference(report['source_sha256'], report['source_bytes']), token, mode='Detailed'))
            if result is None:
                return pause('paused_budget')
            report['ocr_result'] = _ocr_only(result)
            config.write_json(folder / 'source-ocr.json', report['ocr_result'])
            _write(folder, report)
        if stop_after == 'ocr':
            update('source_ocr_completed')
            return pause('paused_checkpoint')
        from .mediakit_subtitles import bind_subtitles
        info = report['source_media']
        binding = bind_subtitles(report['ocr_result'], report['translations'],
            width=info['width'], height=info['height'], duration=info['duration'])
        if report.get('binding') and report['binding'] != binding:
            raise SeedanceError('binding_changed', '字幕绑定算法或断点内容已改变，需检查后再继续。')
        if not report.get('binding'):
            update('binding_reference_translations')
            report['binding'] = binding
            config.write_json(folder / 'translation-binding.json', binding)
            _write(folder, report)
        report['binding_summary'] = {'status': binding['status'], 'cue_count': len(binding['cues']),
            'matched_reference_count': len(binding['groups']), 'unmatched_count': len(binding['unmatched']),
            'unused_translation_indices': binding['unused_translation_indices']}
        if binding['status'] != 'ready' or binding['unmatched']:
            return pause('needs_translation_review')
        render_plan = _render_plan(binding, info)
        if report.get('render_plan') and report['render_plan'] != render_plan:
            raise SeedanceError('render_plan_changed', '断点中的渲染计划与已定稿字幕绑定不一致。')
        if not report.get('render_plan'):
            report['render_plan'] = render_plan
            config.write_json(folder / 'render-plan.json', report['render_plan'])
            _write(folder, report)
        plan = report['render_plan']
        if not report.get('clean_object'):
            update('erasing_chinese_subtitles')
            # Limit detection to the dialogue band. Text also catches short
            # dialogue that OCR classified as Others; AI watermark is outside.
            result = cloud('erase', lambda token: client.submit_erase(
                media.reference(report['source_sha256'], report['source_bytes']), token,
                model_version=report['profile']['model_version'], mode='Text', boxes=plan['erase_boxes']))
            if result is None:
                return pause('paused_budget')
            if stop_after == 'erase':
                update('cloud_erasure_completed')
                return pause('paused_checkpoint')
            update('importing_clean_video_on_server')
            imported = server('import_clean', {'kind':'import', 'task_id':report['cloud_tasks']['erase']['task_id'],
                                             'url': result['video_url']})
            if imported is None:
                return pause('paused_budget')
            report['clean_object'] = imported
            _write(folder, report)
        if not report.get('render_result'):
            update('rendering_korean_on_server')
            clean = report['clean_object']
            result = server('render_korean', {'kind':'render_subtitles', 'source_id':report['source_sha256'],
                'clean_id':clean['id'], 'cues':plan['cues'], 'style':plan['style']})
            if result is None:
                return pause('paused_budget')
            report['render_result'] = result
            report['technical_review'] = result['technical_review']
            _write(folder, report)
        output = report['render_result']['output']
        destination = folder / (Path(report['source']).stem + '-韩文-MediaKit.mp4')
        if not destination.is_file() or _digest(destination) != output['id']:
            update('downloading_final_candidate')
            media.transport.download(media.reference(output['id'],output['bytes']), destination)
        if _digest(destination) != output['id'] or destination.stat().st_size != output['bytes']:
            raise SeedanceError('candidate_hash_mismatch', '下载成片与服务器哈希不一致。')
        measured = _probe(destination)
        checks = {key:measured[key]==info[key] for key in ('width','height','fps_ratio','frame_count','has_audio')}
        checks['duration'] = abs(measured['duration']-info['duration']) <= .001
        if not all(checks.values()):
            raise SeedanceError('candidate_metadata_mismatch', '下载成片的尺寸、帧数、时长或音频与原片不一致。')
        report['candidate'] = str(destination)
        report['technical_review']['download_checks'] = checks
        _write(folder, report)
        if not report.get('final_ocr_result'):
            update('recognizing_final_candidate')
            result = cloud('final_ocr', lambda token: client.submit_video_ocr(
                media.reference(output['id'],output['bytes']), token, mode='Detailed'))
            if result is None:
                return pause('paused_budget')
            report['final_ocr_result'] = _ocr_only(result)
            config.write_json(folder / 'final-ocr.json', report['final_ocr_result'])
        report['final_ocr'] = _ocr_review(report['final_ocr_result'], plan, binding)
        update('candidate_ready')
        return pause('candidate_ready_needs_visual_review')
    except SubtitleBindingError:
        report['error'] = {'code':'subtitle_binding_invalid'}
        return pause('needs_translation_review')
    except SeedanceError as error:
        report['error'] = {key: getattr(error, key, None) for key in ('code', 'http_status',
            'provider_code', 'submission_uncertain', 'failure_stage', 'exception_type', 'request_id')}
        return pause('cancelled' if error.code == 'cancelled' else 'paused_error')


def _ocr_only(value):
    if not isinstance(value,dict) or not isinstance(value.get('subtitles'),list):
        raise SeedanceError('ocr_result_invalid', 'MediaKit没有返回字幕识别结果。')
    return {'duration':value['duration'], 'subtitles':value['subtitles']}


def _render_plan(binding, info):
    width, height, fps = info['width'], info['height'], info['fps']
    cues=[]
    for row in binding['cues']:
        b=row['text_location']
        if not b:
            raise SeedanceError('subtitle_location_missing','缺少字幕坐标，不能限定擦除区域。')
        x=max(0,b['top_left_x']-12); y=max(0,b['top_left_y']-10)
        right=min(width,b['bottom_right_x']+12); bottom=min(height,b['bottom_right_y']+10)
        cues.append({'start':max(0,row['start_time']-2/fps),
            'end':min(info['duration'],row['end_time']+2/fps), 'text':row['target_text'],
            'box':{'x':x,'y':y,'width':right-x,'height':bottom-y}})
    for previous,current in zip(cues,cues[1:]):
        if previous['end']>current['start']:
            middle=(previous['end']+current['start'])/2
            previous['end']=current['start']=middle
    # One bounded dialogue band avoids fragmented short text being missed.
    left=min(c['box']['x'] for c in cues); top=min(c['box']['y'] for c in cues)
    right=max(c['box']['x']+c['box']['width'] for c in cues)
    bottom=max(c['box']['y']+c['box']['height'] for c in cues)
    for item in binding['ignored']:
        b=item.get('text_location')
        if b and max(left,b['top_left_x'])<min(right,b['bottom_right_x']) and max(top,b['top_left_y'])<min(bottom,b['bottom_right_y']):
            raise SeedanceError('erase_region_conflict','擦除范围与非目标文字重叠，需先审阅位置。')
    return {'cues':cues, 'erase_boxes':[{'top_left_x':left/width,'top_left_y':top/height,
        'bottom_right_x':right/width,'bottom_right_y':bottom/height}],
        'style':{'font_name':'Noto Sans CJK KR','font_size':max(16,round(height*.045)),'outline':2},
        'padding_pixels':{'x':12,'y':10},'padding_frames':2,'mode':'Text',
        'target_text_changed':False}


def _ocr_review(result, plan, binding):
    residues=[]
    for row in result['subtitles']:
        b=row.get('text_location')
        if not b or not re.search(r'[\u3400-\u9fff]',row['subtitle_text']):
            continue
        if any(max(b['top_left_x'],c['box']['x'])<min(b['bottom_right_x'],c['box']['x']+c['box']['width'])
               and max(b['top_left_y'],c['box']['y'])<min(b['bottom_right_y'],c['box']['y']+c['box']['height']) for c in plan['cues']):
            residues.append(row)
    # Official video-ocr currently recognizes Simplified Chinese and English,
    # not Korean. Han-looking output after rendering Korean is inconclusive.
    return {'observations_possible_false_positive':residues,
        'status':'manual_review_required_ocr_language_limit',
        'ocr_supported_languages':['zh-Hans','en'], 'target_language':'ko',
        'language_support_source':'https://docs.volcengine.com/docs/intelligent-processing/video-ocr-guide?lang=zh',
        'reference_targets_preserved_in_render':all(g['target_preserved'] for g in binding['groups']),
        'visual_review_required':True}
