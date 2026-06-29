# Hosted crawl worker

The public API does not run Chromium. A private supervisor starts one
`crawl-worker` container per run, attaches its stdin/stdout protocol, and
destroys the container and job directory after importing the manifest.

The container has no published ports, host network, runtime socket, or host
mounts other than its unique `/job` directory. Run the container runtime in
rootless mode. Enforce an outbound firewall on `crawl-egress` that denies
loopback, link-local, RFC1918/ULA, multicast, reserved, and cloud metadata
addresses; the worker repeats those checks for every navigation.

Commands are one JSON object per stdin line. The first command is `start`;
later commands are `auth_resume`, `auth_skip`, or `cancel`. Stdout contains the
same event envelopes consumed by the public SSE endpoint and ends with an
`artifact_manifest`. Credentials are never written to the job directory or
stdout.

The vendored `seccomp_profile.json` is the official Playwright crawling
profile and must remain version-reviewed when the Playwright image changes.

Run the supervisor on the private side of the deployment with the rootless
runtime socket mounted only there. Set the API's `FLOWSTATE_HOSTED_MODE=true`,
`FLOWSTATE_SUPERVISOR_URL`, `FLOWSTATE_SUPERVISOR_TOKEN`, and an identical
absolute `FLOWSTATE_WORKER_JOB_ROOT` shared with the supervisor. The API
mirrors each committed per-run SQLite snapshot before publishing its event,
copies only PNG-validated screenshots, and asks the supervisor to erase the
scratch directory after terminal import. Raw DOM files are never copied into
the public artifacts mount.
