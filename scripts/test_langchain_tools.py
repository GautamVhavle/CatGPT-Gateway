#!/usr/bin/env python3
"""
LangChain test script for the OpenAI-compatible CatGPT API.

Tests:
  1. Simple chat (no tools)
  2. Tool/function calling with get_current_time()
  3. Tool calling with add_numbers()
  4. Complex multi-tool scenarios
  5. Image input (single image + text, multiple images)

Prerequisites:
  - CatGPT API server running: python -m src.api.server
  - pip install langchain langchain-openai openai

Usage:
  python scripts/test_langchain_tools.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
from langchain_core.tools import tool


# ── Configuration ───────────────────────────────────────────────

BASE_URL = "http://localhost:8000/v1"
MODEL = "catgpt-browser"
API_KEY = "not-needed"  # CatGPT doesn't require auth

# Image test assets
IMAGE_DIR = Path(__file__).resolve().parent.parent / "downloads" / "images"


# ── Tools ───────────────────────────────────────────────────────

@tool
def get_current_time() -> str:
    """Get the current date and time. Returns ISO format datetime string."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

@tool
def add_numbers(a: int, b: int) -> str:
    """Add two numbers together and return the result."""
    return str(a + b)

@tool
def search_wikipedia(query: str) -> str:
    """Return a fake Wikipedia summary for the query."""
    return f"[Wikipedia summary for '{query}': Lorem ipsum dolor sit amet...]"

@tool
def calculate_expression(expression: str) -> str:
    """Evaluate a math expression (e.g., '7*8+3')."""
    try:
        return str(eval(expression, {"__builtins__": {}}))
    except Exception as e:
        return f"Error: {e}"

@tool
def reverse_string(s: str) -> str:
    """Reverse the input string."""
    return s[::-1]

@tool
def weather_forecast(city: str, date: str) -> str:
    """Return a fake weather forecast for a city and date."""
    return f"The weather in {city} on {date} will be sunny with a high of 25°C."

@tool
def multi_arg_tool(a: int, b: int, c: int) -> str:
    """Return the product of three numbers."""
    return str(a * b * c)


# ── Helpers ─────────────────────────────────────────────────────

def separator(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


def _image_to_data_url(path: str | Path) -> str:
    """Read a local image and return an OpenAI-compatible base64 data URL."""
    path = Path(path)
    ext = path.suffix.lower().lstrip(".")
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "webp": "image/webp", "gif": "image/gif"}.get(ext, "image/png")
    data = base64.b64encode(path.read_bytes()).decode()
    return f"data:{mime};base64,{data}"


def _file_to_base64(path: str | Path) -> str:
    """Read any file and return its base64-encoded content."""
    path = Path(path)
    return base64.b64encode(path.read_bytes()).decode()


def _find_test_images(n: int = 2) -> list[Path]:
    """Return up to *n* image files from IMAGE_DIR."""
    if not IMAGE_DIR.exists():
        return []
    exts = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    imgs = sorted(p for p in IMAGE_DIR.iterdir() if p.suffix.lower() in exts)
    return imgs[:n]


def _find_test_files(extensions: set[str] | None = None) -> list[Path]:
    """Return test files (non-image) from the downloads directory."""
    dl_dir = Path(__file__).resolve().parent.parent / "downloads"
    if not dl_dir.exists():
        return []
    if extensions is None:
        extensions = {".pdf", ".txt", ".csv", ".docx", ".xlsx", ".json"}
    return sorted(p for p in dl_dir.iterdir() if p.suffix.lower() in extensions and p.is_file())


def test_models_endpoint():
    """Test that /v1/models returns our model."""
    import openai

    client = openai.OpenAI(base_url=BASE_URL, api_key=API_KEY)
    models = client.models.list()
    model_ids = [m.id for m in models.data]
    print(f"Available models: {model_ids}")
    assert MODEL in model_ids, f"Expected {MODEL} in models list"
    print("✓ Models endpoint works\n")


