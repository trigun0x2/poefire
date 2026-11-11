# Security

poefire runs on your PC next to Path of Exile. This page says what it does and how you can
check the copy you downloaded yourself.

## Check your download

Every release is built by GitHub Actions from the public source code, never on a
developer's PC. The build signs a **build attestation** that names the zip's SHA-256, the
workflow that built it and the commit it was built from (keyless signing with
[Sigstore](https://www.sigstore.dev/), recorded in its public transparency log).

To check a zip, use either of these:

- **GitHub CLI:** `gh attestation verify poefire-1.0.0-windows-x64.zip --repo trigun0x2/poefire`
- **poefire itself**, with the `.sigstore.json` file from the same release next to the zip:
  `poefire-cli.exe verify poefire-1.0.0-windows-x64.zip`

Each release's notes also list the zip's SHA-256.

**Updates are checked the same way.** Before the app installs an update, it checks the
update's attestation. The update must have been built by this repository's
`.github/workflows/release.yml` on `master` (or `dev`, for the beta channel), and the zip must
be the exact file that was attested. If the check fails, the update isn't installed.
Uploading a file to a release isn't enough to pass the check: a matching signature can only
come from that workflow.

## What poefire reads and where it connects

**On your PC:**
- the game's `Client.txt` log (read only);
- the game window's title and position, and the game's install folder, to find the log and
  place the overlay;
- your own settings and database in `%APPDATA%\poefire`.

It doesn't read game memory, look at network traffic, or send keys or clicks to the game.

**On the internet** (HTTPS only):

| Host | What for |
| --- | --- |
| `www.pathofexile.com`, `api.pathofexile.com` | your characters, stash tabs and trade sales, with your POESESSID or OAuth login |
| `poe.ninja` | item prices |
| `www.poewiki.net` | which game patch a campaign run was on |
| `www.definitivguide.com` | CyclonDefinitiv's zone layout guides |
| `api.github.com`, `github.com` | checking for and downloading updates |

**No telemetry.** poefire doesn't send anything about you or your use of the app anywhere.

**The local web UI** listens on `127.0.0.1` only, so other devices on your network can't
reach it. It refuses requests from other websites: the Host header must be localhost, and
every change needs a custom header that another site can't add.

**Your POESESSID** is stored in its own file in `%APPDATA%\poefire`, never in the config file
or in debug reports. It is sent only to pathofexile.com. It gives access to your website
account, so treat it like a password. If you think it leaked, log out of the website and the
cookie stops working.

## How the code is checked

- CI runs the tests, linting and type checks on every push, plus a browser test of every page.
- [CodeQL](https://codeql.github.com/) scans the Python, JavaScript and workflows on every push
  and weekly.
- `pip-audit` checks every dependency the app ships with for known vulnerabilities on every
  push.
- Dependabot opens weekly update PRs for dependencies and GitHub Actions.
- All GitHub Actions are pinned to exact commits.
- The [OpenSSF Scorecard](https://scorecard.dev/viewer/?uri=github.com/trigun0x2/poefire) rates
  the project's security practices.

## Reporting a vulnerability

Please report it privately: open
[a private security advisory](https://github.com/trigun0x2/poefire/security/advisories/new)
on GitHub. Please don't open a public issue for a security problem.
