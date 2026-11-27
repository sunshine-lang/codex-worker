"""项目材料预处理联调。Python 3.9+，仅使用标准库。"""
import argparse
import base64
import gzip
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid


FLOW_URL = ""  # The Feishu quickstart supplies its approved test endpoint here.
OUTPUT_NODE = "ChatOutput-Materials"
KEY_FILE = Path(__file__).resolve().with_name("langflow_api_key.txt")
MAX_BYTES = 128 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_key():
    if KEY_FILE.is_file():
        key = KEY_FILE.read_text(encoding="utf-8-sig").strip()
    else:
        key = os.environ.get("LANGFLOW_API_KEY", "").strip()
    if not key or any(ch.isspace() for ch in key):
        raise ValueError("请把单行 langflow_api_key.txt 放到 material_client.py 同一目录。")
    return key


def call(key, operation, **fields):
    """传入一次业务操作，取出材料节点返回的业务 JSON。"""
    business = {"operation": operation, **fields}
    envelope = {
        "input_type": "chat", "output_type": "chat", "output_component": OUTPUT_NODE,
        "input_value": json.dumps(business, ensure_ascii=False),
    }
    url = FLOW_URL or os.environ.get("MATERIALS_FLOW_URL")
    if not url:
        raise ValueError("请设置 MATERIALS_FLOW_URL 为已确认的 Langflow 运行地址。")
    req = Request(url, method="POST", data=json.dumps(envelope, ensure_ascii=False).encode("utf-8"),
                  headers={"Content-Type": "application/json; charset=utf-8",
                           "x-api-key": key, "Accept-Encoding": "gzip"})
    try:
        with build_opener(NoRedirect()).open(req, timeout=240) as response:
            data = response.read()
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                data = gzip.decompress(data)
    except HTTPError as exc:
        raise RuntimeError(f"Langflow 返回 HTTP {exc.code}。请核对入口、网络和 Key。") from None
    except (URLError, OSError) as exc:
        raise RuntimeError(f"连接中断（{type(exc).__name__}）。保留输出目录后重试。") from None
    nodes = [node for group in json.loads(data).get("outputs", []) for node in group.get("outputs", [])
             if node.get("component_id") == OUTPUT_NODE]
    if len(nodes) != 1:
        raise RuntimeError("未找到唯一的 ChatOutput-Materials 输出，请核对 Flow。")
    value = json.loads(nodes[0]["results"]["message"]["text"])
    if not isinstance(value, dict):
        raise ValueError("材料节点返回的业务 JSON 结构异常。")
    if value.get("error"):
        err = value["error"]
        raise RuntimeError(f"{err.get('code')}  {err.get('message')}  {value.get('current_file') or ''}")
    return value


def make_documents(paths):
    """按传入顺序读取文件，保留原文件名。"""
    documents, total = [], 0
    for path in paths:
        if path.stat().st_size > MAX_BYTES - total:
            raise ValueError("每笔任务的附件原始字节合计最多 128 MiB。")
        raw = path.read_bytes()
        total += len(raw)
        if not raw or total > MAX_BYTES:
            raise ValueError(f"附件为空或合计过大：{path.name}")
        documents.append({"source_filename": path.name, "content_base64": base64.b64encode(raw).decode("ascii")})
    return documents


