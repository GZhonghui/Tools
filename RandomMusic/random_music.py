# 本代码由AI生成

import os
import random
import time
from mutagen import File # install this

def get_audio_length(filename):
    """Get the duration of an audio file in seconds."""
    audio = File(filename)
    if audio is not None:
        return audio.info.length
    else:
        raise ValueError("Unsupported file format")

def open_random_audio(folder_path):
    """Randomly select and open an audio file, retry getting the length if unavailable, wait x-5 seconds, then repeat."""
    while True:
        # Get all MP3 and FLAC files in the folder
        audio_files = [f for f in os.listdir(folder_path) if f.endswith(('.mp3', '.flac'))]
        
        # Check if there are audio files available
        if not audio_files:
            print("No MP3 or FLAC audio files found in the folder.")
            break

        # Randomly select an audio file
        chosen_file = random.choice(audio_files)
        file_path = os.path.join(folder_path, chosen_file)
        
        # Open the audio file
        os.system(f'open "{file_path}"')
        
        # Try to get the audio file's duration, retry every 5 seconds if unavailable
        while True:
            try:
                length = get_audio_length(file_path)
                wait_time = max(length - 5, 0)  # Ensure non-negative wait time
                print(f"Now playing: {chosen_file}, Duration: {length} seconds, Waiting {wait_time} seconds")
                time.sleep(wait_time)
                break  # Exit the retry loop once duration is retrieved
            except Exception as e:
                print(f"Could not retrieve audio file duration for {chosen_file}: {e}. Retrying in 5 seconds.")
                time.sleep(5)

# Example usage
folder_path = "/Users/anny/Dropbox/音频/单曲/Vol. 01"  # Replace with the path to your audio folder
open_random_audio(folder_path)
