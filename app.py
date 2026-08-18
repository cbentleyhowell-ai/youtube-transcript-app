import streamlit as st
import io
import re
import time
import zipfile
from yt_dlp import YoutubeDL
from youtube_transcript_api import YouTubeTranscriptApi

# NotebookLM File Safety Limits
MAX_WORDS_PER_FILE = 450000  # Safe threshold under NotebookLM's 500k limit
MAX_CHARS_PER_FILE = 2200000 # Safe character cap

st.set_page_config(page_title="NotebookLM Transcript Bundler", page_icon="📝", layout="wide")
st.title("📝 Advanced YouTube Transcript Bundler for NotebookLM")
st.markdown("Extract transcripts from channels, playlists, or individual links into text files structured for NotebookLM.")

# Input Mode Selection
mode = st.radio("Select Input Type:", ["Playlist or Channel URL", "Multiple Individual Video Links"])

urls_to_process = []
max_videos = 1000

if mode == "Playlist or Channel URL":
    channel_url = st.text_input("Paste YouTube Playlist or Channel URL:")
    max_videos = st.number_input("Maximum videos to pull:", min_value=1, max_value=2000, value=100)
    if channel_url.strip():
        urls_to_process.append(channel_url.strip())
else:
    multi_links = st.text_area("Paste individual YouTube video links (one per line):", height=150)
    if multi_links.strip():
        urls_to_process = [line.strip() for line in multi_links.split("\n") if line.strip()]

doc_title = st.text_input("Base Output Filename (without .txt):", "NotebookLM_Transcript_Bundle")
clean_filler = st.checkbox("Clean caption clutter (remove [Music], [Applause], etc.)", value=True)

def extract_video_id(url_or_id):
    """Extract 11-character YouTube video ID cleanly."""
    if not url_or_id:
        return None
    if len(url_or_id) == 11 and re.match(r'^[a-zA-Z0-9_-]{11}$', url_or_id):
        return url_or_id
    match = re.search(r'(?:v=|\/)([a-zA-Z0-9_-]{11})', url_or_id)
    if match:
        return match.group(1)
    return None

