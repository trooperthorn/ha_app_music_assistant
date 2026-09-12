# Music Assistant (Library Manager)

Same options as the official app:

| Option | Meaning |
| --- | --- |
| `log_level` | Global log level; keep `info` unless debugging. |
| `safe_mode` | Start with only the core controllers, no providers, to troubleshoot. |

The library manager is at `/library` in the app's own interface ("Library
manager" in the navigation). Its user guide lives in the frontend fork:
https://github.com/trooperthorn/HA_int_MA-UI/blob/main/docs/LIBRARY-MANAGER.md

## Updates

Each release of this app pins one upstream server version and one fork
frontend release; the changelog names both. Home Assistant offers the update
like any other app.
