import webview
import os
import sys
from backend import ExpenseManager

# Ensure we can find the backend if running as script
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)

def main():
    # Initialize the API with your default cache file
    api = ExpenseManager("cache.txt")
    # api.replace_database_file(r"C:\Users\lenovo\MyData\mlog.txt")

    # Create the window
    window = webview.create_window(
        title='Financial Journal',
        url='index.html',
        js_api=api,
        width=1200,
        height=800,
        resizable=True,
        min_size=(800, 600),
        background_color='#f8fafc' # Matches Tailwind slate-50
    )

    # Start the application
    webview.start(debug=True) # debug=True enables Right Click -> Inspect Element

if __name__ == '__main__':
    main()