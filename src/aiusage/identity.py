"""Account continuity without persisting emails, tokens or browser cookies."""
from pathlib import Path
import json
import os

CODEX_HOME = Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex')))


def codex_account_id():
    try:
        return (json.loads((CODEX_HOME/'auth.json').read_text()).get('tokens') or {}).get('account_id')
    except (OSError,ValueError,AttributeError):
        return None
