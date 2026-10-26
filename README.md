# Legend Online for Linux PC

A Linux desktop client built with PySide6 and a custom Flash host running through Wine. It does not use Ruffle. **This is the PC version of the project; Android has a separate app.** Windows and macOS installers are not provided.

The [Linux release bundle](https://github.com/ldnovaes/legend-online-linux/releases) contains source code and a launcher. It is not a self-contained binary distribution.

## Requirements

- A Linux PC with a 64-bit Wine environment.
- Python 3.11+ with virtual environment support.
- Wine, its development tools including `winegcc`, and `make`.
- The **64-bit Flash ActiveX OCX**, fetched automatically on first launch (see below).
- Microsoft core fonts in that prefix, including Arial and Verdana.
- Internet access and a Legend Online account.

The proprietary Flash runtime is **not bundled**. On first launch, `run.sh` fetches it from Adobe's official installer archive, which is served by the Wayback Machine. You can also fetch it manually with `.venv/bin/python -m legendclient.flash`. This downloads `fp_15.0.0.167_archive.zip` (~53 MB), checks its SHA-256, and extracts `Flash64_15_0_0_167.ocx` from the ActiveX installer without running it. The result goes to `.local/flash/` and is verified again. Nothing is installed in Wine. The host loads the OCX directly and registers only its type library in the prefix.

A Wine prefix is still required for the fonts below; installing Wine alone is not sufficient. The default prefix is `~/Games/legend-online`. Set `WINEPREFIX` to select another one. `LEGENDCLIENT_FLASH_OCX` overrides the OCX path. Without either, an OCX already present in the prefix is used as a fallback.

Install the fonts once in the selected prefix:

```bash
WINEPREFIX="$HOME/Games/legend-online" winetricks -q corefonts
```

Missing fonts can turn chat text into squares. The client reports missing fonts in its log; see [font troubleshooting](docs/fonts.md) for the original investigation.

## Install and run

Extract the Linux source bundle and open a terminal in its folder, or clone:

```bash
git clone https://github.com/ldnovaes/legend-online-linux.git
cd legend-online-linux
```

Then run:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./run.sh
```

To use another configured Wine prefix:

```bash
WINEPREFIX=/path/to/prefix ./run.sh
```

The Flash host is compiled automatically on first launch when needed. Select a region and server, sign in through the official login page, and open the game. Game content is downloaded from the official services.

## How it works

The client reads the public server list, uses a persistent QtWebEngine profile for the official login, and builds the Flash launch configuration from the selected server's `game.jsp` URL.

`flashhost/flashhost.c` loads the OCX directly in Wine, attaches it to a window, and passes the movie URL and FlashVars through standard input. The host handles Wine's Flash download callback behavior, configures the game's local storage allowance, and caches downloads on disk.

This current client uses its own launcher and host. The older Brov launcher remains under `tools/` for research and comparison; it is not required by `run.sh`.

## Logs and local files

Runtime state is stored under `.local/`, including the web profile and logs:

- `.local/logs/legendclient.log`: client activity and errors.
- `.local/logs/flashhost.log`: Wine host output and game callbacks.

Session values are not printed in the normal launch configuration log, but game callbacks or diagnostic output can still contain account-related data. Review and redact logs before attaching them to a public issue.

Closing the server browser does not automatically close game windows that are already open.

## Tests

From the repository root, with the Python dependencies installed:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -v
```

These tests cover parsing, launch configuration, caching helpers, and the legacy launcher's process handling. They do not authenticate a real account or verify a complete gameplay session.

## Known limitations

The host logs the game's `ExternalInterface` callbacks but does not implement their return values. Some browser-dependent actions, such as reload or favorites, may not work. Loading and entering the game have worked during development; not every feature is validated.

## Earlier research tools

The optional reference launcher expects an existing installation of the Brov client and its local proxy:

```bash
python3 tools/client.py doctor
python3 tools/client.py run --smoke-seconds 15
```

It records diagnostics in `.local/logs/legend-online.log`. The static analyzer in `tools/analyze.py` uses `requirements-analysis.txt`. These tools do not ship the proprietary binaries they inspect.

The Portuguese notes in [`docs/`](docs/) document the original research and intermediate failures. They are historical context, rather than the installation guide for the current client.

## Related repositories

- [Android app and APK downloads](https://github.com/ldnovaes/legend-online-android)

This is an independent community project, not an official client from the game publisher. Report bugs in [Issues](https://github.com/ldnovaes/legend-online-linux/issues).
