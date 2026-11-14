"""Bounded HTTP audit records: never log headers, bodies, query strings or raw paths."""
import json
import logging
from logging.handlers import RotatingFileHandler

from .common import now


class AccessAudit:
    def __init__(self, root):
        self.handler = RotatingFileHandler(root / 'http-access.jsonl', maxBytes=5 * 1024**2,
                                          backupCount=2, encoding='utf-8')
        self.handler.setFormatter(logging.Formatter('%(message)s'))
        self.logger = logging.Logger('worker-access')
        self.logger.addHandler(self.handler)
        self.logger.propagate = False

    def record(self, *, method, route, client_ip, status, elapsed_ms, job_id=None):
        value = {'at': now(), 'method': method, 'route': route, 'client_ip': client_ip,
                 'status': status, 'elapsed_ms': round(elapsed_ms, 2)}
        if job_id:
            value['job_id'] = job_id
        self.logger.info(json.dumps(value, ensure_ascii=False))

    def close(self):
        self.handler.close()
        self.logger.removeHandler(self.handler)
