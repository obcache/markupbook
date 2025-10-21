import json, os, re
import threading
import time
import socket
from flask import Flask, render_template, request, jsonify, abort
from pathlib import Path

# Attempt to import Windows event logging helper (pywin32). If unavailable,
# we'll fall back to standard logging. We import lazily inside the notify
# function to avoid import-time errors on non-Windows platforms.

APP_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = APP_DIR / "config.json"
if not CONFIG_PATH.exists():
    raise SystemExit("Missing config.json. See README.")

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CFG = json.load(f)

NOTEBOOK_PATH = Path(CFG["notebook_path"]).expanduser().resolve()

app = Flask(__name__)

# --- Optional startup signal (requires blinker). If blinker isn't
# available we still log and set a config flag when the server becomes
# reachable.
try:
    from blinker import signal as _blinker_signal
    markupbook_signal = _blinker_signal("markupbook.started")
except Exception:
    markupbook_signal = None

# --- Markdown section parsing ---
# A "page" = a level-2 heading "## Title" and everything until the next "## " or EOF.
H2_RE = re.compile(r"(?m)^##\s+(.*)$")

def read_md() -> str:
    if not NOTEBOOK_PATH.exists():
        return ""
    return NOTEBOOK_PATH.read_text(encoding="utf-8")

def write_md(text: str):
    NOTEBOOK_PATH.write_text(text, encoding="utf-8")

def split_sections(md_text: str):
    """Return ordered list of (title, content, start_idx, end_idx)."""
    sections = []
    matches = list(H2_RE.finditer(md_text))
    if not matches:
        return sections
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        start = m.start()
        # content starts after this heading line
        content_start = m.end()
        if i + 1 < len(matches):
            end = matches[i+1].start()
        else:
            end = len(md_text)
        content = md_text[content_start:end]
        sections.append((title, content, start, end))
    return sections

def rebuild_with_replacement(md_text: str, old_title: str, new_title: str, new_content_html: str):
    """
    Replace the section by title with new title + HTML-as-markdown-ish content.
    We store HTML directly (since you want WYSIWYG). If you prefer markdown,
    you can run an HTML->MD conversion here.
    """
    sections = split_sections(md_text)
    if not sections:
        abort(400, description="No sections found in notebook.")

    found = None
    for idx, (title, content, start, end) in enumerate(sections):
        if title == old_title:
            found = (idx, title, content, start, end)
            break
    if found is None:
        abort(404, description=f"Section '{old_title}' not found.")

    idx, title, content, start, end = found

    # Compose the new section text
    # We keep "## {new_title}\n\n" and then the HTML block fenced for clarity
    # You can choose to store raw HTML without fences if you prefer:
    new_block = f"## {new_title}\n\n{new_content_html.strip()}\n"

    # Rebuild the full document
    new_md = md_text[:start] + new_block + md_text[end:]
    return new_md

def insert_new_section(md_text: str, new_title: str):
    stub = f"\n\n## {new_title}\n\n<p><em>New page.</em></p>\n"
    if md_text.strip():
        return md_text.rstrip() + stub
    else:
        # If file was empty, create a minimal header and first section
        return f"# Notebook\n\n{stub}"

def rename_section(md_text: str, old_title: str, new_title: str):
    sections = split_sections(md_text)
    if not sections:
        abort(400, description="No sections found.")
    for (title, content, start, end) in sections:
        if title == old_title:
            # heading line starts at 'start' like "## Title"
            # Replace only the heading text
            line_end = md_text.find("\n", start)
            if line_end == -1:
                line_end = len(md_text)
            old_line = md_text[start:line_end]
            new_line = f"## {new_title}"
            return md_text[:start] + new_line + md_text[line_end:]
    abort(404, description=f"Section '{old_title}' not found.")

@app.route("/")
def index():
    md = read_md()
    sections = split_sections(md)
    pages = [s[0] for s in sections]  # titles
    initial_title = pages[0] if pages else "New Page"
    initial_html = sections[0][1] if sections else "<p><em>Create your first page.</em></p>"
    return render_template("index.html", pages=pages, initial_title=initial_title, initial_html=initial_html)

@app.route("/load", methods=["GET"])
def load_page():
    title = request.args.get("title")
    md = read_md()
    for t, content, start, end in split_sections(md):
        if t == title:
            return jsonify({"title": t, "html": content})
    abort(404, description=f"Section '{title}' not found.")

