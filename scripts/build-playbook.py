#!/usr/bin/env python3
"""Build an offline playbook from real, closed recordings; never synthesize video."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import quote, urlsplit, urlunsplit


DEFAULT_FFMPEG = Path(shutil.which('ffmpeg') or 'ffmpeg')
MEDIA_EXTENSIONS = {'.webm', '.mp4', '.mov', '.mkv', '.m4v'}
SECRET = re.compile(r'(?i)(?:\bsk-[A-Za-z0-9_-]{16,}|\bBearer\s+[A-Za-z0-9._~+/-]+=*|\b(?:api[_-]?key|access[_-]?token|authorization|password|secret)\s*[=:]\s*[^\s,;]+)')
PRIVATE_FILE = re.compile(r'(?i)(?:^|[/\\])(?:\.env(?:\..*)?|[^/\\]*(?:credentials|admin\.token|secrets\.env)[^/\\]*)(?:$|[/\\])')


def clean(value):
    return SECRET.sub('[敏感字段已隐藏]', '' if value is None else str(value))


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def stamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else None
    except ValueError:
        return None


def offset(value, start):
    if type(value) in (float, int):
        result = float(value)
    elif isinstance(value, str) and re.fullmatch(r'\d+(?::\d{1,2}){0,2}(?:\.\d+)?', value):
        result = 0.0
        for part in value.split(':'):
            result = result * 60 + float(part)
    else:
        instant = stamp(value)
        if instant is None or start is None:
            raise ValueError('章节 at / frame_at 需为片段内秒数、HH:MM:SS 或带时区的 ISO 时间；ISO 时间需要 started_at')
        result = (instant - start).total_seconds()
    if not math.isfinite(result) or result < 0:
        raise ValueError('章节时间必须为有限的非负秒数')
    return round(result, 3)


def safe_url(value):
    if not value:
        return None
    try:
        parts = urlsplit(str(value))
        if parts.scheme not in ('http', 'https', 'about') or parts.username or parts.password:
            return None
        if parts.scheme == 'about':
            return 'about:blank' if parts.path == 'blank' else None
        # Recorded page links are context only: remove queries and secret-bearing fragments.
        fragment = '' if SECRET.search(parts.fragment) or re.search(r'(?i)token|secret|key=', parts.fragment) else parts.fragment
        return clean(urlunsplit((parts.scheme, parts.netloc, parts.path, '', fragment)))
    except ValueError:
        return None


def href(path, output):
    return quote(os.path.relpath(path, output), safe='/')


def text_list(value):
    return [clean(item) for item in value if isinstance(item, (str, int, float))] if isinstance(value, list) else ([clean(value)] if value else [])


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def duration(ffmpeg, source):
    result = subprocess.run([str(ffmpeg), '-hide_banner', '-i', str(source)], capture_output=True, text=True, timeout=30)
    match = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', result.stderr)
    return sum(float(value) * scale for value, scale in zip(match.groups(), (3600, 60, 1))) if match else None


def extract(ffmpeg, source, seconds, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.stem + '.part.png')
    result = subprocess.run([
        str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
        '-ss', f'{seconds:.3f}', '-i', str(source), '-frames:v', '1',
        '-vf', "scale='min(1440,iw)':-2", '-c:v', 'png', str(temporary),
    ], capture_output=True, text=True, timeout=90)
    if result.returncode == 0 and temporary.is_file() and temporary.stat().st_size:
        temporary.replace(target)
        return True
    temporary.unlink(missing_ok=True)
    return False


def load(path):
    return json.loads(path.read_text())


def external_chapters(path):
    if path is None or not path.is_file():
        return []
    value = load(path)
    value = value.get('chapters', []) if isinstance(value, dict) else value
    if not isinstance(value, list):
        raise ValueError('chapters.json 应为章节数组，或包含 chapters 数组的对象')
    return value


def normalize(manifest, chapters_path, output, ffmpeg, copy_videos):
    raw = load(manifest)
    source_segments = raw.get('segments') if isinstance(raw, dict) else raw
    if not isinstance(source_segments, list) or not source_segments:
        raise ValueError('segments.json 必须包含非空 segments 数组')
    metadata = raw if isinstance(raw, dict) else {}
    external = external_chapters(chapters_path)
    warnings, segments, seen_ids = [], [], set()
    external_used = set()
    for ordinal, item in enumerate(source_segments, 1):
        if not isinstance(item, dict):
            raise ValueError('每个录像片段必须为对象')
        identifier = str(item.get('id') or f'segment-{ordinal:02}')
        if identifier in seen_ids:
            raise ValueError('录像片段 id 不能重复')
        seen_ids.add(identifier)
        name = re.sub(r'[^a-zA-Z0-9_-]', '-', identifier).strip('-')[:64] or f'segment-{ordinal:02}'
        name = f'{ordinal:02}-{name}'
        relative = item.get('path')
        if not isinstance(relative, str) or not relative:
            raise ValueError('每个录像片段需要 path')
        source = (manifest.parent / relative).resolve()
        if source.suffix.lower() not in MEDIA_EXTENSIONS or PRIVATE_FILE.search(str(source)):
            raise ValueError('path 只能指向录像媒体文件，不能读取凭据文件')
        if source.parent.name == 'raw' and (output == source.parent or source.parent in output.parents):
            raise ValueError('输出目录不能位于原片 raw 目录内')
        start, end = stamp(item.get('started_at')), stamp(item.get('ended_at'))
        if start and end and end <= start:
            raise ValueError('ended_at 必须晚于 started_at')
        closed = bool(end) and item.get('complete') is True
        state = 'closed' if closed else ('partial' if end else 'recording')
        record = {
            'id': clean(identifier), 'title': clean(item.get('title') or f'录像 {ordinal:02}'),
            'source_path': clean(os.path.relpath(source, manifest.parent)),
            'started_at': clean(item.get('started_at')), 'ended_at': clean(item.get('ended_at')),
            'ended_at_estimated': item.get('ended_at_estimated') is True,
            'wall_duration_seconds': round((end - start).total_seconds(), 3) if start and end else None,
            'media_duration_seconds': None, 'declared_complete': item.get('complete') is True,
            'state': state, 'sha256': None, 'bytes': None, 'video_url': None,
            'recording_note': clean(item.get('recording_note')), 'limitations': text_list(item.get('limitations')),
            'chapters': [],
        }
        # An ended partial recording is useful evidence too. Active files remain untouched.
        can_read = bool(end) and source.is_file()
        before = None
        if can_read:
            before = source.stat()
            record['sha256'] = sha256(source)
            record['bytes'] = before.st_size
            record['media_duration_seconds'] = duration(ffmpeg, source)
            if copy_videos:
                destination = output / 'media' / f'{name}-{record["sha256"][:12]}{source.suffix.lower()}'
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    if sha256(destination) != record['sha256']:
                        raise ValueError('输出目录存在同名但内容不同的媒体，未覆盖')
                else:
                    shutil.copy2(source, destination)
                record['video_url'] = href(destination, output)
            else:
                record['video_url'] = href(source, output)
        elif not source.is_file():
            record['state'] = 'missing'
            warnings.append(f'{record["title"]}：原片缺失，未生成替代视频或关键帧。')
        else:
            warnings.append(f'{record["title"]}：没有闭合时间，跳过原片读取与提帧。')

        chapter_rows = list(item.get('chapters') or [])
        signatures = {(str(row.get('at')), str(row.get('title'))) for row in chapter_rows if isinstance(row, dict)}
        for external_index, row in enumerate(external):
            if not isinstance(row, dict):
                raise ValueError('每个章节必须为对象')
            explicit = row.get('segment_id', row.get('segment'))
            instant = stamp(row.get('at'))
            belongs = (explicit == identifier if explicit is not None else
                       bool(instant and start and end and start <= instant <= end) if instant else
                       len(source_segments) == 1)
            if belongs:
                external_used.add(external_index)
                signature = (str(row.get('at')), str(row.get('title')))
                if signature not in signatures:
                    chapter_rows.append(row)
                    signatures.add(signature)
        if not chapter_rows:
            warnings.append(f'{record["title"]}：无操作章节点；保留原片，不自动补造章节或关键帧。')
        for number, row in enumerate(chapter_rows, 1):
            if not isinstance(row, dict):
                raise ValueError('每个章节必须为对象')
            seconds = offset(row.get('at', 0), start)
            frame_value = row.get('frame_at', row.get('at', 0))
            frame_requested = offset(frame_value, start)
            wall = record['wall_duration_seconds']
            if wall is not None and seconds > wall + 2:
                warnings.append(f'{record["title"]} / {clean(row.get("title"))}：章节标记超过片段结束时间。')
            if wall is not None and frame_requested > wall + 2:
                warnings.append(f'{record["title"]} / {clean(row.get("title"))}：关键帧标记超过片段结束时间。')
            if record['media_duration_seconds'] is not None and seconds > record['media_duration_seconds']:
                warnings.append(f'{record["title"]} / {clean(row.get("title"))}：原始章节点超过实际视频时长；导航保留原标记但只能定位到末尾。')
            chapter = {
                'id': f'{name}-chapter-{number:03}', 'at': clean(row.get('at', 0)), 'seconds': seconds,
                'at_estimated': row.get('at_estimated') is True,
                'frame_at': clean(frame_value), 'frame_at_explicit': 'frame_at' in row,
                'frame_requested_seconds': frame_requested,
                'title': clean(row.get('title') or f'步骤 {number:02}'),
                'url': safe_url(row.get('url')), 'note': clean(row.get('note')),
                'steps': text_list(row.get('steps', row.get('step'))),
                'expected': clean(row.get('expected')), 'observed': clean(row.get('observed')),
                'recovery': clean(row.get('recovery', row.get('error_recovery'))),
                'evidence': [clean(ref) for ref in text_list(row.get('evidence', row.get('evidence_paths'))) if not PRIVATE_FILE.search(ref)],
                'frame_url': None, 'frame_sha256': None, 'frame_seconds': None,
                'frame_note': '未提取',
            }
            if chapter['at_estimated']:
                warnings.append(f'{record["title"]} / {chapter["title"]}：章节点为估计值，不作为准确操作时刻。')
            if can_read:
                media_duration = record['media_duration_seconds']
                sample_at = min(frame_requested, max(0, media_duration - .15)) if media_duration is not None else frame_requested
                target = output / 'frames' / f'{name}-{record["sha256"][:12]}-{number:03}-{round(sample_at * 1000):09}.png'
                if extract(ffmpeg, source, sample_at, target):
                    note = '原片提取；章节标记与关键画面分别定位' if 'frame_at' in row else '原片提取；时间为请求定位点'
                    if sample_at != frame_requested:
                        note += '；末尾标记使用邻近末帧'
                    chapter.update(frame_url=href(target, output), frame_sha256=sha256(target), frame_seconds=sample_at,
                                   frame_note=note)
                else:
                    chapter['frame_note'] = '提帧失败；未使用替代截图'
                    warnings.append(f'{record["title"]} / {chapter["title"]}：提帧失败。')
            record['chapters'].append(chapter)
        record['chapters'].sort(key=lambda chapter: chapter['seconds'])
        if can_read and before:
            after = source.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('原片在读取期间发生变化，请关闭录像后重新生成')
        segments.append(record)

    unmatched = len(external) - len(external_used)
    if unmatched:
        warnings.append(f'外部章节有 {unmatched} 条无法归属片段；多片段的相对时间请填写 segment_id。')
    coverage = metadata.get('coverage') or {}
    if isinstance(coverage, str):
        coverage = {'note': coverage}
    status = coverage.get('status', 'unknown')
    if status not in ('complete', 'partial', 'unknown'):
        status = 'unknown'
    intervals = sorted((stamp(s['started_at']), stamp(s['ended_at']), s['ended_at_estimated']) for s in segments if stamp(s['started_at']) and stamp(s['ended_at']))
    gaps, unmapped = [], []
    if intervals:
        covered_end = intervals[0][1]
        covered_end_estimated = intervals[0][2]
        for interval_start, interval_end, end_estimated in intervals[1:]:
            if interval_start > covered_end:
                interval = {'started_at': covered_end.isoformat(), 'ended_at': interval_start.isoformat(),
                            'seconds': round((interval_start - covered_end).total_seconds(), 3)}
                partials = [s['id'] for s in segments if not stamp(s['started_at'])
                            and stamp(s['ended_at']) and covered_end < stamp(s['ended_at']) <= interval_start]
                if partials or covered_end_estimated:
                    reasons = []
                    if partials:
                        reasons.append('区间内存在起点墙钟未知的原片')
                    if covered_end_estimated:
                        reasons.append('区间起点使用估计的结束墙钟')
                    interval.update(segment_ids=partials, reason='；'.join(reasons) + '；区间跨度不是确知未录制时长。')
                    unmapped.append(interval)
                else:
                    gaps.append(interval)
            if interval_end > covered_end:
                covered_end, covered_end_estimated = interval_end, end_estimated
            elif interval_end == covered_end:
                covered_end_estimated = covered_end_estimated and end_estimated
    if gaps:
        warnings.append(f'录像片段之间有 {len(gaps)} 段未录制间隔，合计 {sum(g["seconds"] for g in gaps):.1f} 秒；未拼接成连续录像。')
    if unmapped:
        warnings.append(f'另有 {len(unmapped)} 段墙钟区间因原片起点未知或结束时间为估计而无法完整映射；这些区间未计入未录制时长。')
    unknown_start = [s for s in segments if not stamp(s['started_at'])]
    if unknown_start:
        warnings.append(f'{len(unknown_start)} 段原片缺少可靠起点墙钟；视频内相对时间仍可定位，不推测其绝对时间。')
    estimated_end = [s for s in segments if s['ended_at_estimated']]
    if estimated_end:
        warnings.append(f'{len(estimated_end)} 段结束墙钟为估计值；不能作为准确停录时间，媒体时长单独保留。')
    ffmpeg_info = subprocess.run([str(ffmpeg), '-version'], capture_output=True, text=True, timeout=15)
    return {
        'schema_version': 'myeval.playbook.v1', 'title': clean(metadata.get('title') or 'MyEval · 操作参考'),
        'description': clean(metadata.get('description') or '从真实操作录像回看每一步，沿证据复核结果。'),
        'built_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'manifest_sha256': sha256(manifest),
        'chapters_manifest_sha256': sha256(chapters_path) if chapters_path and chapters_path.is_file() else None,
        'extraction_tool': {'name': 'ffmpeg', 'sha256': sha256(ffmpeg),
                            'version': clean(ffmpeg_info.stdout.splitlines()[0] if ffmpeg_info.stdout else 'unknown')},
        'coverage': {'status': status, 'note': clean(coverage.get('note') or '全程覆盖范围未声明；片段闭合不等于整个研发过程均已录屏。')},
        'media_mode': 'copied' if copy_videos else 'relative_links',
        'timestamp_note': clean(metadata.get('timestamp_note') or '章节按记录者标记时间映射；墙钟与视频时轴可能有偏差，不宣称帧级同步。'),
        'segments': segments, 'unrecorded_intervals': gaps, 'unmapped_intervals': unmapped, 'warnings': warnings,
    }


PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><title>__TITLE__</title>
<style>
:root{--bg:#f4f3ef;--paper:#fffefa;--ink:#202824;--muted:#5f6a62;--line:#dddfd5;--green:#365a49;--soft:#e8eee6;--amber:#886228;--radius:18px}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}button,input,select{font:inherit}button,a,input{touch-action:manipulation}a{color:var(--green);text-underline-offset:4px}button{cursor:pointer}button:disabled{cursor:default;opacity:.55}button:focus-visible,a:focus-visible,input:focus-visible,summary:focus-visible{outline:3px solid #789581;outline-offset:4px}.skip{position:absolute;left:16px;top:-80px;background:var(--paper);padding:12px;z-index:9}.skip:focus{top:16px}
.shell{max-width:1560px;margin:auto;padding:0 40px 48px}.masthead{height:78px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line);gap:20px}.brand{display:flex;align-items:center;gap:12px;font-weight:650;letter-spacing:-.3px}.mark{width:30px;height:30px;border-radius:9px;background:var(--green);display:grid;place-items:center;color:white;font:20px Georgia,serif}.brand small{font-weight:400;color:var(--muted);font-size:12px;border-left:1px solid var(--line);padding-left:13px;margin-left:3px}.meta{font-size:12px;color:var(--muted)}.hero{padding:40px 0 30px;display:grid;grid-template-columns:1fr auto;align-items:end;gap:24px}.eyebrow{font-size:11px;letter-spacing:2.4px;color:var(--green);text-transform:uppercase;font-weight:650;margin:0 0 10px}h1{font-size:clamp(25px,2.4vw,37px);font-weight:550;line-height:1.28;letter-spacing:-1.1px;margin:0 0 13px}.description{max-width:790px;color:var(--muted);margin:0}.numbers{display:flex;gap:30px;padding-bottom:2px}.number strong{display:block;font-size:26px;font-weight:500;font-variant-numeric:tabular-nums;letter-spacing:-1px}.number span{font-size:11px;color:var(--muted)}.coverage{display:flex;align-items:flex-start;gap:12px;padding:14px 18px;border:1px solid var(--line);border-radius:12px;background:#efeee7;font-size:12px;margin-bottom:26px}.coverage p{margin:0}.badge{white-space:nowrap;border:1px solid #c8d2c5;color:var(--green);background:#edf1e9;font-size:10px;padding:3px 8px;border-radius:20px}.badge.partial,.badge.recording,.badge.missing{background:#f4ebdc;color:var(--amber);border-color:#e1cfac}.workspace{display:grid;grid-template-columns:270px minmax(0,1fr);gap:26px;align-items:start}.rail{position:sticky;top:20px;max-height:calc(100vh - 40px);overflow:auto;padding:17px 12px 20px;background:var(--paper);border:1px solid var(--line);border-radius:var(--radius)}.rail-head{display:flex;justify-content:space-between;align-items:center;padding:0 9px 14px}.rail-head strong{font-size:13px}.rail-head span{font-size:11px;color:var(--muted)}.search{width:calc(100% - 16px);margin:0 8px 18px;padding:9px 12px;border:1px solid var(--line);background:#f8f8f3;border-radius:9px;font-size:12px}.group-title{font-size:11px;color:var(--muted);font-weight:500;margin:17px 10px 8px}.chapter{display:flex;gap:10px;text-align:left;width:100%;border:0;background:transparent;border-radius:10px;padding:11px 10px;color:var(--ink);margin:2px 0;line-height:1.5}.chapter:hover{background:#f0f2eb}.chapter[aria-current=true]{background:var(--soft);color:#244432}.chapter .time{font-size:10px;font-variant-numeric:tabular-nums;color:var(--muted);padding-top:3px;min-width:35px}.chapter .label{font-size:12px}.main{min-width:0}.player-card{background:#1b221e;border-radius:var(--radius);overflow:hidden;border:1px solid #303c33}.player-top{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:13px 18px;color:#d7dfd7;font-size:11px}.player-top small{color:#a4b1a7;font-size:10px}video{display:block;width:100%;max-height:62vh;min-height:220px;background:#111813;aspect-ratio:1.44;object-fit:contain}.video-unavailable{padding:70px 25px;text-align:center;color:#b6c3b9}.player-bottom{padding:11px 18px;color:#b4c0b6;font-size:11px;display:flex;justify-content:space-between;gap:16px}.player-bottom a{color:#d7e7da}.step-card{margin-top:22px;background:var(--paper);border:1px solid var(--line);border-radius:var(--radius);padding:26px}.step-top{display:flex;gap:18px;justify-content:space-between;align-items:flex-start}.step-count{color:var(--green);font-size:10px;font-weight:650;letter-spacing:1.6px;margin-bottom:7px}.step-card h2{font-size:22px;line-height:1.4;font-weight:550;margin:0 0 13px;letter-spacing:-.4px}.seek{background:var(--green);border:0;color:#fff;border-radius:9px;padding:9px 13px;font-size:11px;white-space:nowrap}.note{color:var(--muted);margin:0;font-size:13px}.facts{display:grid;grid-template-columns:1fr 1fr;gap:20px 28px;margin:25px 0 0;padding-top:22px;border-top:1px solid var(--line)}.fact h3{font-size:10px;font-weight:600;color:var(--muted);letter-spacing:.8px;margin:0 0 8px}.fact p,.fact ol,.fact ul{font-size:13px;margin:0;white-space:pre-wrap;overflow-wrap:anywhere}.fact ol,.fact ul{padding-left:19px}.fact .pending{color:#92968e;font-size:12px}.full{grid-column:1/-1}.frame{margin:26px 0 0;padding-top:24px;border-top:1px solid var(--line)}.frame-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px;font-size:11px;color:var(--muted)}.frame img{display:block;width:100%;max-height:520px;object-fit:contain;border-radius:10px;border:1px solid var(--line);background:#eee}.frame figcaption{color:var(--muted);font-size:10px;line-height:1.6;margin-top:10px;overflow-wrap:anywhere}.source-url{font:11px/1.6 ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere;display:block;margin-top:20px;color:var(--muted)}.records{margin-top:26px;background:var(--paper);border:1px solid var(--line);border-radius:var(--radius);padding:0 24px}.records summary{padding:20px 0;cursor:pointer;font-size:13px;font-weight:550}.record{border-top:1px solid var(--line);padding:20px 0}.record-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}.record-head strong{font-size:13px}.record dl{display:grid;grid-template-columns:110px 1fr;gap:7px 16px;font-size:11px;margin:0}.record dt{color:var(--muted)}.record dd{margin:0;overflow-wrap:anywhere}.hash{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:10px}.record ul{font-size:12px;color:var(--muted);padding-left:18px;margin-bottom:0}.footer{padding-top:28px;display:flex;gap:20px;justify-content:space-between;font-size:10px;color:var(--muted)}.empty{color:var(--muted);font-size:12px;padding:14px}.warnings{font-size:12px;margin-top:18px;color:var(--amber);padding:14px 18px;border:1px solid #dfd1b7;border-radius:12px}.warnings ul{margin:6px 0 0;padding-left:18px}.hidden{display:none!important}
@media(min-width:1350px){.step-card{padding:30px}.workspace{grid-template-columns:285px minmax(0,1fr)}}@media(max-width:900px){.shell{padding:0 22px 30px}.workspace{grid-template-columns:215px minmax(0,1fr);gap:16px}.numbers{gap:18px}.number strong{font-size:23px}.step-card{padding:21px}.hero{padding-top:30px}.facts{grid-template-columns:1fr}.full{grid-column:auto}.brand small{display:none}}
@media(max-width:680px){.shell{padding:0 15px 26px}.masthead{height:64px}.meta{font-size:10px}.hero{display:block;padding:27px 0 22px}.numbers{margin-top:22px;gap:30px}.description{font-size:13px}.coverage{padding:12px;font-size:11px;gap:9px}.workspace{display:block}.rail{position:relative;top:0;max-height:255px;margin-bottom:18px;border-radius:14px}.rail-head{padding-bottom:10px}.search{margin-bottom:8px}.group-title{margin-top:10px}.chapter{padding:8px 10px}.player-card{border-radius:14px}video{max-height:60vh;min-height:160px}.player-top,.player-bottom{padding:11px 13px}.player-bottom{display:block}.player-bottom a{display:inline-block;margin-top:5px}.step-card{padding:20px 17px;margin-top:16px;border-radius:14px}.step-card h2{font-size:19px}.step-top{display:block}.seek{margin-top:15px}.facts{margin-top:20px;gap:18px}.records{padding:0 17px}.record dl{grid-template-columns:75px 1fr;gap:8px}.footer{display:block}.footer a{display:inline-block;margin-top:8px}}
.player-card{scroll-margin-top:18px}.player-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:13px 18px;background:#eef2eb;border-top:1px solid #354439}.player-actions button,.player-actions a{display:inline-flex;align-items:center;justify-content:center;min-height:38px;padding:7px 12px;border:1px solid #c8d2c5;border-radius:8px;background:#fffefa;color:#284934;font-size:13px;font-weight:550;text-decoration:none}.player-actions button:first-child{background:var(--green);border-color:var(--green);color:white}.player-status{padding:13px 18px;margin:0;background:#f7f8f2;color:var(--ink);font-size:13px;line-height:1.75;border-top:1px solid var(--line)}.player-status:focus{outline:2px solid #789581;outline-offset:-3px}.chapter .label{flex:1;min-width:0}.chapter .seek-label{font-size:11px;color:var(--green);align-self:center;white-space:nowrap}.frame-head{flex-wrap:wrap}.frame-original{display:inline-flex;align-items:center;min-height:34px;padding:5px 9px;border:1px solid var(--line);border-radius:7px;font-size:12px;text-decoration:none}.frame-head>span:first-child{flex:1}.fact .pending{color:#686f66}.player-bottom{font-size:12px}.player-top small{font-size:12px}
@media(max-width:680px){.player-card{scroll-margin-top:12px}.player-actions{gap:7px;padding:11px 12px}.player-actions button,.player-actions a{flex:1;padding:7px 6px;font-size:12px;white-space:nowrap}.player-status{padding:12px 13px;font-size:13px}.chapter .label{font-size:13px}.chapter .time{font-size:11px}.chapter .seek-label{font-size:11px}.frame-head{font-size:12px}.frame figcaption{font-size:12px}.fact h3{font-size:12px}.step-count{font-size:12px}.seek{min-height:38px;font-size:13px}}
</style></head><body><a class="skip" href="#main">跳到录像与步骤</a><div class="shell">
<header class="masthead"><div class="brand"><span class="mark">m</span>MyEval<small>操作参考 · Playbook</small></div><span class="meta" id="build-date"></span></header>
<section class="hero"><div><p class="eyebrow">Recorded workflow</p><h1 id="title"></h1><p class="description" id="description"></p></div><div class="numbers"><div class="number"><strong id="segment-count"></strong><span>录像片段</span></div><div class="number"><strong id="chapter-count"></strong><span>操作章节</span></div><div class="number"><strong id="duration"></strong><span>已知视频时长</span></div></div></section>
<div class="coverage"><span class="badge" id="coverage-badge"></span><p id="coverage-note"></p></div>
<div class="workspace"><nav class="rail" aria-label="录像章节"><div class="rail-head"><strong>操作目录</strong><span>选择章节定位原片</span></div><input class="search" id="search" type="search" aria-label="搜索章节" placeholder="搜索操作、结果或问题"><div id="chapters"></div></nav>
<main class="main" id="main"><section class="player-card" id="player" aria-label="真实操作录像"><div class="player-top"><span id="segment-title"></span><small id="segment-state"></small></div><video id="video" controls playsinline preload="metadata" aria-label="原始录像播放器"></video><div class="video-unavailable hidden" id="video-unavailable">此片段尚无可播放的闭合原片。</div><div class="player-bottom"><span id="playback-note"></span></div><div class="player-actions"><button id="play-here" type="button" aria-pressed="false">播放此处</button><button id="fullscreen" type="button">全屏观看</button><a id="original" target="_blank" rel="noopener">打开原片 ↗</a></div><p class="player-status" id="player-status" role="status" aria-live="polite" tabindex="-1">选择章节仅定位画面；点击“播放此处”继续。手机可全屏观看，关键帧可打开原尺寸。</p></section>
<article class="step-card" id="step" aria-label="章节操作说明"></article>
<details class="records"><summary>原始证据与记录边界</summary><div id="records"></div></details><section class="warnings hidden" id="warnings" aria-label="记录缺口"></section>
</main></div><footer class="footer"><span id="timestamp-note"></span><a id="download-index" href="evidence-index.json" download="evidence-index.json">下载证据索引 JSON ↗</a></footer></div>
<script id="playbook-data" type="application/json">__DATA__</script>
<script>
'use strict';
const data=JSON.parse(document.getElementById('playbook-data').textContent),$=id=>document.getElementById(id),video=$('video');
const stateLabels={closed:'已闭合原片',partial:'部分片段',recording:'仍在录制',missing:'原片缺失'};
const chapters=data.segments.flatMap((s,si)=>s.chapters.map(c=>({...c,segmentIndex:si})));let active=null,segmentIndex=-1,navButtons=[],seekSequence=0,playSequence=0,pendingSeek=null,clampEvidence=null;
function time(value){if(value===null||value===undefined)return '未知';const n=Math.max(0,Math.floor(value));return (n>=3600?Math.floor(n/3600)+':':'')+String(Math.floor(n/60)%60).padStart(2,'0')+':'+String(n%60).padStart(2,'0')}
function text(el,value){el.textContent=value??'';return el}function node(tag,cls,value){const e=document.createElement(tag);if(cls)e.className=cls;if(value!==undefined)text(e,value);return e}
function wall(value){if(!value)return '未记录';const d=new Date(value);return Number.isNaN(d.getTime())?value:d.toLocaleString('zh-CN',{hour12:false})+'（本地时区）'}
$('download-index').href=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)+'\n'],{type:'application/json;charset=utf-8'}));
text($('title'),data.title);text($('description'),data.description);text($('build-date'),'生成于 '+new Date(data.built_at).toLocaleDateString('zh-CN'));text($('segment-count'),String(data.segments.length).padStart(2,'0'));text($('chapter-count'),String(chapters.length).padStart(2,'0'));
const known=data.segments.filter(s=>s.media_duration_seconds!==null);text($('duration'),known.length?time(known.reduce((n,s)=>n+s.media_duration_seconds,0)):'未知');
text($('coverage-badge'),{complete:'已声明完整覆盖',partial:'部分过程覆盖',unknown:'覆盖范围未确认'}[data.coverage.status]);if(data.coverage.status!=='complete')$('coverage-badge').classList.add('partial');text($('coverage-note'),data.coverage.note);text($('timestamp-note'),data.timestamp_note);
function renderNav(){const query=$('search').value.trim().toLowerCase();$('chapters').replaceChildren();navButtons=[];data.segments.forEach((s,si)=>{const found=chapters.filter(c=>c.segmentIndex===si&&[c.title,c.note,c.observed].join(' ').toLowerCase().includes(query));if(!found.length){if(!s.chapters.length&&(!query||s.title.toLowerCase().includes(query))){$('chapters').append(node('h2','group-title',String(si+1).padStart(2,'0')+' / '+s.title));const note=node('p','empty','无操作章节 · '+stateLabels[s.state]);if(s.video_url){const link=node('a','','打开原片 ↗');link.href=s.video_url;link.target='_blank';link.rel='noopener';note.append(document.createElement('br'),link)}$('chapters').append(note)}return;}$('chapters').append(node('h2','group-title',String(si+1).padStart(2,'0')+' / '+s.title));found.forEach(c=>{const b=node('button','chapter');b.type='button';b.setAttribute('aria-current',String(active?.id===c.id));b.setAttribute('aria-label','定位 '+time(c.seconds)+' · '+c.title);b.append(node('span','time',(c.at_estimated?'约 ':'')+time(c.seconds)),node('span','label',c.title),node('span','seek-label','定位'));b.addEventListener('click',()=>select(c,true));$('chapters').append(b);navButtons.push([b,c.id]);})});if(!$('chapters').childElementCount)$('chapters').append(node('p','empty','没有匹配的章节。'))}
function fact(label,value,full=false,list=false){const e=node('section','fact'+(full?' full':''));e.append(node('h3','',label));const hasValue=Array.isArray(value)?value.length>0:Boolean(value);if(list&&hasValue){const ol=node('ol');value.forEach(v=>ol.append(node('li','',v)));e.append(ol)}else e.append(node('p',hasValue?'':'pending',hasValue?value:'未记录，待补充。'));return e}
function renderStep(c){const s=data.segments[c.segmentIndex],root=$('step');root.replaceChildren();const top=node('div','step-top'),left=node('div');left.append(node('div','step-count','STEP '+String(chapters.findIndex(x=>x.id===c.id)+1).padStart(2,'0')+' · '+(c.at_estimated?'约 ':'')+time(c.seconds)),node('h2','',c.title));top.append(left);const jump=node('button','seek','定位此步骤 →');jump.type='button';jump.disabled=!s.video_url;jump.onclick=()=>seek(c);top.append(jump);root.append(top);if(c.note)root.append(node('p','note',c.note));const facts=node('div','facts');facts.append(fact('操作步骤',c.steps,true,true),fact('预期结果',c.expected),fact('实际观察',c.observed),fact('错误与恢复',c.recovery,true));if(c.evidence.length)facts.append(fact('关联证据路径',c.evidence,true,true));root.append(facts);
if(c.frame_url){const f=node('figure','frame'),head=node('div','frame-head');head.append(node('span','','原片关键帧'),node('span','',time(c.frame_seconds)));const original=node('a','frame-original','打开关键帧原尺寸 ↗');original.href=c.frame_url;original.target='_blank';original.rel='noopener';head.append(original);const link=node('a');link.setAttribute('aria-label','打开 '+c.title+' 的关键帧原尺寸');link.href=c.frame_url;link.target='_blank';link.rel='noopener';const im=node('img');im.src=c.frame_url;im.alt=c.title+'：从实际录像提取的关键帧';im.loading='lazy';link.append(im);f.append(head,link,node('figcaption','',c.frame_note+'。SHA-256 '+c.frame_sha256));root.append(f)}else{root.append(node('p','source-url','关键帧：'+c.frame_note))}if(c.url)root.append(node('span','source-url','录制页面 · '+c.url));navButtons.forEach(([b,id])=>b.setAttribute('aria-current',String(id===c.id)));}
function currentMedia(){const url=data.segments[segmentIndex]?.video_url;return Boolean(url&&video.currentSrc===new URL(url,document.baseURI).href)}
function clampNote(){const e=clampEvidence;if(!e||e.segmentIndex!==segmentIndex||e.chapterId!==active?.id)return '';return '原章节点 '+time(e.originalSeconds)+'（'+Number(e.originalSeconds.toFixed(3))+' 秒）超过原片时长；实际定位 '+time(e.actualSeconds)+'（'+Number(e.actualSeconds.toFixed(3))+' 秒）。原标记仍保留。'}
function playerControls(){const available=Boolean(data.segments[segmentIndex]?.video_url),current=currentMedia(),ended=current&&video.ended,playing=current&&!video.paused&&!ended;$('play-here').disabled=!available||Boolean(pendingSeek);$('fullscreen').disabled=!available;$('play-here').setAttribute('aria-pressed',String(playing));text($('play-here'),ended?'重新播放':playing?'暂停播放':'播放此处')}
function followCurrentChapter(){
  if(!currentMedia()||pendingSeek||video.seeking)return false;
  if(clampEvidence?.segmentIndex===segmentIndex&&clampEvidence.chapterId===active?.id&&video.currentTime>=clampEvidence.actualSeconds-.25)return false;
  const inSegment=chapters.filter(c=>c.segmentIndex===segmentIndex),current=inSegment.filter(c=>c.seconds<=video.currentTime+.1).at(-1)||inSegment[0];
  if(current&&current.id!==active?.id){active=current;renderStep(current);return true}return false;
}
function syncPlaybackState(){
  playerControls();if(pendingSeek||!currentMedia()||video.readyState<1||video.seeking)return;
  if(clampEvidence&&video.currentTime<clampEvidence.actualSeconds-.25)clampEvidence=null;
  followCurrentChapter();const first=chapters.find(c=>c.segmentIndex===segmentIndex),beforeFirst=first&&video.currentTime+.1<first.seconds;
  const title=beforeFirst?data.segments[segmentIndex].title+'（片头；首个章节点 '+time(first.seconds)+'）':active?.title||data.segments[segmentIndex]?.title||'实际原片';
  const state=video.ended?'播放已结束 '+time(video.currentTime)+' · '+title+'。点击“重新播放”从原片开头观看。':video.paused?'已暂停 '+time(video.currentTime)+' · '+title:(video.readyState<3?'正在缓冲 · ':'正在播放 · ')+title;
  text($('player-status'),state+(clampNote()?' '+clampNote():''));
}
function settledSeek(completed=false){
  const pending=pendingSeek;if(!pending||pending.sequence!==seekSequence||pending.chapter.id!==active?.id||pending.chapter.segmentIndex!==segmentIndex||!currentMedia()||video.seeking||video.readyState<2)return;
  const matched=Math.abs(video.currentTime-pending.target)<=.25;if(!matched&&!completed)return;
  pendingSeek=null;playerControls();
  const next=video.ended?'播放已结束，点击“重新播放”从原片开头观看。':'点击“播放此处”开始播放。';
  if(!matched){text($('player-status'),'当前画面位于 '+time(video.currentTime)+'，与所选章节点 '+time(pending.chapter.seconds)+' 不同。可重新定位此步骤。'+next);return;}
  const clamped=pending.target!==pending.chapter.seconds;
  clampEvidence=clamped?{chapterId:pending.chapter.id,segmentIndex:pending.chapter.segmentIndex,originalSeconds:pending.chapter.seconds,actualSeconds:video.currentTime}:null;
  text($('player-status'),'已定位 '+time(video.currentTime)+' · '+pending.chapter.title+'。'+clampNote()+next);
}
function seek(c){
  if(c.segmentIndex!==segmentIndex||!data.segments[c.segmentIndex].video_url)return;
  const sequence=++seekSequence;playSequence++;clampEvidence=null;pendingSeek={sequence,chapter:c,target:c.seconds};video.pause();playerControls();
  text($('player-status'),'正在定位 '+time(c.seconds)+' · '+c.title+'，等候原片画面。');
  $('player').scrollIntoView({block:'start',behavior:'instant'});$('player-status').focus({preventScroll:true});
  const move=()=>{
    if(pendingSeek?.sequence!==sequence||active?.id!==c.id||segmentIndex!==c.segmentIndex||!currentMedia())return;
    const target=Math.min(c.seconds,Number.isFinite(video.duration)?Math.max(0,video.duration-.05):c.seconds);pendingSeek.target=target;video.currentTime=target;settledSeek();
  };
  if(video.readyState>=1&&currentMedia())move();else video.addEventListener('loadedmetadata',move,{once:true});
}
function select(c,position){
  const s=data.segments[c.segmentIndex];active=c;seekSequence++;playSequence++;pendingSeek=null;clampEvidence=null;
  if(segmentIndex!==c.segmentIndex){segmentIndex=c.segmentIndex;video.pause();if(s.video_url){video.src=s.video_url;video.classList.remove('hidden');$('video-unavailable').classList.add('hidden');$('original').href=s.video_url;$('original').classList.remove('hidden');const poster=s.chapters.find(x=>x.frame_url);if(poster)video.poster=poster.frame_url;else video.removeAttribute('poster')}else{video.removeAttribute('src');video.classList.add('hidden');$('video-unavailable').classList.remove('hidden');$('original').removeAttribute('href');$('original').classList.add('hidden')}text($('segment-title'),s.title);text($('segment-state'),stateLabels[s.state]);text($('playback-note'),s.recording_note||'实际操作原片；说明内容来自章节记录。')}
  renderStep(c);playerControls();if(position)seek(c);else text($('player-status'),s.video_url?'当前章节 · '+c.title+'。选择章节仅定位画面；点击“播放此处”继续。':'当前片段没有可播放的闭合原片；章节记录仍保留。');
}
video.addEventListener('timeupdate',()=>{if(pendingSeek){settledSeek();return}if(followCurrentChapter()||video.ended)syncPlaybackState()});
video.addEventListener('seeking',()=>{
  if(!currentMedia()||!video.seeking)return;
  if(pendingSeek&&Math.abs(video.currentTime-pendingSeek.target)<=.25)return;
  seekSequence++;playSequence++;pendingSeek=null;clampEvidence=null;playerControls();
  text($('player-status'),'正在定位 '+time(video.currentTime)+'，等候原片画面。');
});
video.addEventListener('seeked',()=>{if(pendingSeek)settledSeek(true);else syncPlaybackState()});
for(const event of ['loadeddata','canplay'])video.addEventListener(event,()=>settledSeek());
for(const event of ['play','playing','pause','ended','waiting'])video.addEventListener(event,syncPlaybackState);
video.addEventListener('error',()=>{if(!currentMedia()||!video.error)return;pendingSeek=null;playerControls();text($('player-status'),'此原片暂时无法播放。可打开原片核对，或换用支持该编码的浏览器；未生成替代视频。');text($('playback-note'),'浏览器未能播放此原片，原记录与关键帧保留。')});
$('play-here').addEventListener('click',async()=>{
  if(pendingSeek||!data.segments[segmentIndex]?.video_url)return;const request=++playSequence,selection=seekSequence,selectedSegment=segmentIndex;
  const current=()=>request===playSequence&&selection===seekSequence&&selectedSegment===segmentIndex;
  try{if(video.paused||video.ended){if(video.ended)clampEvidence=null;await video.play()}else video.pause();if(current())syncPlaybackState()}
  catch(error){if(!current())return;if(error.name==='AbortError'){syncPlaybackState();return}text($('player-status'),'浏览器未开始播放。请使用原生播放控件或打开原片核对。')}
});
$('fullscreen').addEventListener('click',async()=>{if(!data.segments[segmentIndex]?.video_url)return;const selection=seekSequence,selectedSegment=segmentIndex;try{if(video.requestFullscreen)await video.requestFullscreen();else if(video.webkitEnterFullscreen)video.webkitEnterFullscreen();else text($('player-status'),'当前浏览器未提供全屏接口，请用“打开原片”或关键帧原尺寸查看。')}catch{if(selection===seekSequence&&selectedSegment===segmentIndex)text($('player-status'),'浏览器未进入全屏。仍可用“打开原片”或关键帧原尺寸查看。')}});
$('search').addEventListener('input',renderNav);
for(const s of data.segments){const r=node('section','record'),h=node('div','record-head');h.append(node('strong','',s.title),node('span','badge '+s.state,stateLabels[s.state]));r.append(h);const dl=node('dl');for(const [key,value] of [['原片路径',s.source_path],['开始时间',wall(s.started_at)],['结束时间'+(s.ended_at_estimated?'（估计）':''),wall(s.ended_at)],['视频时长',time(s.media_duration_seconds)],['墙钟跨度'+(s.ended_at_estimated?'（估计）':''),time(s.wall_duration_seconds)],['原片字节',s.bytes===null?'未读取':s.bytes.toLocaleString('zh-CN')],['原片 SHA-256',s.sha256||'未生成；原片缺失或仍在录制']]){dl.append(node('dt','',key),node('dd',key.includes('SHA')?'hash':'',value))}r.append(dl);if(s.recording_note)r.append(node('p','note',s.recording_note));if(s.video_url){const link=node('a','source-url','打开原片 ↗');link.href=s.video_url;link.target='_blank';link.rel='noopener';r.append(link)}if(!s.chapters.length)r.append(node('p','note','无操作章节点；保留原片，不自动补造章节或关键帧。'));if(s.limitations.length){const ul=node('ul');s.limitations.forEach(v=>ul.append(node('li','',v)));r.append(ul)}$('records').append(r)}
if(data.warnings.length){$('warnings').classList.remove('hidden');$('warnings').append(node('strong','','记录缺口'));const ul=node('ul');data.warnings.forEach(w=>ul.append(node('li','',w)));$('warnings').append(ul)}renderNav();if(chapters.length)select(chapters[0],false);
</script></body></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('segments', type=Path, help='segments.json（片段清单）')
    parser.add_argument('--chapters', type=Path, help='独立章节清单；默认读取 segments.json 同目录 chapters.json')
    parser.add_argument('--output-dir', type=Path, help='默认输出到清单同目录 playbook/')
    parser.add_argument('--ffmpeg', type=Path, default=DEFAULT_FFMPEG, help='真实视频解码和 PNG 提帧程序')
    parser.add_argument('--copy-videos', action='store_true', help='复制原片到输出 media/；默认保留相对链接，节省磁盘')
    args = parser.parse_args()
    manifest = args.segments.resolve()
    output = (args.output_dir or manifest.parent / 'playbook').resolve()
    chapters = args.chapters.resolve() if args.chapters else manifest.parent / 'chapters.json'
    try:
        if not args.ffmpeg.is_file():
            raise ValueError('未找到 ffmpeg，请通过 --ffmpeg 提供可执行文件')
        output.mkdir(parents=True, exist_ok=True)
        result = normalize(manifest, chapters, output, args.ffmpeg.resolve(), args.copy_videos)
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
        page = PAGE.replace('__TITLE__', html.escape(result['title'])).replace('__DATA__', encoded)
        (output / 'playbook.html').write_text(page)
        write_json(output / 'evidence-index.json', result)
        print(json.dumps({'html': str(output / 'playbook.html'), 'segments': len(result['segments']),
                          'chapters': sum(len(s['chapters']) for s in result['segments']),
                          'frames': sum(bool(c['frame_url']) for s in result['segments'] for c in s['chapters']),
                          'warnings': result['warnings'], 'media_mode': result['media_mode']}, ensure_ascii=False))
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print('无法生成 playbook：' + clean(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
