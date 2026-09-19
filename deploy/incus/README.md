# Deploying almanac in an Incus container with a GPU

For a host that already runs Incus (for example a CI host) and has an NVIDIA
GPU you want to give to inference. Everything lives in one system container;
the host only gets a profile, the container and two proxy ports.

**Status:** these files are prepared and reviewed, not proven on real
hardware. See "Untested" below.

## Requirements on the host

* NVIDIA driver loaded (`nvidia-smi` works) and `nvidia-container-cli`
  installed: Incus uses it for `nvidia.runtime`.
* An Incus remote you can administer (commands below use `<remote>:`, which
  can be run from another machine that has the remote configured).

## Commands

```sh
R=<remote>                       # e.g. myhost
# 1. Profile (edit REPLACE-* first: GPU PCI address, host IP to listen on)
incus profile create $R:almanac
incus profile edit $R:almanac < deploy/incus/almanac-profile.yaml

# 2. Container: a cloud-init image so the profile's user-data runs
incus launch images:debian/13/cloud $R:almanac --profile default --profile almanac
incus exec $R:almanac -- cloud-init status --wait
incus exec $R:almanac -- nvidia-smi          # the GPU must be visible here

# 3. Model (downloads several GB; pick one that fits the GPU's VRAM)
incus exec $R:almanac -- ollama pull qwen3.5:9b

# 4. almanac itself: copy this checkout and your knowledge repo in
tar -C .. -czf - almanac | incus exec $R:almanac -- tar -C /home/almanac -xzf -
tar -C .. -czf - my-knowledge | incus exec $R:almanac -- tar -C /home/almanac -xzf -
incus exec $R:almanac -- chown -R almanac:almanac /home/almanac
incus exec $R:almanac -- su - almanac -c 'almanac/deploy/install.sh --no-start'
# edit /home/almanac/.config/almanac/config.toml (knowledge_dirs, tools_dirs,
# hosts; listen stays 127.0.0.1 - the proxy devices publish it), then:
incus exec $R:almanac -- su - almanac -c 'XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user enable --now almanac-mcp almanac-gateway'

# 5. Check from a client machine
curl -s http://<host-ip>:41881/healthz
```

The token is created inside the container at
`/home/almanac/.config/almanac/token`; copy it to clients with
`incus file pull $R:almanac/home/almanac/.config/almanac/token -` piped
straight into your client's secret store, never into a shell history.

## Tools that act on other hosts

Tools run *inside the container*. `run_on = "local"` tools see the
container, not the host. To act on hosts, configure them with
`transport = "ssh"` and give the container's `almanac` user an SSH key that
the targets accept (ideally a restricted, read-only account).

## Untested

* GPU passthrough with `nvidia.runtime` on the target host and whether the
  bundled Ollama CUDA runtime supports an older (e.g. Pascal) GPU.
* The cloud-init sequence end to end.
