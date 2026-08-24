# web_ui.py - загрузчик HTML (v10.1)
import os
_P=os.path.join(os.path.dirname(os.path.abspath(__file__)),"web_ui.html")
try:
    with open(_P,encoding="utf-8") as f:WEB_PAGE=f.read()
except Exception as e:
    WEB_PAGE=f"<h1 style='color:red'>web_ui.html не найден: {e}</h1>"