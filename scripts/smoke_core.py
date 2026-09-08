"""Low-volume live checks for the deployed translation and image endpoints.

Run explicitly; this script is not part of the offline test suite. Credentials
are read locally and never written into its JSON report.
"""
import argparse
import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def request(base_url, key, path, payload):
    """Call the live gateway. Args: URL, key, path, JSON payload. Returns: safe report."""
    req = urllib.request.Request(base_url.rstrip("/") + path,
                                 data=json.dumps(payload).encode(),
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"}, method="POST")
    started = time.monotonic()
    try:
        response = urllib.request.urlopen(req, timeout=150)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        data = json.loads(response.read().decode())
        return {"http_status": response.status,
                "elapsed_sec": round(time.monotonic() - started, 3),
                "request_id": response.headers.get("x-request-id"), "response": data}


def check(case, config, base_url):
    """Check one core feature. Args: case, local config, gateway URL. Returns: report."""
    key = (config.get("api_keys") or [""])[0]
    model = config.get("default_model", "gemini-3.8-flash")
    if case == "batch":
        payload = {"systemInstruction": {"parts": [{"text": "将英文逐段翻译为简体中文，保留每段中的换行，只输出译文。"}]},
                   "contents": [{"role": "user", "parts": [
                       {"text": "Good morning.\nThe train leaves at nine."},
                       {"text": "Please keep the original paragraph order."},
                       {"text": "This photograph shows a blue bicycle."}]}]}
        report = request(base_url, key, f"/v1beta/models/{model}:generateContent", payload)
        parts = report["response"].get("candidates", [{}])[0].get("content", {}).get("parts", [])
        report["texts"] = [p.get("text") for p in parts]
        report["segment_count"] = len(parts)
        report["first_segment_kept_line_break"] = bool(parts and "\n" in (parts[0].get("text") or ""))
        report["passed"] = (report["http_status"] == 200 and len(parts) == 3
                            and all(report["texts"]) and report["first_segment_kept_line_break"])
    else:
        if case == "vision":
            image = (ROOT / "logo.png").read_bytes()
            mime = "image/jpeg" if image.startswith(bytes.fromhex("ffd8ff")) else "image/png"
            content = [{"type": "text", "text": "请逐字读取图片最下方的大标题，并简单描述上方符号。只回答可见内容，不要猜测。"},
                       {"type": "image_url", "image_url": {"url": "data:" + mime + ";base64," + base64.b64encode(image).decode()}}]
            messages = [{"role": "user", "content": content}]
        else:
            messages = [{"role": "system", "content": "Translate the user text into Simplified Chinese. Output only the translation."},
                        {"role": "user", "content": "Please confirm the delivery date before Friday."}]
        report = request(base_url, key, "/v1/chat/completions", {"model": model, "messages": messages})
        text = report["response"].get("choices", [{}])[0].get("message", {}).get("content")
        report["text"] = text
        report["passed"] = report["http_status"] == 200 and bool(text)
        if case == "vision":
            report["passed"] = report["passed"] and "geminiweb2api" in "".join(text.lower().split())
    report["case"] = case
    return report


def main():
    """Run selected low-volume checks. Args: CLI options. Returns: process status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("text", "batch", "vision"), required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8081")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    try:
        report = check(args.case, config, args.base_url)
    except Exception as exc:
        report = {"case": args.case, "passed": False, "error_type": type(exc).__name__}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
