#!/usr/bin/env python3
"""
Test script for stateless execution and multi-page concurrency.
"""

import asyncio
import os
import time
import httpx
from dotenv import load_dotenv

load_dotenv()

API_BASE = "http://127.0.0.1:8000/v1"
API_TOKEN = os.getenv("API_TOKEN", "sk-catgpt-c8d7daf612b7f95429424aaa3ab86f78fd771a8b0fc1c138")
HEADERS = {
    "Authorization": f"Bearer {API_TOKEN}",
    "Content-Type": "application/json",
}


async def test_models():
    print("🔍 [1/2] 正在测试 /v1/models 接口...")
    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
        resp = await client.get(f"{API_BASE}/models", headers=HEADERS)
        if resp.status_code == 200:
            models = [m["id"] for m in resp.json().get("data", [])]
            print(f"✅ /v1/models 成功响应: {models}")
            return True
        else:
            print(f"❌ /v1/models 失败: {resp.status_code} {resp.text}")
            return False


async def send_single_prompt(client: httpx.AsyncClient, name: str, prompt: str):
    print(f"🚀 [{name}] 发起请求: {prompt[:30]}...")
    start = time.time()
    payload = {
        "model": "catgpt-browser",
        "messages": [
            {"role": "system", "content": "You are a helpful and concise coding assistant."},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
    }
    resp = await client.post(f"{API_BASE}/chat/completions", headers=HEADERS, json=payload, timeout=120.0)
    elapsed = time.time() - start
    if resp.status_code == 200:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        print(f"✅ [{name}] 成功返回 (耗时 {elapsed:.2f}s, 长度 {len(content)} 字符):\n{content[:120]}...\n")
        return True, elapsed, content
    else:
        print(f"❌ [{name}] 请求失败: {resp.status_code} {resp.text}")
        return False, elapsed, resp.text


async def test_concurrency():
    print("\n⚡ [2/2] 正在启动【双路并发】测试 (使用 asyncio.gather 同时发起 2 个不同的任务)...")
    async with httpx.AsyncClient(trust_env=False) as client:
        t0 = time.time()
        res_a, res_b = await asyncio.gather(
            send_single_prompt(client, "Task A (Python)", "请用Python写一个斐波那契数列函数并测试"),
            send_single_prompt(client, "Task B (JavaScript)", "请用JavaScript写一个快速排序函数并测试"),
        )
        total_time = time.time() - t0
        print(f"\n🎉 双路并发测试完成! 总耗时: {total_time:.2f}s")
        success = res_a[0] and res_b[0]
        if success:
            print("🌟 两个独立任务均成功返回，无状态且并发运行正常！")
        return success


async def main():
    m_ok = await test_models()
    if not m_ok:
        return
    await test_concurrency()


if __name__ == "__main__":
    asyncio.run(main())