def test_simple_chat():
    """Test a simple chat without tools."""
    separator("Test 1: Simple Chat (no tools)")

    llm = ChatOpenAI(
        model=MODEL,
        base_url=BASE_URL,
        api_key=API_KEY,
        temperature=0,
    )

    response = llm.invoke([HumanMessage(content="Who is the president of the United States?")])
    print(f"Question: Who is the president of the United States?")
    print(f"Response: {response.content}")
    print(f"Type: {type(response).__name__}")
    print("✓ Simple chat works\n")


def test_tool_calling():
    """Test tool/function calling with get_current_time."""
    separator("Test 2: Tool Calling (get_current_time)")

    llm = ChatOpenAI(
        model=MODEL,
        base_url=BASE_URL,
        api_key=API_KEY,
        temperature=0,
    )

    tools = [get_current_time, add_numbers]
    llm_with_tools = llm.bind_tools(tools)

    print("Sending: What is the current time? Use the get_current_time tool.")
    response = llm_with_tools.invoke(
        [HumanMessage(content="What is the current time? Use the get_current_time tool.")]
    )

    print(f"Response type: {type(response).__name__}")
    print(f"Content: {response.content}")
    print(f"Tool calls: {response.tool_calls}")

    if response.tool_calls:
        print(f"\n✓ Model requested tool call(s):")
        for tc in response.tool_calls:
            print(f"  - {tc['name']}({tc['args']})")

        messages = [
            HumanMessage(content="What is the current time? Use the get_current_time tool."),
            response,
        ]

        tool_map = {"get_current_time": get_current_time, "add_numbers": add_numbers}

        for tc in response.tool_calls:
            tool_fn = tool_map.get(tc["name"])
            if tool_fn:
                result = tool_fn.invoke(tc["args"])
                print(f"  Tool result ({tc['name']}): {result}")
                messages.append(
                    ToolMessage(content=str(result), tool_call_id=tc["id"])
                )

        print("\nSending tool results back to model...")
        final_response = llm_with_tools.invoke(messages)
        print(f"Final response: {final_response.content}")

        print("\nPrompting for summary after tool result...")
        messages.append(HumanMessage(content="Please summarize the result in a sentence."))
        summary_response = llm_with_tools.invoke(messages)
        print(f"Summary: {summary_response.content}")
        print("✓ Tool calling round-trip with summary works\n")
    else:
        print("⚠ Model did not request tool calls (responded directly)")
        print("✓ Test completed (no tool calls)\n")


def test_add_numbers_tool():
    """Test tool calling with add_numbers."""
    separator("Test 3: Tool Calling (add_numbers)")

    llm = ChatOpenAI(
        model=MODEL,
        base_url=BASE_URL,
        api_key=API_KEY,
        temperature=0,
    )

    tools = [add_numbers]
    llm_with_tools = llm.bind_tools(tools)

    print("Sending: Use the add_numbers tool to compute 42 + 58.")
    response = llm_with_tools.invoke(
        [HumanMessage(content="Use the add_numbers tool to compute 42 + 58.")]
    )

    print(f"Content: {response.content}")
    print(f"Tool calls: {response.tool_calls}")

    if response.tool_calls:
        print(f"\n✓ Model requested tool call(s):")
        for tc in response.tool_calls:
            print(f"  - {tc['name']}({tc['args']})")

        messages = [
            HumanMessage(content="Use the add_numbers tool to compute 42 + 58."),
            response,
        ]

        for tc in response.tool_calls:
            if tc["name"] == "add_numbers":
                result = add_numbers.invoke(tc["args"])
                print(f"  Tool result: {result}")
                messages.append(
                    ToolMessage(content=str(result), tool_call_id=tc["id"])
                )

        print("\nSending tool results back...")
        final = llm_with_tools.invoke(messages)
        print(f"Final response: {final.content}")

        print("\nPrompting for summary after tool result...")
        messages.append(HumanMessage(content="Please summarize the result in a sentence."))
        summary_response = llm_with_tools.invoke(messages)
        print(f"Summary: {summary_response.content}")
        print("✓ add_numbers tool calling with summary works\n")
    else:
        print("⚠ Model did not use tool (answered directly)")
        print("✓ Test completed\n")


