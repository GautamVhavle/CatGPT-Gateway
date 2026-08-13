#!/usr/bin/env python3
"""
Test script for file attachment upload functionality in CatGPT-Gateway.
"""

import asyncio
import base64
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

# Create a sample test file
SAMPLE_CSV_CONTENT = """Product,Category,Q1_Sales,Q2_Sales,Status
SuperGPU-9000,Hardware,45000,68000,InStock
NeuralCore-X,Hardware,32000,51000,InStock
QuantumCompiler,Software,15000,28000,Beta
SecretKey-XYZ,Security,99999,99999,Classified
"""


async def main():
    print("📁 [文件上传测试] 正在准备测试附件 (CSV 数据文件)...")
    file_bytes = SAMPLE_CSV_CONTENT.encode("utf-8")
    b64_data = base64.b64encode(file_bytes).decode("utf-8")

    payload = {
        "model": "catgpt-browser",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "请分析我上传的 CSV 附件，回答以下两个问题：\n1. 销售额最高的 Product 是什么？\n2. 其中的 SecretKey-XYZ 的 Category 是什么？",
                    },
                    {
                        "type": "file",
                        "file": {
                            "filename": "quarterly_sales_report.csv",
                            "data": b64_data,
                            "mime_type": "text/csv",
                        },
                    },
                ],
            }
        ],
        "stream": False,
    }

    print("🚀 正在向网关发送包含附件的请求...")
    t0 = time.time()
    async with httpx.AsyncClient(trust_env=False, timeout=120.0) as client:
        resp = await client.post(f"{API_BASE}/chat/completions", headers=HEADERS, json=payload)
        elapsed = time.time() - t0
        if resp.status_code != 200:
            print(f"❌ 请求失败: {resp.status_code} {resp.text}")
            return

        ans = resp.json()["choices"][0]["message"]["content"]
        print(f"\n✅ 成功接收回答 (耗时 {elapsed:.2f}s):\n{ans}\n")

        # Verify ChatGPT read the file
        if "SecretKey" in ans or "Security" in ans or "SuperGPU" in ans or "99999" in ans:
            print("🎉 文件上传与解析实测成功！ChatGPT 成功读取了附件中的结构化数据！")
        else:
            print("⚠️ 未在回复中检测到文件中的关键词，请核对回复内容。")


if __name__ == "__main__":
    asyncio.run(main())
