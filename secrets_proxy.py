"""
secrets_proxy.py — single entry point for all secret / config reads.

Application code calls get_secret("OPENAI_API_KEY") instead of os.getenv(...).
The actual source is selected by SECRETS_BACKEND:

    env             read from process env / .env file        (default, dev)
    ssm             AWS SSM Parameter Store (SecureString)    (prod, cheap)
    secretsmanager  AWS Secrets Manager                       (prod, rotated)

Swapping backends requires zero code changes — only SECRETS_BACKEND and AWS
credentials on the host (instance role, env vars, or ~/.aws/credentials).

Naming convention on AWS side:
    SSM:            /simple-interview/OPENAI_API_KEY
    SecretsMgr:     simple-interview/OPENAI_API_KEY
Override prefixes with SSM_PREFIX / SECRETSMANAGER_PREFIX if needed.

Blob mode: set SECRETSMANAGER_BLOB=<secret-name> to read ALL keys from one
JSON secret (fetched once, cached). Falls back to per-key lookup, then env.
"""

import json
import logging
import os
from functools import lru_cache
from typing import Optional

from dotenv import load_dotenv
load_dotenv()  # ensures SECRETS_BACKEND itself, plus dev secrets, are visible

_BACKEND    = os.getenv("SECRETS_BACKEND", "env").lower()
_SSM_PREFIX = os.getenv("SSM_PREFIX", "/simple-interview/")
_SM_PREFIX  = os.getenv("SECRETSMANAGER_PREFIX", "simple-interview/")
_SM_BLOB    = os.getenv("SECRETSMANAGER_BLOB", "")
_AWS_REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")


@lru_cache(maxsize=1)
def _ssm():
    import boto3
    return boto3.client("ssm", region_name=_AWS_REGION)


@lru_cache(maxsize=1)
def _sm():
    import boto3
    return boto3.client("secretsmanager", region_name=_AWS_REGION)


@lru_cache(maxsize=None)
def _from_ssm(name: str) -> Optional[str]:
    try:
        resp = _ssm().get_parameter(Name=_SSM_PREFIX + name, WithDecryption=True)
        return resp["Parameter"]["Value"]
    except Exception:
        return None


@lru_cache(maxsize=None)
def _from_sm(name: str) -> Optional[str]:
    try:
        resp = _sm().get_secret_value(SecretId=_SM_PREFIX + name)
        return resp.get("SecretString")
    except Exception:
        return None


@lru_cache(maxsize=1)
def _blob() -> dict:
    """Fetch the single JSON secret holding all keys. Cached for process life."""
    try:
        resp = _sm().get_secret_value(SecretId=_SM_BLOB)
        data = json.loads(resp.get("SecretString") or "{}")
        # Keys pasted into the console sometimes carry stray whitespace.
        return {k.strip(): v for k, v in data.items()}
    except Exception as e:
        logging.getLogger("secrets").error(f"[Secrets] blob '{_SM_BLOB}' fetch failed: {e}")
        return {}


def get_secret(name: str, default: Optional[str] = None) -> str:
    """
    Resolution order:
      1. Configured backend (ssm or secretsmanager), if not 'env'
      2. Process env / .env  (always tried as fallback so AWS_REGION etc. work)
      3. Provided default, else ""
    """
    if _BACKEND == "ssm":
        v = _from_ssm(name)
        if v is not None:
            return v
    elif _BACKEND == "secretsmanager":
        if _SM_BLOB:
            v = _blob().get(name)
            if v is not None and v != "":
                return v
        else:
            v = _from_sm(name)
            if v is not None:
                return v

    v = os.getenv(name)
    if v is not None:
        return v
    return default if default is not None else ""


def backend() -> str:
    """Return active backend name — useful for startup logging."""
    return _BACKEND
