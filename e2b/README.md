# Building the sandbox image

Everything here runs on **your machine** — it needs Docker and an E2B account,
neither of which an agent session has.

Do these in order. Each step is unverifiable until the one above it is done.

---

## 0. Prerequisites

```powershell
docker version          # must print a Server section, not just Client
node --version          # the E2B CLI is an npm package
```

If Docker Desktop isn't running, `e2b template build` fails with a connection
error that reads like an auth problem. Start it first.

---

## 1. Get the key and install the CLI

The key comes from <https://e2b.dev/dashboard> → API Keys. It starts with `e2b_`.

**Do not paste it into a chat window** — it lands in the transcript. Type it
straight into the terminal.

```powershell
npm install -g @e2b/cli
e2b auth login
```

`auth login` opens a browser. That session is what authorises the build; the
env var below is what the *backend* uses at runtime — two separate things that
happen to use the same account.

---

## 2. Build the image

```powershell
cd <repo>\e2b
e2b template create occupella-reporting `
  -d e2b.Dockerfile --cpu-count 2 --memory-mb 2048 `
  -c "sudo /root/.jupyter/start-up.sh" `
  --ready-cmd "curl -sf --retry 60 --retry-delay 1 --retry-connrefused http://localhost:49999/health"
```

This exact invocation built the verified template (`2847f3y6a5l7it8dld37`,
build `c234b7a2`, 19s). Copy it rather than reconstructing it — three of its
details each cost a failed build:

- **`-c` is `--cmd`, not cpu-count.** `-c 2` sets the start command to the
  literal string `2`. CPU count is `--cpu-count`.
- **`sudo` is required** on the start script.
- **Use curl's own `--retry`, never a shell loop.** PowerShell 5.1 splits the
  argument (`unknown option '-lt'`); Git Bash rewrites the POSIX path into
  `C:/Program Files/Git/root/...` and exits 127. Both read as template faults
  and are not.

⚠ **The start/ready pair is not optional.** The CLI refuses one without the
other, and a template built without them boots a VM whose code-interpreter
server never listens — every execution returns `502 the sandbox is running but
port is not open`.
Source: <https://e2b.dev/docs/troubleshooting/templates/49999-port-not-open>

⚠ **Not `e2b template build`.** In CLI v2.17.1 that subcommand is a removal
shim — no options, its one argument documented as "unused". It prints a
deprecation banner and exits 1 *without ever saying it built nothing*, which
reads exactly like an auth failure and isn't one.

⚠ `e2b.toml` is reference only. `template create` neither reads it nor writes
to it, so the config lives on the command line above and the resulting ID is
recorded in the toml **by hand**.

First build takes a couple of minutes (measured: 1m22s, 1.1 GB base pull). The
last line prints a **template ID** — copy it into `template_id` in `e2b.toml`
and into the env var below.

The build ends with `python -c "import reportlab, pypdf, fpdf, xlsxwriter"`. If
that line fails, the build fails, and you have learned it here rather than
inside a user's request.

---

## 3. Point the backend at it

Set these together — the substrate and the key are meaningless apart.

```powershell
$env:HELIXIS_CODEEXEC__SUBSTRATE    = "e2b"
$s = Read-Host "HELIXIS_CODEEXEC__E2B_API_KEY" -AsSecureString
$env:HELIXIS_CODEEXEC__E2B_API_KEY  = [Net.NetworkCredential]::new("", $s).Password
$env:HELIXIS_CODEEXEC__E2B_TEMPLATE = "<template id from step 2>"
```

For production these go in **Render** (web service *and* worker), not just your
shell.

---

## 4. Prove the sandbox actually contains anything

```powershell
cd <repo>
python -m pytest tests\unit\test_codeexec_containment.py -v
```

**Read the skip line before believing the result.**

| What you see | What it means |
|---|---|
| `12 skipped` | The module is `skipif win32`. Nothing ran. This is not a pass. |
| Runs, but `__SUBSTRATE` unset | It tested the **local subprocess** path and proves nothing about E2B. |
| `xpassed` | You will not see this word. The markers are `strict=True`, so an unexpected pass is reported as **FAILED**, carrying `[XPASS(strict)] …delete the xfail marker on this test`. **That FAILED line is the success signal.** |
| An `ERROR` from `_the_substrate_must_actually_execute` | The sandbox cannot run code at all. Nothing in the file measured anything — fix the template before reading any other line. |
| Any of the five non-xfail tests red | Same conclusion. Those assert `result.ok`, so they are the canary: a dead sandbox turns them red while the xfails go vacuously green. |

**The only green is: the three flips report FAILED(XPASS strict), and every
non-xfail guarantee stays passing.** Flips alone are not evidence — a sandbox
that executes nothing produces all three for free.

The three that should flip on real E2B: parent environment unreachable, the
`setsid` grandchild cannot outlive the kill, and application source unreadable.

Windows can't run this file at all. Use WSL, or a Linux box, or CI.

---

## 5. Only now, the flag

`HELIXIS_CODEEXEC__ENABLED=true` — and point it at **your own Buildium
account** first, not a customer's.

The backend refuses to boot with the flag true on any substrate but `e2b`
(`CodeExecSettings._refuse_live_on_an_unsafe_substrate`), so a misconfiguration
fails loudly instead of quietly routing agent code back onto the leaky local
path.

---

## Changing the image later

Re-run the same `template create` command with the same **name**. That updates
the template in place, so `HELIXIS_CODEEXEC__E2B_TEMPLATE` never changes.

Re-run step 4 afterward. **A new image is a new containment claim** — the
previous run's XPASSes were measured against the previous bytes.

---

## Notes from the first build (2026-08-23)

- The base image runs **Python 3.13**; the backend runs 3.11. Agent-authored
  code executes on the *sandbox's* interpreter, so version-sensitive behaviour
  here is not what the rest of the product sees.
- `reportlab` pulls **chardet** transitively, and pip resolved it unpinned on
  the first build. It is pinned now — an unpinned transitive falsifies the
  "pinned" claim in exactly the quiet way that matters.
- A WSL clone that predates the `codeexec` package will run step 4 against
  nothing and report success. Check `git log -1` in the WSL checkout, not just
  that a directory exists.
