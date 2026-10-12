# Contributing to LUMIX Remote

Thanks for helping. Bug reports, camera reports and pull requests are all welcome.

## Development setup

```sh
git clone https://github.com/sapoepsilon/lumix-remote.git
cd lumix-remote
./run.sh --help                                  # creates .venv and installs aiohttp
.venv/bin/python -m unittest discover -s tests   # full suite, no camera needed
```

No camera? Use the emulator ([docs/emulator.md](docs/emulator.md)):

```sh
.venv/bin/python -m emulator
LUMIX_REMOTE_HOME=/tmp/lumix-emu ./run.sh --camera emu --camera-ip 127.0.0.1 \
  --camera-http-port 8080 --camera-upnp-port 60606 --camera-files-port 50001
```

Bluetooth work needs a separate Python 3.10+ venv with `bleak` (see the README's
Bluetooth section).

## Pull requests

- Keep changes focused; one feature or fix per PR.
- Add or update tests in `tests/`. Protocol behaviour that only shows up on a real
  body belongs in the emulator too, so it stays covered.
- Run the whole suite before opening the PR.
- Update the README or `docs/` when behaviour or the protocol notes change.
- Be careful with commands that can wedge a camera (rapid `recmode` switching,
  `SUBSCRIBE` in rec mode); see "Known issues" in the README.

## Supporting a new camera model

See [docs/adding-a-camera.md](docs/adding-a-camera.md). A short issue with the
model, firmware and what works is already useful.

## Privacy

Camera dumps and logs contain identifying data. Before committing captures,
fixtures or screenshots, remove serial numbers, MAC and Bluetooth addresses,
Wi-Fi SSIDs and passwords, and your own LAN addresses. `logs/`, `probe-results/`
and `evidence/` are gitignored for this reason.

## License

By contributing you agree that your contributions are licensed under the MIT license.