def save_result(out, result, names=None, json_name="result.json"):
    organized = result.get("processing_mode") == "organize"
    prompt, docs = result.get("translation_prompt"), result.get("documents_markdown")
    if not isinstance(docs, list) or (not organized and (not isinstance(prompt, str) or not prompt.strip())):
        raise ValueError("结果缺少项目 Prompt 或附件 Markdown 列表。")
    if not json_name or re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', json_name) or json_name in (".",".."):
        raise ValueError("JSON 输出名须为当前目录下的单个文件名。")
    if not all(isinstance(doc, dict) for doc in docs):
        raise ValueError("材料列表中有无效条目。")
    outputs = {} if organized else {"翻译Prompt.md": prompt}
    used = {unicodedata.normalize("NFC", n).casefold() for n in (json_name, "翻译Prompt.md")}
    if len(used) != 2:
        raise ValueError("JSON 输出名与项目 Prompt 冲突。")
    has_names = any("markdown_filename" in doc for doc in docs)
    for i, doc in enumerate(docs, 1):
        name, body = doc["source_filename"], doc["markdown"]
        if not isinstance(name, str) or not name or "/" in name or "\\" in name or not isinstance(body, str) or not body.strip():
            raise ValueError("返回的原件文件名或 Markdown 正文异常。")
        # New results name the file themselves. Only historical results use the old convention.
        filename = doc.get("markdown_filename") if has_names else f"{i:02d}-{Path(name).stem}.md"
        if (not isinstance(filename, str) or Path(filename).suffix.lower() != ".md"
                or re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', filename)
                or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9¹²³])", filename.split(".", 1)[0].rstrip(" "))):
            raise ValueError("返回的 markdown_filename 不是可保存的 Markdown 文件名。")
        normalized = unicodedata.normalize("NFC", filename).casefold()
        if normalized in used:
            raise ValueError(f"返回的 Markdown 文件名重复或与输出文件冲突：{filename}")
        used.add(normalized)
        outputs[filename] = body
    if names is not None and [doc["source_filename"] for doc in docs] != names:
        raise ValueError("返回附件与提交清单的文件名或顺序不同。")
    outputs[json_name] = json.dumps(result, ensure_ascii=False, indent=2)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out) as tmp:
        stage = Path(tmp)
        for filename, text in outputs.items():
            if (out / filename).is_dir():
                raise ValueError(f"输出目录中已有同名文件夹：{filename}")
            (stage / filename).write_bytes(text.encode("utf-8"))
        for filename in outputs:
            (stage / filename).replace(out / filename)
    print(f"已保存 {len(docs)} 份材料 Markdown" + ("" if organized else " 和 1 份项目 Prompt")
          + f"，目录为 {out.resolve()}")


def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--out", type=Path, required=True)
    for name in ("project", "tag", "source", "target"):
        args.add_argument("--" + name)
    args.add_argument("--requirements", default="")
    args.add_argument("--file", action="append", type=Path, default=[])
    args.add_argument("--resume", action="store_true")
    args.add_argument("--organize", action="store_true", help="仅整理 IM 项目材料，交付可编辑的 Markdown")
    opt = args.parse_args()
    if not opt.resume and not all((opt.project, opt.tag, opt.source, opt.target)):
        args.error("首次提交需提供 --project、--tag、--source 和 --target。")
    key = read_key()
    opt.out.mkdir(parents=True, exist_ok=True)
    task_file = opt.out / "task_id.txt"
    names_file = opt.out / "source_filenames.json"
    if opt.resume:
        if not task_file.exists():
            raise ValueError("此目录尚无任务号，请用相同目录、原参数和原附件重发首次命令。")
        task = task_file.read_text(encoding="utf-8").strip()
        names = json.loads(names_file.read_text(encoding="utf-8")) if names_file.exists() else None
    else:
        docs = make_documents(opt.file)
        names = [doc["source_filename"] for doc in docs]
        key_file = opt.out / "submission_key.txt"
        try:
            with key_file.open("x", encoding="utf-8") as stream:
                stream.write(str(uuid.uuid4()))
        except FileExistsError:
            pass
        idem = str(uuid.UUID(key_file.read_text(encoding="utf-8").strip()))
        receipt = call(key, "organize" if opt.organize else "submit", project_name=opt.project, business_tag=opt.tag,
                       source_language=opt.source, target_language=opt.target,
                       translation_requirements=opt.requirements, documents=docs, submission_key=idem)
        task = receipt.get("task_id", "")
        if receipt.get("submission_key") != idem or not re.fullmatch(r"mat_[0-9a-f]{32}", task):
            raise ValueError("任务回执与本次提交不符。")
        names_file.write_bytes(json.dumps(names, ensure_ascii=False).encode("utf-8"))
        task_file.write_text(task, encoding="utf-8")
    if not re.fullmatch(r"mat_[0-9a-f]{32}", task):
        raise ValueError("任务号格式无效。")
    print("task_id =", task, flush=True)
    while True:
        state = call(key, "status", task_id=task)
        if state.get("task_id") != task:
            raise ValueError("状态回执的任务号不匹配。")
        print(state["status"], state.get("stage", ""), state.get("current_file") or "",
              f"{state.get('documents_completed', 0)}/{state.get('documents_total', 0)}", flush=True)
        if state["status"] == "completed":
            break
        if state["status"] not in ("queued", "running"):
            raise RuntimeError(f"任务已终止：{state['status']}。请保留任务号。")
        time.sleep(5)
    save_result(opt.out, call(key, "result", task_id=task), names)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, RuntimeError, OSError) as exc:
        sys.exit(str(exc))
