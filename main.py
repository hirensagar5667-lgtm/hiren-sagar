"""Entry point.
 - Android APK : starts the Flask server in a thread and shows it in a full-screen WebView.
 - PC / Termux : `python main.py` just runs the server (open http://127.0.0.1:5000)."""
import os
import socket
import sys
import threading
import time

PORT = 5000
URL = f"http://127.0.0.1:{PORT}"
ON_ANDROID = "ANDROID_ARGUMENT" in os.environ or hasattr(sys, "getandroidapilevel")


def start_server():
    import app
    app.run(host="127.0.0.1", port=PORT)


def wait_for_server(timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", PORT), 0.5).close()
            return True
        except OSError:
            time.sleep(0.3)
    return False


if ON_ANDROID:
    from android.runnable import run_on_ui_thread
    from jnius import autoclass
    from kivy.app import App
    from kivy.core.window import Window
    from kivy.uix.label import Label

    WebView = autoclass("android.webkit.WebView")
    WebViewClient = autoclass("android.webkit.WebViewClient")
    LayoutParams = autoclass("android.view.ViewGroup$LayoutParams")
    PythonActivity = autoclass("org.kivy.android.PythonActivity")
    _webview = {}

    @run_on_ui_thread
    def _create_webview():
        activity = PythonActivity.mActivity
        wv = WebView(activity)
        s = wv.getSettings()
        s.setJavaScriptEnabled(True)
        s.setDomStorageEnabled(True)
        wv.setWebViewClient(WebViewClient())
        activity.addContentView(wv, LayoutParams(-1, -1))
        wv.loadUrl(URL)
        _webview["wv"] = wv

    @run_on_ui_thread
    def _go_back_or_exit():
        wv = _webview.get("wv")
        if wv is not None and wv.canGoBack():
            wv.goBack()
        else:
            PythonActivity.mActivity.moveTaskToBack(True)

    class HirenApp(App):
        def build(self):
            threading.Thread(target=start_server, daemon=True).start()
            threading.Thread(target=self._open, daemon=True).start()
            Window.bind(on_keyboard=self._on_key)
            return Label(text="HIREN SAGAR starting...")

        def _open(self):
            if wait_for_server():
                _create_webview()

        def _on_key(self, window, key, *args):
            if key == 27:  # back button
                _go_back_or_exit()
                return True
            return False

        def on_pause(self):
            return True  # keep the server + scan running in background

    if __name__ == "__main__":
        HirenApp().run()
else:
    if __name__ == "__main__":
        start_server()
