"""Structured logging with secret redaction and request context."""
from __future__ import annotations
import contextvars
import json
import logging
from logging.handlers import RotatingFileHandler
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

_shop_id=contextvars.ContextVar('log_shop_id',default=None)
_user_id=contextvars.ContextVar('log_user_id',default=None)
_job=contextvars.ContextVar('log_job',default=None)


def set_log_context(*, shop_id=None, user_id=None, job=None):
    tokens=[]
    if shop_id is not None: tokens.append((_shop_id,_shop_id.set(shop_id)))
    if user_id is not None: tokens.append((_user_id,_user_id.set(user_id)))
    if job is not None: tokens.append((_job,_job.set(job)))
    return tokens


def reset_log_context(tokens):
    for var,token in reversed(tokens): var.reset(token)


class SecretRedactionFilter(logging.Filter):
    def __init__(self,secrets: Iterable[str]):
        super().__init__()
        self.secrets=tuple(sorted({s for s in secrets if s},key=len,reverse=True))

    def redact(self, value: str) -> str:
        text=value
        for secret in self.secrets:
            text=text.replace(secret,'***REDACTED***')
        return text

    def filter(self,record: logging.LogRecord) -> bool:
        try: text=record.getMessage()
        except Exception: return True
        record.msg=self.redact(text); record.args=()
        # Standard logging formatters cache traceback text in ``exc_text``.
        # Populate a redacted copy so secrets cannot leak through exceptions.
        if record.exc_info:
            try:
                raw=logging.Formatter().formatException(record.exc_info)
                record.exc_text=self.redact(raw)
            except Exception:
                record.exc_text='*** exception formatting failed ***'
        setattr(record,'_sellerbot_redact',self.redact)
        return True


class JsonFormatter(logging.Formatter):
    def format(self,record: logging.LogRecord) -> str:
        payload={
            'ts':datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
            'level':record.levelname,
            'logger':record.name,
            'message':record.getMessage(),
        }
        shop=_shop_id.get(); user=_user_id.get(); job=_job.get()
        if shop is not None: payload['shop_id']=shop
        if user is not None: payload['telegram_user_id']=user
        if job is not None: payload['job']=job
        if record.exc_info:
            exc_text=record.exc_text or self.formatException(record.exc_info)
            redactor=getattr(record,'_sellerbot_redact',None)
            payload['exception']=redactor(exc_text) if callable(redactor) else exc_text
        return json.dumps(payload,ensure_ascii=False,default=str)


def configure_logging(settings) -> None:
    Path('logs').mkdir(parents=True,exist_ok=True)
    level=getattr(logging,settings.log_level,logging.INFO)
    formatter=JsonFormatter() if settings.log_format=='json' else logging.Formatter(
        '%(asctime)s %(levelname)s %(name)s: %(message)s')
    handlers=[logging.StreamHandler(sys.stdout)]
    try:
        log_path=Path('logs/bot.log')
        file_handler=RotatingFileHandler(log_path,maxBytes=5*1024*1024,backupCount=3,encoding='utf-8')
        try: log_path.chmod(0o600)
        except OSError: pass
        handlers.append(file_handler)
    except OSError: pass
    secrets=[settings.telegram_token,settings.ozon_api_key,settings.wb_api_token,
             settings.ozon_perf_client_secret]
    # Include secrets from all discovered profiles without logging profile values.
    for profile in settings.available_credential_profiles():
        try:
            c=settings.credentials_for_profile(profile)
            secrets.extend([c.ozon_api_key,c.wb_api_token,c.ozon_perf_client_secret])
        except Exception: pass
    filt=SecretRedactionFilter(secrets)
    root=logging.getLogger(); root.handlers.clear(); root.setLevel(level)
    for handler in handlers:
        handler.setLevel(level); handler.setFormatter(formatter); handler.addFilter(filt); root.addHandler(handler)
