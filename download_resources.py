import os
import requests

# Configuration
ASSETS_DIR = "assets"
FILES = {
    "tailwind.js": "https://cdn.tailwindcss.com",
    "chart.js": "https://cdn.jsdelivr.net/npm/chart.js",
    # Phosphor Icons
    "phosphor.css": "https://unpkg.com/@phosphor-icons/web@2.1.1/src/regular/style.css",
    "Phosphor.woff2": "https://unpkg.com/@phosphor-icons/web@2.1.1/src/regular/Phosphor.woff2",
    "Phosphor.ttf": "https://unpkg.com/@phosphor-icons/web@2.1.1/src/regular/Phosphor.ttf"
}

def download_assets():
    if not os.path.exists(ASSETS_DIR):
        os.makedirs(ASSETS_DIR)
        print(f"Created directory: {ASSETS_DIR}")

    print("Downloading assets...")
    
    for filename, url in FILES.items():
        filepath = os.path.join(ASSETS_DIR, filename)
        print(f"Fetching {filename}...")
        
        try:
            response = requests.get(url)
            response.raise_for_status()
            
            content = response.content
            
            # Fix Phosphor CSS to look for fonts in the same folder
            if filename == "phosphor.css":
                text_content = response.text
                # The original CSS might look for url('./Phosphor.woff2') which is fine
                # We ensure it's saved correctly
                content = text_content.encode('utf-8')

            with open(filepath, "wb") as f:
                f.write(content)
            print(f"Saved {filepath}")
            
        except Exception as e:
            print(f"Failed to download {filename}: {e}")

    print("\nDone! Assets are ready.")

if __name__ == "__main__":
    download_assets()