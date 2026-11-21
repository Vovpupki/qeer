import os
import json
import math
import re
import time
import urllib.parse
import threading
from random import random

import requests
from bs4 import BeautifulSoup

try:
    import certifi
    SSL_VERIFY = certifi.where()
except ImportError:
    SSL_VERIFY = True

from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.button import Button
from kivy.uix.textinput import TextInput
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.slider import Slider
from kivy.uix.widget import Widget
from kivy.uix.image import Image
from kivy.uix.popup import Popup
from kivy.uix.filechooser import FileChooserListView
from kivy.core.audio import SoundLoader
from kivy.clock import Clock
from kivy.graphics import Color, Rectangle, RoundedRectangle, Line, Ellipse
from kivy.metrics import dp, sp
from kivy.utils import platform


AUDIO_EXTENSIONS = ('.mp3', '.m4a', '.aac', '.ogg', '.flac', '.wav', '.opus')


def format_time(seconds):
    try:
        secs = max(0, int(float(seconds)))
    except Exception:
        secs = 0
    mins, secs = divmod(secs, 60)
    return f"{mins:02d}:{secs:02d}"


def _decode_id3_text(raw):
    if not raw:
        return ""
    enc_byte = raw[0] if len(raw) > 0 else 0
    payload = raw[1:] if enc_byte in (0, 1, 2, 3) else raw
    if enc_byte == 1:
        encs = ('utf-16', 'utf-16-le', 'utf-8', 'cp1251')
    elif enc_byte == 2:
        encs = ('utf-16-be', 'utf-16', 'utf-8')
    elif enc_byte == 3:
        encs = ('utf-8', 'cp1251', 'latin1')
    else:
        encs = ('cp1251', 'utf-8', 'latin1')

    for enc in encs:
        try:
            text = payload.decode(enc, errors='strict').replace('\x00', '').strip()
            if text:
                return text
        except Exception:
            continue
    return payload.decode('latin1', errors='ignore').replace('\x00', '').strip()


def extract_id3_info(filepath, cover_save_path=None):
    """
    Читает название, исполнителя, альбом и обложку (APIC) из ID3v2 / ID3v1 тегов MP3.
    Возвращает dict: {'artist': str, 'title': str, 'album': str, 'cover_path': str|None}.
    """
    info = {'artist': '', 'title': '', 'album': '', 'cover_path': None}
    try:
        size = os.path.getsize(filepath)
        with open(filepath, 'rb') as f:
            head = f.read(10)
            if len(head) == 10 and head[:3] == b'ID3':
                ver_major = head[3]
                tag_size = (
                    ((head[6] & 0x7F) << 21)
                    | ((head[7] & 0x7F) << 14)
                    | ((head[8] & 0x7F) << 7)
                    | (head[9] & 0x7F)
                )
                read_len = min(tag_size, 6 * 1024 * 1024)
                tag_data = f.read(read_len)
                pos = 0
                if ver_major in (3, 4):
                    while pos + 10 <= len(tag_data):
                        frame_id_raw = tag_data[pos:pos + 4]
                        if frame_id_raw == b'\x00\x00\x00\x00':
                            break
                        try:
                            frame_id = frame_id_raw.decode('ascii')
                        except Exception:
                            break
                        if not re.fullmatch(r'[A-Z0-9]{4}', frame_id):
                            break

                        sb = tag_data[pos + 4:pos + 8]
                        if ver_major == 4:
                            frame_size = (
                                ((sb[0] & 0x7F) << 21)
                                | ((sb[1] & 0x7F) << 14)
                                | ((sb[2] & 0x7F) << 7)
                                | (sb[3] & 0x7F)
                            )
                        else:
                            frame_size = int.from_bytes(sb, 'big')

                        pos += 10
                        if frame_size <= 0 or pos + frame_size > len(tag_data):
                            break
                        payload = tag_data[pos:pos + frame_size]
                        pos += frame_size

                        if frame_id == 'TIT2' and not info['title']:
                            info['title'] = _decode_id3_text(payload)
                        elif frame_id == 'TPE1' and not info['artist']:
                            info['artist'] = _decode_id3_text(payload)
                        elif frame_id == 'TALB' and not info['album']:
                            info['album'] = _decode_id3_text(payload)
                        elif frame_id == 'APIC' and cover_save_path and not info['cover_path']:
                            img_bytes = None
                            jpg_idx = payload.find(b'\xff\xd8\xff')
                            png_idx = payload.find(b'\x89PNG\r\n\x1a\n')
                            if jpg_idx != -1 and (png_idx == -1 or jpg_idx < png_idx):
                                img_bytes = payload[jpg_idx:]
                            elif png_idx != -1:
                                img_bytes = payload[png_idx:]
                            if img_bytes and len(img_bytes) > 256:
                                with open(cover_save_path, 'wb') as img_f:
                                    img_f.write(img_bytes)
                                info['cover_path'] = cover_save_path

            # Резерв: ID3v1 в конце файла
            if (not info['title'] or not info['artist']) and size >= 128:
                f.seek(-128, os.SEEK_END)
                tag1 = f.read(128)
                if tag1[:3] == b'TAG':
                    def dec1(raw_bytes):
                        raw_bytes = raw_bytes.rstrip(b'\x00 ')
                        for enc in ('utf-8', 'cp1251', 'latin1'):
                            try:
                                val = raw_bytes.decode(enc).strip()
                                if val:
                                    return val
                            except Exception:
                                continue
                        return ""

                    if not info['title']:
                        info['title'] = dec1(tag1[3:33])
                    if not info['artist']:
                        info['artist'] = dec1(tag1[33:63])
                    if not info['album']:
                        info['album'] = dec1(tag1[63:93])
    except Exception:
        pass
    return info


def fetch_online_cover(query_title, save_path):
    """Загружает обложку альбома через iTunes Search API, если в MP3 нет картинки."""
    try:
        clean_q = re.sub(r'\(.*?\)|\[.*?\]', '', query_title)
        clean_q = re.sub(r'[_]+', ' ', clean_q).strip()
        if not clean_q or re.fullmatch(r'Трек\s+\d+', clean_q):
            return None
        res = requests.get(
            "https://itunes.apple.com/search",
            params={"term": clean_q, "entity": "song", "limit": 1},
            timeout=5,
            verify=SSL_VERIFY,
        )
        if res.status_code != 200:
            return None
        data = res.json()
        results = data.get("results") or []
        if not results:
            return None
        art_url = results[0].get("artworkUrl100") or results[0].get("artworkUrl60")
        if not art_url:
            return None
        art_url = art_url.replace("100x100bb", "400x400bb").replace("60x60bb", "400x400bb")
        img_res = requests.get(art_url, timeout=6, verify=SSL_VERIFY)
        if img_res.status_code == 200 and len(img_res.content) > 512:
            with open(save_path, 'wb') as f:
                f.write(img_res.content)
            return save_path
    except Exception:
        pass
    return None


def estimate_mp3_duration(filepath):
    """Резервная оценка длительности MP3 по первому фрейму."""
    bitrates_mpeg1_l3 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
    try:
        file_size = os.path.getsize(filepath)
        with open(filepath, 'rb') as f:
            head = f.read(10)
            offset = 0
            if head[:3] == b'ID3' and len(head) == 10:
                tag_size = (
                    ((head[6] & 0x7F) << 21)
                    | ((head[7] & 0x7F) << 14)
                    | ((head[8] & 0x7F) << 7)
                    | (head[9] & 0x7F)
                )
                offset = 10 + tag_size
            f.seek(offset)
            chunk = f.read(16384)
            for i in range(len(chunk) - 4):
                b1, b2, b3 = chunk[i], chunk[i + 1], chunk[i + 2]
                if b1 == 0xFF and (b2 & 0xE0) == 0xE0:
                    version = (b2 >> 3) & 0x03
                    layer = (b2 >> 1) & 0x03
                    br_idx = (b3 >> 4) & 0x0F
                    if version == 3 and layer == 1 and 0 < br_idx < 15:
                        kbps = bitrates_mpeg1_l3[br_idx]
                        audio_bytes = max(0, file_size - offset)
                        return audio_bytes / (kbps * 125.0)
    except Exception:
        pass
    return 0.0


def compute_audio_envelope(filepath, buckets=96):
    """Быстро строит огибающую энергии аудиофайла для реактивного анализатора спектра."""
    vals = []
    try:
        size = os.path.getsize(filepath)
        if size < 4096:
            return [0.5] * buckets
        with open(filepath, 'rb') as f:
            head = f.read(10)
            start = 0
            if head[:3] == b'ID3' and len(head) == 10:
                tag_size = (
                    ((head[6] & 0x7F) << 21)
                    | ((head[7] & 0x7F) << 14)
                    | ((head[8] & 0x7F) << 7)
                    | (head[9] & 0x7F)
                )
                start = min(size - 1024, 10 + tag_size)
            span = max(1, size - start - 256)
            for i in range(buckets):
                pos = start + int((i / max(1, buckets - 1)) * span)
                f.seek(pos)
                block = f.read(256)
                if not block:
                    vals.append(0.3)
                    continue
                mean = sum(block) / len(block)
                var = sum(abs(b - mean) for b in block) / len(block)
                vals.append(var)
        mx = max(vals) if vals else 1.0
        mn = min(vals) if vals else 0.0
        denom = max(1e-5, mx - mn)
        norm = [0.22 + 0.78 * ((v - mn) / denom) for v in vals]
        return norm
    except Exception:
        return [0.55] * buckets


