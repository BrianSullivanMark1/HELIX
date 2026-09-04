# HELIX on your phone (Tailscale)

The full HELIX face — orb, console, studio, voice buttons — on your phone, from anywhere, with
nothing exposed to the internet. Tailscale is a free private network between your own devices;
HELIX stays bound to this PC and Tailscale carries it over.

One-time setup (~15 minutes):

1. **On this PC**: install Tailscale from <https://tailscale.com/download>, sign in (Google or
   Microsoft account works), and it just sits in the tray.
2. **On your phone**: install the Tailscale app from the store, sign in with the SAME account.
   Both devices now see each other privately.
3. **In HELIX**: Settings → turn **Remote access** on. (This is what tells HELIX to accept its
   tailnet name; loopback-only is the default and stays that way while it's off.)
4. **On this PC**, in a terminal:

   ```bash
   tailscale serve --bg 8737
   ```

   That publishes HELIX **inside your tailnet only** (never the public internet) at
   `https://<this-pc>.<your-tailnet>.ts.net`, with HTTPS handled by Tailscale.
5. **On your phone**, open that address. You'll need the access token once — it's the `t=` value
   in the address bar of the HELIX tab on your PC; open the tab there and copy the full URL to
   your phone, then bookmark it.

Turn it off any time: flip Remote access off in HELIX Settings (takes effect immediately, no
restart), or `tailscale serve --https=443 off` on the PC.

Security posture, plainly: the page is only reachable by devices signed into YOUR tailnet, the
API still demands HELIX's own token on every call, and turning the Settings toggle off closes the
door even if `tailscale serve` is still running.

There's also the lightweight companion (ask/status only, no full face) on port 8770 — same
Remote-access toggle, `LAN` option in Settings for same-network use without Tailscale.
