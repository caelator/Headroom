from pathlib import Path
import os

PROVIDERS = {
    'codex': {'name': 'Codex / ChatGPT', 'connection_url': 'https://chatgpt.com/codex/settings/usage'},
    'muse': {'name': 'Muse', 'connection_url': 'https://dev.meta.ai/billing'},
    'zai': {'name': 'z.ai', 'connection_url': 'https://z.ai/manage-apikey/coding-plan/personal/my-plan'},
    'cursor': {'name': 'Cursor', 'connection_url': 'https://cursor.com/settings'},
    'antigravity': {'name': 'Antigravity', 'connection_url': 'https://antigravity.google/'},
    'claude': {'name': 'Claude', 'connection_url': 'https://claude.ai/settings/usage'},
}
DATA_DIR = Path(os.environ.get('HEADROOM_DATA_DIR', os.environ.get('AIUSAGE_DATA_DIR', str(Path.home() / '.local/share/aiusage'))))
DEFAULT_SETTINGS = {'refresh_interval_seconds': 30, 'warn_remaining_pct': 25, 'critical_remaining_pct': 10}
MAX_DATA_AGE_SECONDS = 90
TIMEZONE = 'America/Bogota'
