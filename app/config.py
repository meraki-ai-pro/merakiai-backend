from pathlib import Path

from dotenv import load_dotenv

_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def load_env() -> None:
	load_dotenv(dotenv_path=_ENV_PATH, override=False)
