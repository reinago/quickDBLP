import os
import requests
import re
from datetime import datetime

# Configuration
dblp_file = 'dblp.rdf.gz'
max_age_days = 14  # Maximum age of the file in days
dblp_dir = 'https://dblp.org/rdf/'

# Check if the file exists
download_from_dblp = False
if not os.path.exists(dblp_file):
    print(f"File {dblp_file} not found.")
    download_from_dblp = True
else:
    # Get the modification time of the local file
    filetime = datetime.fromtimestamp(os.path.getmtime(dblp_file))

    # Fetch the DBLP directory
    try:
        response = requests.get(dblp_dir)
        response.raise_for_status()
        contents = response.text
    except requests.RequestException as e:
        print(f"Error fetching DBLP directory: {e}")
        exit(1)

    # Search for the file's timestamp in the directory listing
    for line in contents.splitlines():
        if dblp_file in line:
            # Extract the timestamp from the directory listing
            match = re.search(r'>(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})\s*<', line)
            if match:
                year, month, day, hour, minute = map(int, match.groups())
                onlinetime = datetime(year, month, day, hour, minute)
                print(f"Online file timestamp: {onlinetime}")
                print(f"Local file timestamp: {filetime}")
                # Calculate the difference in days
                diff = (onlinetime - filetime).days
                if diff > max_age_days:
                    print(f"File {dblp_file} is too old ({diff} days).")
                    download_from_dblp = True
            break

# Download the file if needed
if download_from_dblp:
    print("Fetching remote file, please wait... ", end='', flush=True)
    try:
        data = requests.get(f"{dblp_dir}{dblp_file}")
        data.raise_for_status()
        with open(dblp_file, 'wb') as f:
            f.write(data.content)
        print("done.")
    except requests.RequestException as e:
        print(f"Error downloading {dblp_file}: {e}")
        exit(1)
else:
    print(f"File {dblp_file} is up to date (less than {max_age_days} days old).")