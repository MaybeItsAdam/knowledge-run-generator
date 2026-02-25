import requests
from bs4 import BeautifulSoup
import re

def scrape_knowledge_of_london_runs():
    """
    Scrape the 320 runs from knowledgeoflondon.com
    This returns a mapping of run_id -> list of streets.
    """
    url = "http://www.knowledgeoflondon.com/theruns.html"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
    except Exception as e:
        return {"error": str(e)}

    soup = BeautifulSoup(response.text, 'html.parser')
    
    # Looking for the run text. 
    # Usually it's in <pre> or specific <div> tags
    # This is a guestimate based on common patterns for this site
    content = soup.get_text()
    
    runs = {}
    # Pattern: "Run 1  Manor House Station to Gibson Square"
    # Followed by a list of streets
    # (Site specific logic would go here)
    
    return {"info": "Scraper skeleton implemented. Needs site-specific tuning."}

def parse_street_list_text(text):
    """
    Parse a block of text containing a list of streets.
    Useful for copy-pasting from the internet.
    """
    lines = text.split('\n')
    streets = []
    for line in lines:
        line = line.strip()
        if not line: continue
        # Remove numbers and leading/trailing punctuation
        clean = re.sub(r'^\d+\.?\s*', '', line)
        if clean:
            streets.append(clean.upper())
    return streets
