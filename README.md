# SAE cronist-source

An SAE source stage that plays recorded videos into the pipeline, as assigned by [cronist](https://github.com/starwit/cronist).

Instead of a live camera, the pipeline is fed from recordings, e.g. to count traffic or record trajectories for a past time period. cronist decides which video (or which part of a video) an SAE should process. This stage downloads the video, decodes it and publishes its frames with their real recording times, as if they came from a live camera. It reports progress back to cronist.

## How it works
1. **Get a task:** the stage regularly asks cronist what to play (the *desired state*). A task names a video URL, the video's recording start time and optionally a range of the video to play.
2. **Download the video** to a temp file.
3. **Publish frames:** each frame (or each frame of the range) is published as a JPEG `SaeMessage` to `<output_stream_prefix>:<stream_id>`, timestamped with the time it was recorded. Backpressure keeps the stage from running ahead of the pipeline.
4. **Report progress:** the stage reports its *observed state* (`PLAYING` with frame counts, then `FINISHED` or `FAILED`) back to cronist.

Frame times are never guessed: a video whose frame times cannot be derived exactly fails before any of its frames are published.

## Configuration
See [settings.template.yaml](settings.template.yaml). Settings can also be given as env vars, e.g. `CRONIST__BASE_URL`.

- `cronist.auth`: set it if cronist runs with the `auth` profile. The stage then fetches a bearer token via OAuth2 client credentials, e.g. from a Keycloak client whose service account has the realm role `sae`.
  - Keycloak client: client authentication on, service accounts enabled, realm role `sae` assigned to the service account.
  - `token_url` must use the same Keycloak hostname as cronist's `spring.security.oauth2.resourceserver.jwt.issuer-uri` (unless Keycloak's hostname is fixed via `KC_HOSTNAME`). Otherwise the token's `iss` claim does not match and cronist rejects every request with 401.
  - Without a token cronist redirects to its login page. The stage does not follow the redirect and logs it as an error.
- TLS: certificates are verified against the [certifi](https://pypi.org/project/certifi/) CA bundle, not the OS trust store. For a private CA, set `REQUESTS_CA_BUNDLE=/path/to/ca.pem` (this also applies to the video downloads).
- `redis.stream_maxlen` / `redis.backpressure.threshold`: backpressure only re-checks consumer lag every `monitor_interval`, so frames published in between can overshoot the threshold. Keep `stream_maxlen - threshold` well above `publish rate x monitor_interval`, otherwise unread frames get trimmed.
- `redis.backpressure.fail_open_timeout`: if no consumer is attached to the `visionlib-default-group` consumer group for this long, publishing continues and frames may be lost.

## Development
```bash
poetry install
poetry run pytest
cp settings.template.yaml settings.yaml && poetry run python main.py
```

## Technical details

### Syncing with cronist
cronist owns the state and this stage reconciles against it, similar to a Kubernetes controller. Every `cronist.sync_interval` it:
1. polls `GET /api/sae/{saeId}/desired-state` and reconciles it
2. reports `PUT /api/sae/{saeId}/observed-state`

A changed `taskId` stops the current task and starts a new one, and a `null` `taskId` means idle.

On every start the stage generates a random `instanceId`, kept in memory only, and reports it with each observed state. cronist binds tasks to the instance they were assigned to. A task bound to another instance, e.g. the one before a crash, is treated as idle and never resumed, because its playback state is lost. cronist marks it as failed. `observedGeneration` is only advanced once the desired state has actually been reconciled, i.e. after a previous task has really stopped.

### Task lifecycle
For each task the stage:
1. downloads the video from `videoUrl`, e.g. a presigned S3 URL, to a temp file (see [Downloading videos](#downloading-videos))
2. checks the frame times (see [Frame timestamps](#frame-timestamps)) and counts the frames that will be published
3. trims the output stream, so frames left over from a previous task are not fed downstream
4. decodes the video with PyAV and publishes one JPEG `SaeMessage` per frame, with backpressure enabled
5. reports `PLAYING` with `processedFrames` and `totalFrames` while running, then `FINISHED` or `FAILED` (with `message`) until cronist assigns something else. `totalFrames` is the exact number of frames that will be published (counted while checking the video, `target_fps` included), and is `null` until that check is done.

### Frame timestamps
Frame timestamps are `videoStart` (the recording time of the first frame) plus the frame's PTS relative to the first frame. The task fails with a clear message if:
- `videoStart` is missing
- the format carries no timestamps (raw `.h264`/`.m4v` streams)
- a frame has no PTS
- PTS values repeat or go backwards

A demux-only check runs before anything is published, so such videos never feed partial data downstream. For a range, it covers everything from the keyframe decoding starts at to the end of the range.

### Playing a range
cronist can ask for a part of a video, e.g. 30 min around noon from a 24h recording:
- `playbackOffsetMicros`: start of the range, relative to the PTS of the video's **first frame**. `null` (or missing) means 0.
- `playbackDurationMicros`: length of the range. `null` (or missing) means until the end of the video.

Only frames with `offset <= pts - firstPts < offset + duration` are published, where `firstPts` is the PTS of the first frame of the whole file. Their timestamps stay `videoStart + (pts - firstPts)`, exactly as in full playback, so the same frame always gets the same `timestamp_utc_ms`. `target_fps` sampling starts at the first frame of the range, and `totalFrames`/`processedFrames` count only the frames of the range.

The stage seeks to the last keyframe at or before the offset and decodes from there. Frames before the offset are dropped without colour conversion, and decoding stops at the end of the range. A range that runs past the end of the video plays to the end. A range that contains no frame, a negative offset or a non-positive duration fail the task. Both fields `null` behaves exactly like full playback.

### Downloading videos
The whole video is downloaded before playback starts, also when only a range is played, because the presigned URL (cronist `s3.sae-url-validity`, default 30 min) may expire while backpressure stalls playback, and reconnecting would then fail mid-video. Even 24h videos are small enough to download in reasonable time, and for a range only the decoding is limited to the range (see [Playing a range](#playing-a-range)).

## Changelog

### 0.3.0
- Play only a range of a video (`playbackOffsetMicros`/`playbackDurationMicros` in the desired state): seeks to the range instead of decoding the whole video
- Metric `cronist_source_frames_decoded`
- Download progress is logged every 10 s

### 0.2.1
- Clearer auth/request error logs (Keycloak vs. cronist), client secret masked
- Documented Keycloak/TLS setup

### 0.2.0
- Initial release