class AndroidAudioPlayer:
    """Нативный плеер Android MediaPlayer с точной перемоткой и позицией."""

    def __init__(self, filepath):
        from jnius import autoclass
        MediaPlayer = autoclass('android.media.MediaPlayer')
        self.mp = MediaPlayer()
        try:
            AudioAttributesBuilder = autoclass('android.media.AudioAttributes$Builder')
            attrs = (
                AudioAttributesBuilder()
                .setUsage(1)
                .setContentType(2)
                .build()
            )
            self.mp.setAudioAttributes(attrs)
        except Exception:
            pass
        self.mp.setDataSource(filepath)
        self.mp.prepare()
        dur_ms = self.mp.getDuration()
        self.duration = max(0.0, float(dur_ms) / 1000.0) if dur_ms > 0 else 0.0
        self._released = False
        self._started = False

    def play(self):
        if not self._released:
            self.mp.start()
            self._started = True

    def pause(self):
        if not self._released and self.mp.isPlaying():
            self.mp.pause()

    def resume(self):
        if not self._released:
            self.mp.start()

    def seek(self, seconds):
        if self._released:
            return
        target = max(0.0, float(seconds))
        if self.duration > 1.0:
            target = min(target, self.duration - 0.35)
        ms = int(target * 1000)
        try:
            self.mp.seekTo(ms, 3)
        except Exception:
            self.mp.seekTo(ms)

    def get_pos(self):
        if self._released:
            return 0.0
        try:
            return max(0.0, float(self.mp.getCurrentPosition()) / 1000.0)
        except Exception:
            return 0.0

    def is_playing(self):
        if self._released:
            return False
        try:
            return bool(self.mp.isPlaying())
        except Exception:
            return False

    def release(self):
        if not self._released:
            self._released = True
            for fn in (self.mp.stop, self.mp.reset, self.mp.release):
                try:
                    fn()
                except Exception:
                    pass


class DesktopAudioPlayer:
    """Обёртка над Kivy SoundLoader для запуска на ПК/Linux."""

    def __init__(self, filepath):
        self.sound = SoundLoader.load(filepath)
        if not self.sound:
            raise RuntimeError("SoundLoader failed")
        self.duration = float(self.sound.length or 0.0)
        self._paused_pos = 0.0
        self._manual_stop = False
        self._started = False

    def play(self):
        self._manual_stop = False
        self.sound.play()
        self._started = True

    def pause(self):
        try:
            self._paused_pos = float(self.sound.get_pos() or 0.0)
        except Exception:
            pass
        self._manual_stop = True
        self.sound.stop()

    def resume(self):
        self._manual_stop = False
        self.sound.play()
        if self._paused_pos > 0:
            try:
                self.sound.seek(self._paused_pos)
            except Exception:
                pass

    def seek(self, seconds):
        target = max(0.0, float(seconds))
        self._paused_pos = target
        try:
            self.sound.seek(target)
        except Exception:
            pass

    def get_pos(self):
        try:
            if self.sound.state == 'play':
                return max(0.0, float(self.sound.get_pos() or 0.0))
        except Exception:
            pass
        return self._paused_pos

    def is_playing(self):
        return bool(self.sound and self.sound.state == 'play')

    def release(self):
        self._manual_stop = True
        try:
            self.sound.stop()
            self.sound.unload()
        except Exception:
            pass


class SeekSlider(Slider):
    """Ползунок перемотки, гарантированно срабатывающий при нажатии и перетаскивании."""

    def __init__(self, on_seek_commit=None, on_seek_preview=None, **kwargs):
        super().__init__(**kwargs)
        self.on_seek_commit = on_seek_commit
        self.on_seek_preview = on_seek_preview
        self._dragging = False

    def on_touch_down(self, touch):
        if self.disabled or not self.collide_point(*touch.pos):
            return False
        self._dragging = True
        res = super().on_touch_down(touch)
        if self.on_seek_preview:
            self.on_seek_preview(self.value)
        return res or True

    def on_touch_move(self, touch):
        if self._dragging and touch.grab_current is self:
            res = super().on_touch_move(touch)
            if self.on_seek_preview:
                self.on_seek_preview(self.value)
            return res
        return super().on_touch_move(touch)

    def on_touch_up(self, touch):
        was_mine = self._dragging and (touch.grab_current is self or touch.grab_current is None)
        res = super().on_touch_up(touch)
        if was_mine:
            self._dragging = False
            if self.on_seek_commit:
                self.on_seek_commit(self.value)
            return True
        return res


