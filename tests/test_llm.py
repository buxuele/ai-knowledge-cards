"""模型连通性探测，确认哪个模型能稳定输出中文正文。

用法：
    set -a; . ./.env; set +a
    .venv/bin/python tests/test_llm.py --dry-run   只看配置，不发请求
    .venv/bin/python tests/test_llm.py --nim       只测 NVIDIA 这一家
    .venv/bin/python tests/test_llm.py             两家都测

这个文件只做探测，不写任何数据。批量补解释用 scripts/enrich_cards.py。

这个脚本会发请求且可能计费，所以：
1. 必须显式加 --dry-run 或指定 provider 才会真正发请求。
2. 参数解析用 argparse，未知参数直接报错退出。以前 enrich_cards.py 没有
   argparse，--help 被静默忽略直接开跑，那次白花了 0.33 美元。

2026-11-16 当前配置：

openrouter：
1. nvidia/nemotron-3.5-super-vl:free 主模型，实测能出中文正文。
2. nvidia/nemotron-3.5-lightning:free 常返回 0 字正文。
3. dots-studio/dots-3-note-preview:free 能出正文，作为第二备用。

NVIDIA NIM：
4. 模型名是单层前缀 nvidia/nemotron-3.5-super-vl-120b-a12b。
   用户原先给的 nvidia/nemotron-3-super-120b-a12b 已返回 410 Gone。
5. 推理会占用 max_tokens，建议 2000 并关掉思考。
   参数是 chat_template_kwargs 里的 enable_thinking 与 force_nonempty_content。
"""

import argparse
import os
import sys

QUESTION = "什么是幂等性？"
ANSWER = "同一个操作执行一次或多次，对系统状态产生的最终效果相同。"

# 关思考的参数。少了它 content 必为空，这是 nemotron 的通病
NO_THINK = {"chat_template_kwargs": {"enable_thinking": False,
                                     "force_nonempty_content": True}}

OPENROUTER_MODELS = [
    ("nvidia/nemotron-3.5-super-vl:free", 2000, None),
    ("nvidia/nemotron-3.5-lightning:free", 2000, None),
    ("dots-studio/dots-3-note-preview:free", 2000, None),
]

NIM_MODELS = [
    ("nvidia/nemotron-3.5-super-vl-120b-a12b", 2000, NO_THINK),
]


def probe(client, model, max_tokens, extra):
    """发一次最小请求，返回可读的结果字符串。"""
    kwargs = {"extra_body": extra} if extra else {}
    r = client.chat.completions.create(
        model=model,
        messages=[{"role": "user",
                   "content": f"{QUESTION}\n{ANSWER}\n请用一句话解释它为什么重要。"}],
        max_tokens=max_tokens,
        **kwargs,
    )
    m = r.choices[0].message
    text = m.content or ""
    reason = getattr(m, "reasoning_content", None) or getattr(m, "reasoning", None) or ""
    usable = 30 <= len(text) <= 700
    line = (f"{'可用' if usable else '不可用'} {model:42} 正文 {len(text):4} 字  "
            f"推理 {len(reason):4} 字  finish={r.choices[0].finish_reason}")
    return usable, line, text


def run_openrouter():
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        print("缺少环境变量 OPENROUTER_API_KEY，跳过 openrouter")
        return []
    from openai import OpenAI

    base = os.environ.get("OPENROUTER_BASE", "https://openrouter.ai/api/v1")
    client = OpenAI(base_url=base, api_key=key, timeout=90.0, max_retries=0)
    usable = []
    for model, mt, extra in OPENROUTER_MODELS:
        try:
            ok, line, text = probe(client, model, mt, extra)
            print(line)
            if ok:
                print(f"      {text[:100]}")
                usable.append(model)
        except Exception as e:
            print(f"失败   {model:42} {type(e).__name__} {str(e)[:70]}")
    return usable


def run_nim():
    key = os.environ.get("NVIDIA_API_KEY")
    if not key:
        print("缺少环境变量 NVIDIA_API_KEY，跳过 NVIDIA NIM")
        return []
    from openai import OpenAI

    base = os.environ.get("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
    client = OpenAI(base_url=base, api_key=key, timeout=180.0, max_retries=0)
    usable = []
    for model, mt, extra in NIM_MODELS:
        tag = "关思考" if extra else "不关思考"
        try:
            ok, line, text = probe(client, model, mt, extra)
            print(f"[{tag}] {line}")
            if ok:
                print(f"      {text[:100]}")
                if extra:
                    usable.append(model)
        except Exception as e:
            print(f"失败   {tag} {model:42} {type(e).__name__} {str(e)[:70]}")
    return usable


def main():
    ap = argparse.ArgumentParser(description="模型连通性探测，会发请求且可能计费")
    ap.add_argument("--dry-run", action="store_true", help="只打印将发的请求，不实际调用")
    ap.add_argument("--nim", action="store_true", help="只测 NVIDIA NIM")
    ap.add_argument("--openrouter", action="store_true", help="只测 openrouter")
    args = ap.parse_args()

    both = not (args.nim or args.openrouter)

    if args.dry_run:
        print("干跑，不发请求")
        if not args.nim or both:
            for m, mt, _ in OPENROUTER_MODELS:
                print(f"  openrouter  {m:42} max_tokens={mt} "
                      f"密钥已配置={bool(os.environ.get('OPENROUTER_API_KEY'))}")
        if not args.openrouter or both:
            for m, mt, e in NIM_MODELS:
                print(f"  nim         {m:42} max_tokens={mt} "
                      f"关思考={bool(e)} "
                      f"密钥已配置={bool(os.environ.get('NVIDIA_API_KEY'))}")
        return

    # 单测一家时另一家不会跑，变量必须先给空列表，否则收尾打印时未赋值
    a = []
    b = []
    if not args.nim or both:
        print("=== openrouter ===")
        a = run_openrouter()
    if not args.openrouter or both:
        print("\n=== NVIDIA NIM ===")
        b = run_nim()

    print("\n可用于批量:")
    print("  openrouter:", a or "无")
    print("  nim:      ", b or "无")


if __name__ == "__main__":
    main()
