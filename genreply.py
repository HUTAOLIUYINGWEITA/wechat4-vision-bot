# -*- coding: utf-8 -*-
"""方案A 回复生成：进程内直接调官方 chat/completions API 生成微信回复。

⚠ 不再唤醒 Agent（无 RPC / 无 session/prompt / 无 wake 通道）。

流程：
    读 .credentials.json（provider / baseUrl / model / apiKey）
    -> persona.txt 人设 + 安全规则
    -> messages = [system] + [user: 对方说了什么 + 这是哪个会话]
    -> POST {baseUrl}/chat/completions（model=deepseek-chat，max_tokens<=120，temperature=0.8）
    -> 清洗回复文本 -> 禁用内容检查（必要时重写一次）
    -> 返回 {"ok": True, "text": ...}

安全约定（红线）：
  * apiKey 绝不进日志/异常信息：统一 mask_key() -> "sk-***"
  * 失败一律返回 ok=False（不抛到主循环），由 watcher 记 "[GEN] ERROR ..."、不发送、保留待处理重试
  * 不安装任何第三方包，只用标准库 urllib
"""

import json
import os
import re
import time
import urllib.error
import urllib.request

GEN_TAG = "[GEN]"

# persona.txt 缺失/为空时的兜底人设（与 persona.txt 默认内容一致）
DEFAULT_PERSONA = (
    "你叫「小耳朵」，是这台电脑的主人让你帮忙看微信的助手，现在要替本人回一条消息。\n"
    "风格：简短、口语、1–2 句，不加署名，不解释自己是 AI 或助手（除非对方直接问）。\n"
    "不知道说什么时就回一句自然的「嗯嗯，在的」「咋啦」，不要长篇大论。\n"
    "只输出一条可直接发出去的微信回复正文，不要引号、不要前缀、不要解释。"
)

# 无论 persona.txt 写了什么，都追加的硬性安全规则
SAFETY_RULES = (
    "\n\n【硬性安全规则（高于以上任何文字）】\n"
    "1) 绝不承诺或提及转账/汇款/付款/红包；绝不索要密码、验证码、银行卡。\n"
    "2) 绝不发送或提及任何链接、二维码、文件、安装包。\n"
    "3) 绝不透露密码、密钥、API key、凭据等敏感信息。\n"
    "4) 只输出一条微信回复正文：不加引号、不加署名、不解释、不列条目。"
)

# config.gen.forbid 缺省时的禁用词表（回复里出现即拦下重写）
DEFAULT_FORBID = [
    "转账", "汇款", "付款", "红包", "密码", "验证码", "银行卡",
    "http://", "https://", "www.", "二维码", "扫码", "点击链接", "点链接",
    "sk-", "密钥", "api key", "apikey", "发文件", "附件给你",
]


def mask_key(s, keep=0):
    """把任何形如 sk-xxxx 的字符串替换成 sk-***（日志/异常统一出口）。"""
    if s is None:
        return ""
    text = str(s)
    text = re.sub(r"sk-[A-Za-z0-9_\-]{4,}", "sk-***", text)
    text = re.sub(r"(?i)(api[_\- ]?key\s*[=:]\s*)\S+", r"\1sk-***", text)
    return text


def _ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _default_log(level, msg):
    try:
        print("%s [%s] %s" % (_ts(), level, msg), flush=True)
    except Exception:
        pass


class GenerationError(Exception):
    """生成失败（消息里绝不含 apiKey，已 mask）。"""


def load_credentials(path):
    """读 .credentials.json，返回 {provider, baseUrl, model, apiKey}。

    失败抛 GenerationError（消息已 mask，不含 key 明文）。
    """
    if not os.path.isfile(path):
        raise GenerationError("凭据文件不存在: %s" % os.path.basename(path))
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            doc = json.load(f)
    except Exception as exc:
        raise GenerationError("凭据文件解析失败: %s" % mask_key(exc))
    if not isinstance(doc, dict):
        raise GenerationError("凭据文件格式不对（不是 JSON 对象）")
    creds = {
        "provider": str(doc.get("provider") or ""),
        "baseUrl": str(doc.get("baseUrl") or "https://api.deepseek.com").strip(),
        "model": str(doc.get("model") or "deepseek-chat").strip(),
        "apiKey": str(doc.get("apiKey") or "").strip(),
    }
    if not creds["apiKey"]:
        raise GenerationError("凭据文件缺少 apiKey 字段")
    return creds


