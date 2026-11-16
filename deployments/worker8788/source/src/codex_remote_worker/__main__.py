import argparse
import asyncio
import json
from pathlib import Path
import sys
from pydantic import ValidationError

from .app import create_app
from .common import JobError
from .config import Settings, load_settings
from .doctor import verify
from .runner import CodexRunner


def main():
    parser = argparse.ArgumentParser(description='Windows Codex translation task worker')
    parser.add_argument('command', choices=['serve', 'doctor', 'probe', 'recheck'])
    parser.add_argument('--job-id', help='Existing failed job whose completed output should be checked again; never calls a model')
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--credentials-file', type=Path, help='Protected JSON under data_root/private; never a command-line secret')
    parser.add_argument('--verify-sandbox', action='store_true', help='Run fixed native probes; never calls a model')
    args = parser.parse_args()
    try:
        settings = load_settings(args.config, args.credentials_file)
        if args.command == 'recheck':
            if not args.job_id or args.verify_sandbox:
                parser.error('recheck requires --job-id and does not accept --verify-sandbox')
            from .worker import recheck_delivery
            result = asyncio.run(recheck_delivery(settings, args.job_id))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result['status'] in {'completed', 'needs_review'} else 1
        if args.job_id:
            parser.error('--job-id is only valid with recheck')
        if args.command == 'probe':
            if args.verify_sandbox:
                parser.error('probe already runs fixed sandbox checks')
            result = asyncio.run(verify(settings, live=True))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get('passed') else 1
        if args.command == 'doctor':
            result = asyncio.run(verify(settings)) if args.verify_sandbox else CodexRunner(settings).ready()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get('ready', result.get('passed', False)) else 1
        if args.verify_sandbox:
            parser.error('--verify-sandbox is only valid with doctor')
        import uvicorn
        uvicorn.run(create_app(settings), host=settings.bind_host, port=settings.port, workers=1,
                    access_log=False, timeout_graceful_shutdown=30)
        return 0
    except JobError as exc:
        print(json.dumps({'error': exc.public()}, ensure_ascii=False), file=sys.stderr)
        return 1
    except ValidationError as exc:
        fields = sorted({str(e['loc'][0]) if e['loc'] and e['loc'][0] in Settings.model_fields else 'unknown_field'
                         for e in exc.errors(include_input=False, include_context=False)})
        print('配置字段未通过校验：' + ', '.join(fields), file=sys.stderr)
        return 1
    except Exception:
        # Pydantic/file errors can contain credential inputs; do not print exception repr.
        print('配置或启动失败，请核对配置文件、环境变量和依赖。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
