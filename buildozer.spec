[app]

title = Kulikov Sound
package.name = kulikovsound
package.domain = ru.kulikov

source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json

version = 0.2

# Зависимости для сети, парсинга HTML, SSL и хранения на Android 10+
requirements = python3,kivy==2.3.1,requests==2.32.3,beautifulsoup4,soupsieve,urllib3,chardet==5.2.0,charset_normalizer==3.3.2,idna,certifi,openssl,pyjnius,androidstorage4kivy

orientation = portrait
fullscreen = 0

# Разрешения для сети и аудио на Android (включая Android 13+)
android.permissions = INTERNET, READ_MEDIA_AUDIO, READ_EXTERNAL_STORAGE, WRITE_EXTERNAL_STORAGE

android.api = 33
android.minapi = 24
android.ndk = 25b
android.archs = arm64-v8a
android.allow_backup = True
android.accept_sdk_license = True

[buildozer]

log_level = 2
warn_on_root = 1
