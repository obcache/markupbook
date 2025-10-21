Markupbook — Windows event / startup notifier

New configuration options (add to `config.json`)

- `event_id` (integer, optional)
  - The Windows Event ID to write when the server becomes reachable. Default: 1000.

- `event_description` (string, optional)
  - A human-readable description to include in the event. Default: "Markupbook server started and reachable on {host}:{port}". The default will include the host/port.

Example `config.json` snippet

{
  "notebook_path": "markups/Emanations_Echoes_Lyrics_Notebook.md",
  "host": "127.0.0.1",
  "port": 5000,
  "event_id": 2001,
  "event_description": "Markupbook server started (custom description)."
}

Monitoring the event on Windows

- Event Viewer
  - Open Event Viewer -> Windows Logs -> Application
  - Filter or search for Provider/Source "Python" and the configured Event ID.

- PowerShell

Get recent events from the Python provider:

```powershell
Get-WinEvent -FilterHashtable @{LogName='Application'; ProviderName='Python'} -MaxEvents 20
```

Filter by ID:

```powershell
Get-WinEvent -FilterHashtable @{LogName='Application'; ProviderName='Python'; Id=2001} -MaxEvents 10
```

Blinker listener example (optional)

If you have the `blinker` package installed, the app will emit a `markupbook.started` signal when the server becomes reachable. Example listener:

```python
from blinker import signal

def on_started(sender, **kwargs):
    print("Markupbook started", sender, kwargs)

signal("markupbook.started").connect(on_started)
```

Notes

- The Windows event writer uses the pre-existing "Python" event source to avoid requiring administrative rights to register a custom source. If you prefer a custom provider name, you can modify `app.py` and register the source manually (may require admin rights).
- Writing to the Windows Event Log requires `pywin32`. Install with `pip install pywin32`.
- The notifier polls the configured host/port for up to 10 seconds; adjust the timeout in `app.py` if you need a different wait behavior.
