"""
Umbra CLI — interactive terminal interface.
Shows real-time command output (last 5 lines, updates every second).
"""
import os, sys, threading, time, shutil
from dotenv import load_dotenv

load_dotenv()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from agent.core   import UmbraAgent
from agent.tools  import set_output_callback
from agent.providers import get_current

# ─────────────────────────────────────────
#  Terminal output buffer — last 5 lines
# ─────────────────────────────────────────
_output_lines: list[str] = []
_output_lock  = threading.Lock()
_running      = False
_start_time   = 0.0
_current_cmd  = ""

def _on_output(line: str):
    with _output_lock:
        if line.strip():
            _output_lines.append(line)
            if len(_output_lines) > 100:
                _output_lines.pop(0)

def _display_loop():
    """Updates the terminal status block every second while running."""
    term_width = shutil.get_terminal_size().columns

    while _running:
        time.sleep(1)
        if not _running:
            break

        elapsed = int(time.time() - _start_time)
        with _output_lock:
            last = _output_lines[-5:] if _output_lines else []

        # clear previous block (5 lines + header)
        lines_to_clear = len(last) + 2
        sys.stdout.write(f"\033[{lines_to_clear}A\033[J")

        # header
        sys.stdout.write(f"\033[90m{'─' * min(term_width, 60)}\033[0m\n")
        sys.stdout.write(f"\033[36m  processing... [{elapsed}s]\033[0m\n")

        for line in last:
            truncated = line[:term_width - 4]
            sys.stdout.write(f"\033[90m  {truncated}\033[0m\n")

        sys.stdout.flush()


# ─────────────────────────────────────────
#  Main CLI loop
# ─────────────────────────────────────────
def main():
    global _running, _start_time, _output_lines

    agent = UmbraAgent()
    set_output_callback(_on_output)

    current = get_current()
    model_str = f"[{current['provider']}] {current['model']}" if current else "no model set — use /model"

    print(f"\033[35m{'═' * 50}\033[0m")
    print(f"\033[35m  Umbra\033[0m")
    print(f"\033[90m  {model_str}\033[0m")
    print(f"\033[35m{'═' * 50}\033[0m\n")

    def send_inline(text: str):
        """Called by agent for mid-task notifications (session ID, rules, etc.)"""
        print(f"\n\033[33m  ▸ {text}\033[0m\n")

    def send_file(path: str):
        print(f"\n\033[32m  ▸ File ready: {path}\033[0m\n")

    agent.set_send_callback(send_inline)
    agent.set_file_callback(send_file)

    while True:
        try:
            user_input = input("\033[32m> \033[0m").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\033[90mExiting.\033[0m")
            break

        if not user_input:
            continue

        if user_input.lower() in {"exit", "quit", "bye"}:
            print("\033[90mExiting.\033[0m")
            break

        if user_input.lower() in {"cancel", "cancelar"}:
            agent.reset()
            print("\033[33m  Cancelled.\033[0m")
            continue

        # start display loop
        _output_lines = []
        _running      = True
        _start_time   = time.time()

        display_thread = threading.Thread(target=_display_loop, daemon=True)
        display_thread.start()

        # reserve space for the display block
        print(f"\033[90m{'─' * 50}\033[0m")
        print(f"\033[36m  processing... [0s]\033[0m")
        print()  # space for output lines

        try:
            response = agent.chat(user_input)
        except KeyboardInterrupt:
            _running = False
            display_thread.join(timeout=1)
            agent.reset()
            print("\n\033[33m  Cancelled.\033[0m\n")
            continue
        except Exception as e:
            _running = False
            display_thread.join(timeout=1)
            print(f"\n\033[31m  Error: {e}\033[0m\n")
            continue

        _running = False
        display_thread.join(timeout=1)

        elapsed = int(time.time() - _start_time)

        # clear processing block
        print(f"\033[90m{'─' * 50}\033[0m")
        print(f"\033[90m  done in {elapsed}s\033[0m\n")

        # print response
        print(f"\033[37m{response}\033[0m\n")


if __name__ == "__main__":
    main()
