[app]
title = Hiren Sagar OB
package.name = hirensagarob
package.domain = com.hirensagar
source.dir = .
source.include_exts = py,html,csv,png,jpg
source.include_patterns = templates/*,data/*
# credentials must NEVER be bundled into the APK
source.exclude_patterns = .env,*.env,env.py,tests/*,.github/*,README.md
version = 1.0.0
requirements = python3==3.11.5,hostpython3==3.11.5,kivy==2.3.0,pyjnius,android,numpy,flask==2.2.5,werkzeug==2.2.3,jinja2==3.1.4,markupsafe,itsdangerous==2.1.2,click==8.1.7,blinker==1.7.0,requests==2.31.0,urllib3==1.26.18,idna==3.7,charset-normalizer==3.3.2,certifi,pyotp==2.9.0,liblzma,libbz2
orientation = portrait
fullscreen = 0
android.permissions = INTERNET,WAKE_LOCK
android.api = 33
android.minapi = 26
android.ndk_api = 26
android.ndk = 25b
android.archs = arm64-v8a
android.accept_sdk_license = True
android.allow_backup = False
# WebView talks to the local Flask server over http://127.0.0.1
android.extra_manifest_application_arguments = ./extra_application_args.xml
p4a.branch = v2024.01.21

[buildozer]
log_level = 2
warn_on_root = 1
