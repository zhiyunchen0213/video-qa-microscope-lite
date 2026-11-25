#!/usr/bin/env python3
"""Standalone local FFmpeg + Pillow microscope probe for reference breakdown and QA.

Dependencies: Python 3.9+, Pillow, ffmpeg & ffprobe in PATH.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import struct
import subprocess
import tempfile
import time
import wave
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageDraw

_PROBE_TIMEOUT = 60
_MAX_PAIRS = 16
_MAX_FRAMES = 60
_MAX_WAV_BYTES = 128 * 1024**2
_TILE = (320, 180)
_CUT = re.compile(r'\blavfi\.scd\.time:\s*([\d.]+)')
_BLACK = re.compile(r'\bblack_start:([\d.]+)\s+black_end:([\d.]+)')
_FRAME_TIME = re.compile(r'\bn:\s*\d+\s+pts:\s*-?\d+\s+pts_time:([\d.eE+-]+)')


class ProbeError(RuntimeError):
    pass


class MediaProbe:
    def __init__(self, path: Path, *, timeout: int = _PROBE_TIMEOUT):
        if not path.is_file():
            raise ProbeError(f'video file not found: {path}')
        self.path = str(path.resolve(strict=True))
        self.deadline = time.monotonic() + timeout
        self.ffmpeg = shutil.which('ffmpeg')
        self.ffprobe = shutil.which('ffprobe')
        if not self.ffprobe or not self.ffmpeg:
            raise ProbeError('ffmpeg and ffprobe must be installed and available in PATH')
        result = self.run([
            self.ffprobe, '-v', 'error', '-show_entries',
            'format=duration,bit_rate:stream=index,codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,duration'
            ':stream_disposition=attached_pic',
            '-of', 'json', self.path,
        ], probe=True)
        info = json.loads(result.stdout)
        video = next(
            (s for s in info.get('streams', [])
             if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic')),
            None,
        )
        if video is None:
            raise ProbeError('no_video_stream')
        audio = next((s for s in info.get('streams', []) if s.get('codec_type') == 'audio'), None)
        self.video_index = video['index']
        self.audio_index = audio['index'] if audio else None
        container = info.get('format', {})
        duration = float(video.get('duration') or container.get('duration') or 0)
        if not math.isfinite(duration) or duration <= 0:
            raise ProbeError('invalid_video_duration')
        rate = video.get('avg_frame_rate', '0/0')
        if rate in ('0/0', '0/1'):
            rate = video.get('r_frame_rate', '0/0')
        fps = float(Fraction(rate)) if rate not in ('0/0', '0/1') else None
        bit_rate = str(container.get('bit_rate', ''))
        self.media = {
            'width': video['width'],
            'height': video['height'],
            'fps': fps,
            'duration_sec': duration,
            'bit_rate': int(bit_rate) if bit_rate.isdigit() else None,
            'video_codec': video.get('codec_name'),
            'audio_codec': audio.get('codec_name') if audio else None,
        }

    def run(self, argv: list[str], *, probe: bool = False):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ProbeError('probe_timeout')
        result = subprocess.run(argv, capture_output=True, timeout=remaining, check=False)
        if result.returncode:
            raise ProbeError('ffprobe_failed' if probe else 'ffmpeg_failed')
        return result

    def decode(self, *output: str, loglevel: str = 'error', seek: float | None = None):
        seek_options = ['-ss', f'{seek:.6f}', '-copyts', '-start_at_zero'] if seek is not None else []
        return self.run([
            self.ffmpeg, '-hide_banner', '-nostdin', '-nostats', '-loglevel', loglevel, '-y',
            *seek_options, '-i', self.path, *output,
        ])

    def scan(self, threshold: float, *, black: bool = False):
        filters = f'scale=320:320:force_original_aspect_ratio=decrease:force_divisible_by=2,scdet=threshold={threshold:g}'
        if black:
            filters += ',blackdetect=d=0.15:pix_th=0.10'
        result = self.decode('-map', f'0:{self.video_index}', '-an', '-vf', filters, '-f', 'null', '-', loglevel='info')
        log = result.stderr.decode(errors='replace')
        duration = self.media['duration_sec']
        cuts = sorted({float(v) for v in _CUT.findall(log) if 0 < float(v) < duration})
        windows = [{'start_sec': float(s), 'end_sec': float(e)} for s, e in _BLACK.findall(log)]
        return cuts, windows

    def rgb_frames(self, width: int, height: int, filters: str, limit: int, *, seek: float | None = None, timestamps: bool = False):
        result = self.decode(
            '-map', f'0:{self.video_index}', '-an', '-vf', filters,
            '-frames:v', str(limit), '-fps_mode', 'passthrough', '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-',
            seek=seek, loglevel='info' if timestamps else 'error',
        )
        stride = width * height * 3
        frames = [Image.frombytes('RGB', (width, height), result.stdout[i:i + stride])
                  for i in range(0, len(result.stdout), stride)]
        if not timestamps:
            return frames
        times = _FRAME_TIME.findall(result.stderr.decode(errors='replace'))
        if len(times) < len(frames):
            raise ProbeError('missing_frame_timestamps')
        return list(zip(map(float, times), frames))

    def sample(self, fps: float, limit: int, width: int, height: int, scale: str):
        select = f"select='isnan(prev_selected_t)+gt(floor(t*{fps:.15g}),floor(prev_selected_t*{fps:.15g}))'"
        return self.rgb_frames(width, height, f'{select},{scale},showinfo', limit, timestamps=True)


def _fit(width: int, height: int) -> str:
    return (f'scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,'
            f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2')


def _label(image: Image.Image, origin: tuple[int, int], text: str) -> None:
    x, y = origin
    draw = ImageDraw.Draw(image)
    width = math.ceil(draw.textlength(text))
    draw.rectangle((x, y, x + width + 8, y + 16), fill='black')
    draw.text((x + 4, y + 2), text, fill='white')


@contextmanager
def _atomic_output(path: Path, mode: str, **options):
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode=mode, dir=path.parent, delete=False, **options) as handle:
            temp_path = Path(handle.name)
            yield handle
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _transition_pairs(probe: MediaProbe, cuts: list[float], out_dir: Path, limit: int):
    width, height = _TILE
    interval = 1 / (probe.media['fps'] or 25)
    filters = _fit(width, height) + ',showinfo'

    def frame(at: float):
        samples = probe.rgb_frames(width, height, filters, 1, seek=at, timestamps=True)
        return samples[0] if samples else None

    results = []
    for index, cut in enumerate(cuts[:limit], 1):
        target_before = max(0.0, cut - 0.15)
        before = frame(target_before)
        if before is not None and before[0] >= cut:
            before = frame(max(0.0, target_before - interval))
        after = frame(cut + 0.15) or frame(cut)
        if before is None or after is None or not before[0] < cut <= after[0]:
            continue
        pair = Image.new('RGB', (width * 2, height))
        for column, (at, image) in enumerate((before, after)):
            x = column * width
            pair.paste(image, (x, 0))
            _label(pair, (x, 0), f'Cut {index:02d} | {at:07.3f}s')
        path = out_dir / 'transition_pairs' / f'cut_{index:02d}.jpg'
        path.parent.mkdir(parents=True, exist_ok=True)
        with _atomic_output(path, 'wb') as output:
            pair.save(output, format='JPEG', quality=88)
        results.append({'cut_sec': cut, 'before_sec': before[0], 'after_sec': after[0], 'path': str(path)})
    return results


def _audio_evidence(probe: MediaProbe, out_dir: Path) -> dict:
    if probe.audio_index is None:
        return {'has_audio': False}

    peaks, drops = [], []
    with tempfile.TemporaryDirectory(prefix='.audio-', dir=out_dir) as temp:
        wav = Path(temp) / 'track.wav'
        probe.decode('-map', f'0:{probe.audio_index}', '-vn', '-ac', '1', '-ar', '16000',
                     '-c:a', 'pcm_s16le', '-fs', str(_MAX_WAV_BYTES), str(wav))
        if wav.stat().st_size >= _MAX_WAV_BYTES:
            raise ProbeError('audio_size_limit')
        with wave.open(str(wav), 'rb') as track:
            previous, index = None, 0
            while data := track.readframes(1600):
                if time.monotonic() >= probe.deadline:
                    raise ProbeError('probe_timeout')
                samples = struct.unpack(f'<{len(data) // 2}h', data)
                rms = math.sqrt(sum(v * v for v in samples) / len(samples))
                level = 20 * math.log10(max(rms / 32768, 1e-6))
                if previous is not None:
                    change = level - previous
                    event = {'time_sec': index / 10, 'level_db': round(level, 2)}
                    if change >= 9.5 and level > -30.0:
                        peaks = sorted([*peaks, {**event, 'jump_db': round(change, 2)}],
                                       key=lambda e: -e['jump_db'])[:10]
                    if change <= -12.0 and level < -35.0:
                        drops = sorted([*drops, {**event, 'drop_db': round(change, 2)}],
                                       key=lambda e: e['drop_db'])[:10]
                previous, index = level, index + 1
    return {'has_audio': True, 'top_transient_peaks': peaks, 'cliff_drops': drops}


def _picture_pixels(image: Image.Image):
    width, height = image.size
    gray = image.convert('L').tobytes()
    top, bottom = 0, height
    border = int(height * 0.12)

    def dark(row: int) -> bool:
        return sum(v <= 16 for v in gray[row * width:(row + 1) * width]) >= 0.95 * width

    while top < border and dark(top):
        top += 1
    while bottom > height - border and dark(bottom - 1):
        bottom -= 1
    start, end = top * width, bottom * width
    pixels = struct.iter_unpack('BBB', image.tobytes()[start * 3:end * 3])
    return zip(gray[start:end], pixels)


def _palette(probe: MediaProbe, out_dir: Path) -> dict:
    samples = probe.sample(1, 40, 96, 96, 'scale=96:96')
    pixels = sorted((px for _, frame in samples for px in _picture_pixels(frame)), key=lambda px: px[0])
    if not pixels:
        raise ProbeError('no_sample_frames')
    swatches = Image.new('RGB', (480, 76))
    colors = []
    for index in range(5):
        band = pixels[index * len(pixels) // 5:(index + 1) * len(pixels) // 5]
        rgb = tuple(round(sum(c) / len(band)) for c in zip(*(px_rgb for _, px_rgb in band)))
        value = '#{:02X}{:02X}{:02X}'.format(*rgb)
        colors.append(value)
        x = index * 96
        ImageDraw.Draw(swatches).rectangle((x, 0, x + 95, 75), fill=rgb)
        _label(swatches, (x + 8, 52), value)
    path = out_dir / 'palette_5hex.png'
    with _atomic_output(path, 'wb') as output:
        swatches.save(output, format='PNG')
    p05 = pixels[int(0.05 * (len(pixels) - 1))][0]
    return {
        'palette_5hex': colors,
        'palette_path': str(path),
        'p05_black_point': p05,
        'faded_film_black_likely': p05 > 18.0,
    }


def probe_reference(video_path: Path, out_dir: Path, *, max_pairs: int = 16, sc_thresh: float = 14.0) -> dict:
    probe = MediaProbe(video_path)
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    cuts, black = probe.scan(sc_thresh, black=True)
    return {
        'status': 'ok',
        'mode': 'reference',
        'media': probe.media,
        'detected_cuts_sec': cuts,
        'black_or_title_windows': black,
        'transition_pairs': _transition_pairs(probe, cuts, out_dir, min(max(max_pairs, 0), _MAX_PAIRS)),
        'audio': _audio_evidence(probe, out_dir),
        **_palette(probe, out_dir),
    }


def probe_qa(
    video_path: Path, out_dir: Path, *, fps: float = 2.0, scene_thresh: float = 0.28,
    max_frames: int = 60, cols: int = 4, dense_ref: bool = False,
) -> dict:
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError('fps must be finite and positive')
    probe = MediaProbe(video_path)
    duration = probe.media['duration_sec']
    cuts, _ = probe.scan(scene_thresh * 100)
    tail = [c for c in cuts if round(duration - 2.0, 6) <= c <= round(duration - 0.15, 6)]
    limit = min(max(1, max_frames), 600 if dense_ref else _MAX_FRAMES)
    effective = min(fps, limit / max(duration, 0.5))
    width, height = (160, 90) if dense_ref and duration > 60 else _TILE
    samples = probe.sample(effective, limit, width, height, _fit(width, height))
    if not samples:
        raise ProbeError('no_sample_frames')
    cols = min(max(1, cols), 8, len(samples))
    sheet = Image.new('RGB', (cols * width, math.ceil(len(samples) / cols) * height))
    for index, (at, image) in enumerate(samples):
        origin = ((index % cols) * width, (index // cols) * height)
        sheet.paste(image, origin)
        _label(sheet, origin, f'{at:08.3f}s')
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / 'dense_sheet.png'
    with _atomic_output(path, 'wb') as output:
        sheet.save(output, format='PNG')
    return {
        'status': 'ok',
        'mode': 'qa' if not dense_ref else 'reference_dense',
        'media': probe.media,
        'detected_cuts_sec': cuts,
        'suspected_tail_utility_frame_cuts': tail,
        'suggested_safe_out_sec': round(max(0.0, tail[0] - 0.05), 6) if tail else None,
        'effective_fps': effective,
        'frame_count': len(samples),
        'sampled_times_sec': [at for at, _ in samples],
        'dense_sheet_path': str(path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='0.1s FFmpeg microscope probe for AI video reference & QA')
    parser.add_argument('video', type=Path, help='Path to local video file (.mp4 / .mov / .mkv)')
    parser.add_argument('--mode', choices=('reference', 'qa', 'reference_dense'), default='qa')
    parser.add_argument('--out', type=Path, required=True, help='Output directory for evidence & summary.json')
    parser.add_argument('--fps', type=float, default=2.0, help='Sampling fps for qa / reference_dense mode')
    parser.add_argument('--sc-thresh', type=float, default=14.0, help='FFmpeg scdet threshold for reference mode')
    parser.add_argument('--qa-thresh', type=float, default=0.28, help='Scene cut threshold (0-1) for qa mode')
    args = parser.parse_args()

    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.mode == 'reference':
        result = probe_reference(args.video, out_dir, sc_thresh=args.sc_thresh)
    elif args.mode == 'reference_dense':
        result = probe_qa(args.video, out_dir, fps=args.fps, scene_thresh=args.qa_thresh, max_frames=600, cols=8, dense_ref=True)
    else:
        result = probe_qa(args.video, out_dir, fps=args.fps, scene_thresh=args.qa_thresh)

    summary_path = out_dir / 'summary.json'
    with _atomic_output(summary_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
