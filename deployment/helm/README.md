# SAE cronist-source Helm Chart
Deploys `sae-cronist-source` next to an existing [SAE](https://github.com/starwit/starwit-awareness-engine) installation. It plays videos assigned by [cronist](https://github.com/starwit/cronist) into the SAE Valkey (see the [component README](../../README.md)).

# How-To Install
- Copy `customvalues.template.yaml` to `customvalues.yaml` and adapt it (esp. `sae_id`, cronist URL and Valkey host)
- If cronist runs with the `auth` profile, create a Secret with the OAuth2 client secret and reference it in `authClientSecret`:
  ```sh
  kubectl create secret generic sae-cronist-source-auth --from-literal=client-secret=<secret>
  ```
- `helm install sae-cronist-source oci://registry-1.docker.io/starwitorg/sae-cronist-source-chart --version <version> -f customvalues.yaml`

# Notes
- The chart always runs exactly one replica (StatefulSet, i.e. never two pods at once, also not during updates), as cronist expects one instance per `sae_id`. Deploy one release per `sae_id`.
- Frames are written to `<output_stream_prefix>:<stream_id>`. The next SAE stage (e.g. the object detector) has to read from that stream instead of the video source.
- Videos are downloaded completely to `/tmp` (an `emptyDir`) before playback. Set `downloadVolume.sizeLimit` and/or `resources` for ephemeral storage accordingly.
- Chart and image are released together under the component's version.
