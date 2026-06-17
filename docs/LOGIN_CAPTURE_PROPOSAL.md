# Proposal: Login Capture — Auto-Pull Buckler Cookies via an Embedded Browser

- **Date:** 2026-06-13
- **Status:** **IMPLEMENTED** — Phase 1 shipped in PR #8 (merged 2026-06-16) and confirmed working on a live login. Phases 2–3 remain deferred (§10).
- **Feasibility:** **CONFIRMED by spike (2026-06-13)**, then by an end-to-end live run (2026-06-16) — see §2.
- **Reference:** Voltmeter-Aimlabs `aimlabs_auth.py` (`login_and_capture`) and its `[login]` optional-dependency pattern. This proposal adapts that mechanism; it does not copy it verbatim (three cookies vs. one, `config.toml` vs. `.env`, no bearer-token exchange).
- **Scheduling:** post-roadmap quality-of-life. Self-contained; depends on nothing in `CODEBASE_REVIEW.md`'s remaining L16/L17.

---

## 1. Problem

Today, getting (and re-getting) Buckler credentials is a manual, technical chore. The README's "Getting Buckler Variables" and "Refreshing expired cookies" sections (`README.md:51-73`) tell the user to:

1. Log into the CFN website in a browser.
2. Open DevTools → Network inspector.
3. Find a request and copy three values out of the **Request Cookies** header.
4. Paste `buckler_id`, `buckler_r_id`, `buckler_praise_date` into `config.toml`.
5. Restart the monitor.

This is awkward for a non-technical user and isn't a one-time cost: Buckler session cookies **expire routinely**, and each expiry forces the whole DevTools dance again. When they expire, the monitor goes blind and fires the emergency `AUTH_EXPIRED_MESSAGE` alert (`task.py:32-35`) whose remediation text is literally "go edit `config.toml`."

The goal: replace steps 1–4 with **"log in normally in a window that pops up, and the app extracts the cookies for you."**

## 2. Feasibility: confirmed by spike

The one genuine unknown was whether an embedded browser can read the Buckler cookies — `buckler_id` is `httpOnly`, which is exactly why DevTools is needed and why JavaScript's `document.cookie` can't see it. The reason Voltmeter's approach works is that **pywebview's `get_cookies()` reads the webview's *native* cookie store** (WebView2 on Windows), which includes `httpOnly` cookies.

A throwaway spike (`spike_login.py`, since deleted) opened a pywebview window at the Buckler site, let the author log in via Capcom ID, and polled `get_cookies()`, logging cookie **names and lengths only** (never values). Result:

```
spike: SUCCESS -- all three buckler cookies captured.
  buckler_id: present, length=64
  buckler_r_id: present, length=36
  buckler_praise_date: present, length=13
```

All three are visible to WebView2 — including the `httpOnly` one. The lengths match expectations (`buckler_id` a 64-char token, `buckler_r_id` a 36-char UUID, `buckler_praise_date` a 13-digit epoch-millis timestamp — consistent with the `int` field at `config.py:31`). **Auto-capture is viable on this machine's backend.** Caveat carried from Voltmeter: cookie capture is verified on Windows WebView2 only; this is a Windows-only project, so that's fine, but the implementation should fail loudly (not silently) if a future/other backend hides the cookies.

## 3. Proposal: a standalone `login` capture command

A new **separate script, `login.py`**, run on demand:

```shell
uv run python login.py
```

Behavior, mirroring `login_and_capture` but adapted to SF6:

1. Open a pywebview window at the Buckler landing page; the user logs in normally. **MFA and any captcha are handled natively by the Capcom ID page** — no special code, the embedded browser just renders the real login UI.
2. Poll `get_cookies()` (~1 s cadence, with a timeout) until **all three** `buckler_*` cookies are present and non-empty. (Voltmeter waits for one cookie; we wait for the set.)
3. **Verify before persisting:** make one real call via `api_service.get_character_win_rates` with the captured cookies. If it raises `AuthExpiredError`, the capture was bad — report and write nothing. This reuses existing code and turns "captured" into "captured *and proven to work*."
4. Write the three values to the secret store (§5), close the window, and print a confirmation (e.g. "captured and verified; restart the monitor").

### Why a standalone script, not a CLI subcommand