class AlbumCoverWidget(BoxLayout):
    """Квадратный виджет обложки альбома с красивой виниловой заглушкой."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.has_image = False
        self.img = Image(fit_mode='cover', opacity=0)
        self.add_widget(self.img)
        self.bind(pos=self._redraw, size=self._redraw)

    def set_cover(self, image_path=None):
        if image_path and os.path.exists(image_path):
            self.img.source = image_path
            self.img.reload()
            self.img.opacity = 1
            self.has_image = True
        else:
            self.img.source = ''
            self.img.opacity = 0
            self.has_image = False
        self._redraw()

    def _redraw(self, *args):
        self.canvas.before.clear()
        with self.canvas.before:
            Color(0.11, 0.11, 0.13, 1)
            RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(10)])
            if not self.has_image:
                cx = self.x + self.width / 2.0
                cy = self.y + self.height / 2.0
                r = max(dp(12), min(self.width, self.height) * 0.39)
                Color(0.04, 0.04, 0.05, 1)
                Ellipse(pos=(cx - r, cy - r), size=(2 * r, 2 * r))
                Color(0.22, 0.22, 0.24, 1)
                Line(circle=(cx, cy, r * 0.85), width=1.1)
                Line(circle=(cx, cy, r * 0.65), width=1.0)
                r_inner = r * 0.36
                Color(1, 0.48, 0.05, 1)
                Ellipse(pos=(cx - r_inner, cy - r_inner), size=(2 * r_inner, 2 * r_inner))
                r_hole = max(dp(2.5), r * 0.1)
                Color(0.08, 0.08, 0.08, 1)
                Ellipse(pos=(cx - r_hole, cy - r_hole), size=(2 * r_hole, 2 * r_hole))


class SpectrumWidget(Widget):
    """Мульти-режимный анализатор спектра (Пики, Зеркало, Волна, Неон)."""

    MODES = ("Пики", "Зеркало", "Волна", "Неон")

    def __init__(self, on_mode_change=None, **kwargs):
        super().__init__(**kwargs)
        self.num_bars = 18
        self.values = [0.08] * self.num_bars
        self.peaks = [0.12] * self.num_bars
        self.mode_index = 0
        self.phase = 0.0
        self.envelope = []
        self.on_mode_change = on_mode_change
        self.bind(pos=self._draw, size=self._draw)

    @property
    def current_mode_name(self):
        return self.MODES[self.mode_index]

    def cycle_mode(self):
        self.mode_index = (self.mode_index + 1) % len(self.MODES)
        if self.on_mode_change:
            self.on_mode_change(self.current_mode_name)
        self._draw()
        return self.current_mode_name

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos):
            self.cycle_mode()
            return True
        return super().on_touch_down(touch)

    def set_envelope(self, env):
        self.envelope = env or []

    def step(self, is_playing, progress_ratio=0.0):
        self.phase += 0.28
        energy = 0.65
        if is_playing and self.envelope:
            idx = min(len(self.envelope) - 1, max(0, int(progress_ratio * (len(self.envelope) - 1))))
            energy = self.envelope[idx]

        for i in range(self.num_bars):
            if is_playing:
                wave1 = 0.5 + 0.5 * math.sin(self.phase * (1.0 + (i % 4) * 0.22) + i * 0.65)
                wave2 = 0.5 + 0.5 * math.cos(self.phase * 1.7 - i * 0.42)
                bass_boost = 1.15 if i < 5 else (0.95 if i < 12 else 0.85)
                jitter = 0.72 + 0.45 * random()
                target = min(1.0, max(0.08, energy * bass_boost * (0.45 * wave1 + 0.35 * wave2 + 0.25) * jitter))
                self.values[i] += (target - self.values[i]) * 0.48
                if self.values[i] >= self.peaks[i]:
                    self.peaks[i] = self.values[i]
                else:
                    self.peaks[i] = max(0.08, self.peaks[i] - 0.024)
            else:
                self.values[i] += (0.06 - self.values[i]) * 0.22
                self.peaks[i] = max(0.06, self.peaks[i] - 0.03)
        self._draw()

    def _draw(self, *args):
        self.canvas.clear()
        w = max(10.0, self.width)
        h = max(10.0, self.height)
        pad_x = dp(4)
        pad_y = dp(4)
        usable_w = max(10.0, w - 2 * pad_x)
        usable_h = max(10.0, h - 2 * pad_y)
        mode = self.current_mode_name

        with self.canvas:
            if mode == "Пики":
                gap = dp(3)
                bar_w = max(dp(3), (usable_w - gap * (self.num_bars - 1)) / self.num_bars)
                seg_h = dp(4)
                seg_gap = dp(2)
                for i, val in enumerate(self.values):
                    bx = self.x + pad_x + i * (bar_w + gap)
                    total_h = max(dp(4), val * usable_h)
                    y_cur = self.y + pad_y
                    while y_cur < self.y + pad_y + total_h:
                        frac = (y_cur - (self.y + pad_y)) / usable_h
                        Color(1.0, max(0.18, 0.72 - frac * 0.55), 0.05, 1)
                        Rectangle(pos=(bx, y_cur), size=(bar_w, min(seg_h, self.y + pad_y + total_h - y_cur)))
                        y_cur += seg_h + seg_gap
                    py = self.y + pad_y + min(usable_h - dp(3), self.peaks[i] * usable_h)
                    Color(1, 0.96, 0.85, 0.95)
                    Rectangle(pos=(bx, py), size=(bar_w, dp(2.5)))

            elif mode == "Зеркало":
                gap = dp(3)
                bar_w = max(dp(3), (usable_w - gap * (self.num_bars - 1)) / self.num_bars)
                cy = self.y + h / 2.0
                Color(0.25, 0.25, 0.3, 0.6)
                Line(points=[self.x + pad_x, cy, self.x + w - pad_x, cy], width=1.0)
                for i, val in enumerate(self.values):
                    bx = self.x + pad_x + i * (bar_w + gap)
                    half_h = max(dp(3), val * (usable_h * 0.48))
                    Color(1.0, 0.52, 0.08, 0.95)
                    RoundedRectangle(pos=(bx, cy + dp(1)), size=(bar_w, half_h), radius=[dp(2)])
                    Color(0.15, 0.78, 0.92, 0.85)
                    RoundedRectangle(pos=(bx, cy - half_h - dp(1)), size=(bar_w, half_h), radius=[dp(2)])

            elif mode == "Волна":
                cy = self.y + h / 2.0
                pts_main = []
                pts_echo = []
                n = 36
                for k in range(n):
                    t = k / (n - 1)
                    idx = min(self.num_bars - 1, int(t * (self.num_bars - 1)))
                    amp = self.values[idx] * usable_h * 0.44
                    x = self.x + pad_x + t * usable_w
                    y1 = cy + amp * math.sin(t * 10.0 + self.phase * 1.4)
                    y2 = cy + amp * 0.65 * math.cos(t * 7.5 - self.phase * 1.1)
                    pts_main.extend([x, y1])
                    pts_echo.extend([x, y2])
                Color(0.95, 0.25, 0.65, 0.55)
                Line(points=pts_echo, width=dp(2.0))
                Color(1.0, 0.58, 0.05, 1.0)
                Line(points=pts_main, width=dp(2.6))

            else:  # "Неон"
                gap = dp(4)
                bar_w = max(dp(3), (usable_w - gap * (self.num_bars - 1)) / self.num_bars)
                for i, val in enumerate(self.values):
                    bx = self.x + pad_x + i * (bar_w + gap)
                    bh = max(dp(5), val * usable_h)
                    t = i / max(1, self.num_bars - 1)
                    Color(1.0 - t * 0.7, 0.25 + 0.55 * math.sin(t * math.pi), 0.3 + t * 0.65, 0.95)
                    RoundedRectangle(pos=(bx, self.y + pad_y), size=(bar_w, bh), radius=[dp(3)])
                    Color(1, 1, 1, 0.8)
                    Rectangle(pos=(bx, self.y + pad_y + bh - dp(2)), size=(bar_w, dp(2)))


class MusicApp(App):
    def build(self):
        self.title = "Kulikov Sound"
        self.track_list = []
        self.current_track_index = 0
        self.player = None
        self.track_duration = 0.0
        self.is_paused = False
        self._ignore_pos_until = 0.0
        self._manual_stop = False
        self.play_request_id = 0
        self.search_request_id = 0
        self.vis_event = None
        self.progress_event = None
        self.selected_quality = "320"

        self.headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 13; Mobile) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
        }

        os.makedirs(self.user_data_dir, exist_ok=True)
        self.playlist_file = os.path.join(self.user_data_dir, "playlists.json")
        self.playlists_db, self.active_playlist_name = self.load_playlists()

        self._request_android_permissions()

        main_layout = BoxLayout(orientation='vertical', padding=dp(10), spacing=dp(7))
        with main_layout.canvas.before:
            Color(0.05, 0.05, 0.06, 1)
            self.rect = Rectangle(size=main_layout.size, pos=main_layout.pos)
        main_layout.bind(size=self._update_rect, pos=self._update_rect)

        # --- ШАПКА БРЕНДА ---
        header_box = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(34), spacing=dp(6))
        brand_col = BoxLayout(orientation='vertical', size_hint_x=0.65)
        brand_title = Label(
            text='KULIKOV SOUND',
            font_size=sp(17),
            bold=True,
            color=(1, 0.52, 0.05, 1),
            halign='left',
            valign='middle',
        )
        brand_title.bind(size=lambda inst, val: setattr(inst, 'text_size', val))
        brand_sub = Label(
            text='Александр Куликов',
            font_size=sp(10.5),
            color=(0.68, 0.68, 0.72, 1),
            halign='left',
            valign='middle',
        )
        brand_sub.bind(size=lambda inst, val: setattr(inst, 'text_size', val))
        brand_col.add_widget(brand_title)
        brand_col.add_widget(brand_sub)

        self.spec_mode_btn = Button(
            text='Спектр: Пики',
            size_hint_x=0.35,
            font_size=sp(11.5),
            bold=True,
            background_normal='',
            background_color=(0.18, 0.18, 0.2, 1),
            color=(1, 0.65, 0.15, 1),
        )
        header_box.add_widget(brand_col)
        header_box.add_widget(self.spec_mode_btn)
        main_layout.add_widget(header_box)

        # --- ПОИСК ---
        search_box = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(44), spacing=dp(6))
        self.search_input = TextInput(
            hint_text='Поиск песни или группы...',
            multiline=False,
            font_size=sp(14.5),
            padding=[dp(10), dp(11)],
            background_color=(0.14, 0.14, 0.16, 1),
            foreground_color=(1, 0.62, 0.15, 1),
            hint_text_color=(0.55, 0.42, 0.25, 1),
            cursor_color=(1, 0.55, 0.1, 1),
        )
        self.search_input.bind(on_text_validate=self.start_search)

        search_btn = Button(
            text='Поиск',
            size_hint_x=0.26,
            font_size=sp(14.5),
            bold=True,
            background_normal='',
            background_color=(1, 0.5, 0, 1),
            color=(0, 0, 0, 1),
        )
        search_btn.bind(on_press=self.start_search)
        search_box.add_widget(self.search_input)
        search_box.add_widget(search_btn)
        main_layout.add_widget(search_box)

        # --- КАРТОЧКА: ОБЛОЖКА АЛЬБОМА + ИНФО + СПЕКТР ---
        now_card = BoxLayout(
            orientation='horizontal',
            size_hint_y=None,
            height=dp(104),
            spacing=dp(8),
            padding=dp(6),
        )
        with now_card.canvas.before:
            Color(0.10, 0.10, 0.12, 1)
            self.card_bg = RoundedRectangle(pos=now_card.pos, size=now_card.size, radius=[dp(10)])
        now_card.bind(pos=self._update_card_bg, size=self._update_card_bg)

        self.cover_widget = AlbumCoverWidget(size_hint=(None, None), size=(dp(92), dp(92)))
        now_card.add_widget(self.cover_widget)

        right_card = BoxLayout(orientation='vertical', spacing=dp(3))
        self.status_label = Label(
            text='Добро пожаловать в Kulikov Sound!',
            size_hint_y=None,
            height=dp(26),
            font_size=sp(13),
            bold=True,
            color=(1, 0.68, 0.15, 1),
            halign='left',
            valign='middle',
            shorten=True,
            shorten_from='right',
        )
        self.status_label.bind(size=lambda inst, val: setattr(inst, 'text_size', val))

        self.spectrum = SpectrumWidget(
            on_mode_change=lambda m: setattr(self.spec_mode_btn, 'text', f'Спектр: {m}')
        )
        self.spec_mode_btn.bind(on_press=lambda inst: self.spectrum.cycle_mode())

        right_card.add_widget(self.status_label)
        right_card.add_widget(self.spectrum)
        now_card.add_widget(right_card)
        main_layout.add_widget(now_card)

        # --- СПИСОК ТРЕКОВ ---
        scroll_view = ScrollView(size_hint_y=1)
        self.results_layout = GridLayout(cols=1, spacing=dp(5), size_hint_y=None)
        self.results_layout.bind(minimum_height=self.results_layout.setter('height'))
        scroll_view.add_widget(self.results_layout)
        main_layout.add_widget(scroll_view)

        # --- ПОЛЗУНОК ПЕРЕМОТКИ ---
        seek_box = BoxLayout(
            orientation='horizontal',
            size_hint_y=None,
            height=dp(40),
            spacing=dp(6),
            padding=[dp(2), 0],
        )
        self.time_current_label = Label(
            text='00:00',
            size_hint_x=None,
            width=dp(44),
            font_size=sp(12.5),
            color=(1, 0.65, 0.1, 1),
        )
        self.seek_slider = SeekSlider(
            min=0,
            max=100,
            value=0,
            cursor_size=(dp(22), dp(22)),
            value_track=True,
            value_track_color=(1, 0.5, 0, 1),
            value_track_width=dp(4),
            on_seek_preview=self._on_seek_preview,
            on_seek_commit=self._on_seek_commit,
        )
        self.time_total_label = Label(
            text='00:00',
            size_hint_x=None,
            width=dp(44),
            font_size=sp(12.5),
            color=(0.72, 0.72, 0.76, 1),
        )
        seek_box.add_widget(self.time_current_label)
        seek_box.add_widget(self.seek_slider)
        seek_box.add_widget(self.time_total_label)
        main_layout.add_widget(seek_box)

        # --- ПАНЕЛЬ ДЕЙСТВИЙ И УПРАВЛЕНИЯ ---
        bottom_panel = BoxLayout(orientation='vertical', size_hint_y=None, height=dp(106), spacing=dp(6))

        action_box = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(44), spacing=dp(6))
        download_btn = Button(
            text='Скачать',
            font_size=sp(13),
            bold=True,
            background_normal='',
            background_color=(1, 0.42, 0, 1),
            color=(0, 0, 0, 1),
        )
        download_btn.bind(on_press=self.open_quality_popup)

        add_playlist_btn = Button(
            text='+ Плейлист',
            font_size=sp(13),
            bold=True,
            background_normal='',
            background_color=(1, 0.5, 0.05, 1),
            color=(0, 0, 0, 1),
        )
        add_playlist_btn.bind(on_press=self.open_add_to_playlist_popup)

        show_playlist_btn = Button(
            text='Плейлисты',
            font_size=sp(13),
            bold=True,
            background_normal='',
            background_color=(0.72, 0.28, 0, 1),
            color=(1, 1, 1, 1),
        )
        show_playlist_btn.bind(on_press=self.show_playlists_manager)

        action_box.add_widget(download_btn)
        action_box.add_widget(add_playlist_btn)
        action_box.add_widget(show_playlist_btn)
        bottom_panel.add_widget(action_box)

        nav_box = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(54), spacing=dp(6))
        prev_btn = Button(
            text='<<',
            font_size=sp(16),
            bold=True,
            background_normal='',
            background_color=(0.2, 0.2, 0.22, 1),
            color=(1, 1, 1, 1),
        )
        prev_btn.bind(on_press=self.prev_track)

        self.play_btn = Button(
            text='PLAY',
            font_size=sp(16),
            bold=True,
            background_normal='',
            background_color=(1, 0.5, 0, 1),
            color=(0, 0, 0, 1),
        )
        self.play_btn.bind(on_press=self.toggle_play)

        next_btn = Button(
            text='>>',
            font_size=sp(16),
            bold=True,
            background_normal='',
            background_color=(0.2, 0.2, 0.22, 1),
            color=(1, 1, 1, 1),
        )
        next_btn.bind(on_press=self.next_track)

        nav_box.add_widget(prev_btn)
        nav_box.add_widget(self.play_btn)
        nav_box.add_widget(next_btn)
        bottom_panel.add_widget(nav_box)

        main_layout.add_widget(bottom_panel)
        return main_layout

    def _request_android_permissions(self):
        if platform != 'android':
            return
        try:
            from android.permissions import request_permissions, Permission
            from jnius import autoclass

            version = autoclass('android.os.Build$VERSION')
            sdk_int = int(version.SDK_INT)

            perms = [Permission.INTERNET]
            if sdk_int >= 33:
                if hasattr(Permission, 'READ_MEDIA_AUDIO'):
                    perms.append(Permission.READ_MEDIA_AUDIO)
            else:
                perms.append(Permission.READ_EXTERNAL_STORAGE)
                if sdk_int <= 29:
                    perms.append(Permission.WRITE_EXTERNAL_STORAGE)

            request_permissions(perms)
        except Exception:
            pass

    def _update_rect(self, instance, value):
        self.rect.pos = instance.pos
        self.rect.size = instance.size

    def _update_card_bg(self, instance, value):
        self.card_bg.pos = instance.pos
        self.card_bg.size = instance.size

    # --- ВИЗУАЛИЗАТОР ---
    def _start_visualizer(self):
        if self.vis_event is None:
            self.vis_event = Clock.schedule_interval(self._animate_spectrum, 0.05)

    def _stop_visualizer(self):
        if self.vis_event is not None:
            self.vis_event.cancel()
            self.vis_event = None
        self.spectrum.step(False, 0.0)

    def _animate_spectrum(self, dt):
        is_playing = bool(self.player and not self.is_paused and self.player.is_playing())
        if not is_playing:
            self.spectrum.step(False, 0.0)
            if self.is_paused or not self.player:
                self._stop_visualizer()
            return
        ratio = 0.0
        if self.track_duration > 0:
            ratio = min(1.0, max(0.0, self.seek_slider.value / self.track_duration))
        self.spectrum.step(True, ratio)

    # --- ПЕРЕМОТКА И ПРОГРЕСС ---
    def _start_progress_timer(self):
        if self.progress_event is None:
            self.progress_event = Clock.schedule_interval(self._update_progress, 0.25)

    def _stop_progress_timer(self):
        if self.progress_event is not None:
            self.progress_event.cancel()
            self.progress_event = None

    def _reset_seek_bar(self):
        self._stop_progress_timer()
        self.track_duration = 0.0
        self.seek_slider._dragging = False
        self.seek_slider.max = 100
        self.seek_slider.value = 0
        self.time_current_label.text = "00:00"
        self.time_total_label.text = "00:00"

    def _update_progress(self, dt):
        if not self.player:
            return
        if self.seek_slider._dragging:
            return
        if Clock.get_time() < self._ignore_pos_until:
            return

        if self.player.duration and self.player.duration > 0:
            self.track_duration = float(self.player.duration)

        if self.track_duration > 0:
            self.seek_slider.max = max(1.0, self.track_duration)
            self.time_total_label.text = format_time(self.track_duration)

        if not self.is_paused:
            if not self.player.is_playing():
                self._on_track_finished()
                return
            pos = self.player.get_pos()
            if self.track_duration > 0:
                pos = min(pos, self.track_duration)
            elif pos > self.seek_slider.max:
                self.seek_slider.max = pos + 10
            self.seek_slider.value = pos
            self.time_current_label.text = format_time(pos)

    def _on_seek_preview(self, value):
        self.time_current_label.text = format_time(value)

    def _on_seek_commit(self, value):
        target = float(value)
        self.time_current_label.text = format_time(target)
        self._ignore_pos_until = Clock.get_time() + 0.45
        if self.player:
            self.player.seek(target)

    def set_status(self, text):
        Clock.schedule_once(lambda dt: setattr(self.status_label, 'text', text))

    # --- МНОГОПЛЕЙЛИСТНАЯ СИСТЕМА ---
    def load_playlists(self):
        default_name = "Мой плейлист"
        if not os.path.exists(self.playlist_file):
            return {default_name: []}, default_name
        try:
            with open(self.playlist_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, list):
                return {default_name: data}, default_name
            if isinstance(data, dict) and isinstance(data.get("playlists"), dict):
                pls = data["playlists"] or {default_name: []}
                active = data.get("active")
                if active not in pls:
                    active = next(iter(pls.keys()))
                return pls, active
        except Exception:
            pass
        return {default_name: []}, default_name

    def save_playlists(self):
        try:
            os.makedirs(self.user_data_dir, exist_ok=True)
            payload = {
                "active": self.active_playlist_name,
                "playlists": self.playlists_db,
            }
            with open(self.playlist_file, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.set_status(f"Ошибка сохранения: {str(e)[:25]}")

    def _add_track_to_named_playlist(self, pl_name, track):
        if pl_name not in self.playlists_db:
            self.playlists_db[pl_name] = []
        items = self.playlists_db[pl_name]
        if any(t.get('url') == track.get('url') for t in items):
            return False
        items.append({
            "title": track["title"],
            "url": track["url"],
            "local": bool(track.get("local")),
        })
        self.active_playlist_name = pl_name
        self.save_playlists()
        return True

    def open_add_to_playlist_popup(self, instance):
        if not self.track_list or not (0 <= self.current_track_index < len(self.track_list)):
            self.set_status("Сначала выберите трек")
            return
        track = self.track_list[self.current_track_index]

        content = BoxLayout(orientation='vertical', spacing=dp(8), padding=dp(8))
        content.add_widget(Label(
            text=f"Трек: {track['title'][:36]}",
            size_hint_y=None,
            height=dp(28),
            font_size=sp(13),
            color=(1, 0.7, 0.2, 1),
        ))

        scroll = ScrollView(size_hint_y=1)
        pl_grid = GridLayout(cols=1, spacing=dp(6), size_hint_y=None)
        pl_grid.bind(minimum_height=pl_grid.setter('height'))

        popup = Popup(title="Добавить в плейлист", content=content, size_hint=(0.92, 0.72))

        for name in list(self.playlists_db.keys()):
            cnt = len(self.playlists_db[name])
            btn = Button(
                text=f"{name} ({cnt})",
                size_hint_y=None,
                height=dp(46),
                font_size=sp(14),
                bold=(name == self.active_playlist_name),
                background_normal='',
                background_color=(1, 0.5, 0, 1) if name == self.active_playlist_name else (0.18, 0.18, 0.2, 1),
                color=(0, 0, 0, 1) if name == self.active_playlist_name else (1, 1, 1, 1),
            )

            def on_pick(inst, pl=name):
                added = self._add_track_to_named_playlist(pl, track)
                popup.dismiss()
                if added:
                    self.set_status(f"Добавлено в «{pl}»")
                else:
                    self.set_status(f"Уже есть в «{pl}»")

            btn.bind(on_press=on_pick)
            pl_grid.add_widget(btn)

        scroll.add_widget(pl_grid)
        content.add_widget(scroll)

        new_row = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(44), spacing=dp(6))
        new_input = TextInput(
            hint_text='Имя нового плейлиста...',
            multiline=False,
            font_size=sp(13.5),
            padding=[dp(8), dp(10)],
        )
        create_btn = Button(
            text='+ Создать',
            size_hint_x=0.36,
            font_size=sp(13),
            bold=True,
            background_normal='',
            background_color=(0.22, 0.65, 0.28, 1),
            color=(1, 1, 1, 1),
        )

        def on_create(inst):
            new_name = new_input.text.strip()
            if not new_name:
                return
            self._add_track_to_named_playlist(new_name, track)
            popup.dismiss()
            self.set_status(f"Создан «{new_name}» + трек")

        create_btn.bind(on_press=on_create)
        new_row.add_widget(new_input)
        new_row.add_widget(create_btn)
        content.add_widget(new_row)

        cancel_btn = Button(
            text="Закрыть",
            size_hint_y=None,
            height=dp(42),
            font_size=sp(14),
            background_normal='',
            background_color=(0.25, 0.25, 0.25, 1),
            color=(1, 1, 1, 1),
        )
        cancel_btn.bind(on_press=popup.dismiss)
        content.add_widget(cancel_btn)
        popup.open()

    def show_playlists_manager(self, instance):
        content = BoxLayout(orientation='vertical', spacing=dp(7), padding=dp(8))

        top_row = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(42), spacing=dp(5))
        switch_btn = Button(
            text=f"{self.active_playlist_name} ▾",
            size_hint_x=0.56,
            font_size=sp(13),
            bold=True,
            background_normal='',
            background_color=(1, 0.5, 0, 1),
            color=(0, 0, 0, 1),
        )
        new_pl_btn = Button(
            text="+ Новый",
            size_hint_x=0.26,
            font_size=sp(12.5),
            bold=True,
            background_normal='',
            background_color=(0.22, 0.62, 0.28, 1),
            color=(1, 1, 1, 1),
        )
        del_pl_btn = Button(
            text="Удал.",
            size_hint_x=0.18,
            font_size=sp(12),
            bold=True,
            background_normal='',
            background_color=(0.72, 0.16, 0.16, 1),
            color=(1, 1, 1, 1),
        )
        top_row.add_widget(switch_btn)
        top_row.add_widget(new_pl_btn)
        top_row.add_widget(del_pl_btn)
        content.add_widget(top_row)

        tools_row = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(40), spacing=dp(5))
        phone_btn = Button(
            text="+ С телефона",
            font_size=sp(12.5),
            bold=True,
            background_normal='',
            background_color=(0.2, 0.45, 0.78, 1),
            color=(1, 1, 1, 1),
        )
        save_search_btn = Button(
            text="Сохр. поиск",
            font_size=sp(12.5),
            bold=True,
            background_normal='',
            background_color=(0.3, 0.3, 0.35, 1),
            color=(1, 1, 1, 1),
        )
        play_all_btn = Button(
            text="Играть всё",
            font_size=sp(12.5),
            bold=True,
            background_normal='',
            background_color=(0.75, 0.3, 0, 1),
            color=(1, 1, 1, 1),
        )
        tools_row.add_widget(phone_btn)
        tools_row.add_widget(save_search_btn)
        tools_row.add_widget(play_all_btn)
        content.add_widget(tools_row)

        scroll = ScrollView(size_hint_y=1)
        grid = GridLayout(cols=1, spacing=dp(5), size_hint_y=None)
        grid.bind(minimum_height=grid.setter('height'))
        scroll.add_widget(grid)
        content.add_widget(scroll)

        popup = Popup(
            title=f"Плейлист: {self.active_playlist_name}",
            content=content,
            size_hint=(0.95, 0.86),
        )

        def rebuild_items():
            grid.clear_widgets()
            items = self.playlists_db.get(self.active_playlist_name, [])
            switch_btn.text = f"{self.active_playlist_name} ▾"
            popup.title = f"Плейлист: {self.active_playlist_name} ({len(items)})"

            if not items:
                grid.add_widget(Label(
                    text="Плейлист пуст.\nНажмите «+ С телефона» или добавьте из поиска.",
                    size_hint_y=None,
                    height=dp(72),
                    font_size=sp(13.5),
                    halign='center',
                ))
                return

            for idx, track in enumerate(items):
                row = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(48), spacing=dp(4))
                prefix = "[тел] " if track.get("local") else ""
                play_item_btn = Button(
                    text=f"{prefix}{track['title']}",
                    font_size=sp(13),
                    halign='left',
                    valign='middle',
                    shorten=True,
                    shorten_from='right',
                    padding=[dp(10), 0],
                    background_normal='',
                    background_color=(0.16, 0.16, 0.18, 1),
                    color=(1, 1, 1, 1),
                )
                play_item_btn.bind(size=lambda inst, val: setattr(inst, 'text_size', val))

                del_btn = Button(
                    text='X',
                    size_hint_x=None,
                    width=dp(44),
                    font_size=sp(14),
                    bold=True,
                    background_normal='',
                    background_color=(0.75, 0.15, 0.15, 1),
                    color=(1, 1, 1, 1),
                )

                def play_from_pl(inst, i=idx):
                    self.track_list = list(self.playlists_db.get(self.active_playlist_name, []))
                    self.populate_track_list(self.track_list)
                    popup.dismiss()
                    self.select_and_play(i)

                def remove_from_pl(inst, i=idx):
                    cur = self.playlists_db.get(self.active_playlist_name, [])
                    if 0 <= i < len(cur):
                        cur.pop(i)
                        self.save_playlists()
                        rebuild_items()

                play_item_btn.bind(on_press=play_from_pl)
                del_btn.bind(on_press=remove_from_pl)
                row.add_widget(play_item_btn)
                row.add_widget(del_btn)
                grid.add_widget(row)

        def open_switch_popup(inst):
            sw_box = BoxLayout(orientation='vertical', spacing=dp(6), padding=dp(8))
            sw_scroll = ScrollView(size_hint_y=1)
            sw_grid = GridLayout(cols=1, spacing=dp(5), size_hint_y=None)
            sw_grid.bind(minimum_height=sw_grid.setter('height'))
            sw_popup = Popup(title="Выберите плейлист", content=sw_box, size_hint=(0.86, 0.65))
            for name in list(self.playlists_db.keys()):
                b = Button(
                    text=f"{name} ({len(self.playlists_db[name])})",
                    size_hint_y=None,
                    height=dp(46),
                    font_size=sp(14),
                    background_normal='',
                    background_color=(1, 0.5, 0, 1) if name == self.active_playlist_name else (0.2, 0.2, 0.22, 1),
                    color=(0, 0, 0, 1) if name == self.active_playlist_name else (1, 1, 1, 1),
                )

                def choose_pl(x, n=name):
                    self.active_playlist_name = n
                    self.save_playlists()
                    sw_popup.dismiss()
                    rebuild_items()

                b.bind(on_press=choose_pl)
                sw_grid.add_widget(b)
            sw_scroll.add_widget(sw_grid)
            sw_box.add_widget(sw_scroll)
            sw_popup.open()

        def open_new_pl_popup(inst):
            np_box = BoxLayout(orientation='vertical', spacing=dp(8), padding=dp(10))
            np_input = TextInput(
                hint_text="Название нового плейлиста",
                multiline=False,
                size_hint_y=None,
                height=dp(44),
                font_size=sp(14),
            )
            np_popup = Popup(title="Новый плейлист", content=np_box, size_hint=(0.86, 0.34))
            btn_ok = Button(
                text="Создать и сохранить",
                size_hint_y=None,
                height=dp(44),
                background_normal='',
                background_color=(1, 0.5, 0, 1),
                color=(0, 0, 0, 1),
            )

            def do_create(x):
                name = np_input.text.strip()
                if name:
                    if name not in self.playlists_db:
                        self.playlists_db[name] = []
                    self.active_playlist_name = name
                    self.save_playlists()
                    np_popup.dismiss()
                    rebuild_items()

            btn_ok.bind(on_press=do_create)
            np_box.add_widget(np_input)
            np_box.add_widget(btn_ok)
            np_popup.open()

        def delete_current_pl(inst):
            if len(self.playlists_db) <= 1:
                self.playlists_db[self.active_playlist_name] = []
            else:
                self.playlists_db.pop(self.active_playlist_name, None)
                self.active_playlist_name = next(iter(self.playlists_db.keys()))
            self.save_playlists()
            rebuild_items()

        def save_current_search_into_pl(inst):
            if not self.track_list:
                self.set_status("Текущий список пуст")
                return
            added_cnt = 0
            for t in self.track_list:
                if self._add_track_to_named_playlist(self.active_playlist_name, t):
                    added_cnt += 1
            rebuild_items()
            self.set_status(f"Сохранено треков: {added_cnt}")

        def play_all_pl(inst):
            items = self.playlists_db.get(self.active_playlist_name, [])
            if not items:
                return
            self.track_list = list(items)
            self.populate_track_list(self.track_list)
            popup.dismiss()
            self.select_and_play(0)

        switch_btn.bind(on_press=open_switch_popup)
        new_pl_btn.bind(on_press=open_new_pl_popup)
        del_pl_btn.bind(on_press=delete_current_pl)
        phone_btn.bind(on_press=lambda x: self.open_phone_music_popup(on_added_callback=rebuild_items))
        save_search_btn.bind(on_press=save_current_search_into_pl)
        play_all_btn.bind(on_press=play_all_pl)

        rebuild_items()

        close_btn = Button(
            text="Закрыть",
            size_hint_y=None,
            height=dp(44),
            font_size=sp(14.5),
            background_normal='',
            background_color=(1, 0.5, 0, 1),
            color=(0, 0, 0, 1),
        )
        close_btn.bind(on_press=popup.dismiss)
        content.add_widget(close_btn)
        popup.open()

    # --- ПОИСК МУЗЫКИ НА ТЕЛЕФОНЕ ---
    def _scan_phone_audio_files(self):
        found = []
        seen_paths = set()

        def add_file(path, title_hint="", artist_hint=""):
            if not path:
                return
            low = path.lower()
            if not low.endswith(AUDIO_EXTENSIONS):
                return
            if not os.path.exists(path):
                return
            real = os.path.realpath(path)
            if real in seen_paths:
                return
            seen_paths.add(real)
            fname = os.path.splitext(os.path.basename(path))[0]
            if artist_hint and artist_hint != "<unknown>" and title_hint:
                disp = f"{artist_hint} - {title_hint}"
            elif title_hint:
                disp = title_hint
            else:
                disp = fname
            found.append({"title": disp, "url": real, "local": True})

        if platform == 'android':
            try:
                from jnius import autoclass
                PythonActivity = autoclass('org.kivy.android.PythonActivity')
                Media = autoclass('android.provider.MediaStore$Audio$Media')
                activity = PythonActivity.mActivity
                resolver = activity.getContentResolver()
                projection = ["_data", "title", "artist"]
                cursor = resolver.query(Media.EXTERNAL_CONTENT_URI, projection, None, None, "title ASC")
                if cursor is not None:
                    try:
                        idx_data = cursor.getColumnIndexOrThrow("_data")
                        idx_title = cursor.getColumnIndexOrThrow("title")
                        idx_artist = cursor.getColumnIndexOrThrow("artist")
                        while cursor.moveToNext():
                            p = cursor.getString(idx_data)
                            t = cursor.getString(idx_title) or ""
                            a = cursor.getString(idx_artist) or ""
                            add_file(p, t, a)
                    finally:
                        cursor.close()
            except Exception:
                pass

            for folder in (
                "/storage/emulated/0/Music",
                "/storage/emulated/0/Download",
                "/storage/emulated/0/Downloads",
                "/storage/emulated/0/Audio",
            ):
                if os.path.isdir(folder):
                    try:
                        for root, _, files in os.walk(folder):
                            for fn in files:
                                add_file(os.path.join(root, fn))
                    except Exception:
                        pass
        else:
            for folder in (
                os.path.expanduser("~/Music"),
                os.path.expanduser("~/Downloads"),
            ):
                if os.path.isdir(folder):
                    for root, _, files in os.walk(folder):
                        for fn in files:
                            add_file(os.path.join(root, fn))

        return found

    def open_phone_music_popup(self, on_added_callback=None):
        self._request_android_permissions()
        content = BoxLayout(orientation='vertical', spacing=dp(7), padding=dp(8))

        filter_input = TextInput(
            hint_text="Фильтр песен на телефоне...",
            multiline=False,
            size_hint_y=None,
            height=dp(42),
            font_size=sp(14),
        )
        content.add_widget(filter_input)

        status_lbl = Label(
            text="Сканирование памяти телефона...",
            size_hint_y=None,
            height=dp(24),
            font_size=sp(12.5),
            color=(1, 0.7, 0.2, 1),
        )
        content.add_widget(status_lbl)

        scroll = ScrollView(size_hint_y=1)
        grid = GridLayout(cols=1, spacing=dp(5), size_hint_y=None)
        grid.bind(minimum_height=grid.setter('height'))
        scroll.add_widget(grid)
        content.add_widget(scroll)

        btn_row = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(44), spacing=dp(6))
        browse_btn = Button(
            text="Обзор папок",
            font_size=sp(13),
            background_normal='',
            background_color=(0.25, 0.45, 0.75, 1),
            color=(1, 1, 1, 1),
        )
        done_btn = Button(
            text="Готово",
            font_size=sp(13.5),
            bold=True,
            background_normal='',
            background_color=(1, 0.5, 0, 1),
            color=(0, 0, 0, 1),
        )
        btn_row.add_widget(browse_btn)
        btn_row.add_widget(done_btn)
        content.add_widget(btn_row)

        popup = Popup(
            title=f"Песни с телефона -> {self.active_playlist_name}",
            content=content,
            size_hint=(0.95, 0.88),
        )

        scanned_tracks = []

        def render_list(*args):
            grid.clear_widgets()
            q = filter_input.text.strip().lower()
            shown = [t for t in scanned_tracks if not q or q in t['title'].lower()]
            if not shown:
                grid.add_widget(Label(
                    text="Треки не найдены.\nПопробуйте «Обзор папок».",
                    size_hint_y=None,
                    height=dp(64),
                    font_size=sp(13),
                    halign='center',
                ))
                return
            for t in shown[:200]:
                b = Button(
                    text=t['title'],
                    size_hint_y=None,
                    height=dp(46),
                    font_size=sp(13),
                    halign='left',
                    valign='middle',
                    shorten=True,
                    shorten_from='right',
                    padding=[dp(10), 0],
                    background_normal='',
                    background_color=(0.16, 0.16, 0.18, 1),
                    color=(1, 1, 1, 1),
                )
                b.bind(size=lambda inst, val: setattr(inst, 'text_size', val))

                def add_local(btn_inst, item=t):
                    self._add_track_to_named_playlist(self.active_playlist_name, item)
                    btn_inst.background_color = (0.16, 0.45, 0.22, 1)
                    btn_inst.text = f"✓ {item['title']}"
                    if on_added_callback:
                        on_added_callback()

                b.bind(on_press=add_local)
                grid.add_widget(b)

        filter_input.bind(text=render_list)

        def open_manual_browser(inst):
            fc_box = BoxLayout(orientation='vertical', spacing=dp(8), padding=dp(8))
            start_dir = "/storage/emulated/0" if os.path.isdir("/storage/emulated/0") else os.path.expanduser("~")
            fc = FileChooserListView(
                path=start_dir,
                filters=[f"*{ext}" for ext in AUDIO_EXTENSIONS],
                multiselect=True,
            )
            fc_box.add_widget(fc)
            fc_btns = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(44), spacing=dp(6))
            add_sel_btn = Button(
                text="Добавить выбранные",
                background_normal='',
                background_color=(1, 0.5, 0, 1),
                color=(0, 0, 0, 1),
            )
            cancel_fc_btn = Button(
                text="Назад",
                background_normal='',
                background_color=(0.25, 0.25, 0.25, 1),
                color=(1, 1, 1, 1),
            )
            fc_btns.add_widget(add_sel_btn)
            fc_btns.add_widget(cancel_fc_btn)
            fc_box.add_widget(fc_btns)
            fc_pop = Popup(title="Выберите аудиофайлы", content=fc_box, size_hint=(0.95, 0.88))

            def add_chosen(x):
                cnt = 0
                for fp in fc.selection:
                    if os.path.isfile(fp):
                        info = extract_id3_info(fp)
                        title = (
                            f"{info['artist']} - {info['title']}"
                            if info['artist'] and info['title']
                            else (info['title'] or os.path.splitext(os.path.basename(fp))[0])
                        )
                        if self._add_track_to_named_playlist(
                            self.active_playlist_name,
                            {"title": title, "url": fp, "local": True},
                        ):
                            cnt += 1
                fc_pop.dismiss()
                if on_added_callback:
                    on_added_callback()
                status_lbl.text = f"Добавлено из папок: {cnt}"

            add_sel_btn.bind(on_press=add_chosen)
            cancel_fc_btn.bind(on_press=fc_pop.dismiss)
            fc_pop.open()

        browse_btn.bind(on_press=open_manual_browser)
        done_btn.bind(on_press=popup.dismiss)

        def bg_scan():
            res = self._scan_phone_audio_files()

            def apply_scan(dt):
                scanned_tracks.clear()
                scanned_tracks.extend(res)
                status_lbl.text = f"Найдено песен на телефоне: {len(scanned_tracks)} (нажмите для добавления)"
                render_list()

            Clock.schedule_once(apply_scan)

        threading.Thread(target=bg_scan, daemon=True).start()
        popup.open()

    # --- УМНОЕ ИЗВЛЕЧЕНИЕ ИМЕНИ ГРУППЫ И ПЕСНИ ---
    def _clean_text(self, text):
        if not text:
            return ""
        text = re.sub(r'\s+', ' ', text).strip()
        text = re.sub(r'^(Скачать|Download|Слушать|Play)\s+', '', text, flags=re.I)
        text = re.sub(r'\.mp3$', '', text, flags=re.I)
        return text.strip(" -–—|")

    def _extract_track_title(self, a_tag, href, fallback_num):
        for attr in ('data-title', 'data-name', 'title', 'download', 'aria-label'):
            val = self._clean_text(a_tag.get(attr, ''))
            if val and not re.fullmatch(r'(скачать|download|mp3|трек)', val, re.I):
                return val

        own_text = self._clean_text(a_tag.get_text(" ", strip=True))
        if own_text and not re.fullmatch(r'(скачать|download|mp3|\d+\s*(kbps|кб/с|mb|мб))', own_text, re.I):
            return own_text

        row = None
        parent = a_tag.parent
        for _ in range(5):
            if not parent or parent.name in ('body', 'html', '[document]'):
                break
            dl_links = [
                x for x in parent.find_all('a', href=True)
                if 'download' in x['href'] or '.mp3' in x['href']
            ]
            if len(dl_links) == 1 and parent.name in ('li', 'div', 'tr', 'article', 'section'):
                row = parent
            elif len(dl_links) > 1:
                break
            parent = parent.parent

        if row is not None:
            d_artist = self._clean_text(row.get('data-artist') or row.get('data-author') or '')
            d_title = self._clean_text(row.get('data-title') or row.get('data-song') or row.get('data-name') or '')
            if d_artist and d_title:
                return f"{d_artist} - {d_title}"
            if d_title:
                return d_title

            artist_el = row.find(class_=re.compile(r'(artist|author|singer|group|band|performer|exec)', re.I))
            title_el = row.find(class_=re.compile(r'(title|song|track[-_]?name|name)', re.I))
            a_txt = self._clean_text(artist_el.get_text(" ", strip=True)) if artist_el else ""
            t_txt = self._clean_text(title_el.get_text(" ", strip=True)) if title_el else ""
            if a_txt and t_txt and a_txt.lower() != t_txt.lower():
                if a_txt.lower() in t_txt.lower():
                    return t_txt
                return f"{a_txt} - {t_txt}"
            if t_txt:
                return t_txt

            parts = []
            for s in row.stripped_strings:
                cleaned = self._clean_text(s)
                if not cleaned or len(cleaned) < 2:
                    continue
                if re.fullmatch(r'[\d:.\s]+', cleaned):
                    continue
                if re.fullmatch(r'[\d.,]+\s*(mb|мб|kb|кб|kbps|кб/с)', cleaned, re.I):
                    continue
                if re.fullmatch(r'(скачать|download|слушать|play|mp3|►|▶|⬇)', cleaned, re.I):
                    continue
                if cleaned not in parts:
                    parts.append(cleaned)
                if len(parts) >= 2:
                    break
            if len(parts) >= 2:
                if parts[0].lower() in parts[1].lower():
                    return parts[1]
                return f"{parts[0]} - {parts[1]}"
            if len(parts) == 1:
                return parts[0]

        try:
            decoded_path = urllib.parse.unquote(urllib.parse.urlparse(href).path)
            slug = os.path.basename(decoded_path)
            slug = re.sub(r'\.mp3$', '', slug, flags=re.I)
            slug = slug.replace('_', ' ').strip()
            if slug and not slug.isdigit() and not re.fullmatch(r'download', slug, re.I):
                return slug
        except Exception:
            pass

        return f"Трек {fallback_num}"

    # --- ПОИСК ---
    def start_search(self, instance):
        query = self.search_input.text.strip()
        if not query:
            self.set_status("Введите запрос для поиска")
            return
        self.search_request_id += 1
        req_id = self.search_request_id
        self.set_status("Поиск треков...")
        self.results_layout.clear_widgets()
        threading.Thread(target=self.search_music, args=(query, req_id), daemon=True).start()

    def search_music(self, query, req_id):
        encoded_query = urllib.parse.quote_plus(query)
        url = f"https://new.zmus.org/search?q={encoded_query}"

        try:
            res = requests.get(url, headers=self.headers, timeout=12, verify=SSL_VERIFY)
            res.raise_for_status()
            soup = BeautifulSoup(res.text, 'html.parser')
            found_tracks = []
            seen_urls = set()

            for a_tag in soup.find_all('a', href=True):
                href = a_tag['href'].strip()
                if "download" in href or ".mp3" in href:
                    if href.startswith("/"):
                        href = "https://new.zmus.org" + href
                    if not href.startswith("http") or href in seen_urls:
                        continue
                    seen_urls.add(href)
                    title = self._extract_track_title(a_tag, href, len(found_tracks) + 1)
                    found_tracks.append({"title": title, "url": href, "local": False})

            def apply_results(dt):
                if req_id != self.search_request_id:
                    return
                self.track_list = found_tracks
                self.current_track_index = 0
                self.populate_track_list(self.track_list)

            Clock.schedule_once(apply_results)
        except Exception as e:
            if req_id == self.search_request_id:
                self.set_status(f"Ошибка сети: {str(e)[:32]}")

    def populate_track_list(self, tracks):
        self.results_layout.clear_widgets()
        if tracks:
            self.status_label.text = f"Треков в списке: {len(tracks)}"
            for idx, track in enumerate(tracks):
                is_active = (idx == self.current_track_index and self.player is not None)
                btn = Button(
                    text=track['title'],
                    size_hint_y=None,
                    height=dp(50),
                    font_size=sp(13.5),
                    halign='left',
                    valign='middle',
                    shorten=True,
                    shorten_from='right',
                    padding=[dp(12), 0],
                    background_normal='',
                    background_color=(0.24, 0.13, 0.03, 1) if is_active else (0.12, 0.12, 0.14, 1),
                    color=(1, 0.68, 0.18, 1) if is_active else (1, 1, 1, 1),
                )
                btn.bind(size=lambda inst, val: setattr(inst, 'text_size', val))
                btn.bind(on_press=lambda inst, i=idx: self.select_and_play(i))
                self.results_layout.add_widget(btn)
        else:
            self.status_label.text = "Ничего не найдено."

    def _highlight_active_track(self):
        children = list(reversed(self.results_layout.children))
        for idx, btn in enumerate(children):
            if idx == self.current_track_index:
                btn.background_color = (0.24, 0.13, 0.03, 1)
                btn.color = (1, 0.68, 0.18, 1)
            else:
                btn.background_color = (0.12, 0.12, 0.14, 1)
                btn.color = (1, 1, 1, 1)

    # --- ВОСПРОИЗВЕДЕНИЕ ---
    def select_and_play(self, index):
        self.current_track_index = index
        self.play_current()

    def _unload_current_sound(self):
        self._stop_visualizer()
        self._reset_seek_bar()
        if self.player:
            self._manual_stop = True
            self.player.release()
            self.player = None
        self.is_paused = False

    def play_current(self):
        self._unload_current_sound()
        if not self.track_list or not (0 <= self.current_track_index < len(self.track_list)):
            self.set_status("Список треков пуст")
            return

        self.play_request_id += 1
        req_id = self.play_request_id
        track = self.track_list[self.current_track_index]
        self._highlight_active_track()
        self.cover_widget.set_cover(None)

        self.set_status(f"Загрузка: {track['title'][:32]}...")
        self.play_btn.text = "WAIT"
        threading.Thread(
            target=self._prepare_and_play,
            args=(track, self.current_track_index, req_id),
            daemon=True,
        ).start()

    def _cleanup_old_temp_files(self, req_id):
        try:
            for name in os.listdir(self.user_data_dir):
                if (name.startswith("temp_track_") and name != f"temp_track_{req_id}.mp3") or (
                    name.startswith("cover_") and name != f"cover_{req_id}.jpg"
                ):
                    try:
                        os.remove(os.path.join(self.user_data_dir, name))
                    except OSError:
                        pass
        except Exception:
            pass

    def _prepare_and_play(self, track, track_idx, req_id):
        try:
            os.makedirs(self.user_data_dir, exist_ok=True)
            is_local = bool(track.get("local") or (track.get("url") and os.path.exists(track["url"])))

            if is_local:
                audio_path = track["url"]
                if not os.path.exists(audio_path):
                    raise FileNotFoundError("Файл не найден на телефоне")
            else:
                audio_path = os.path.join(self.user_data_dir, f"temp_track_{req_id}.mp3")
                res = requests.get(
                    track['url'],
                    headers=self.headers,
                    stream=True,
                    timeout=15,
                    verify=SSL_VERIFY,
                )
                res.raise_for_status()

                content_type = res.headers.get("Content-Type", "").lower()
                if "text/html" in content_type:
                    raise ValueError("Сайт вернул страницу вместо аудио")

                with open(audio_path, 'wb') as f:
                    for chunk in res.iter_content(chunk_size=8192):
                        if req_id != self.play_request_id:
                            return
                        if chunk:
                            f.write(chunk)

            if req_id != self.play_request_id:
                return

            self._cleanup_old_temp_files(req_id)

            cover_path = os.path.join(self.user_data_dir, f"cover_{req_id}.jpg")
            id3 = extract_id3_info(audio_path, cover_save_path=cover_path)

            resolved_title = track['title']
            if re.fullmatch(r'Трек\s+\d+', resolved_title):
                if id3['artist'] and id3['title']:
                    resolved_title = f"{id3['artist']} - {id3['title']}"
                elif id3['title']:
                    resolved_title = id3['title']

            est_duration = estimate_mp3_duration(audio_path)
            envelope = compute_audio_envelope(audio_path)

            Clock.schedule_once(
                lambda dt: self._start_sound(
                    audio_path,
                    resolved_title,
                    track_idx,
                    est_duration,
                    envelope,
                    id3.get('cover_path'),
                    req_id,
                )
            )

            if not id3.get('cover_path'):
                online_cover = fetch_online_cover(resolved_title, cover_path)
                if online_cover and req_id == self.play_request_id:
                    Clock.schedule_once(
                        lambda dt: self.cover_widget.set_cover(online_cover)
                        if req_id == self.play_request_id else None
                    )
        except Exception as e:
            if req_id == self.play_request_id:
                self.set_status(f"Ошибка аудио: {str(e)[:30]}")
                Clock.schedule_once(lambda dt: setattr(self.play_btn, 'text', 'PLAY'))

    def _start_sound(self, path, title, track_idx, est_duration, envelope, cover_path, req_id):
        if req_id != self.play_request_id:
            return
        if 0 <= track_idx < len(self.track_list) and self.track_list[track_idx]['title'] != title:
            self.track_list[track_idx]['title'] = title
            children = list(reversed(self.results_layout.children))
            if track_idx < len(children):
                children[track_idx].text = title

        self.cover_widget.set_cover(cover_path)
        self.spectrum.set_envelope(envelope)

        player = None
        if platform == 'android':
            try:
                player = AndroidAudioPlayer(path)
            except Exception:
                player = None
        if player is None:
            try:
                player = DesktopAudioPlayer(path)
            except Exception:
                player = None

        if player:
            self.player = player
            self._manual_stop = False
            self.is_paused = False
            self._ignore_pos_until = Clock.get_time() + 0.35
            self.player.play()

            dur = float(self.player.duration or 0.0)
            if dur <= 0 and est_duration > 0:
                dur = est_duration
            self.track_duration = dur
            if dur > 0:
                self.seek_slider.max = dur
                self.time_total_label.text = format_time(dur)

            self.play_btn.text = "PAUSE"
            self.status_label.text = title
            self._start_visualizer()
            self._start_progress_timer()
        else:
            self.play_btn.text = "PLAY"
            self.status_label.text = "Не удалось открыть аудиофайл"

    def _on_track_finished(self):
        if self._manual_stop or self.is_paused:
            return
        self._stop_visualizer()
        self._stop_progress_timer()
        self.play_btn.text = "PLAY"
        if self.track_list and self.current_track_index < len(self.track_list) - 1:
            self.current_track_index += 1
            self.play_current()

    def toggle_play(self, instance):
        if self.player:
            if not self.is_paused and self.player.is_playing():
                self.is_paused = True
                self.player.pause()
                self._stop_visualizer()
                self.play_btn.text = "PLAY"
            else:
                self.is_paused = False
                self._ignore_pos_until = Clock.get_time() + 0.35
                self.player.resume()
                self._start_visualizer()
                self._start_progress_timer()
                self.play_btn.text = "PAUSE"
        else:
            self.play_current()

    def prev_track(self, instance):
        if not self.track_list:
            return
        if self.current_track_index > 0:
            self.current_track_index -= 1
            self.play_current()
        else:
            self.set_status("Это первый трек в списке")

    def next_track(self, instance):
        if not self.track_list:
            return
        if self.current_track_index < len(self.track_list) - 1:
            self.current_track_index += 1
            self.play_current()
        else:
            self.set_status("Это последний трек в списке")

    # --- СКАЧИВАНИЕ ---
    def open_quality_popup(self, instance):
        if not self.track_list or not (0 <= self.current_track_index < len(self.track_list)):
            self.set_status("Сначала выберите трек")
            return
        track = self.track_list[self.current_track_index]
        if track.get("local") or os.path.exists(track.get("url", "")):
            self.set_status("Этот трек уже находится на телефоне")
            return

        content = BoxLayout(orientation='vertical', spacing=dp(8), padding=dp(10))
        content.add_widget(Label(
            text="Выберите битрейт (для имени файла):",
            size_hint_y=None,
            height=dp(36),
            font_size=sp(14),
        ))

        popup = Popup(title="Качество скачивания", content=content, size_hint=(0.85, 0.45))

        for q in ("128", "192", "320"):
            btn = Button(
                text=f"{q} kbps",
                size_hint_y=None,
                height=dp(48),
                font_size=sp(15),
                background_normal='',
                background_color=(1, 0.45, 0, 1),
                color=(0, 0, 0, 1),
            )

            def select_q(inst, quality=q):
                self.selected_quality = quality
                popup.dismiss()
                self.choose_download_target()

            btn.bind(on_press=select_q)
            content.add_widget(btn)

        cancel_btn = Button(
            text="Отмена",
            size_hint_y=None,
            height=dp(44),
            font_size=sp(14),
            background_normal='',
            background_color=(0.25, 0.25, 0.25, 1),
            color=(1, 1, 1, 1),
        )
        cancel_btn.bind(on_press=popup.dismiss)
        content.add_widget(cancel_btn)
        popup.open()

    def choose_download_target(self):
        if platform == 'android':
            self.start_download(self.user_data_dir)
        else:
            self.open_folder_popup()

    def open_folder_popup(self):
        content = BoxLayout(orientation='vertical', spacing=dp(8), padding=dp(8))
        start_dir = os.path.expanduser("~")
        filechooser = FileChooserListView(path=start_dir, dirselect=True)
        content.add_widget(filechooser)

        btn_row = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(48), spacing=dp(8))
        btn_save = Button(
            text="Сохранить сюда",
            font_size=sp(14),
            background_normal='',
            background_color=(1, 0.5, 0, 1),
            color=(0, 0, 0, 1),
        )
        btn_cancel = Button(
            text="Отмена",
            font_size=sp(14),
            background_normal='',
            background_color=(0.25, 0.25, 0.25, 1),
            color=(1, 1, 1, 1),
        )
        btn_row.add_widget(btn_save)
        btn_row.add_widget(btn_cancel)
        content.add_widget(btn_row)

        self.folder_popup = Popup(
            title="Выберите папку для сохранения",
            content=content,
            size_hint=(0.94, 0.85),
        )

        def on_save(inst):
            selected_dir = filechooser.path
            self.folder_popup.dismiss()
            self.start_download(selected_dir)

        btn_save.bind(on_press=on_save)
        btn_cancel.bind(on_press=self.folder_popup.dismiss)
        self.folder_popup.open()

    def start_download(self, folder_path):
        if not self.track_list or not (0 <= self.current_track_index < len(self.track_list)):
            return
        track = self.track_list[self.current_track_index]
        self.set_status(f"Скачивание ({self.selected_quality} kbps)...")
        threading.Thread(
            target=self._download_worker,
            args=(track, folder_path),
            daemon=True,
        ).start()

    def _download_worker(self, track, folder_path):
        try:
            safe_name = re.sub(r'[\\/*?:"<>|]', "", track['title'])[:50].strip() or "track"
            filename = f"{safe_name}_{self.selected_quality}kbps.mp3"

            res = requests.get(
                track['url'],
                headers=self.headers,
                stream=True,
                timeout=20,
                verify=SSL_VERIFY,
            )
            res.raise_for_status()

            content_type = res.headers.get("Content-Type", "").lower()
            if "text/html" in content_type:
                raise ValueError("Сайт вернул HTML вместо MP3")

            if platform == 'android':
                temp_file = os.path.join(self.user_data_dir, filename)
                os.makedirs(self.user_data_dir, exist_ok=True)
                with open(temp_file, 'wb') as f:
                    for chunk in res.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)

                try:
                    from androidstorage4kivy import SharedStorage
                    from jnius import autoclass

                    Environment = autoclass('android.os.Environment')
                    SharedStorage().copy_to_shared(
                        temp_file,
                        collection=Environment.DIRECTORY_MUSIC,
                        filepath=filename,
                    )
                    os.remove(temp_file)
                    self.set_status(f"Сохранено в Music: {filename[:22]}")
                    return
                except Exception:
                    pass

                for public_dir in (
                    "/storage/emulated/0/Music/KulikovSound",
                    "/storage/emulated/0/Download/KulikovSound",
                ):
                    try:
                        os.makedirs(public_dir, exist_ok=True)
                        dest = os.path.join(public_dir, filename)
                        with open(temp_file, 'rb') as src, open(dest, 'wb') as dst:
                            dst.write(src.read())
                        os.remove(temp_file)
                        short_dir = os.path.basename(os.path.dirname(public_dir))
                        self.set_status(f"Сохранено в {short_dir}: {filename[:18]}")
                        return
                    except Exception:
                        continue

                self.set_status(f"Сохранено во внутр. папку: {filename[:18]}")
                return

            os.makedirs(folder_path, exist_ok=True)
            full_path = os.path.join(folder_path, filename)
            with open(full_path, 'wb') as f:
                for chunk in res.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            self.set_status(f"Сохранено: {filename[:28]}")
        except Exception as e:
            self.set_status(f"Ошибка скачивания: {str(e)[:28]}")

    def on_stop(self):
        self._unload_current_sound()


if __name__ == '__main__':
    MusicApp().run()
