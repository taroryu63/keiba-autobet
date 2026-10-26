import os
import sys
from pathlib import Path

# app.config は読み込んだ時点の環境変数を使うので、どのテストよりも先に設定する
os.environ.setdefault("APP_PASSWORD", "testpass123")
os.environ.setdefault("SECRET_KEY", "x" * 40)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
