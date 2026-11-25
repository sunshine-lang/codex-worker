"""Call the three-node Langflow with local project attachments (standard library only)."""
import argparse
import base64
import getpass
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flow-url", default=os.environ.get("MATERIALS_FLOW_URL"),
                        help="完整的 Langflow /api/v1/run/<flow_id> 地址")
    sub = parser.add_subparsers(dest="operation", required=True)
    submit = sub.add_parser("submit")
    for field in ("project-name", "business-tag", "source-language", "target-language"):
        submit.add_argument("--" + field, required=True)
    submit.add_argument("--requirements", default="")
    submit.add_argument("--document", type=Path, action="append", default=[])
    submit.add_argument("--submission-key", type=uuid.UUID)
    sub.add_parser("status").add_argument("task_id")
    result = sub.add_parser("result")
    result.add_argument("task_id")
    result.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.flow_url:
        parser.error("请设置 MATERIALS_FLOW_URL 或传 --flow-url。")
    key = os.environ.get("LANGFLOW_API_KEY") or getpass.getpass("Langflow API Key（隐藏）：")
    payload = {"operation": args.operation}
    if args.operation == "submit":
        submission = str(args.submission_key or uuid.uuid4())
        print("submission_key=" + submission, file=sys.stderr, flush=True)
        payload.update(project_name=args.project_name, business_tag=args.business_tag,
                       source_language=args.source_language, target_language=args.target_language,
                       translation_requirements=args.requirements, submission_key=submission,
                       documents=[{"source_filename": p.name, "content_base64": base64.b64encode(p.read_bytes()).decode("ascii")}
                                  for p in args.document])
    else:
        payload["task_id"] = args.task_id
    body = {"input_type": "chat", "output_type": "chat", "output_component": "ChatOutput-Materials",
            "input_value": json.dumps(payload, ensure_ascii=False)}
    req = Request(args.flow_url, data=json.dumps(body, ensure_ascii=False).encode(),
                  headers={"Content-Type": "application/json", "x-api-key": key})
    try:
        with build_opener(NoRedirect()).open(req, timeout=120) as response:
            run = json.load(response)
    except HTTPError as exc:
        raise SystemExit(f"Langflow HTTP {exc.code}") from None
    values = [json.loads(node["results"]["message"]["text"]) for group in run["outputs"]
              for node in group["outputs"] if node["component_id"] == "ChatOutput-Materials"]
    if len(values) != 1 or not isinstance(values[0], dict):
        raise SystemExit("Langflow 未返回材料节点结果。")
    value = values[0]
    if args.operation == "result" and "error" not in value:
        if not isinstance(value.get("translation_prompt"), str) or not isinstance(value.get("documents_markdown"), list):
            raise SystemExit("结果缺少 Prompt 或 Markdown 列表。")
        args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        print(args.output)
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    return 1 if "error" in value else 0


if __name__ == "__main__":
    sys.exit(main())