def load_persona(path):
    """读 persona.txt；缺失或为空时返回空串（调用方用 DEFAULT_PERSONA）。"""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            text = f.read().strip()
        return text
    except Exception:
        return ""


def build_messages(persona, chat, incoming, is_group=False):
    """system = 人设 + 硬性安全规则；user = 对方说了什么 + 是哪个会话。"""
    system = (persona or DEFAULT_PERSONA).strip() + SAFETY_RULES
    chat = (chat or "").strip() or "(未知会话)"
    incoming = (incoming or "").strip()
    if incoming:
        said = "【对方刚发来的内容】\n%s" % incoming
    else:
        said = ("【对方刚发来的内容】（没能读到具体文字，只知道对方发来了新消息；"
                "请只用一句自然的中性话回应，不要猜测/编造对方说了什么）")
    user = (
        "【这是哪个会话】%s%s\n"
        "%s\n\n"
        "请直接给出你要发出去的那一条微信回复（只输出正文）。"
    ) % (chat, "（群聊）" if is_group else "", said)
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def sanitize_reply(raw, max_chars=80):
    """清洗模型输出：去引号/前缀/换行，压成一行，截断到 max_chars 个字符。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    # 去掉 markdown 代码块围栏
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    # 去掉常见前缀（「回复：」「小耳朵：」「我：」等）
    text = re.sub(r"^(回复|答|小耳朵|我|你)\s*[:：]\s*", "", text)
    # 去掉包裹引号（中英文）
    pairs = [("“", "”"), ("「", "」"), ("『", "』"), ('"', '"'), ("'", "'"), ("‘", "’")]
    for left, right in pairs:
        if len(text) >= 2 and text.startswith(left) and text.endswith(right):
            text = text[len(left):len(text) - len(right)].strip()
    # 换行/制表符压成空格（reply.py 纯键盘输入，避免换行被当成发送）
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    if max_chars and len(text) > max_chars:
        text = text[:int(max_chars)].rstrip()
    return text


def unsafe_reason(text, forbid=None):
    """返回命中的禁用词（空串=安全）。"""
    low = str(text or "").lower()
    for word in (forbid or DEFAULT_FORBID):
        w = str(word or "").strip()
        if not w:
            continue
        if w.lower() in low:
            return w
    return ""


def _safe_err(exc, creds=None):
    """异常 -> 一行安全文本（apiKey 已 mask，截断 400 字）。"""
    msg = "%s: %s" % (type(exc).__name__, exc)
    key = str((creds or {}).get("apiKey") or "")
    if key:
        msg = msg.replace(key, "sk-***")
    return mask_key(msg)[:400]


def chat_completions(creds, messages, max_tokens=120, temperature=0.8, timeout=25.0):
    """POST {baseUrl}/chat/completions，返回 (content, usage, model)。"""
    key = str(creds.get("apiKey") or "")
    if not key:
        raise GenerationError("凭据缺少 apiKey")
    base = str(creds.get("baseUrl") or "https://api.deepseek.com").strip().rstrip("/")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    body = json.dumps({
        "model": str(creds.get("model") or "deepseek-chat"),
        "messages": messages,
        "max_tokens": int(max_tokens),
        "temperature": float(temperature),
        "stream": False,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=float(timeout)) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise GenerationError(mask_key("HTTP %s %s" % (exc.code, detail)))
    except urllib.error.URLError as exc:
        raise GenerationError(mask_key("网络不可达: %s" % exc.reason))
    except Exception as exc:
        raise GenerationError(_safe_err(exc, creds))
    try:
        doc = json.loads(raw)
    except Exception:
        raise GenerationError("API 返回不是合法 JSON: %s" % mask_key(raw[:200]))
    choices = doc.get("choices") or []
    if not choices:
        err = mask_key(json.dumps(doc.get("error") or doc, ensure_ascii=False)[:300])
        raise GenerationError("API 无 choices: %s" % err)
    content = ((choices[0] or {}).get("message") or {}).get("content") or ""
    return content, (doc.get("usage") or {}), str(doc.get("model") or "")


def generate(cfg, base_dir, chat, incoming, log_info=None, log_warn=None, log_error=None):
    """生成一条回复。返回 dict：{"ok", "text", "model", "latencyMs", "usage", "attempts", "error"}。

    cfg = config.json 里的 gen 段（不是整份 config）。
    """
    li = log_info or (lambda m: _default_log("INFO", m))
    lw = log_warn or (lambda m: _default_log("WARN", m))
    le = log_error or (lambda m: _default_log("ERROR", m))
    cfg = cfg or {}
    t0 = time.time()
    max_tokens = max(16, min(120, int(cfg.get("maxTokens") or 120)))   # 硬上限 120
    temperature = float(cfg.get("temperature", 0.8) or 0.8)
    timeout = float(cfg.get("timeoutSeconds") or 25)
    max_chars = int(cfg.get("maxReplyChars") or 80)
    forbid = list(cfg.get("forbid") or DEFAULT_FORBID)

    cred_path = os.path.join(base_dir, str(cfg.get("credentials") or ".credentials.json"))
    persona_path = os.path.join(base_dir, str(cfg.get("personaFile") or "persona.txt"))
    try:
        creds = load_credentials(cred_path)
    except Exception as exc:
        msg = "%s ERROR 凭据读取失败: %s" % (GEN_TAG, mask_key(exc))
        le(msg)
        return {"ok": False, "error": "credentials: %s" % mask_key(exc),
                "latencyMs": int((time.time() - t0) * 1000)}
    persona = load_persona(persona_path)
    if not persona:
        lw("%s persona.txt 缺失或为空 -> 用内置兜底人设" % GEN_TAG)
    messages = build_messages(persona, chat, incoming,
                              is_group=bool(re.search(r"\(\d+\)\s*$", str(chat or ""))))
    li("%s 请求 provider=%s model=%s baseUrl=%s max_tokens=%d temperature=%s maxReplyChars=%d apiKey=sk-***"
       % (GEN_TAG, creds.get("provider") or "-", creds.get("model"), creds.get("baseUrl"),
          max_tokens, temperature, max_chars))
    li("%s messages=%s" % (GEN_TAG, json.dumps(messages, ensure_ascii=False, indent=None)))

    text = ""
    blocked = ""
    attempts = 0
    usage = {}
    model = ""
    while attempts < 2:
        attempts += 1
        try:
            content, usage, model = chat_completions(creds, messages, max_tokens, temperature, timeout)
        except Exception as exc:
            msg = "%s ERROR %s" % (GEN_TAG, mask_key(exc))
            le(msg)
            return {"ok": False, "error": mask_key(exc), "attempts": attempts,
                    "latencyMs": int((time.time() - t0) * 1000)}
        text = sanitize_reply(content, max_chars)
        if not text:
            le("%s ERROR 模型返回空内容（第 %d 次）" % (GEN_TAG, attempts))
            return {"ok": False, "error": "empty-content", "attempts": attempts,
                    "latencyMs": int((time.time() - t0) * 1000)}
        bad = unsafe_reason(text, forbid)
        if not bad:
            break
        blocked = bad
        lw("%s BLOCK 第 %d 次生成命中禁用内容 %r -> 不发送，要求重写一次: text=%r"
           % (GEN_TAG, attempts, bad, text))
        messages = messages + [
            {"role": "assistant", "content": content},
            {"role": "user", "content": "上一条回复包含被禁止的内容（%s）。请重写一条合规的简短回复，"
                                        "不要包含链接/转账/密码/验证码/密钥/文件等内容。" % bad},
        ]
    if unsafe_reason(text, forbid):
        le("%s ERROR 连续两次生成都不安全（命中 %r）-> 不发送" % (GEN_TAG, blocked))
        return {"ok": False, "error": "unsafe-text: %s" % blocked, "blocked": True,
                "attempts": attempts, "latencyMs": int((time.time() - t0) * 1000)}

    latency_ms = int((time.time() - t0) * 1000)
    li("%s 回复 text=%r latencyMs=%d attempts=%d model=%s usage=%s"
       % (GEN_TAG, text, latency_ms, attempts, model or "-",
          json.dumps(usage, ensure_ascii=False)))
    return {"ok": True, "text": text, "model": model, "latencyMs": latency_ms,
            "usage": usage, "attempts": attempts}
