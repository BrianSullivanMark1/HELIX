# HELIX RADIO

One music library for the company, playable from HELIX and from every app we build. Songs and
music videos go into one private bucket; a small API hands out short-lived play links to anyone
signed in with a Mark1 Google account; the same player drops into any app in a few lines.

Status: LIVE inside HELIX (2026-09-22): the deck, streamed uploads with real progress, playback
across pages (media streams down over HTTPS and plays while it arrives; cached on the PC after),
shelves (folders), station names, the gear (backgrounds, intensity, layout, the cache), the DJ -
the real face - and the music-video performance; all through the user's own gcloud login.
Next: `helix-radio-api` on Cloud Run with Google sign-in, then the drop-in for the apps.

---

## 1. The pieces

| Piece | Where | What it does |
|---|---|---|
| Bucket `helix-radio-<suffix>` | GCS, windy-celerity, us-west2 | Private. `tracks/`, `videos/`, `art/`, `catalog.json` |
| `helix-radio-api` | Cloud Run, same project | Catalog, play links, upload links, station names. Verifies a Google sign-in from `mark1online.com` on every call |
| The player | HELIX header (left of the menu) and one React component for the apps | Deck: now playing, queue, shuffle, volume, upload; grows into a video window; or shows the dancing avatar |
| The DJ | Same component | HELIX's real face (the Talk avatar) inside the stage: head-bangs on the kick, grooves, shrugs, grins, shuts its eyes, winks; the mouse and clicks work its lattice inside the stage. A **music video** gets a performance: a wink, the head zooms off, a curtain of neural net knits over the stage and falls into the picture; on resume the DJ peeks in from the side, nods, and the picture ripples back. "Show the DJ" swaps the video out |
| CURRENT TASKS | HELIX | Every upload, fetch and station cache is a task: the section above PROJECTS, the dock at the bottom of every page, the log, Stop |

Why an API instead of the bucket directly: a private bucket needs signed URLs, signing needs a
service account key or impersonation, and we never hand keys to browsers. The API holds the
one identity (the `helix-radio` service account) and asks *you* to prove yours with Google.

## 2. Make the bucket (your end, once)

Bucket names are global on GCS, so pick a suffix that is yours, e.g. `helix-radio-mark1`. Run in
PowerShell from anywhere. Everything is ASCII. If `gcloud` is blocked by the execution policy,
use `gcloud.cmd`.

```powershell
gcloud config set project windy-celerity-392822

# 1. the bucket: private, uniform access, nothing ever public
gcloud storage buckets create gs://helix-radio-mark1 --location=us-west2 --uniform-bucket-level-access --public-access-prevention

# 2. the API's identity
gcloud iam service-accounts create helix-radio --display-name "HELIX Radio"
gcloud storage buckets add-iam-policy-binding gs://helix-radio-mark1 --member=serviceAccount:helix-radio@windy-celerity-392822.iam.gserviceaccount.com --role=roles/storage.objectAdmin
gcloud iam service-accounts add-iam-policy-binding helix-radio@windy-celerity-392822.iam.gserviceaccount.com --member=serviceAccount:helix-radio@windy-celerity-392822.iam.gserviceaccount.com --role=roles/iam.serviceAccountTokenCreator

# 3. humans who may open the bucket in the console (optional; the apps go through the API)
gcloud storage buckets add-iam-policy-binding gs://helix-radio-mark1 --member=user:brian_sullivan@mark1online.com --role=roles/storage.objectAdmin
gcloud storage buckets add-iam-policy-binding gs://helix-radio-mark1 --member=user:brendan_sullivan@mark1online.com --role=roles/storage.objectAdmin

# 4. browser playback and upload need CORS on the bucket
Set-Content -Encoding ascii -Path cors.json -Value '[{"origin":["*"],"method":["GET","HEAD","PUT"],"responseHeader":["Content-Type","Range","x-goog-resumable"],"maxAgeSeconds":3600}]'
gcloud storage buckets update gs://helix-radio-mark1 --cors-file=cors.json

# 5. the empty catalog
Set-Content -Encoding ascii -Path catalog.json -Value '{"version":1,"stations":{"default":"HELIX RADIO"},"tracks":[]}'
gcloud storage cp catalog.json gs://helix-radio-mark1/catalog.json

# verify
gcloud storage ls gs://helix-radio-mark1/
```

Step 3 uses named people rather than a group so it works today; when the Workspace has a
`radio@mark1online.com` group, one line swaps the members for `group:radio@mark1online.com`.

`Set-Content -Encoding ascii` matters: PowerShell's `>` writes UTF-16 and gcloud will refuse
the file.

Narrow the CORS origins to the real app hosts once they exist (the `*.web.app` sites and
`http://127.0.0.1:8737` for HELIX on a PC).

## 3. Google Workspace and sign-in

The bucket lives in the windy-celerity project, which sits under the mark1online.com
organization, so IAM already understands Workspace users and groups. Nothing to "add" in
Workspace admin. Sign-in for the player is Google Identity: the app asks for a Google token,
`helix-radio-api` checks the token is valid and `hd == mark1online.com`, and only then answers.
Someone outside the domain gets a 403 and no link. That is the safety protocol you asked for,
and the same identity the prod push gate uses.

## 4. The catalog

`catalog.json` at the bucket root. The API is the only writer.

