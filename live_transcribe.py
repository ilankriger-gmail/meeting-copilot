#!/usr/bin/env python3
"""
Live Meeting Transcriber — BlackHole + Soniox
Captura áudio do sistema via BlackHole e transcreve em tempo real com diarização.
"""

import argparse
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime

import numpy as np
import sounddevice as sd
from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosedOK, ConnectionClosedError

# ── Config ──────────────────────────────────────────────────────
SONIOX_WS_URL = "wss://stt-rt.soniox.com/transcribe-websocket"
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_SAMPLES = 1920  # 120ms at 16kHz
CHUNK_BYTES = CHUNK_SAMPLES * 2  # 16-bit = 2 bytes per sample
MAX_SESSION_MIN = 300


def find_blackhole_device():
    """Find BlackHole audio input device index."""
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0 and "blackhole" in d["name"].lower():
            return i, d["name"]
    return None, None


class LiveTranscriber:
    def __init__(self, api_key, title="Reunião", context="", domain="", output_dir="."):
        self.api_key = api_key
        self.title = title
        self.context = context
        self.domain = domain
        self.output_dir = output_dir
        self.running = False

        # Transcript state
        self.final_tokens = []
        self.speakers = {}  # speaker_id -> name
        self.speaker_count = 0
        self.segments = []  # [{time, speaker, text}]
        self.start_time = None

        # Output file
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        safe_title = self.title.replace(" ", "-").replace("/", "-")
        self.md_path = os.path.join(output_dir, f"{ts}_{safe_title}.md")

        # Audio buffer for WebSocket
        self.audio_buffer = bytearray()
        self.buffer_lock = threading.Lock()

        # WebSocket
        self.ws = None

    def get_speaker_name(self, speaker_id):
        if speaker_id not in self.speakers:
            self.speaker_count += 1
            self.speakers[speaker_id] = f"Speaker_{self.speaker_count}"
        return self.speakers[speaker_id]

    def elapsed(self):
        if not self.start_time:
            return "00:00"
        s = int(time.time() - self.start_time)
        return f"{s // 60:02d}:{s % 60:02d}"

    def write_md(self):
        """Rewrite the .md file with current transcript state."""
        lines = [
            "---",
            f"title: \"{self.title}\"",
            f"date: \"{datetime.now().strftime('%Y-%m-%d %H:%M')}\"",
            f"duration: \"{self.elapsed()}\"",
            f"domain: \"{self.domain}\"",
            f"speakers: {json.dumps(self.speakers)}",
            "status: recording",
            "---",
            "",
            f"# {self.title}",
            "",
        ]

        if self.context:
            lines += [f"> Contexto: {self.context}", ""]

        lines.append("## Transcrição\n")

        for seg in self.segments:
            speaker = seg.get("speaker", "")
            time_str = seg["time"]
            text = seg["text"]
            if speaker:
                lines.append(f"**[{time_str}] [{speaker}]** {text}\n")
            else:
                lines.append(f"**[{time_str}]** {text}\n")

        with open(self.md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def process_tokens(self, tokens):
        """Process incoming tokens from Soniox, group by speaker, append segments."""
        current_speaker = None
        current_words = []

        for token in tokens:
            if not token.get("is_final"):
                continue
            text = token.get("text", "")
            if not text.strip():
                continue

            speaker_id = token.get("speaker")
            speaker_name = self.get_speaker_name(speaker_id) if speaker_id else None

            if speaker_name != current_speaker and current_words:
                # Flush previous group
                joined = "".join(current_words).strip()
                if joined:
                    self.segments.append({
                        "time": self.elapsed(),
                        "speaker": current_speaker or "",
                        "text": joined,
                    })
                current_words = []

            current_speaker = speaker_name
            current_words.append(text)

        # Flush remaining
        if current_words:
            joined = "".join(current_words).strip()
            if joined:
                self.segments.append({
                    "time": self.elapsed(),
                    "speaker": current_speaker or "",
                    "text": joined,
                })

    def audio_callback(self, indata, frames, time_info, status):
        """Called by sounddevice for each audio chunk."""
        if status:
            print(f"  [audio] {status}", file=sys.stderr)
        # Convert float32 -> int16 PCM
        pcm = (indata[:, 0] * 32767).astype(np.int16)
        with self.buffer_lock:
            self.audio_buffer.extend(pcm.tobytes())

    def send_audio_loop(self):
        """Thread: continuously send buffered audio to Soniox WebSocket."""
        while self.running and self.ws:
            with self.buffer_lock:
                if len(self.audio_buffer) >= CHUNK_BYTES:
                    chunk = bytes(self.audio_buffer[:CHUNK_BYTES])
                    del self.audio_buffer[:CHUNK_BYTES]
                else:
                    chunk = None

            if chunk:
                try:
                    self.ws.send(chunk)
                except Exception:
                    break
            else:
                time.sleep(0.02)

    def receive_loop(self):
        """Thread: receive transcription results from Soniox."""
        last_write = time.time()
        try:
            while self.running and self.ws:
                try:
                    message = self.ws.recv(timeout=1.0)
                except TimeoutError:
                    continue

                res = json.loads(message)

                if res.get("error_code"):
                    print(f"\n  [soniox] Erro: {res.get('error_message', 'unknown')}", file=sys.stderr)
                    self.running = False
                    break

                tokens = res.get("tokens", [])
                final_tokens = [t for t in tokens if t.get("is_final") and t.get("text", "").strip()]

                if final_tokens:
                    self.process_tokens(final_tokens)

                    # Print to terminal
                    for t in final_tokens:
                        speaker = t.get("speaker", "?")
                        text = t.get("text", "")
                        if text.strip():
                            print(f"  [S{speaker}] {text}", end="", flush=True)

                    # Rewrite .md periodically (every 2s or on new content)
                    now = time.time()
                    if now - last_write > 2.0:
                        self.write_md()
                        last_write = now

                if res.get("finished"):
                    print("\n\n  [soniox] Sessão finalizada.")
                    break

        except (ConnectionClosedOK, ConnectionClosedError):
            pass
        except Exception as e:
            print(f"\n  [soniox] Erro recv: {e}", file=sys.stderr)

        # Final write
        self.write_md()

    def run(self):
        """Main entry point."""
        dev_idx, dev_name = find_blackhole_device()
        if dev_idx is None:
            print("ERRO: BlackHole não encontrado nos dispositivos de áudio.", file=sys.stderr)
            print("Instale com: brew install blackhole-2ch", file=sys.stderr)
            sys.exit(1)

        print(f"""
╔══════════════════════════════════════════════╗
║         LIVE MEETING TRANSCRIBER             ║
╠══════════════════════════════════════════════╣
║  Reunião:  {self.title:<33}║
║  Device:   {dev_name:<33}║
║  Output:   {os.path.basename(self.md_path):<33}║
║  Idioma:   pt-BR                             ║
║  Speakers: diarização ativa                  ║
╠══════════════════════════════════════════════╣
║  Ctrl+C para parar                           ║
╚══════════════════════════════════════════════╝
""")

        # Connect to Soniox
        config = {
            "api_key": self.api_key,
            "model": "stt-rt-v4",
            "audio_format": "pcm_s16le",
            "sample_rate": SAMPLE_RATE,
            "num_channels": CHANNELS,
            "language_hints": ["pt"],
            "language_hints_strict": True,
            "enable_speaker_diarization": True,
            "enable_endpoint_detection": True,
            "max_endpoint_delay_ms": 1500,
        }

        if self.context:
            config["context"] = {"text": self.context}

        print("  Conectando ao Soniox...", end=" ", flush=True)
        try:
            self.ws = connect(SONIOX_WS_URL)
            self.ws.send(json.dumps(config))
            print("OK")
        except Exception as e:
            print(f"FALHOU: {e}", file=sys.stderr)
            sys.exit(1)

        self.running = True
        self.start_time = time.time()

        # Write initial .md
        self.write_md()
        print(f"  Arquivo: {self.md_path}")
        print(f"  Gravando... (max {MAX_SESSION_MIN} min)\n")

        # Start audio capture
        stream = sd.InputStream(
            device=dev_idx,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=CHUNK_SAMPLES,
            callback=self.audio_callback,
        )

        # Start threads
        send_thread = threading.Thread(target=self.send_audio_loop, daemon=True)
        recv_thread = threading.Thread(target=self.receive_loop, daemon=True)

        def handle_stop(*_):
            print("\n\n  Parando...")
            self.running = False

        signal.signal(signal.SIGINT, handle_stop)
        signal.signal(signal.SIGTERM, handle_stop)

        try:
            stream.start()
            send_thread.start()
            recv_thread.start()

            # Wait until stopped or max duration
            max_seconds = MAX_SESSION_MIN * 60
            while self.running and (time.time() - self.start_time) < max_seconds:
                time.sleep(0.5)

        finally:
            self.running = False
            stream.stop()
            stream.close()

            # Signal end of audio to Soniox
            if self.ws:
                try:
                    self.ws.send("")
                except Exception:
                    pass

            # Wait for receive thread to process remaining
            recv_thread.join(timeout=5)

            if self.ws:
                try:
                    self.ws.close()
                except Exception:
                    pass

            # Final write with status update
            self.write_md()
            # Update status to finished
            with open(self.md_path, "r", encoding="utf-8") as f:
                content = f.read()
            content = content.replace("status: recording", "status: finished")
            with open(self.md_path, "w", encoding="utf-8") as f:
                f.write(content)

            total = self.elapsed()
            word_count = sum(len(s["text"].split()) for s in self.segments)
            print(f"\n  Transcrição salva: {self.md_path}")
            print(f"  Duração: {total} | Segmentos: {len(self.segments)} | Palavras: ~{word_count}")
            print(f"  Speakers: {json.dumps(self.speakers)}")


def main():
    parser = argparse.ArgumentParser(description="Live Meeting Transcriber — BlackHole + Soniox")
    parser.add_argument("--title", "-t", default="Reunião", help="Título da reunião")
    parser.add_argument("--context", "-c", default="", help="Contexto da reunião")
    parser.add_argument("--domain", "-d", default="negócios", help="Domínio (jurídico, financeiro, negócios)")
    parser.add_argument("--output", "-o", default=".", help="Diretório de saída")
    args = parser.parse_args()

    api_key = os.environ.get("SONIOX_API_KEY")
    if not api_key:
        print("ERRO: Configure a variável SONIOX_API_KEY", file=sys.stderr)
        print("  export SONIOX_API_KEY=<sua-chave>", file=sys.stderr)
        sys.exit(1)

    transcriber = LiveTranscriber(
        api_key=api_key,
        title=args.title,
        context=args.context,
        domain=args.domain,
        output_dir=args.output,
    )
    transcriber.run()


if __name__ == "__main__":
    main()