Voltmeter has a full `argparse` subcommand CLI (`voltmeter login` / `sync`). SF6 has **no CLI** — `app.py` and `status_server.py` are plain standalone scripts. A `login.py` script matches that existing shape (one script per job, run with `uv run python <script>.py`) and avoids introducing an argparse command layer this project doesn't otherwise have. Simplicity-first: don't build a CLI framework to host one command.

## 4. Architecture

```
login.py  ──opens──►  pywebview window (WebView2)  ──user logs in──►  cookies in native store
   │                                                                        │
   │  get_cookies() poll ◄───────────────────────────────────────────────┘
   │
   ├─ verify: api_service.get_character_win_rates(captured)   (reuses existing module)
   │
   └─ write ──►  secret store  ◄──reads at startup──  app.py (monitor)
```

- **Separate process from the monitor.** Same reliability principle as the status page (`STATUS_PAGE_PROPOSAL.md` §3): the monitor never imports the GUI stack and is unaffected whether `login.py` is ever run. `login.py` is the **only** module that imports `webview` (Voltmeter's "login is the only command that may open a window" decision).
- **`pywebview` as an optional dependency.** Add a `[project.optional-dependencies] login = ["pywebview>=5"]` extra, exactly as Voltmeter does. The core monitor stays dependency-light; `login.py` prints an install hint if `webview` import fails (fall back to the manual DevTools procedure, which still works). WebView2 ships with Windows 11, so no separate runtime install is needed on the target machine.
- **Cookie-shape handling.** Reuse the defensive `_iter_cookie_pairs` shape-normalizer from the spike / Voltmeter (WebView2 returns `SimpleCookie`/`Morsel`; other backends differ). Extraction here is *simpler* than Voltmeter's — no chunked-cookie reassembly, no bearer exchange — just pull three cookies by exact name.

## 5. Cookie storage — **OPEN DECISION (deferred 2026-06-13)**

Where `login.py` writes the captured values. Deliberately left open per the planning discussion; both options below are viable. **Recommendation: Option A.**

### Option A — rewrite `config.toml` in place (recommended)

Surgical line-replace of the three keys, preserving comments and other settings (the technique Voltmeter's `write_env_var` uses for `.env`, adapted to TOML).

- **Pros:** keeps `config.toml` as the *single* secret store; matches the user's existing mental model and the current "refresh" procedure; no new file, no precedence rule.
- **Cons / care needed:**
  - **No stdlib TOML writer.** `tomllib` is read-only; a full-rewrite library (`tomli-w`) drops comments. So this must be a *targeted* line replace, not a parse-and-redump.
  - **Type asymmetry:** `buckler_id` / `buckler_r_id` are quoted strings; `buckler_praise_date` is an **unquoted `int`** (`config.py:31`). The writer must emit the right form per key, or pydantic load fails.
  - **Write safety:** write to a temp file and `os.replace` (the atomic-write discipline from review M1) so a crash mid-write can't corrupt `config.toml`.

### Option B — separate cookie file

`login.py` writes e.g. `data/buckler_cookies.json`; the config layer loads cookies from there, overriding the `config.toml` values.

- **Pros:** trivial, safe write path (just dump JSON atomically); no TOML mutation; no string/int quoting concern.
- **Cons:** introduces a *second* secret location and a precedence rule (`config.toml` vs. cookie file — which wins?); `config.py` grows merge logic; `example.toml`'s three keys become vestigial/confusing. More moving parts for a project whose whole ethos is "single artifact" (cf. the single-`database.json` decision).

Either way: the file already lives under gitignored paths (`config.toml` is gitignored per README §"Data and logs"; `data/` is gitignored), so no new secret-leak surface.

## 6. The "as needed" question — and an honest limitation

The original ask was to pull the cookie "as needed." A clarification worth stating plainly: **capture can never be fully headless.** A *fresh* login requires the human whenever Capcom's own SSO session has expired — that's the entire point of logging in (credentials, possibly MFA). There is no API to mint Buckler cookies without a real authentication.

What *can* reduce friction, in increasing order of scope:

- **Phase 1 (this proposal):** `login.py` is explicit. The expired-cookie alert text (`AUTH_EXPIRED_MESSAGE`, `task.py:32-35`) and README change from "open DevTools and edit `config.toml`" to "run `uv run python login.py`, then restart." That alone removes the technical barrier.
- **Phase 2 (optional):** **persist the webview profile** between runs (`private_mode=False` + a `storage_path`). Then most refreshes are a single click with no retyping — if the embedded browser still holds a valid Capcom SSO session, the user just re-opens `login.py` and the redirect silently re-mints fresh `buckler_*` cookies. Only a *true* SSO expiry makes them type credentials again.
- **Phase 3 (optional, likely skip):** have the **monitor auto-launch the login window** when it hits `AuthExpiredError`. **Pushing back on this:** popping a GUI window from a 24/7 background process is intrusive and breaks the moment the monitor runs headless / as a service / on a box without a desktop session. Keep `login` an explicit human action; the monitor's job is to *alert*, not to seize the screen.

Related but **out of scope:** hot-reloading cookies into a *running* monitor. Today cookies are read once at startup (README §"Refreshing expired cookies": you must restart). This proposal does not change that — `login.py` captures; you still restart `app.py`. Hot-reload is a separable enhancement; bundling it here would expand scope.

## 7. Security posture

- **The credential never leaves the machine.** No value is sent anywhere except the same `streetfighter.com` API the monitor already calls.
- **Never log cookie values.** Any diagnostics print names and lengths only (the spike's discipline). This matches the existing `api_service` rule of dumping bodies only at DEBUG.
- **At rest:** values land in already-gitignored files. On POSIX, Voltmeter chmods the secret file `0600`; that's a no-op on Windows (this project's only target), so it's optional here.

## 8. Testing strategy

The interactive window itself isn't unit-testable (Voltmeter doesn't test it either), but the logic around it is — and should be, to match this repo's coverage habit:

- **Cookie extraction** (`_iter_cookie_pairs` + "pick the three by name"): pure function over faked WebView2/cookiejar shapes, including the "only two of three present" and "empty value" edge cases. Direct analog of `tests/test_aimlabs_auth.py`.
- **Storage writer:** round-trip in `tmp_path` — write into a sample `config.toml` (Option A) or cookie file (Option B), re-load via `config.load_config`, assert the three values and that **comments/other keys survive** (Option A) and that `praise_date` stays an int.
- **Verify step:** already covered indirectly by `tests/test_api_service.py`; the new code just calls the existing function.

## 9. Documentation changes this entails

- **README:** rewrite "Getting Buckler Variables" and "Refreshing expired cookies" (`README.md:51-73`) around `uv run python login.py`; keep the manual DevTools steps as a fallback for when `pywebview` is unavailable.
- **`AUTH_EXPIRED_MESSAGE`** (`task.py:32-35`): update remediation text to point at `login.py`.
- **`example.toml`:** note that the three `buckler_*` keys can be populated by `login.py` (Option A) or are managed in the cookie file (Option B).
- **`pyproject.toml`:** add the `[login]` optional-dependency extra.

## 10. Proposed scope (phasing)

1. **Phase 1 — capture command (the actual ask):** `login.py` (window → poll → verify → write), `[login]` extra, storage per §5 decision, tests per §8, doc updates per §9.
2. **Phase 2 — persisted profile** for near-silent refresh (§6). Small add once Phase 1 works; recommend doing it, but it's separable.
3. **Phase 3 — monitor auto-trigger / hot reload:** **not recommended** for now (§6); revisit only if the explicit-restart flow proves annoying in practice.

## 11. Open questions

1. **Cookie storage: Option A (rewrite `config.toml`) vs. Option B (separate file)?** **RESOLVED 2026-06-16: Option A** — in-place `config.toml` rewrite, as shipped (§5).
2. **Include Phase 2 (persisted profile) in the first cut, or ship capture-only first?** **RESOLVED 2026-06-16: capture-only** shipped first; Phase 2 deferred (§10).
3. **Start URL:** land on the Buckler top page and let the user click "Login" (robust, no guessing), or deep-link to a protected page that force-redirects to Capcom ID? **RESOLVED 2026-06-16: top page** — confirmed working on the live run (the poll waits through the Auth0 redirect and captures the cookies on return).
4. **Login timeout** before the window auto-closes. **RESOLVED 2026-06-16: 300 s** default (Voltmeter's default), as shipped.
