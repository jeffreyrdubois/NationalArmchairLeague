# NationalArmchairLeague

Fun Family Football League — a weekly NFL confidence-pick game for the family.

Read [MANUAL.md](MANUAL.md) for how to play and how to run it.

## Running it

Every merge to `main` publishes a multi-arch image (amd64 + arm64) to the GitHub
Container Registry, so there is nothing to build:

    ghcr.io/jeffreyrdubois/nationalarmchairleague:latest

**On Unraid**, drop the container template on the flash drive — there is no
field in the UI to paste a template URL into:

    wget -O /boot/config/plugins/dockerMan/templates-user/my-NationalArmchairLeague.xml \
      https://raw.githubusercontent.com/jeffreyrdubois/NationalArmchairLeague/main/unraid/nal.xml

Then *Docker -> Add Container*, and pick **NationalArmchairLeague** from the
**Template** dropdown. From then on Unraid flags the container as "update ready"
whenever a new image is published, and **Apply** pulls it. Nothing else is
needed — no configuration is required for a first run.

**With Compose**, on Unraid or anywhere else:

    cp .env.example .env      # optional — every value has a default
    docker compose up -d

and to update:

    ./update.sh

## What is running

`/health` reports the running build without needing a login, and the same
version appears in the footer of every page:

    $ curl -s http://your-server:5950/health
    {"status":"ok","version":"1.0.0+a1b2c3d","built_at":"...","commit":"..."}

A version ending in `-dev` means the image was built by hand rather than
published by CI.

## Data

Everything the league owns — the SQLite database and the session signing key —
lives in the single `/app/data` volume, so one backup of that folder is a
complete backup.
