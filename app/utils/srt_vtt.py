import requests
import re

def convert_srt_to_vtt(srt_url: str) -> str:
    """Fetches SRT from URL and converts it to WebVTT format."""
    try:
        response = requests.get(srt_url)
        response.raise_for_status()
        srt_content = response.text

        # 1. Add the WEBVTT header
        vtt_content = "WEBVTT\n\n"
        
        # 2. Convert timestamp format from 00:00:01,000 to 00:00:01.000
        # SRT uses commas, VTT uses periods.
        vtt_content += re.sub(r'(\d{2}:\d{2}:\d{2}),(\d{3})', r'\1.\2', srt_content)
        
        return vtt_content
    except Exception as e:
        print(f"Subtitle conversion failed: {e}")
        return ""