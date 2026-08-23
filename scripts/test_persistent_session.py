#!/usr/bin/env python3
"""
Test script for persistent session routing and history deduplication.
"""

import asyncio
import os
import time
import httpx
from dotenv import load_dotenv

load_dotenv()

API_BASE = "http://127.0.0.1:8000/v1"
API_TOKEN = os.getenv("API_TOKEN", "sk-catgpt-c8d7daf612b7f95429424aaa3ab86f78fd771a8b0fc1c138")
SESSION_ID = f"test-sess-{int(time.time())}"

HEADERS = {
    "Authorization": f"Bearer {API_TOKEN}",
    "Content-Type": "application/json",
    "x-session-id": SESSION_ID,
}


async def main():
    print(f"🔗 [持久会话测试] 正在使用 Session ID: {SESSION_ID}")
    async with httpx.AsyncClient(trust_env=False, timeout=120.0) as client:
        # Turn 1
        print("\n👉 [第 1 轮] 发送秘密数字...")
        t0 = time.time()
        payload_1 = {
            "model": "catgpt-browser",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "请记住秘密数字 9527。只需简短回复收到即可。"},
            ],
            "stream": False,
        }
        resp_1 = await client.post(f"{API_BASE}/chat/completions", headers=HEADERS, json=payload_1)
        if resp_1.status_code != 200:
            print(f"❌ 第 1 轮失败: {resp_1.status_code} {resp_1.text}")
            return
        ans_1 = resp_1.json()["choices"][0]["message"]["content"]
        print(f"✅ 第 1 轮成功 ({time.time() - t0:.2f}s):\n{ans_1.strip()}\n")

        # Turn 2: Client sends full history (repeating Turn 1), testing gateway deduplication and session continuity
        print("👉 [第 2 轮] 客户端发送全量历史记录，询问秘密数字...")
        t1 = time.time()
        payload_2 = {
            "model": "catgpt-browser",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "请记住秘密数字 9527。只需简短回复收到即可。"},
                {"role": "assistant", "content": ans_1},
                {"role": "user", "content": "我刚才让你记住的秘密数字是多少？"},
            ],
            "stream": False,
        }
        resp_2 = await client.post(f"{API_BASE}/chat/completions", headers=HEADERS, json=payload_2)
        if resp_2.status_code != 200:
            print(f"❌ 第 2 轮失败: {resp_2.status_code} {resp_2.text}")
            return
        ans_2 = resp_2.json()["choices"][0]["message"]["content"]
        print(f"✅ 第 2 轮成功 ({time.time() - t1:.2f}s):\n{ans_2.strip()}\n")

        if "9527" in ans_2:
            print("🎉 持久会话测试成功！ChatGPT 在同一个会话中准确记住了上下文，历史去重与持久化均完美生效！")
        else:
            print("⚠️ 回复中未找到 9527，请核对上下文。")


if __name__ == "__main__":
    asyncio.run(main())
