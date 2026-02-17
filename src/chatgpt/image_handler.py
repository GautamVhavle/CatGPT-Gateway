"""
Image handler — detects, extracts, and downloads generated images.

When ChatGPT generates an image via DALL-E, the response contains:
- An <img> tag with the image URL (hosted on openai.com)
- A "Image created" text indicator
- An image title/alt text (description of what was generated)

This module:
1. Detects if the last assistant message contains generated images
2. Extracts image URLs and metadata
3. Downloads images to local disk
4. Returns ImageInfo objects with URLs and local paths
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from patchright.async_api import Page

from src.config import Config
from src.selectors import Selectors
from src.chatgpt.models import ImageInfo
from src.log import setup_logging

log = setup_logging("image_handler")


async def detect_images_in_response(page: Page) -> list[dict]:
    """
    Check the last conversation turn for generated images.

    ChatGPT DALL-E image responses do NOT use data-message-author-role.
    Instead, images appear inside an article turn with:
    - img[alt="Generated image"]
    - div[id^="image-"] containers
    - src from chatgpt.com/backend-api/estuary/content

    Returns a list of dicts: [{url, alt, title}, ...] or empty list.
    """
    result = await page.evaluate("""
        () => {
            const articles = document.querySelectorAll('article');
            if (articles.length === 0) return [];

            const lastTurn = articles[articles.length - 1];

            // Find generated images — primary: alt="Generated image"
            let images = lastTurn.querySelectorAll('img[alt="Generated image"]');

            // Fallback: images inside imagegen containers
            if (images.length === 0) {
                const containers = lastTurn.querySelectorAll('div[id^="image-"]');
                if (containers.length > 0) {
                    const imgSet = new Set();
                    for (const c of containers) {
                        const imgs = c.querySelectorAll('img');
                        for (const img of imgs) imgSet.add(img);
                    }
                    images = [...imgSet];
                }
            }

            // Fallback: any large image from chatgpt backend
            if (images.length === 0) {
                const allImgs = lastTurn.querySelectorAll('img');
                const large = [];
                for (const img of allImgs) {
                    const w = img.naturalWidth || img.width || 0;
                    const src = img.src || '';
                    if (w > 200 && (
                        src.includes('backend-api/estuary') ||
                        src.includes('chatgpt.com')
                    )) {
                        large.push(img);
                    }
                }
                images = large;
            }

            if (!images || images.length === 0) return [];

            // Deduplicate by src URL
            const seen = new Set();
            const results = [];

            for (const img of images) {
                const src = img.src || '';
                if (!src || seen.has(src)) continue;
                seen.add(src);

                const alt = img.alt || '';

                // Extract the image title from nearby text in the turn
                // ChatGPT shows "Creating image • Image Title" in a button/span
                let title = '';
                const buttons = lastTurn.querySelectorAll('button');
                for (const btn of buttons) {
                    const text = (btn.innerText || '').trim();
                    // Parse "Creating image • Title" or just "Title"
                    const bulletIdx = text.indexOf('•');
                    if (bulletIdx > -1) {
                        title = text.substring(bulletIdx + 1).trim();
                        break;
                    }
                }
                // Fallback: look for text spans in the turn
                if (!title) {
                    const spans = lastTurn.querySelectorAll(
                        'span.text-token-text-tertiary'
                    );
                    for (const span of spans) {
                        const t = (span.innerText || '').trim();
                        if (t.length > 5 && t.length < 200) {
                            title = t;
                            break;
                        }
                    }
                }

                results.push({ url: src, alt, title });
            }

            return results;
        }
    """)

    if result:
        log.info(f"Detected {len(result)} generated image(s) in response")
        for i, img in enumerate(result):
            log.debug(f"  Image {i+1}: alt='{img.get('alt', '')[:50]}', url={img.get('url', '')[:80]}...")
    else:
        log.debug("No generated images detected in response")

    return result or []


async def download_image(page: Page, url: str, filename_hint: str = "") -> str:
    """
    Download an image from a URL using the browser's fetch API.

    Uses the browser context so cookies/auth are preserved (required
    for OpenAI-hosted images that may need authentication).

    Returns the local file path.
    """
    Config.ensure_dirs()

    # Generate a filename from the URL or hint
    if filename_hint:
        # Clean the hint for use as filename
        safe_name = re.sub(r'[^\w\s-]', '', filename_hint)[:60].strip()
        safe_name = re.sub(r'\s+', '_', safe_name)
    else:
        # Use hash of URL as filename
        safe_name = hashlib.md5(url.encode()).hexdigest()[:12]

    # Add timestamp to avoid collisions
    ts = int(time.time())
    filename = f"{safe_name}_{ts}.png"
    local_path = Config.IMAGES_DIR / filename

    log.info(f"Downloading image to {local_path}...")

    try:
        # Use browser's fetch to download (preserves auth cookies)
        image_data = await page.evaluate("""
            async (url) => {
                try {
                    const response = await fetch(url);
                    if (!response.ok) return null;
                    const blob = await response.blob();
                    const reader = new FileReader();
                    return new Promise((resolve) => {
                        reader.onloadend = () => resolve(reader.result);
                        reader.readAsDataURL(blob);
                    });
                } catch (e) {
                    return null;
                }
            }
        """, url)

        if image_data and image_data.startswith("data:"):
            # Strip the data URL prefix to get raw base64
            import base64
            header, b64data = image_data.split(",", 1)

            # Detect actual format from MIME type
            if "png" in header:
                ext = ".png"
            elif "jpeg" in header or "jpg" in header:
                ext = ".jpg"
            elif "webp" in header:
                ext = ".webp"
            else:
                ext = ".png"

            # Update filename with correct extension
            filename = f"{safe_name}_{ts}{ext}"
            local_path = Config.IMAGES_DIR / filename

            raw_bytes = base64.b64decode(b64data)
            local_path.write_bytes(raw_bytes)

            size_kb = len(raw_bytes) / 1024
            log.info(f"Image saved: {local_path} ({size_kb:.1f} KB)")
            return str(local_path)

        else:
            log.warning("Failed to fetch image data via browser")

    except Exception as e:
        log.error(f"Image download failed: {e}", exc_info=True)

    # Fallback: try using the page to download via navigation
    # (less reliable but works for some cases)
    try:
        import urllib.request
        urllib.request.urlretrieve(url, str(local_path))
        log.info(f"Image saved via urllib: {local_path}")
        return str(local_path)
    except Exception as e2:
        log.error(f"Fallback download also failed: {e2}")

    return ""


async def extract_images_from_response(page: Page) -> list[ImageInfo]:
    """
    Full pipeline: detect images in the last response, download them,
    and return ImageInfo objects with both URLs and local paths.
    """
    raw_images = await detect_images_in_response(page)

    if not raw_images:
        return []

    image_infos = []
    for img_data in raw_images:
        url = img_data.get("url", "")
        alt = img_data.get("alt", "")
        title = img_data.get("title", "")

        # Download the image
        hint = alt or title or "chatgpt_image"
        local_path = await download_image(page, url, filename_hint=hint)

        image_infos.append(ImageInfo(
            url=url,
            alt=alt,
            local_path=local_path,
            prompt_title=title,
        ))

    log.info(f"Processed {len(image_infos)} image(s)")
    return image_infos
