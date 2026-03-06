"""
view_call_log.py - Pretty-print the last call's flow from Docker logs.

Usage:
  python view_call_log.py           # Show last call flow
  python view_call_log.py --raw     # Also show raw matching lines
  python view_call_log.py --lines N # Parse last N docker log lines (default 300)
"""

import subprocess, re, argparse, sys

# ANSI colors
C = {
    "R": "\033[0m",       # Reset
    "BOLD": "\033[1m",
    "CYAN": "\033[96m",   # Section headers
    "GREEN": "\033[92m",  # Success / AI response
    "YELLOW": "\033[93m", # User speech
    "RED": "\033[91m",    # Errors
    "BLUE": "\033[94m",   # Info
    "DIM": "\033[2m",     # Dim text
    "MAG": "\033[95m",    # Synthesizer
}

def get_logs(n=300):
    result = subprocess.run(
        ["docker", "compose", "logs", "bolna-app", "--tail", str(n)],
        capture_output=True, text=True, cwd="."
    )
    return result.stdout + result.stderr

def parse_and_display(log_text, show_raw=False):
    lines = log_text.split("\n")
    events = []

    for line in lines:
        # Extract timestamp
        ts_match = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)", line)
        ts = ts_match.group(1).split(" ")[1][:12] if ts_match else ""

        # ── Call Start ──
        if "Connected to ws" in line:
            events.append(("CALL", ts, "📞 Call Connected (WebSocket established)"))

        elif "Retrieved agent config" in line:
            events.append(("CALL", ts, "⚙️  Agent config loaded from Redis"))

        # ── Transcriber / STT ──
        elif "speech_started" in line and "HTTP transcription" in line:
            events.append(("STT", ts, "🎤 User started speaking"))

        elif "speech_ended" in line and "HTTP transcription" in line:
            events.append(("STT", ts, "🎤 User stopped speaking"))

        elif "Sarvam transcriber connection established" in line:
            events.append(("STT", ts, "🔌 Sarvam STT connected"))

        elif "'type': 'transcript'" in line or "'type': 'speech_final'" in line:
            content_match = re.search(r"'content':\s*'([^']*)'", line)
            content = content_match.group(1) if content_match else "?"
            events.append(("STT", ts, f"📝 Transcript: \"{content}\""))

        elif "'type': 'interim_transcript_received'" in line:
            content_match = re.search(r"'content':\s*'([^']*)'", line)
            content = content_match.group(1) if content_match else "?"
            events.append(("STT_INTERIM", ts, f"📝 Interim: \"{content}\""))

        elif "Received transcript, sending for further processing" in line:
            events.append(("STT", ts, "✅ Transcript sent to pipeline"))

        elif "false interruption" in line:
            content_match = re.search(r"transcript received \((.+?)\)", line)
            content = content_match.group(1) if content_match else "?"
            events.append(("STT", ts, f"⚠️  False interruption ignored: \"{content}\""))

        elif "HTTP transcription: ignoring" in line:
            events.append(("STT", ts, "⚠️  Non-transcript message filtered"))

        elif "Transcriber connection has been closed" in line:
            events.append(("STT", ts, "🔌 STT connection closed"))

        # ── LLM ──
        elif "Running llm Tasks" in line:
            events.append(("LLM", ts, "🧠 LLM processing started"))

        elif "__do_llm_generation" in line and "Got a response from LLM" in line:
            resp_match = re.search(r"Got a response from LLM\s+(.+?)(?:\s+\{|$)", line)
            resp = resp_match.group(1).strip() if resp_match else "?"
            events.append(("LLM", ts, f"🤖 LLM Response: \"{resp}\""))

        elif "Cancelling existing LLM task" in line:
            events.append(("LLM", ts, "⚠️  Previous LLM task cancelled (new speech)"))

        # ── Synthesizer / TTS ──
        elif "_synthesize" in line and "not a valid sequence" in line:
            seq_match = re.search(r"(\d+) is not a valid sequence", line)
            seq = seq_match.group(1) if seq_match else "?"
            events.append(("TTS", ts, f"❌ Audio DISCARDED (sequence {seq} invalidated)"))

        elif "sending preprocessed audio" in line:
            events.append(("TTS", ts, "🔊 Sending cached/preprocessed audio"))

        elif "Listening to synthesizer" in line and events and events[-1][0] != "TTS_LISTEN":
            events.append(("TTS_LISTEN", ts, "🔊 Synthesizer processing audio..."))

        elif "Stream not enabled, sending entire audio" in line:
            events.append(("TTS", ts, "🔊 Sending synthesized audio (HTTP mode)"))

        elif "Processing message with sequence_id" in line:
            seq_match = re.search(r"sequence_id:\s*(\d+)", line)
            seq = seq_match.group(1) if seq_match else "?"
            events.append(("TTS", ts, f"🔊 Audio accepted (sequence {seq})"))

        elif "end of synthesizer stream" in line or "end_of_synthesizer_stream" in line:
            events.append(("TTS", ts, "🔊 TTS stream complete"))

        elif "Sarvam TTS" in line and "Connected" in line:
            events.append(("TTS", ts, "🔌 Sarvam TTS connected"))

        elif "cleaning sarvam synthesizer" in line:
            events.append(("TTS", ts, "🔌 Sarvam TTS cleanup"))

        # ── Interruption ──
        elif "Interruption triggered" in line:
            events.append(("INT", ts, "⚡ Interruption triggered"))

        elif "Pending responses invalidated" in line:
            events.append(("INT", ts, "⚡ All pending audio invalidated"))

        elif "User started speaking" in line and "interruption_manager" in line:
            events.append(("INT", ts, "🎤 User speech detected (interruption check)"))

        # ── Output ──
        elif "welcome_message" in line.lower() and "played" in line.lower():
            events.append(("OUT", ts, "🔊 Welcome message played"))

        # ── Call End ──
        elif "Conversation completed" in line:
            events.append(("CALL", ts, "📞 Call completed"))

        elif "connection closed" in line and "INFO:" in line:
            events.append(("CALL", ts, "📞 Connection closed"))

        elif "'messages'" in line and "'conversation_time'" in line:
            time_match = re.search(r"'conversation_time':\s*([\d.]+)", line)
            chars_match = re.search(r"'synthesizer_characters':\s*(\d+)", line)
            ended_match = re.search(r"'ended_by_assistant':\s*(True|False)", line)
            dur = f"{float(time_match.group(1)):.1f}s" if time_match else "?"
            chars = chars_match.group(1) if chars_match else "?"
            ended = ended_match.group(1) if ended_match else "?"
            events.append(("CALL", ts, f"📊 Summary: duration={dur}, synth_chars={chars}, ended_by_ai={ended}"))

        # ── Errors ──
        elif "ERROR" in line:
            msg = line.split("ERROR")[-1].strip()[:120]
            events.append(("ERR", ts, f"❌ {msg}"))

    # ── Display ──
    if not events:
        print(f"{C['RED']}No call events found in the last logs.{C['R']}")
        print("Try: python view_call_log.py --lines 500")
        return

    # Deduplicate consecutive identical events
    deduped = [events[0]]
    for e in events[1:]:
        if e[2] != deduped[-1][2]:
            deduped.append(e)
    events = deduped

    color_map = {
        "CALL": C["CYAN"],
        "STT": C["YELLOW"],
        "STT_INTERIM": C["DIM"],
        "LLM": C["BLUE"],
        "TTS": C["MAG"],
        "TTS_LISTEN": C["DIM"],
        "INT": C["RED"],
        "OUT": C["GREEN"],
        "ERR": C["RED"],
    }

    print(f"\n{C['BOLD']}{C['CYAN']}{'═' * 70}{C['R']}")
    print(f"{C['BOLD']}{C['CYAN']}  📞 CALL FLOW LOG{C['R']}")
    print(f"{C['BOLD']}{C['CYAN']}{'═' * 70}{C['R']}\n")

    last_category = ""
    for cat, ts, msg in events:
        color = color_map.get(cat, C["R"])
        cat_label = f"[{cat:>5}]" if cat != last_category else "       "
        if cat != last_category and last_category:
            print(f"  {C['DIM']}{'─' * 60}{C['R']}")
        print(f"  {C['DIM']}{ts}{C['R']}  {color}{cat_label}  {msg}{C['R']}")
        last_category = cat

    print(f"\n{C['BOLD']}{C['CYAN']}{'═' * 70}{C['R']}\n")

    if show_raw:
        print(f"\n{C['DIM']}--- RAW MATCHING LINES ---{C['R']}")
        for line in lines:
            if any(kw in line.lower() for kw in [
                "transcript", "llm", "synthes", "speech", "sequence",
                "error", "conversation complete", "connection closed"
            ]):
                print(f"  {C['DIM']}{line.strip()}{C['R']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="View Bolna call flow logs")
    parser.add_argument("--raw", action="store_true", help="Show raw log lines too")
    parser.add_argument("--lines", type=int, default=300, help="Number of Docker log lines to parse")
    args = parser.parse_args()

    logs = get_logs(args.lines)
    parse_and_display(logs, show_raw=args.raw)
