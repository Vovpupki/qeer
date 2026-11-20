[app]

title = ZMus Player
package.name = zmusplayer
package.domain = org.example

source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json

version = 0.1

requirements = python3,kivy==2.3.1,requests==2.32.3,beautifulsoup4,soupsieve,urllib3,chardet==5.2.0,idna,certifi,openssl,pyjnius,androidstorage4kivy

orientation = portrait
fullscreen = 0

android.permissions = INTERNET, READ_MEDIA_AUDIO, READ_EXTERNAL_STORAGE, WRITE_EXTERNAL_STORAGE

android.api = 33
android.minapi = 24
android.ndk = 25b
android.archs = arm64-v8a, armeabi-v7a
android.allow_backup = True
android.accept_sdk_license = True

[buildozer]

log_level = 2
warn_on_root = 1