def format_time(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours > 0 else f"{minutes:02d}:{secs:02d}"

def clean_transcript_text(text):
    if clean_filler:
        text = re.sub(r'\[.*?\]', '', text)
        text = re.sub(r'\(.*?\)', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
    return text

def fetch_ytdlp_subtitles(v_url):
    """Fallback transcript engine using yt-dlp to bypass cloud IP blocks."""
    ydl_opts = {
        'skip_download': True,
        'writesubtitles': True,
        'writeautomaticsub': True,
        'subtitleslangs': ['en', 'en-US', 'en-GB'],
        'quiet': True
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(v_url, download=False)
        subtitles = info.get('subtitles') or info.get('automatic_captions')
        if not subtitles:
            raise Exception("No subtitles found via web fallback.")
        
        # Grab first available English track
        for lang in ['en', 'en-US', 'en-GB']:
            if lang in subtitles:
                # Extract text entries from webvtt / json
                formats = subtitles[lang]
                for fmt in formats:
                    if fmt.get('ext') == 'json3':
                        import requests
                        res = requests.get(fmt['url']).json()
                        text_chunks = []
                        for event in res.get('events', []):
                            for seg in event.get('segs', []):
                                chunk = seg.get('utf8', '').strip()
                                if chunk and chunk != '\n':
                                    text_chunks.append(chunk)
                        full_text = " ".join(text_chunks)
                        if full_text:
                            return [{"start": 0, "duration": 0, "text": full_text}]
    raise Exception("Could not parse subtitle track via fallback.")

def get_video_transcript(v_id, v_url):
    """Dual-engine transcript fetcher (API primary -> yt-dlp fallback)."""
    # Engine 1: youtube-transcript-api
    try:
        yt = YouTubeTranscriptApi()
        try:
            transcript_list = yt.list(v_id)
            for t in transcript_list:
                if t.language_code.startswith('en'):
                    fetched = t.fetch()
                    return fetched.to_raw_data() if hasattr(fetched, 'to_raw_data') else fetched
            for t in transcript_list:
                fetched = t.fetch()
                return fetched.to_raw_data() if hasattr(fetched, 'to_raw_data') else fetched
        except Exception:
            pass

        fetched = yt.fetch(v_id, languages=['en', 'en-US', 'en-GB', 'a.en'])
        return fetched.to_raw_data() if hasattr(fetched, 'to_raw_data') else fetched
    except Exception:
        pass

    # Engine 2: Fallback to yt-dlp browser-mimicking extractor
    return fetch_ytdlp_subtitles(v_url)

if st.button("🚀 Process & Generate Text Files"):
    if not urls_to_process:
        st.error("Please provide at least one valid URL!")
    else:
        st.info("Gathering video list from YouTube...")
        video_entries = []

        ydl_opts = {
            'extract_flat': 'in_playlist',
            'skip_download': True,
            'quiet': True,
            'ignoreerrors': True
        }

        with YoutubeDL(ydl_opts) as ydl:
            for url in urls_to_process:
                try:
                    info = ydl.extract_info(url, download=False)
                    if info:
                        if 'entries' in info:
                            for entry in list(info['entries'])[:max_videos]:
                                if entry:
                                    video_entries.append(entry)
                        else:
                            video_entries.append(info)
                except Exception as e:
                    st.warning(f"Could not extract info for {url}: {e}")

        if not video_entries:
            st.error("No valid video links could be extracted from your input.")
        else:
            st.success(f"Found {len(video_entries)} video entry/entries. Fetching transcripts...")
            
            output_files = {}
            file_part = 1
            current_words = 0
            current_chars = 0
            
            current_bundle = f"{'='*80}\nNOTEBOOK LM SOURCE BUNDLE: {doc_title} (Part {file_part:02d})\n{'='*80}\n\n"
            progress_bar = st.progress(0)
            successful_count = 0

            for idx, entry in enumerate(video_entries):
                time.sleep(1.5)  # Pause to avoid rapid-fire cloud blocking

                raw_id = entry.get('id') or entry.get('url') or entry.get('webpage_url')
                v_id = extract_video_id(str(raw_id)) if raw_id else None

                v_title = entry.get('title') or entry.get('fulltitle') or f"Video {idx+1}"
                v_channel = entry.get('uploader') or entry.get('channel') or 'Unknown Channel'
                v_url = f"https://www.youtube.com/watch?v={v_id}" if v_id else "N/A"
                v_desc = entry.get('description') or 'No description available.'

                if not v_id:
                    st.write(f"⚠️ Skipped '{v_title}' (Could not determine Video ID)")
                    progress_bar.progress((idx + 1) / len(video_entries))
                    continue

                try:
                    transcript = get_video_transcript(v_id, v_url)
                    
                    start_time = format_time(transcript[0]['start'])
                    end_time = format_time(transcript[-1]['start'] + transcript[-1]['duration']) if transcript[-1]['duration'] > 0 else "End"
                    
                    raw_text = " ".join([item['text'] for item in transcript])
                    full_transcript_text = clean_transcript_text(raw_text)

                    # Build transcript chunk
                    video_block = f"--- START OF TRANSCRIPT ---\n"
                    video_block += f"CHANNEL NAME: {v_channel}\n"
                    video_block += f"VIDEO TITLE: {v_title}\n"
                    video_block += f"VIDEO URL: {v_url}\n"
                    video_block += f"VIDEO TIMEFRAME: Starts at {start_time} | Ends at {end_time}\n"
                    video_block += f"\n--- VIDEO DESCRIPTION ---\n{v_desc}\n"
                    video_block += f"\n--- FULL TRANSCRIPT CONTENT ---\n{full_transcript_text}\n"
                    video_block += f"--- END OF TRANSCRIPT ---\n\n\n"

                    block_words = len(video_block.split())
                    block_chars = len(video_block)

                    # Check limits
                    if (current_words + block_words > MAX_WORDS_PER_FILE) or (current_chars + block_chars > MAX_CHARS_PER_FILE):
                        file_name = f"{doc_title}_Part_{file_part:02d}.txt"
                        output_files[file_name] = current_bundle
                        
                        file_part += 1
                        current_bundle = f"{'='*80}\nNOTEBOOK LM SOURCE BUNDLE: {doc_title} (Part {file_part:02d})\n{'='*80}\n\n"
                        current_words = 0
                        current_chars = 0

                    current_bundle += video_block
                    current_words += block_words
                    current_chars += block_chars
                    successful_count += 1

                except Exception as err:
                    st.write(f"⚠️ Skipped '{v_title}' (Reason: {err})")

                progress_bar.progress((idx + 1) / len(video_entries))

            # Save remaining content
            if current_words > 0:
                file_name = f"{doc_title}_Part_{file_part:02d}.txt" if file_part > 1 else f"{doc_title}.txt"
                output_files[file_name] = current_bundle

            # Render Download Buttons
            if successful_count > 0:
                st.success(f"Done! Successfully compiled {successful_count} transcript(s) into {len(output_files)} document(s).")

                if len(output_files) == 1:
                    filename, content = list(output_files.items())[0]
                    st.download_button(
                        label=f"💾 Download {filename}",
                        data=content,
                        file_name=filename,
                        mime="text/plain"
                    )
                else:
                    zip_buffer = io.BytesIO()
                    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
                        for fname, fcontent in output_files.items():
                            zip_file.writestr(fname, fcontent)
                    
                    st.download_button(
                        label=f"📦 Download All {len(output_files)} Files as ZIP",
                        data=zip_buffer.getvalue(),
                        file_name=f"{doc_title}_All_Parts.zip",
                        mime="application/zip"
                    )
            else:
                st.error("Could not retrieve captions for any videos in this set.")