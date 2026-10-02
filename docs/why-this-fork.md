# Why this fork exists

This is the community edition of
[segwaynavimow/navimow-sdk](https://github.com/segwaynavimow/navimow-sdk).
This document gives the reasons for it and points elsewhere for the facts:
[UPSTREAM.md](UPSTREAM.md) is the map of the fork (where it left upstream,
what came from other forks and who wrote it, what moved), and
[CHANGELOG.md](../CHANGELOG.md) says what changed in each version.

## The situation at the fork

Upstream published three releases in March and April 2026, and its `main`
branch has not changed since the last of them; UPSTREAM.md's
[fork point](UPSTREAM.md#fork-point) table has the dates. The repository's
issue tracker stayed in use. On 2026-10-01 it showed 20 open issues, 11 of
them opened after the last commit, and one open pull request, opened on
2026-03-28 and not yet reviewed; the owner's latest reply in any of them was
dated 2026-04-09.

Users fixed what they met in forks of their own. UPSTREAM.md lists
[the forks that were reviewed](UPSTREAM.md#commits-taken-from-elsewhere),
their authors and what was taken from each. The fixes were spread across
those forks and no one of them had them all, so a consumer could have one
fork's fixes or another's, not both.

## What a consumer met at 0.1.2

Each row can be read in the fork point's code with `git show 6596aa0:<file>`,
at the lines given. The last column is the version whose CHANGELOG section
describes the change.

| At the fork point | In `6596aa0` | Changed in |
|---|---|---|
| A REST request carried no timeout of its own, so a call could wait as long as the caller's session allowed. | `mower_sdk/api.py`, line 80 | [0.2.0a2] |
| A payload without a readable battery value gave a battery of 0. | `mower_sdk/models.py`, lines 39–70 | [0.2.0a2] |
| The facade's cached state had no age: nothing recorded when a message arrived, so an old reading could not be told from a current one. Issue #71 of `segwaynavimow/NavimowHA` reports a cached state, applied whatever its age, overwriting newer REST readings. | `mower_sdk/sdk.py`, lines 104–108 and 134 | [0.2.0a2] |
| Credentials changed while connected were stored and not set on the paho client, so its automatic reconnect used the old ones. | `mower_sdk/mqtt.py`, lines 564–572 | [0.2.0a2] |
| A state, event or attributes callback that raised stopped the callbacks registered after it. | `mower_sdk/sdk.py`, lines 135–147 | [0.2.0a2] |
| The facade has four command methods that send over MQTT: `NavimowSDK.start_mowing`, `pause`, `return_to_base` and `set_blade_height`. Starting, pausing and docking do work, over REST (`MowerAPI.async_send_command`), which is what the official integration uses; the three MQTT methods are a second route to the same commands, published to a topic no mower has been reported to answer. Blade height has only the MQTT route, and upstream issue #20 reports it on an X450: the publish is logged and the cutting height does not change. | `mower_sdk/sdk.py`, lines 169–207; `mower_sdk/mqtt.py`, lines 739–741 | [0.2.0a2] |
| The MQTT client used paho's callback API version 1, which paho-mqtt 2 reports as deprecated, while the dependency (`paho-mqtt>=1.6.1`) allowed paho-mqtt 2. | `mower_sdk/mqtt.py`, lines 494, 673 and 689; `pyproject.toml`, line 30 | [0.2.0a3] |
| The eleven `ERROR_MESSAGES` strings, from which the REST client builds its exception text, were Chinese. | `mower_sdk/errors.py`, lines 79–91 | [0.2.0a3] |

None of these needs a new design. Each has a local fix, and several of the
fixes had already been written in one fork or another. They matter most to a
process that stays connected for weeks: the longer it runs, the likelier it is
to meet a request that hangs, a credential that rotates or a callback that
raises.

### Three ways to the feed, one that works

Upstream's package offered three ways to receive the mower's messages, side
by side and without a word on which to use. Its README's example began with
the first.

- **`MowerClient`, on the generation-1 MQTT client `MowerMQTT`.** That client
  subscribes to `device/<id>/status` and `device/<id>/event`, under a comment
  of upstream's own that the topic format is still to be adjusted to the real
  one (`mower_sdk/mqtt.py`, lines 149 to 171 at the fork point). The broker's
  topics have another form, so nothing arrives. `MowerClient`'s REST methods
  do work: they forward to `MowerAPI`.
- **`Navimow`, with `NavimowCloud`, `NavimowCloudDevice` and
  `StateManager`.** It is built on the client that does connect,
  `NavimowMQTT`, but reads the channel from a topic of the form
  `navimow/<id>/<channel>` (`mower_sdk/cloud.py`, line 70), which is not what
  that client subscribes to and delivers (`mower_sdk/mqtt.py`, lines 642 to
  646). Its three message events never fire.
- **`NavimowSDK`, on `NavimowMQTT`.** This one receives messages, and it is
  the one the official integration uses.

The topics of the first two were already these in upstream's first release
and stayed so through 0.1.2, whatever else changed around them: as
published, neither appears ever to have been usable for the feed.

What the other topic forms are cannot be told from the repository. They may
be something the cloud is meant to serve one day, or what is left of an
earlier design. Upstream's code says only, of the first route's topics, that
the format is still to be adjusted, and nothing upstream has published says
more. The same holds for the MQTT command topic in the table above, which has
the `navimow/<id>/...` form too. The fork therefore claims no more than this:
with the topics the broker serves today, the two routes receive nothing, and
no mower has been reported to act on a command published to that topic.

The fork does not repair them and does not delete them. It moves them out of
the way, into `mower_sdk.legacy`, and makes them explicitly deprecated: they
still import from their old names, and the first use of one emits a
`DeprecationWarning`, so the state of these routes is no longer silent.
[`mower_sdk/legacy/README.md`](../mower_sdk/legacy/README.md) describes each
module, and [migrating.md](migrating.md) gives the way over to `NavimowSDK`.

## Why a fork

A fix reaches a library's users when it is merged and released. Upstream's
repository has shown neither since April 2026, so a pull request there
changes nothing for a consumer until someone merges it. A fork with a
distribution of its own can release, and it gives the fixes from the other
forks one place to live, with credit.

Offering the changes upstream remains the intent. That intent, more than any
single fix, decides how the fork is built: a fork that only had to work could
rename the package and delete what it does not use. This one does neither.

## What follows from that

- **The import name stays `mower_sdk`; only the distribution is renamed.** A
  consumer switches by changing a requirement, not its code (the README's
  [installation](../README.md#installation) section says how,
  [migrating.md](migrating.md) what to expect), and a patch to the live
  modules names the same paths upstream's tree has. The distribution needs a
  name of its own because `navimow-sdk` on PyPI is upstream's.
- **Nothing upstream published is removed.** Code written against 0.1.2 still
  imports, so adopting the fork does not begin with a rewrite, and a
  merge-back never asks upstream to accept a deletion. Where behaviour
  differs, the CHANGELOG lists it.
- **A live path and a frozen legacy folder.** The fixes go to the classes a
  consumer's code runs on, which [architecture.md](architecture.md) describes.
  The rest of what upstream published, the two routes that receive nothing
  among it, is kept as upstream wrote it, so that upstream's own commits can
  still be ported onto it;
  [`mower_sdk/legacy/README.md`](../mower_sdk/legacy/README.md) says what is
  there and why it is frozen.
- **Changes are written so they can be offered as they are.** A change that
  does not depend on the fork's identity stays free of it, and for a change
  of behaviour the CHANGELOG names the parameter that restores upstream's,
  where there is a sensible way back.

UPSTREAM.md states these as
[rules](UPSTREAM.md#rules-that-keep-a-merge-back-possible).

## What the fork is not

- **Not a rewrite.** `MowerAPI`, `NavimowSDK` and `NavimowMQTT` are
  upstream's classes, changed commit by commit from the fork point; the
  history continues upstream's.
- **Not a different API.** What is new is added beside upstream's names.
- **Not affiliated with Segway-Navimow.** It is not a Segway-Navimow product.
  Problems with it are reported to this repository, not to upstream.
- **Not a change of licence.** It is GPL-3.0-only, the licence of upstream's
  `LICENSE` file; upstream's owner wrote in upstream issue #3 that the GPL is
  used for the entire project. The package metadata still said MIT at the
  fork point and was corrected in [0.2.0a1].

## If upstream becomes active again

The fork is built for that case. Upstream's new commits are ported in with
their authorship, as UPSTREAM.md's
[porting](UPSTREAM.md#porting-upstream-commits) section describes. The
fork's changes go the other way as pull requests. What upstream takes is
upstream's decision; UPSTREAM.md marks the changes that are community-only.
Once upstream carries what a consumer uses, that consumer goes back by
changing its requirement again, since the import name never changed. Whether
a separate distribution is still needed then depends on what upstream takes;
nothing here decides it.

## Who it is for

Home Assistant integrations and other long-running consumers of the Navimow
cloud, and so their authors: the SDK distribution is chosen in an
integration's requirements, not by its users. The official integration,
`segwaynavimow/NavimowHA`, requires upstream's `navimow-sdk`. A consumer that
none of the rows above affects, and that needs nothing the CHANGELOG adds,
has no need to switch.

To report a problem or offer a change, see
[CONTRIBUTING.md](../CONTRIBUTING.md); [development.md](development.md) covers
working on the code.

[0.2.0a1]: ../CHANGELOG.md#020a1---2026-09-27
[0.2.0a2]: ../CHANGELOG.md#020a2---2026-09-28
[0.2.0a3]: ../CHANGELOG.md#020a3---2026-09-30