```json
{
  "version": 2,
  "stations": { "default": "HELIX RADIO", "MES": "MES FM", "WMS": "Dock Radio" },
  "folders": ["Rock", "Rock/80s", "Late night"],
  "tracks": [
    {
      "id": "8f3a1c",
      "title": "Overnight",
      "artist": "Mark 1",
      "theme": "chill",
      "bpm": 96,
      "kind": "video",
      "audio": "tracks/8f3a1c.m4a",
      "video": "videos/8f3a1c.mp4",
      "art": "art/8f3a1c.jpg",
      "seconds": 214,
      "folder": "Late night",
      "bytes": 236000000,
      "uploaded_by": "brian_sullivan@mark1online.com",
      "at": "2026-09-21T02:10:00Z"
    }
  ]
}
```

**Shelves** (folders, 2026-09-22): a track sits in one folder, like Explorer; a folder is a path such
as `Rock/80s`, four levels deep at most, letters/digits/spaces/dashes/dots. `folders` lists the
empty ones too; a track's `folder` implies its parents. Everyone sees the same shelves (they are
in the catalog). Rename moves the tracks with it; a shelf is removed only when empty - shelving
never drops a track. Playlists (a track in many) are a later layer on top; nothing forecloses them.

`theme` and `bpm` are typed at upload (your idea) and are the avatar's hints; the player also
measures the beat live, so a wrong bpm is corrected by the ears, not trusted.

Station names live in the catalog under `stations` (one per app key) so a rename in MES shows in
MES everywhere, and each app also keeps a local override in its own settings for the case where
someone wants "Dock Radio" on one screen only. Resolution: local override, then catalog, then
`default`.

## 5. Uploads and formats

Anyone signed in can upload. The flow: the app asks the API for an upload link, the browser PUTs
the file straight to the bucket on that link (no file passes through Cloud Run), then the app
posts the metadata and the API finalizes: reads tags, makes the catalog entry, and if the file is
not web-playable, transcodes it.

| You give | We keep | Note |
|---|---|---|
| mp3, m4a, aac, ogg, opus, wav, flac | as is (wav/flac re-encoded to m4a to save space) | audio |
| mp4 (H.264/AAC), webm | as is | video, plays in Chrome, Edge and WebView2 |
| mov, mkv, avi, m4v, wmv, mpg | transcoded to mp4 H.264/AAC | ffmpeg in the API's container; the original is kept under `raw/` |
| anything else | refused with one sentence | |

Every video also gets its audio pulled out into `tracks/` so the same entry plays as audio when
the video is hidden, and gets a still for the art.

Deleting is not in the app. A track can be hidden (`"hidden": true`) from the deck; removing the
file is done in the GCS console by a human, on purpose.

**Inside HELIX today** (no API yet), the upload is `PUT /api/radio/upload?name=&title=&artist=&theme=&bpm=&folder=`
with the file as the body: HELIX counts the bytes as they arrive (the first half of the bar),
then sends the file to the bucket by a **resumable HTTPS upload** with an access token from the
user's own `gcloud auth print-access-token` (8 MB pieces, the second half of the bar), then writes
the catalog. It is a task in CURRENT TASKS from the first byte; Stop leaves nothing in the catalog
(the bucket abandons the half-sent session). Without a token the gcloud CLI copy is the fallback.

**Playback inside HELIX** (`GET /api/radio/play/{id}`): a cached file is served with Range (seek);
a file not yet on the PC is **streamed down** over HTTPS with the same token into the cache and
served while it arrives (`X-Helix-Arriving: 1`), so a video starts in a second, not after the
whole download. The next track in the list prefetches when a track starts. The cache lives in
`data/radio_cache`, capped (Settings/the gear: `radio_cache_gb`, 5 GB default), oldest-played
goes first; **Cache the whole station** runs as one task. No gcloud console windows anywhere.

## 6. The player, in every app

The React component and a 40-line helper are the whole drop-in. Same look in every app: the
button left of the menu, the deck that slides down, the video mode, the avatar.

```tsx
import { HelixRadio } from "@mark1/helix-radio";   // one folder copied into the app, no registry

<HelixRadio app="MES" api="https://helix-radio-api-XXXX-uw.a.run.app" />
```

The component does everything: Google sign-in prompt on first use, catalog read, station name,
queue, playback with signed links, the avatar, the upload zone. The app passes its key (`MES`),
so the station name resolves per app. For a Flask app that has no React, the same thing ships as
one `<script>` tag that mounts into a `<div id="helix-radio">`.

The avatar reacts through the Web Audio API: an `AnalyserNode` on the playing element, low-band
energy flux for the beat, mid/high bands for the arms and head, `theme` picking the move set
(chill sways, hype bounces, dark stalks). Customization is a few sliders saved locally: body
color, accent, glow, size, and which of the three characters. When a video is playing the deck
shows the video by default with a button to swap to the avatar; the choice is remembered.

## 7. Build order

1. UI shell in HELIX: header button, deck, queue, video mode, avatar placeholder, upload zone
   with the theme/bpm fields (no bucket yet). You approve the look.
2. `helix-radio-api`: catalog, play links, upload links, finalize (tags, transcode), stations,
   Google check. Deployed to Cloud Run by hand the first time; HELIX's Deploy lane after that.
3. HELIX upload lane wired to the API; the first songs in.
4. The avatar for real: beat detection, three characters, the customizer.
5. The drop-in for MES, then the rest, one line each. The station-name setting per app.

## 8. Rules

- The bucket is private, always. Play links expire in 15 minutes; upload links in 1 hour.
- No deletes from any app. Hide, yes. Delete, GCS console, by a human.
- Every API call carries a Google sign-in from mark1online.com. No anonymous listening.
- Files never pass through the API's memory; the browser talks to the bucket on signed links.
- ASCII in every script; the catalog is the only writer's job.
