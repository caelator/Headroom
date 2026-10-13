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


def configured_credential_scope(provider):
    """Opaque account binding only; credential bytes never leave this module's caller."""
    try:
        config = json.loads((Path.home()/'.config/codexbar/config.json').read_text())
        item = next(p for p in config.get('providers',[]) if p.get('id')==provider)
        accounts = (item.get('tokenAccounts') or {}).get('accounts') or []
        if accounts:
            index = (item.get('tokenAccounts') or {}).get('activeIndex',0)
            if type(index) is int and 0 <= index < len(accounts):
                account = accounts[index]
                return '|'.join(str(account.get(k,'')) for k in ('id','token','organizationId','workspaceID'))
        if provider=='zai':
            key = item.get('apiKey') or os.environ.get('Z_AI_API_KEY')
            if key: return str(item.get('region','global'))+'|'+key
    except (OSError,ValueError,AttributeError,StopIteration,TypeError):
        pass
    return None


def identity_hasher(store):
    def hash_identity(provider, identity):
        configured = configured_credential_scope(provider)
        if configured:
            return store.fingerprint(provider+'|configured|'+configured)
        if provider=='codex' and codex_account_id():
            return store.fingerprint('codex|'+codex_account_id())
        subject = identity.get('accountID') or identity.get('accountEmail')
        if not subject: return None
        return store.fingerprint(provider+'|'+str(subject).strip().lower())
    return hash_identity
