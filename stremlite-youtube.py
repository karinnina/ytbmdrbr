import sys
import subprocess
import threading
import os
import signal
import time
import queue
import streamlit.components.v1 as components

# Install streamlit jika belum ada
try:
    import streamlit as st
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "streamlit"])
    import streamlit as st


# ============================================================
# GLOBAL STATE STREAMLIT
# ============================================================
if "ffmpeg_process" not in st.session_state:
    st.session_state["ffmpeg_process"] = None

if "ffmpeg_thread" not in st.session_state:
    st.session_state["ffmpeg_thread"] = None

if "streaming" not in st.session_state:
    st.session_state["streaming"] = False

if "log_queue" not in st.session_state:
    st.session_state["log_queue"] = queue.Queue()

if "logs" not in st.session_state:
    st.session_state["logs"] = []


def add_log(msg):
    """Masukkan log ke queue agar aman dipakai dari thread."""
    try:
        st.session_state["log_queue"].put(str(msg))
    except Exception:
        print(msg)


def drain_logs():
    """Ambil log dari queue lalu tampilkan di Streamlit."""
    try:
        while not st.session_state["log_queue"].empty():
            st.session_state["logs"].append(st.session_state["log_queue"].get_nowait())
    except Exception:
        pass

    return "\n".join(st.session_state["logs"][-30:])


def stop_ffmpeg():
    """Hentikan hanya proses FFmpeg milik aplikasi ini, bukan semua ffmpeg di server."""
    process = st.session_state.get("ffmpeg_process")

    if process and process.poll() is None:
        try:
            if os.name == "nt":
                process.terminate()
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)

            time.sleep(1)

            if process.poll() is None:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)

        except Exception as e:
            add_log(f"Error saat menghentikan FFmpeg: {e}")

    st.session_state["ffmpeg_process"] = None
    st.session_state["streaming"] = False


def run_ffmpeg(video_path, stream_key, is_vertical, log_callback):
    # ✅ Server YouTube Live (Secure RTMP)
    output_url = f"rtmps://a.rtmps.youtube.com/live2/{stream_key}"

    # ============================================================
    # FILTER VIDEO UNTUK LIVE LOOP SMOOTH
    # - fps stabil 30
    # - scale aman
    # - format yuv420p kompatibel YouTube
    # - setpts reset timestamp agar loop tidak patah
    # ============================================================
    if is_vertical:
        vf_filter = (
            "scale=720:1280:force_original_aspect_ratio=decrease,"
            "pad=720:1280:(ow-iw)/2:(oh-ih)/2,"
            "fps=30,format=yuv420p,setpts=N/(30*TB)"
        )
    else:
        vf_filter = (
            "scale=1280:720:force_original_aspect_ratio=decrease,"
            "pad=1280:720:(ow-iw)/2:(oh-ih)/2,"
            "fps=30,format=yuv420p,setpts=N/(30*TB)"
        )

    # ============================================================
    # COMMAND FFMPEG UNTUK LOOP LIVE LEBIH HALUS
    # Catatan:
    # - stream_loop -1 tetap dipakai agar fungsi loop tidak berubah
    # - -fflags +genpts membuat timestamp baru
    # - aresample async membantu audio tidak putus saat loop
    # - keyframe/GOP 60 cocok untuk 30fps (rekomendasi YouTube 2 detik)
    # - zerolatency + CBR membuat live lebih stabil
    # ============================================================
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "info",

        "-re",
        "-stream_loop", "-1",

        "-fflags", "+genpts",
        "-avoid_negative_ts", "make_zero",

        "-i", video_path,

        "-vf", vf_filter,

        "-c:v", "libx264",
        "-preset", "veryfast",
        "-tune", "zerolatency",
        "-profile:v", "main",
        "-level", "4.1",
        "-pix_fmt", "yuv420p",

        "-r", "30",
        "-g", "60",
        "-keyint_min", "60",
        "-sc_threshold", "0",

        "-b:v", "2500k",
        "-minrate", "2500k",
        "-maxrate", "2500k",
        "-bufsize", "5000k",

        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-ac", "2",
        "-af", "aresample=async=1:first_pts=0",

        "-flvflags", "no_duration_filesize",
        "-f", "flv",
        output_url
    ]

    log_callback(f"Menjalankan FFmpeg:\n{' '.join(cmd)}")

    try:
        popen_kwargs = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "bufsize": 1
        }

        if os.name != "nt":
            popen_kwargs["preexec_fn"] = os.setsid
        else:
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        process = subprocess.Popen(cmd, **popen_kwargs)
        st.session_state["ffmpeg_process"] = process

        for line in process.stdout:
            if line:
                log_callback(line.strip())

        process.wait()

    except Exception as e:
        log_callback(f"Error: {e}")

    finally:
        st.session_state["streaming"] = False
        st.session_state["ffmpeg_process"] = None
        log_callback("Streaming selesai atau dihentikan.")


def main():
    st.set_page_config(
        page_title="Streaming YouTube Live",
        page_icon="🟥",
        layout="wide"
    )

    st.config.set_option("server.maxUploadSize", 1000)

    st.title("Live Streaming ke YouTube")

    show_ads = st.checkbox("Tampilkan Iklan", value=False)
    if show_ads:
        st.subheader("Iklan Sponsor")
        components.html(
            """
            <div style="background:#f0f2f6;padding:20px;border-radius:10px;text-align:center">
                <p style="color:#888">Iklan akan muncul di sini</p>
            </div>
            """,
            height=200
        )

    video_files = [
        f for f in os.listdir(".")
        if f.lower().endswith((".mp4", ".flv"))
    ]

    st.write("Video yang tersedia:")
    selected_video = st.selectbox("Pilih video", video_files) if video_files else None

    uploaded_file = st.file_uploader(
        "Atau upload video baru (mp4/flv - codec H264/AAC)",
        type=["mp4", "flv"]
    )

    if uploaded_file:
        with open(uploaded_file.name, "wb") as f:
            f.write(uploaded_file.read())
        st.success("Video berhasil diupload!")
        video_path = uploaded_file.name
    elif selected_video:
        video_path = selected_video
    else:
        video_path = None

    # ✅ Stream Key YouTube Live
    stream_key = st.text_input("YouTube Stream Key", type="password", help="Dapatkan dari YouTube Studio (Live Control Room)")
    is_vertical = st.checkbox("Mode Vertikal (YouTube Shorts Live - 720x1280)")

    col1, col2 = st.columns(2)

    with col1:
        start_clicked = st.button(
            "Mulai Streaming",
            disabled=st.session_state.get("streaming", False)
        )

    with col2:
        stop_clicked = st.button(
            "Hentikan Streaming",
            disabled=not st.session_state.get("streaming", False)
        )

    log_placeholder = st.empty()

    if start_clicked:
        if not video_path or not stream_key:
            st.error("Video dan stream key harus diisi!")
        else:
            st.session_state["streaming"] = True
            st.session_state["logs"] = []

            st.session_state["ffmpeg_thread"] = threading.Thread(
                target=run_ffmpeg,
                args=(video_path, stream_key, is_vertical, add_log),
                daemon=True
            )
            st.session_state["ffmpeg_thread"].start()
            st.success("Streaming dimulai ke YouTube!")

    if stop_clicked:
        stop_ffmpeg()
        st.warning("Streaming dihentikan!")

    current_logs = drain_logs()
    log_placeholder.text(current_logs)

    if st.session_state.get("streaming", False):
        st.info("Status: Streaming aktif")
    else:
        st.info("Status: Tidak streaming")


if __name__ == "__main__":
    main()