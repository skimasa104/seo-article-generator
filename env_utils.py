import os

from dotenv import load_dotenv


def load_project_env() -> None:
    """プロジェクト直下の .env を読み込む"""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    load_dotenv(env_path, override=False)