@app.route("/save", methods=["POST"])
def save_page():
    data = request.get_json(force=True)
    old_title = data.get("oldTitle")
    new_title = data.get("newTitle") or old_title
    html = data.get("html", "")
    if not old_title:
        abort(400, description="Missing oldTitle.")

    md = read_md()
    new_md = rebuild_with_replacement(md, old_title, new_title, html)
    write_md(new_md)
    return jsonify({"ok": True})

@app.route("/new", methods=["POST"])
def new_page():
    data = request.get_json(force=True)
    new_title = data.get("title", "").strip()
    if not new_title:
        abort(400, description="Missing title.")
    md = read_md()
    new_md = insert_new_section(md, new_title)
    write_md(new_md)
    return jsonify({"ok": True})

@app.route("/rename", methods=["POST"])
def rename_page_route():
    data = request.get_json(force=True)
    old_title = data.get("oldTitle", "").strip()
    new_title = data.get("newTitle", "").strip()
    if not old_title or not new_title:
        abort(400, description="Missing oldTitle/newTitle.")
    md = read_md()
    new_md = rename_section(md, old_title, new_title)
    write_md(new_md)
    return jsonify({"ok": True})

@app.route("/pages", methods=["GET"])
def list_pages():
    md = read_md()
    pages = [s[0] for s in split_sections(md)]
    return jsonify({"pages": pages})

if __name__ == "__main__":
    host = CFG.get("host", "127.0.0.1")
    port = int(CFG.get("port", 5000))

    def _notify_when_listening(host: str, port: int, timeout: float = 10.0):
        """Background thread: poll the TCP port until it accepts connections,
        then emit the application event (signal) and set a config flag.
        This avoids racing with the blocking app.run() call and guarantees
        the server is actually reachable before raising the event.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                # connect briefly to ensure the server is accepting
                with socket.create_connection((host, port), timeout=1):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            app.logger.warning("Markupbook: server did not become reachable on %s:%s within %.1fs", host, port, timeout)
            return

        # mark in-app state and emit signal if available
        app.config["MARKUPBOOK_STARTED"] = True
        # Allow optional event metadata via config.json
        event_id = int(CFG.get("event_id", 1000))
        event_description = CFG.get("event_description", f"Markupbook server started and reachable on {host}:{port}")
        msg = event_description
        app.logger.info(msg)

        # Emit blinker signal if available
        if markupbook_signal is not None:
            try:
                # include metadata in the signal payload
                markupbook_signal.send(app, port=port, host=host, event_id=event_id, description=event_description)
            except Exception:
                app.logger.exception("Failed to send markupbook.started signal")

        # Try to write a Windows Application Event so external monitors can
        # subscribe to the Windows Event Log. This uses pywin32 (win32evtlogutil)
        # if available. We do a platform check and import lazily to avoid
        # import-time errors on non-Windows systems.
        try:
            if os.name == "nt":
                try:
                    import win32evtlogutil
                    import win32evtlog

                    # Use the pre-existing 'Python' event source to avoid needing
                    # to register a custom source (reduces permission issues).
                    event_source = "Python"
                    # Write the event; use INFO level and include event id
                    try:
                        win32evtlogutil.ReportEvent(event_source, event_id, eventType=win32evtlog.EVENTLOG_INFORMATION_TYPE, strings=[msg])
                        app.logger.info("Windows event log entry written (source=%s, id=%s)", event_source, event_id)
                    except TypeError:
                        # Older pywin32 variants expect (appName, eventID, eventType, strings)
                        try:
                            win32evtlogutil.ReportEvent(event_source, event_id, win32evtlog.EVENTLOG_INFORMATION_TYPE, [msg])
                            app.logger.info("Windows event log entry written (source=%s, id=%s)", event_source, event_id)
                        except Exception:
                            app.logger.exception("Failed to write Windows event log entry with fallback signature")
                except Exception:
                    app.logger.exception("Failed to write Windows event log entry; pywin32 may be missing or permission denied")
        except Exception:
            # Defensive: any failure here shouldn't stop the app
            app.logger.exception("Unexpected error while attempting to write Windows event")

    # Start the notifier thread for the configured host/port so we emit
    # the startup event regardless of which port is used.
    t = threading.Thread(target=_notify_when_listening, args=(host, port), daemon=True)
    t.start()

    app.run(host=host, port=port, debug=True)
    