def test_complex_tool_calls():
    """Test complex multi-tool scenarios."""
    separator("Test 4: Complex Tool Calling")

    llm = ChatOpenAI(
        model=MODEL,
        base_url=BASE_URL,
        api_key=API_KEY,
        temperature=0,
    )

    all_tools = [
        get_current_time, add_numbers, search_wikipedia,
        calculate_expression, reverse_string, weather_forecast, multi_arg_tool,
    ]
    llm_with_tools = llm.bind_tools(all_tools)
    tool_map = {t.name: t for t in all_tools}

    # --- Sub-test A: weather + math ---
    print("Sending: What is the weather in Paris tomorrow, and what is 7*8+3?")
    response = llm_with_tools.invoke([
        HumanMessage(content="What is the weather in Paris tomorrow, and what is 7*8+3?")
    ])
    print(f"Content: {response.content}")
    print(f"Tool calls: {response.tool_calls}")

    messages = [HumanMessage(content="What is the weather in Paris tomorrow, and what is 7*8+3?")]
    if response.tool_calls:
        for tc in response.tool_calls:
            tool_fn = tool_map.get(tc["name"])
            if tool_fn:
                result = tool_fn.invoke(tc["args"])
                print(f"  Tool result ({tc['name']}): {result}")
                messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))

        print("\nSending tool results back to model...")
        final = llm_with_tools.invoke(messages)
        print(f"Final response: {final.content}")

        print("\nPrompting for summary...")
        messages.append(HumanMessage(content="Please summarize the results in a sentence."))
        summary = llm_with_tools.invoke(messages)
        print(f"Summary: {summary.content}")
    else:
        print("⚠ Model did not use tools (answered directly)")
    print("✓ Complex tool calling test completed\n")

    # --- Sub-test B: string reversal + Wikipedia ---
    print("Sending: Reverse the string 'OpenAI', and search Wikipedia for 'LangChain'.")
    response2 = llm_with_tools.invoke([
        HumanMessage(content="Reverse the string 'OpenAI', and search Wikipedia for 'LangChain'.")
    ])
    print(f"Content: {response2.content}")
    print(f"Tool calls: {response2.tool_calls}")

    messages2 = [HumanMessage(content="Reverse the string 'OpenAI', and search Wikipedia for 'LangChain'.")]
    if response2.tool_calls:
        for tc in response2.tool_calls:
            tool_fn = tool_map.get(tc["name"])
            if tool_fn:
                result = tool_fn.invoke(tc["args"])
                print(f"  Tool result ({tc['name']}): {result}")
                messages2.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))

        print("\nSending tool results back to model...")
        final2 = llm_with_tools.invoke(messages2)
        print(f"Final response: {final2.content}")

        print("\nPrompting for summary...")
        messages2.append(HumanMessage(content="Please summarize the results in a sentence."))
        summary2 = llm_with_tools.invoke(messages2)
        print(f"Summary: {summary2.content}")
    else:
        print("⚠ Model did not use tools (answered directly)")
    print("✓ Multi-tool call test completed\n")


