#!/usr/bin/env python3
"""
Get a short title for an image using OpenAI's GPT Vision API.

Usage:
    python describe_image.py <image_path_or_url>

Requirements:
    pip install openai
"""

import base64
import sys
from openai import OpenAI
import config


def get_image_title(image_source: str) -> str:
    """Get a short title (5 words or less) for an image."""
    client = OpenAI(api_key=config.OPENAI_API_KEY)

    # Check if URL or local file
    if image_source.startswith(("http://", "https://")):
        image_content = {
            "type": "image_url",
            "image_url": {"url": image_source, "detail": "low"},
        }
    else:
        # Local file - encode as base64
        with open(image_source, "rb") as f:
            base64_image = base64.b64encode(f.read()).decode("utf-8")
        
        ext = image_source.lower().split(".")[-1]
        media_type = "image/png" if ext == "png" else "image/jpeg"
        
        image_content = {
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{base64_image}", "detail": "low"},
        }

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Give a short title for this image in 5 words or less. Reply with only the title, nothing else."},
                    image_content,
                ],
            }
        ],
        max_tokens=20,
        temperature=0,
    )

    return response.choices[0].message.content.strip().strip('"').strip("'")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python describe_image.py <image_path_or_url>")
        sys.exit(1)
    
    print(get_image_title(sys.argv[1]))