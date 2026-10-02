# Contributing

Issues and pull requests are welcome at
[geordiekorper/navimow-sdk-community](https://github.com/geordiekorper/navimow-sdk-community/issues).
This is the community edition of the Navimow SDK and is not run by
Segway-Navimow; [docs/why-this-fork.md](docs/why-this-fork.md) says why it
exists.

## Reporting a problem

Is it the SDK? If you use the mower through Home Assistant, a Home Assistant
integration sits between you and this library. A problem with entities, the
setup flow or the dashboard belongs with that integration's project
([segwaynavimow/NavimowHA](https://github.com/segwaynavimow/NavimowHA) is the
official one). A wrong value, a dropped connection or an exception raised
from `mower_sdk` belongs here.

Please include:

- The SDK version, both as installed and as imported:
  `pip show navimow-sdk-community` and
  `python -c "import mower_sdk; print(mower_sdk.__version__)"`. If the two
  differ, the upstream `navimow-sdk` distribution is probably installed over
  this one.
- The Python, aiohttp and paho-mqtt versions.
- The mower model and its firmware version, when the problem depends on what
  the mower sends.
- What you expected, what happened, and the log lines or the payload that
  show it.

**Check logs and payloads before posting them.** The SDK's own log lines mask
the bearer token and leave the account id out of the client id and the
WebSocket path, but a payload you captured yourself is posted as it is.
Remove access tokens, the MQTT username (it is your account id), device ids
and serial numbers, and positions if you would not publish where your garden
is.

Captured payloads are useful in themselves. The cloud's protocol is not
documented, so a message the SDK does not understand, with the mower model
and firmware it came from, is a contribution.

## Proposing a change

For anything larger than a small fix, open an issue first so the approach can
be agreed before the work is done.

A pull request needs tests for the change, an entry in `CHANGELOG.md` when a
consumer can see it, and commits that pass the checks.
[docs/development.md](docs/development.md) has the setup, the checks, the
commit rules and what a change has to keep; [tests/README.md](tests/README.md)
has the style the tests are written in.

## Licence

The project is GPL-3.0-only, as upstream is. A contribution is accepted under
the same licence.
