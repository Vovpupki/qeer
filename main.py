import os
import json
import re
import urllib.parse
import threading
from random import randint

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
from kivy.uix.popup import Popup
from kivy.uix.filechooser import FileChooserListView
from kivy.core.audio import SoundLoader
from kivy.clock import Clock
from kivy.graphics import Color, Rectangle
from kivy.metrics import dp, sp
from kivy.utils import platform


def format_time(seconds):
    try:
        secs = max(0, int(float(seconds)))
    except Exception:
        secs = 0
    mins, secs = divmod(secs, 60)
    return f"{mins:02d}:{secs:02d}"


def extract_id3_title(filepath):
    """Читает Название и Исполнителя из ID3v1 тега в конце MP3 (если есть)."""
    try:
        size = os.path.getsize(filepath)
        if size < 128:
            return None
        with open(filepath, 'rb') as f:
            f.seek(-128, os.SEEK_END)
            tag = f.read(128)
        if tag[:3] != b'TAG':
            return None

        def decode_field(raw):
            raw = raw.rstrip(b'\x00 ')
            for enc in ('utf-8', 'cp1251', 'latin1'):
                try:
                    val = raw.decode(enc).strip()
                    if val:
                        return val
                except Exception:
                    continue
            return ""

        song = decode_field(tag[3:33])
        artist = decode_field(tag[33:63])
        if artist and song:
            return f"{artist} - {song}"
        return song or artist or None
    except Exception:
        return None


def estimate_mp3_duration(filepath):
    """Резервная оценка длительности MP3 по первому фрейму, если SoundLoader вернул 0."""
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


