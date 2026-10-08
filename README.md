# SAE cronist-source

An SAE source stage that plays videos assigned by [cronist](https://github.com/starwit/cronist) into the pipeline.

Cronist owns the state and this stage reconciles against it, similar to a Kubernetes controller. Every `cronist.sync_interval` it:
1. polls `GET /api/sae/{saeId}/desired-state` and reconciles it
2. reports `PUT /api/sae/{saeId}/observed-state`

A changed `taskId` stops the current task and starts a new one, and a `null` `taskId` means idle.

On every start the stage generates a random `instanceId`, kept in memory only, and reports it with each observed state. Cronist binds tasks to the instance they were assigned to. A task bound to another instance, e.g. the one before a crash, is treated as idle and never resumed, because its playback state is lost. Cronist marks it as failed. `observedGeneration` is only advanced once the desired state has actually been reconciled, i.e. after a previous task has really stopped.

For each task the stage:
1. downloads the video from `videoUrl`, e.g. a presigned S3 URL, to a temp file. Downloading first avoids problems with expiring URLs while playback is stalled by backpressure.
2. trims the output stream, so frames left over from a previous task are not fed downstream
3. decodes the video with PyAV and publishes one JPEG `SaeMessage` per frame to `<output_stream_prefix>:<stream_id>`, with backpressure enabled
4. derives frame timestamps from `videoStart` (the recording time of the first frame) plus the frame's PTS. Frame times are never guessed. The task fails with a clear message if:
   - `videoStart` is missing
   - the format carries no timestamps (raw `.h264`/`.m4v` streams)
   - a frame has no PTS
   - PTS values repeat or go backwards

   A demux-only check runs before anything is published, so such videos never feed partial data downstream.
5. reports `PLAYING` with `processedFrames` and `totalFrames` while running. `totalFrames` is the exact number of frames that will be published (counted while checking the video, `target_fps` included), and is `null` until that check is done., then `FINISHED` or `FAILED` (with `message`) until cronist assigns something else

## Configuration
See [settings.template.yaml](settings.template.yaml). Settings can also be given as env vars, e.g. `CRONIST__BASE_URL`.

- `cronist.auth`: set it if cronist runs with the `auth` profile. The stage then fetches a bearer token via OAuth2 client credentials, e.g. from a Keycloak client whose service account has the realm role `sae`.
- `redis.stream_maxlen` / `redis.backpressure.threshold`: backpressure only re-checks consumer lag every `monitor_interval`, so frames published in between can overshoot the threshold. Keep `stream_maxlen - threshold` well above `publish rate x monitor_interval`, otherwise unread frames get trimmed.
- `redis.backpressure.fail_open_timeout`: if no consumer is attached to the `visionlib-default-group` consumer group for this long, publishing continues and frames may be lost.

## Development
```bash
poetry install
poetry run pytest
cp settings.template.yaml settings.yaml && poetry run python main.py
```
