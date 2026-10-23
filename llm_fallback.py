"""
LLM Fallback Extractor for Snow Reports

Fires only when a resort's primary scraper triggers quarantine (parse failed).
Uses Claude Haiku to read page text and extract structured snow data.

Requires env var: ANTHROPIC_API_KEY
"""

import json
import logging
import os
import re
from typing import Optional

from bs4 import BeautifulSoup
from pydantic import BaseModel, field_validator


class SkiReportExtraction(BaseModel):
    """Fields extractable from a resort snow report page."""
    last_updated: Optional[str] = None
    snow_overnight: Optional[float] = None
    snow_24h_base: Optional[float] = None
    snow_24h_summit: Optional[float] = None
    snow_48h_base: Optional[float] = None
    snow_48h_summit: Optional[float] = None
    snow_7day: Optional[float] = None
    base_depth: Optional[float] = None
    summit_depth: Optional[float] = None
    temp_base: Optional[float] = None
    temp_summit: Optional[float] = None
    wind_speed: Optional[float] = None
    lifts_open: Optional[str] = None
    runs_open: Optional[str] = None
    conditions_surface: Optional[str] = None
    weather_current: Optional[str] = None
    comments: Optional[str] = None

    @field_validator('comments')
    @classmethod
    def cap_comments(cls, v):
        if v and len(v) > 400:
            return v[:400]
        return v


_SYSTEM_PROMPT = """\
You are a data extraction assistant. Extract snow report data from the provided page text.
Return ONLY a valid JSON object — no markdown fences, no explanation, no extra text.

Fields to extract (use null for any missing or unclear value):
- last_updated: the date/time the report was updated, as printed on the page (string)
- snow_overnight: overnight snowfall in inches (number)
- snow_24h_base: past 24-hour snowfall at base in inches (number)
- snow_24h_summit: past 24-hour snowfall at summit in inches (number)
- snow_48h_base: past 48-hour snowfall at base in inches (number)
- snow_48h_summit: past 48-hour snowfall at summit in inches (number)
- snow_7day: past 7-day snowfall total in inches (number)
- base_depth: base snow depth in inches (number)
- summit_depth: summit snow depth in inches (number)
- temp_base: base temperature in Fahrenheit (number)
- temp_summit: summit temperature in Fahrenheit (number)
- wind_speed: wind speed in mph (number)
- lifts_open: lifts open, as "X / Y" or just a count (string)
- runs_open: runs/trails open, as "X / Y" or just a count (string)
- conditions_surface: surface condition description (string, max 100 chars)
- weather_current: current weather description (string, max 100 chars)
- comments: brief condition notes or bulletin (string, max 400 chars)

Important: if the page shows "closed", "opening soon", or pre-season content with no snow data,
return all numeric fields as null and put any relevant message in comments.\
"""


def _page_to_text(html: str, max_chars: int = 6000) -> str:
    """Strip HTML to clean readable text for the LLM."""
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['script', 'style', 'svg', 'iframe', 'noscript', 'head']):
        tag.decompose()
    text = soup.get_text('\n', strip=True)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text[:max_chars]


def llm_extract(resort_name: str, page_html: str) -> Optional[dict]:
    """
    Extract snow report fields from page HTML using Claude Haiku.

    Returns a dict of extracted fields (None values excluded),
    or None if the API key is missing or extraction fails.
    """
    api_key = os.getenv('ANTHROPIC_API_KEY')
    if not api_key:
        logging.warning("LLM fallback skipped: ANTHROPIC_API_KEY not set")
        return None

    page_text = _page_to_text(page_html)
    if not page_text.strip():
        logging.warning(f"LLM fallback for {resort_name}: page text is empty after stripping")
        return None

    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)

        response = client.messages.create(
            model="claude-haiku-5-5",
            max_tokens=600,
            system=_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": f"Resort: {resort_name}\n\nPage text:\n{page_text}"
            }]
        )

        raw = response.content[0].text.strip()
        # Strip accidental markdown fences
        raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE).strip()
        raw = re.sub(r'\s*```$', '', raw, flags=re.MULTILINE).strip()

        parsed = json.loads(raw)
        report = SkiReportExtraction(**parsed)
        result = report.model_dump(exclude_none=True)

        logging.info(f"LLM fallback extracted {len(result)} fields for {resort_name}")
        return result

    except Exception as e:
        logging.warning(f"LLM fallback failed for {resort_name}: {e}")
        return None