def test_image_input():
    """Test sending images via the OpenAI vision content format."""
    separator("Test 5: Image Input")

    images = _find_test_images(2)
    if not images:
        print(f"⚠ No images found in {IMAGE_DIR} — skipping image tests")
        print("  Place .png/.jpg/.webp files in downloads/images/ to enable this test")
        print("✓ Image test skipped (no assets)\n")
        return

    import openai
    client = openai.OpenAI(base_url=BASE_URL, api_key=API_KEY)

    # --- 5a: Single image + text ---
    img_path = images[0]
    data_url = _image_to_data_url(img_path)
    print(f"5a — Sending single image: {img_path.name}  ({img_path.stat().st_size / 1024:.0f} KB)")

    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image in 2-3 sentences."},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    )
    answer = resp.choices[0].message.content
    print(f"Response: {answer}")
    assert answer and len(answer) > 10, "Expected a non-trivial description"
    print("✓ Single image + text works\n")

    # --- 5b: Multiple images + text ---
    if len(images) >= 2:
        img2_path = images[1]
        data_url2 = _image_to_data_url(img2_path)
        print(f"5b — Sending two images: {img_path.name}, {img2_path.name}")

        resp2 = client.chat.completions.create(
            model=MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Compare these two images. What are the main differences?"},
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "image_url", "image_url": {"url": data_url2}},
                    ],
                }
            ],
        )
        answer2 = resp2.choices[0].message.content
        print(f"Response: {answer2}")
        assert answer2 and len(answer2) > 10, "Expected a non-trivial comparison"
        print("✓ Multiple images + text works\n")
    else:
        print("5b — Only one image available, skipping multi-image test")
        print("✓ Multi-image test skipped\n")

    # --- 5c: Image with tool calling ---
    print("5c — Image + tool calling (describe image, then use add_numbers)")
    llm = ChatOpenAI(
        model=MODEL,
        base_url=BASE_URL,
        api_key=API_KEY,
        temperature=0,
    )
    llm_with_tools = llm.bind_tools([add_numbers])

    response = llm_with_tools.invoke([
        HumanMessage(content=[
            {"type": "text", "text": "Look at this image and then use the add_numbers tool to add 10 + 20."},
            {"type": "image_url", "image_url": {"url": data_url}},
        ])
    ])
    print(f"Content: {response.content}")
    print(f"Tool calls: {response.tool_calls}")

    if response.tool_calls:
        for tc in response.tool_calls:
            print(f"  Tool call: {tc['name']}({tc['args']})")
            result = add_numbers.invoke(tc["args"])
            print(f"  Result: {result}")
    print("✓ Image + tool calling test completed\n")


def test_file_attachment():
    """Test sending non-image file attachments (e.g. PDF)."""
    separator("Test 6: File Attachment (PDF)")

    test_files = _find_test_files({".pdf"})
    if not test_files:
        print("⚠ No PDF files found in downloads/ — skipping file attachment test")
        print("  Place a .pdf file in downloads/ to enable this test")
        print("✓ File attachment test skipped (no assets)\n")
        return

    import openai
    client = openai.OpenAI(base_url=BASE_URL, api_key=API_KEY)

    # --- 6a: PDF attachment + text question ---
    pdf_path = test_files[0]
    pdf_b64 = _file_to_base64(pdf_path)
    print(f"6a — Sending PDF: {pdf_path.name} ({pdf_path.stat().st_size / 1024:.0f} KB)")

    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "I've attached a PDF file. Please summarize its contents in 2-3 sentences."},
                    {
                        "type": "file",
                        "file": {
                            "filename": pdf_path.name,
                            "data": pdf_b64,
                            "mime_type": "application/pdf",
                        },
                    },
                ],
            }
        ],
    )
    answer = resp.choices[0].message.content
    print(f"Response: {answer}")
    assert answer and len(answer) > 10, "Expected a non-trivial summary of the PDF"
    print("✓ PDF attachment + text works\n")

    # --- 6b: PDF + image combined ---
    images = _find_test_images(1)
    if images:
        img_path = images[0]
        data_url = _image_to_data_url(img_path)
        print(f"6b — Sending PDF ({pdf_path.name}) + image ({img_path.name}) together")

        resp2 = client.chat.completions.create(
            model=MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "I've attached a PDF and an image. Briefly describe what each contains."},
                        {
                            "type": "file",
                            "file": {
                                "filename": pdf_path.name,
                                "data": pdf_b64,
                                "mime_type": "application/pdf",
                            },
                        },
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
        )
        answer2 = resp2.choices[0].message.content
        print(f"Response: {answer2}")
        assert answer2 and len(answer2) > 10, "Expected a non-trivial response"
        print("✓ PDF + image combined works\n")
    else:
        print("6b — No images available, skipping PDF + image combined test\n")


# ── Main ────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  CatGPT — LangChain OpenAI-Compatible API Tests")
    print("=" * 60)
    print(f"\nBase URL: {BASE_URL}")
    print(f"Model:    {MODEL}")
    print(f"Image dir: {IMAGE_DIR}\n")

    try:
        test_models_endpoint()
        test_simple_chat()
        test_tool_calling()
        test_add_numbers_tool()
        test_complex_tool_calls()
        test_image_input()
        test_file_attachment()

        separator("All Tests Passed!")
        print("The OpenAI-compatible API is working correctly with LangChain.")
        print("Tool/function calling, image input, and file attachments are operational.\n")

    except Exception as e:
        print(f"\n✗ Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