class MusicApp(App):
    def build(self):
        self.title = "ZMus Player"
        self.track_list = []
        self.current_track_index = 0
        self.sound = None
        self.track_duration = 0.0
        self.paused_pos = 0.0
        self.is_paused = False
        self.is_seeking = False
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
        self.playlists = self.load_playlists()

        self._request_android_permissions()

        # Главный контейнер (Черный фон)
        main_layout = BoxLayout(orientation='vertical', padding=dp(10), spacing=dp(8))
        with main_layout.canvas.before:
            Color(0.05, 0.05, 0.05, 1)
            self.rect = Rectangle(size=main_layout.size, pos=main_layout.pos)
        main_layout.bind(size=self._update_rect, pos=self._update_rect)

        # --- СЕКЦИЯ ПОИСКА ---
        search_box = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(48), spacing=dp(6))
        self.search_input = TextInput(
            hint_text='Введите песню или исполнителя...',
            multiline=False,
            font_size=sp(15),
            padding=[dp(10), dp(12)],
            background_color=(0.15, 0.15, 0.15, 1),
            foreground_color=(1, 0.55, 0.1, 1),
            hint_text_color=(0.55, 0.35, 0.1, 1),
            cursor_color=(1, 0.55, 0.1, 1),
        )
        self.search_input.bind(on_text_validate=self.start_search)

        search_btn = Button(
            text='Поиск',
            size_hint_x=0.28,
            font_size=sp(15),
            bold=True,
            background_normal='',
            background_color=(1, 0.5, 0, 1),
            color=(0, 0, 0, 1),
        )
        search_btn.bind(on_press=self.start_search)
        search_box.add_widget(self.search_input)
        search_box.add_widget(search_btn)
        main_layout.add_widget(search_box)

        # --- СЕКЦИЯ ВИЗУАЛИЗАТОРА ---
        self.visualizer = BoxLayout(
            orientation='horizontal',
            size_hint_y=None,
            height=dp(76),
            spacing=dp(4),
            padding=[dp(12), dp(6)],
        )
        self.bars = []
        for _ in range(15):
            bar = Widget(size_hint_x=1)
            with bar.canvas:
                Color(1, 0.45, 0, 1)
                bar.rect = Rectangle(pos=bar.pos, size=(dp(8), dp(6)))
            bar.bind(pos=self._update_bar, size=self._update_bar)
            self.visualizer.add_widget(bar)
            self.bars.append(bar)
        main_layout.add_widget(self.visualizer)

        # Информационная строка
        self.status_label = Label(
            text='Добро пожаловать!',
            size_hint_y=None,
            height=dp(28),
            font_size=sp(14),
            color=(1, 0.65, 0.1, 1),
            halign='center',
            valign='middle',
            shorten=True,
            shorten_from='right',
        )
        self.status_label.bind(size=lambda inst, val: setattr(inst, 'text_size', val))
        main_layout.add_widget(self.status_label)

        # --- СПИСОК ПЕСЕН ---
        scroll_view = ScrollView(size_hint_y=1)
        self.results_layout = GridLayout(cols=1, spacing=dp(6), size_hint_y=None)
        self.results_layout.bind(minimum_height=self.results_layout.setter('height'))
        scroll_view.add_widget(self.results_layout)
        main_layout.add_widget(scroll_view)

        # --- ПОЛЗУНОК ПЕРЕМОТКИ ---
        seek_box = BoxLayout(
            orientation='horizontal',
            size_hint_y=None,
            height=dp(38),
            spacing=dp(6),
            padding=[dp(2), 0],
        )
        self.time_current_label = Label(
            text='00:00',
            size_hint_x=None,
            width=dp(44),
            font_size=sp(12),
            color=(1, 0.65, 0.1, 1),
        )
        self.seek_slider = Slider(
            min=0,
            max=100,
            value=0,
            step=1,
            cursor_size=(dp(20), dp(20)),
            value_track=True,
            value_track_color=(1, 0.5, 0, 1),
            value_track_width=dp(4),
        )
        self.seek_slider.bind(
            on_touch_down=self._on_seek_touch_down,
            on_touch_up=self._on_seek_touch_up,
            value=self._on_seek_value_change,
        )
        self.time_total_label = Label(
            text='00:00',
            size_hint_x=None,
            width=dp(44),
            font_size=sp(12),
            color=(0.7, 0.7, 0.7, 1),
        )
        seek_box.add_widget(self.time_current_label)
        seek_box.add_widget(self.seek_slider)
        seek_box.add_widget(self.time_total_label)
        main_layout.add_widget(seek_box)

        # --- ПАНЕЛЬ УПРАВЛЕНИЯ И ПЛЕЙЛИСТОВ ---
        bottom_panel = BoxLayout(orientation='vertical', size_hint_y=None, height=dp(112), spacing=dp(6))

        action_box = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(48), spacing=dp(6))
        download_btn = Button(
            text='Скачать',
            font_size=sp(13),
            bold=True,
            background_normal='',
            background_color=(1, 0.4, 0, 1),
            color=(0, 0, 0, 1),
        )
        download_btn.bind(on_press=self.open_quality_popup)

        add_playlist_btn = Button(
            text='+ Плейлист',
            font_size=sp(13),
            bold=True,
            background_normal='',
            background_color=(1, 0.4, 0, 1),
            color=(0, 0, 0, 1),
        )
        add_playlist_btn.bind(on_press=self.add_to_playlist)

        show_playlist_btn = Button(
            text='Плейлист',
            font_size=sp(13),
            bold=True,
            background_normal='',
            background_color=(0.75, 0.28, 0, 1),
            color=(1, 1, 1, 1),
        )
        show_playlist_btn.bind(on_press=self.show_playlist)

        action_box.add_widget(download_btn)
        action_box.add_widget(add_playlist_btn)
        action_box.add_widget(show_playlist_btn)
        bottom_panel.add_widget(action_box)

        nav_box = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(56), spacing=dp(6))
        prev_btn = Button(
            text='<<',
            font_size=sp(16),
            bold=True,
            background_normal='',
            background_color=(0.2, 0.2, 0.2, 1),
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
            background_color=(0.2, 0.2, 0.2, 1),
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

    def _update_bar(self, instance, value):
        if hasattr(instance, 'rect'):
            instance.rect.pos = instance.pos
            instance.rect.size = (instance.width, instance.rect.size[1])

    def _start_visualizer(self):
        if self.vis_event is None:
            self.vis_event = Clock.schedule_interval(self.animate_visualizer, 0.1)

    def _stop_visualizer(self):
        if self.vis_event is not None:
            self.vis_event.cancel()
            self.vis_event = None
        for bar in self.bars:
            bar.rect.size = (bar.width, dp(6))

    def animate_visualizer(self, dt):
        is_playing = bool(self.sound and self.sound.state == 'play')
        if not is_playing:
            self._stop_visualizer()
            return
        max_height = max(dp(16), self.visualizer.height - dp(12))
        for bar in self.bars:
            new_height = randint(int(dp(8)), int(max_height))
            bar.rect.size = (bar.width, new_height)

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
        self.seek_slider.max = 100
        self.seek_slider.value = 0
        self.time_current_label.text = "00:00"
        self.time_total_label.text = "00:00"

    def _update_progress(self, dt):
        if not self.sound or self.is_seeking:
            return
        if Clock.get_time() < self._ignore_pos_until:
            return

        if self.sound.length and self.sound.length > 0:
            self.track_duration = float(self.sound.length)

        if self.track_duration > 0:
            self.seek_slider.max = max(1.0, self.track_duration)
            self.time_total_label.text = format_time(self.track_duration)

        if self.sound.state == 'play':
            try:
                pos = float(self.sound.get_pos() or 0.0)
            except Exception:
                pos = 0.0
            if pos >= 0:
                if self.track_duration > 0:
                    pos = min(pos, self.track_duration)
                elif pos > self.seek_slider.max:
                    self.seek_slider.max = pos + 10
                self.seek_slider.value = pos
                self.time_current_label.text = format_time(pos)

    def _on_seek_touch_down(self, slider, touch):
        if slider.collide_point(*touch.pos) and self.sound:
            self.is_seeking = True
        return False

    def _on_seek_value_change(self, slider, value):
        if self.is_seeking:
            self.time_current_label.text = format_time(value)

    def _on_seek_touch_up(self, slider, touch):
        if self.is_seeking:
            self.is_seeking = False
            target = float(slider.value)
            self.time_current_label.text = format_time(target)
            self._ignore_pos_until = Clock.get_time() + 0.35
            if self.sound:
                if self.sound.state == 'play':
                    self._safe_seek(target)
                else:
                    self.paused_pos = target
        return False

    def set_status(self, text):
        Clock.schedule_once(lambda dt: setattr(self.status_label, 'text', text))

    # --- ПЛЕЙЛИСТЫ ---
    def load_playlists(self):
        if not os.path.exists(self.playlist_file):
            return []
        try:
            with open(self.playlist_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            return []

    def save_playlists(self):
        try:
            os.makedirs(self.user_data_dir, exist_ok=True)
            with open(self.playlist_file, 'w', encoding='utf-8') as f:
                json.dump(self.playlists, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.set_status(f"Ошибка сохранения: {str(e)[:28]}")

    def add_to_playlist(self, instance):
        if not self.track_list or not (0 <= self.current_track_index < len(self.track_list)):
            self.set_status("Сначала выберите трек")
            return
        track = self.track_list[self.current_track_index]
        if any(t.get('url') == track.get('url') for t in self.playlists):
            self.set_status("Трек уже в плейлисте")
            return
        self.playlists.append({"title": track["title"], "url": track["url"]})
        self.save_playlists()
        self.set_status(f"В плейлисте: {track['title'][:25]}")

    def show_playlist(self, instance):
        content = BoxLayout(orientation='vertical', spacing=dp(8), padding=dp(8))
        scroll = ScrollView(size_hint_y=1)
        grid = GridLayout(cols=1, spacing=dp(6), size_hint_y=None)
        grid.bind(minimum_height=grid.setter('height'))

        popup = Popup(
            title=f"Плейлист ({len(self.playlists)})",
            content=content,
            size_hint=(0.94, 0.8),
        )

        def rebuild_items():
            grid.clear_widgets()
            popup.title = f"Плейлист ({len(self.playlists)})"
            if not self.playlists:
                grid.add_widget(Label(
                    text="Плейлист пока пуст",
                    size_hint_y=None,
                    height=dp(48),
                    font_size=sp(15),
                ))
                return

            for idx, track in enumerate(self.playlists):
                row = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(50), spacing=dp(4))
                play_item_btn = Button(
                    text=track['title'][:40],
                    font_size=sp(13),
                    halign='left',
                    valign='middle',
                    shorten=True,
                    shorten_from='right',
                    padding=[dp(10), 0],
                    background_normal='',
                    background_color=(0.16, 0.16, 0.16, 1),
                    color=(1, 1, 1, 1),
                )
                play_item_btn.bind(size=lambda inst, val: setattr(inst, 'text_size', val))

                del_btn = Button(
                    text='X',
                    size_hint_x=None,
                    width=dp(46),
                    font_size=sp(14),
                    bold=True,
                    background_normal='',
                    background_color=(0.75, 0.15, 0.15, 1),
                    color=(1, 1, 1, 1),
                )

                def play_from_pl(inst, i=idx):
                    self.track_list = list(self.playlists)
                    self.populate_track_list(self.track_list)
                    popup.dismiss()
                    self.select_and_play(i)

                def remove_from_pl(inst, i=idx):
                    if 0 <= i < len(self.playlists):
                        self.playlists.pop(i)
                        self.save_playlists()
                        rebuild_items()

                play_item_btn.bind(on_press=play_from_pl)
                del_btn.bind(on_press=remove_from_pl)
                row.add_widget(play_item_btn)
                row.add_widget(del_btn)
                grid.add_widget(row)

        rebuild_items()
        scroll.add_widget(grid)
        content.add_widget(scroll)

        close_btn = Button(
            text="Закрыть",
            size_hint_y=None,
            height=dp(48),
            font_size=sp(15),
            background_normal='',
            background_color=(1, 0.5, 0, 1),
            color=(0, 0, 0, 1),
        )
        close_btn.bind(on_press=popup.dismiss)
        content.add_widget(close_btn)
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
        # 1. Пробуем data-атрибуты и title/download на самой ссылке
        for attr in ('data-title', 'data-name', 'title', 'download', 'aria-label'):
            val = self._clean_text(a_tag.get(attr, ''))
            if val and not re.fullmatch(r'(скачать|download|mp3|трек)', val, re.I):
                return val

        # 2. Текст внутри самой ссылки
        own_text = self._clean_text(a_tag.get_text(" ", strip=True))
        if own_text and not re.fullmatch(r'(скачать|download|mp3|\d+\s*(kbps|кб/с|mb|мб))', own_text, re.I):
            return own_text

        # 3. Ищем родительскую карточку трека (где ровно одна ссылка на скачивание)
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
            # Data-атрибуты на карточке
            d_artist = self._clean_text(row.get('data-artist') or row.get('data-author') or '')
            d_title = self._clean_text(row.get('data-title') or row.get('data-song') or row.get('data-name') or '')
            if d_artist and d_title:
                return f"{d_artist} - {d_title}"
            if d_title:
                return d_title

            # Ищем отдельные теги исполнителя и названия по классам
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

            # Собираем чистые текстовые фрагменты строки (без длительности, размера и кнопок)
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

        # 4. Фоллбэк: вытаскиваем имя из URL
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
                    found_tracks.append({"title": title, "url": href})

            def apply_results(dt):
                if req_id != self.search_request_id:
                    return
                self.track_list = found_tracks
                self.current_track_index = 0
                self.populate_track_list(self.track_list)

            Clock.schedule_once(apply_results)
        except Exception as e:
            if req_id == self.search_request_id:
                self.set_status(f"Ошибка сети: {str(e)[:35]}")

    def populate_track_list(self, tracks):
        self.results_layout.clear_widgets()
        if tracks:
            self.status_label.text = f"Треков в списке: {len(tracks)}"
            for idx, track in enumerate(tracks):
                btn = Button(
                    text=track['title'],
                    size_hint_y=None,
                    height=dp(54),
                    font_size=sp(14),
                    halign='left',
                    valign='middle',
                    shorten=True,
                    shorten_from='right',
                    padding=[dp(12), 0],
                    background_normal='',
                    background_color=(0.12, 0.12, 0.12, 1),
                    color=(1, 1, 1, 1),
                )
                btn.bind(size=lambda inst, val: setattr(inst, 'text_size', val))
                btn.bind(on_press=lambda inst, i=idx: self.select_and_play(i))
                self.results_layout.add_widget(btn)
        else:
            self.status_label.text = "Ничего не найдено."

    # --- ВОСПРОИЗВЕДЕНИЕ ---
    def select_and_play(self, index):
        self.current_track_index = index
        self.play_current()

    def _unload_current_sound(self):
        self._stop_visualizer()
        self._reset_seek_bar()
        if self.sound:
            self._manual_stop = True
            try:
                self.sound.stop()
                self.sound.unload()
            except Exception:
                pass
            self.sound = None
        self.paused_pos = 0.0
        self.is_paused = False

    def play_current(self):
        self._unload_current_sound()
        if not self.track_list or not (0 <= self.current_track_index < len(self.track_list)):
            self.set_status("Список треков пуст")
            return

        self.play_request_id += 1
        req_id = self.play_request_id
        track = self.track_list[self.current_track_index]

        self.set_status(f"Загрузка: {track['title'][:32]}...")
        self.play_btn.text = "WAIT"
        threading.Thread(
            target=self._stream_and_play,
            args=(track, self.current_track_index, req_id),
            daemon=True,
        ).start()

    def _stream_and_play(self, track, track_idx, req_id):
        try:
            os.makedirs(self.user_data_dir, exist_ok=True)
            temp_path = os.path.join(self.user_data_dir, f"temp_track_{req_id}.mp3")

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

            with open(temp_path, 'wb') as f:
                for chunk in res.iter_content(chunk_size=8192):
                    if req_id != self.play_request_id:
                        return
                    if chunk:
                        f.write(chunk)

            if req_id != self.play_request_id:
                return

            for name in os.listdir(self.user_data_dir):
                if name.startswith("temp_track_") and name != f"temp_track_{req_id}.mp3":
                    try:
                        os.remove(os.path.join(self.user_data_dir, name))
                    except OSError:
                        pass

            # Если имя было вида "Трек N", пробуем прочитать его из ID3-тега MP3
            resolved_title = track['title']
            if re.fullmatch(r'Трек\s+\d+', resolved_title):
                id3 = extract_id3_title(temp_path)
                if id3:
                    resolved_title = id3

            est_duration = estimate_mp3_duration(temp_path)
            Clock.schedule_once(
                lambda dt: self._start_sound(temp_path, resolved_title, track_idx, est_duration, req_id)
            )
        except Exception as e:
            if req_id == self.play_request_id:
                self.set_status(f"Ошибка аудио: {str(e)[:32]}")
                Clock.schedule_once(lambda dt: setattr(self.play_btn, 'text', 'PLAY'))

    def _start_sound(self, path, title, track_idx, est_duration, req_id):
        if req_id != self.play_request_id:
            return
        if 0 <= track_idx < len(self.track_list) and self.track_list[track_idx]['title'] != title:
            self.track_list[track_idx]['title'] = title
            children = list(reversed(self.results_layout.children))
            if track_idx < len(children):
                children[track_idx].text = title

        self.sound = SoundLoader.load(path)
        if self.sound:
            self._manual_stop = False
            self.is_paused = False
            self.paused_pos = 0.0
            self.sound.bind(on_stop=self._on_sound_stop)
            self.sound.play()

            dur = float(self.sound.length or 0.0)
            if dur <= 0 and est_duration > 0:
                dur = est_duration
            self.track_duration = dur
            if dur > 0:
                self.seek_slider.max = dur
                self.time_total_label.text = format_time(dur)

            self.play_btn.text = "PAUSE"
            self.status_label.text = f"Играет: {title}"
            self._start_visualizer()
            self._start_progress_timer()
        else:
            self.play_btn.text = "PLAY"
            self.status_label.text = "Не удалось открыть формат файла"

    def _on_sound_stop(self, instance):
        if self._manual_stop or self.is_paused:
            return
        self._stop_visualizer()
        self._stop_progress_timer()
        self.play_btn.text = "PLAY"
        if self.track_list and self.current_track_index < len(self.track_list) - 1:
            self.current_track_index += 1
            self.play_current()

    def toggle_play(self, instance):
        if self.sound:
            if self.sound.state == 'play':
                try:
                    self.paused_pos = float(self.sound.get_pos() or self.seek_slider.value or 0.0)
                except Exception:
                    self.paused_pos = float(self.seek_slider.value or 0.0)
                self.is_paused = True
                self._manual_stop = True
                self.sound.stop()
                self._stop_visualizer()
                self.play_btn.text = "PLAY"
            else:
                self.is_paused = False
                self._manual_stop = False
                self._ignore_pos_until = Clock.get_time() + 0.35
                self.sound.play()
                if self.paused_pos > 0:
                    pos = self.paused_pos
                    Clock.schedule_once(lambda dt: self._safe_seek(pos), 0.05)
                self._start_visualizer()
                self._start_progress_timer()
                self.play_btn.text = "PAUSE"
        else:
            self.play_current()

    def _safe_seek(self, pos):
        if self.sound:
            try:
                self.sound.seek(pos)
            except Exception:
                pass

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

    # --- ВЫБОР КАЧЕСТВА И СКАЧИВАНИЕ ---
    def open_quality_popup(self, instance):
        if not self.track_list or not (0 <= self.current_track_index < len(self.track_list)):
            self.set_status("Сначала выберите трек")
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
                    "/storage/emulated/0/Music/ZMus",
                    "/storage/emulated/0/Download/ZMus",
                ):
                    try:
                        os.makedirs(public_dir, exist_ok=True)
                        dest = os.path.join(public_dir, filename)
                        with open(temp_file, 'rb') as src, open(dest, 'wb') as dst:
                            dst.write(src.read())
                        os.remove(temp_file)
                        short_dir = os.path.basename(os.path.dirname(public_dir))
                        self.set_status(f"Сохранено в {short_dir}/ZMus: {filename[:18]}")
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
